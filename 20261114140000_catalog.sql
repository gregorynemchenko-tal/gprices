-- Independent catalog project. Does not migrate or modify the finance project.
begin;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists pgmq;
create extension if not exists pg_cron;
create schema if not exists catalog_private;
revoke all on schema catalog_private from public, anon, authenticated;

create table public.retail_chains (
 id text primary key, name text not null, code text not null unique,
 source_url text not null, adapter text not null,
 enabled boolean not null default false, coverage_status text not null default 'not_connected',
 last_checked_at timestamptz, last_success_at timestamptz, last_error text
);
create table public.retail_stores (
 id text primary key, chain_id text not null references public.retail_chains,
 external_id text not null, name text not null, address text, city_code text,
 enabled boolean not null default false, metadata jsonb not null default '{}',
 unique(chain_id,external_id)
);
create table public.catalog_products (
 id text primary key, gtin text unique check(gtin is null or gtin ~ '^[0-9]{14}$'),
 name_he text not null, name_ru text, category text not null,
 search_text text not null, payload jsonb not null,
 updated_at timestamptz not null default now()
);
create index catalog_search_idx on public.catalog_products using gin(search_text extensions.gin_trgm_ops);
create index catalog_category_idx on public.catalog_products(category,id);
create table public.retail_product_aliases (
 chain_id text not null references public.retail_chains, raw_code text not null,
 product_id text not null references public.catalog_products, raw_name text not null,
 primary key(chain_id,raw_code)
);
create table public.source_files (
 id uuid primary key default gen_random_uuid(), store_id text not null references public.retail_stores,
 kind text not null check(kind in ('prices','promotions')),
 filename text not null, published_at timestamptz not null, fetched_at timestamptz not null,
 sha256 text, raw_path text, unique(store_id,kind,filename)
);
create table public.store_snapshots (
 id uuid primary key default gen_random_uuid(), source_file_id uuid not null unique references public.source_files,
 store_id text not null references public.retail_stores, kind text not null,
 state text not null default 'staging' check(state in ('staging','published','superseded','quarantined')),
 row_count integer, created_at timestamptz not null default now(), published_at timestamptz,
 error text
);
create table catalog_private.stage_rows (
 snapshot_id uuid not null references public.store_snapshots on delete cascade,
 row_key text not null, payload jsonb not null, primary key(snapshot_id,row_key)
);
create table public.retail_prices_current (
 store_id text not null references public.retail_stores,
 product_id text not null references public.catalog_products,
 price_minor integer not null check(price_minor>0), snapshot_id uuid not null references public.store_snapshots,
 available boolean not null default true, primary key(store_id,product_id)
);
create index catalog_prices_product_idx on public.retail_prices_current(product_id,store_id) where available;
create table public.retail_price_changes (
 id bigint generated always as identity primary key,
 store_id text not null, product_id text not null, old_price_minor integer, price_minor integer not null,
 snapshot_id uuid not null references public.store_snapshots, changed_at timestamptz not null default now(),
 unique(snapshot_id,product_id)
);
create index catalog_price_history_idx on public.retail_price_changes(product_id,store_id,changed_at desc);
create table public.retail_promotions (
 id text primary key, store_id text not null references public.retail_stores,
 product_id text not null references public.catalog_products, snapshot_id uuid not null references public.store_snapshots,
 starts_at timestamptz not null, ends_at timestamptz not null,
 payload jsonb not null, check(ends_at>=starts_at)
);
create index catalog_promos_product_idx on public.retail_promotions(product_id,ends_at);
create index catalog_promos_page_idx on public.retail_promotions(id,ends_at);
-- Preserve complete source mechanics in JSON as well as indexed product rows.
create table public.retail_promotion_groups (
 snapshot_id uuid not null references public.store_snapshots,
 promotion_code text not null, group_code text not null, payload jsonb not null,
 primary key(snapshot_id,promotion_code,group_code)
);
create table public.product_images (
 product_id text primary key references public.catalog_products,
 status text not null default 'pending' check(status in ('pending','ready','missing','blocked')),
 storage_path text, source_url text, license text, sha256 text, version text,
 checked_at timestamptz, check(status<>'ready' or (storage_path is not null and license is not null and sha256 is not null))
);
create table catalog_private.job_keys (
 key text primary key, message_id bigint not null, created_at timestamptz not null default now()
);
select pgmq.create('catalog_import');
select pgmq.create('catalog_dead_letters');
select pgmq.create('catalog_images');

create function public.begin_catalog_snapshot(p_store_id text,p_kind text,p_filename text,
 p_published_at timestamptz,p_fetched_at timestamptz,p_sha256 text default null)
returns uuid language plpgsql security definer set search_path='' as $$
declare f uuid; s uuid;
begin
 if p_published_at>now()+interval '5 minutes' then raise exception 'Future source timestamp'; end if;
 insert into public.source_files(store_id,kind,filename,published_at,fetched_at,sha256)
 values(p_store_id,p_kind,p_filename,p_published_at,p_fetched_at,p_sha256)
 on conflict(store_id,kind,filename) do update set fetched_at=excluded.fetched_at returning id into f;
 insert into public.store_snapshots(source_file_id,store_id,kind) values(f,p_store_id,p_kind)
 on conflict(source_file_id) do update set source_file_id=excluded.source_file_id returning id into s;
 return s;
end $$;
create function public.stage_catalog_rows(p_snapshot_id uuid,p_rows jsonb)
returns integer language plpgsql security definer set search_path='' as $$
declare n integer;
begin
 perform 1 from public.store_snapshots where id=p_snapshot_id and state='staging' for update;
 if not found then return 0; end if;
 if jsonb_array_length(p_rows)>500 then raise exception 'Batch limit 500'; end if;
 insert into catalog_private.stage_rows(snapshot_id,row_key,payload)
 select p_snapshot_id,x->>'id',x from jsonb_array_elements(p_rows) x
 on conflict(snapshot_id,row_key) do update set payload=excluded.payload;
 get diagnostics n=row_count; return n;
end $$;
create function public.publish_catalog_snapshot(p_snapshot_id uuid,p_expected_count integer)
returns jsonb language plpgsql security definer set search_path='' as $$
declare s public.store_snapshots; f public.source_files; n integer; previous_count integer; chain text;
begin
 select * into strict s from public.store_snapshots where id=p_snapshot_id for update;
 if s.state='published' then return jsonb_build_object('status','already_published'); end if;
 if s.state<>'staging' then raise exception 'Snapshot is not staging'; end if;
 perform 1 from public.retail_stores where id=s.store_id for update;
 select * into strict f from public.source_files where id=s.source_file_id;
 select chain_id into chain from public.retail_stores where id=s.store_id;
 if exists(select 1 from public.store_snapshots old join public.source_files ofile on ofile.id=old.source_file_id
  where old.store_id=s.store_id and old.kind=s.kind and old.state='published' and ofile.published_at>=f.published_at) then
  update public.store_snapshots set state='superseded' where id=s.id;
  delete from catalog_private.stage_rows where snapshot_id=s.id;
  return jsonb_build_object('status','older_version');
 end if;
 select count(*) into n from catalog_private.stage_rows where snapshot_id=s.id;
 if n<>p_expected_count or (s.kind='prices' and n=0) then raise exception 'Incomplete snapshot: %/%',n,p_expected_count; end if;
 select row_count into previous_count from public.store_snapshots where store_id=s.store_id and kind=s.kind and state='published';
 if s.kind='prices' and previous_count>100 and n<previous_count*0.5 then raise exception 'Assortment shrank more than 50 percent'; end if;
 if s.kind='prices' then
  if exists(select 1 from catalog_private.stage_rows where snapshot_id=s.id and
   (coalesce((payload->>'price')::integer,0)<=0 or coalesce(payload->>'original','')='' or payload->>'id' is distinct from row_key))
   then raise exception 'Invalid price row'; end if;
  insert into public.catalog_products(id,gtin,name_he,name_ru,category,search_text,payload)
  select row_key,case when row_key like 'gtin:%' then substr(row_key,6) end,payload->>'original',payload->>'name',
   payload->>'category',lower(payload->>'searchText'),payload from catalog_private.stage_rows where snapshot_id=s.id
  on conflict(id) do update set name_he=excluded.name_he,name_ru=excluded.name_ru,category=excluded.category,
   search_text=excluded.search_text,payload=excluded.payload,updated_at=now();
  insert into public.retail_product_aliases(chain_id,raw_code,product_id,raw_name)
  select chain,payload->>'barcode',row_key,payload->>'original' from catalog_private.stage_rows where snapshot_id=s.id
  on conflict(chain_id,raw_code) do update set product_id=excluded.product_id,raw_name=excluded.raw_name;
  insert into public.retail_price_changes(store_id,product_id,old_price_minor,price_minor,snapshot_id)
  select s.store_id,r.row_key,p.price_minor,(r.payload->>'price')::integer,s.id
  from catalog_private.stage_rows r left join public.retail_prices_current p on p.store_id=s.store_id and p.product_id=r.row_key
  where r.snapshot_id=s.id and (p.price_minor is distinct from (r.payload->>'price')::integer or not p.available)
  on conflict do nothing;
  update public.retail_prices_current set available=false where store_id=s.store_id and available;
  insert into public.retail_prices_current(store_id,product_id,price_minor,snapshot_id)
  select s.store_id,row_key,(payload->>'price')::integer,s.id from catalog_private.stage_rows where snapshot_id=s.id
  on conflict(store_id,product_id) do update set price_minor=excluded.price_minor,snapshot_id=excluded.snapshot_id,available=true;
  insert into public.product_images(product_id) select row_key from catalog_private.stage_rows where snapshot_id=s.id on conflict do nothing;
 else
  delete from public.retail_promotions where store_id=s.store_id;
  insert into public.retail_promotions(id,store_id,product_id,snapshot_id,starts_at,ends_at,payload)
  select row_key,s.store_id,payload->>'productId',s.id,(payload->>'startsAt')::timestamptz,(payload->>'endsAt')::timestamptz,payload
  from catalog_private.stage_rows where snapshot_id=s.id;
 end if;
 update public.store_snapshots set state='superseded' where store_id=s.store_id and kind=s.kind and state='published';
 update public.store_snapshots set state='published',published_at=now(),row_count=n where id=s.id;
 update public.retail_chains set last_success_at=now(),coverage_status='partial',last_error=null where id=chain;
 delete from catalog_private.stage_rows where snapshot_id=s.id;
 return jsonb_build_object('status','published','rows',n);
end $$;

create function public.get_product_offers(p_product_ids text[],p_store_ids text[] default null)
returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_agg(jsonb_build_object('product_id',p.product_id,'store_id',p.store_id,'chain_id',st.chain_id,
 'price_minor',p.price_minor,'currency','ILS','store_name',st.name,'address',st.address,'published_at',f.published_at,
 'fetched_at',f.fetched_at,'last_checked_at',c.last_checked_at,'snapshot_id',p.snapshot_id,
 'availability_status',case when p.available then 'published' else 'not_published' end,
 'freshness_status',case when f.published_at>=now()-interval '48 hours' and p.available then 'current' else 'last_known' end)
 order by p.product_id,p.price_minor,p.store_id),'[]') from public.retail_prices_current p
 join public.retail_stores st on st.id=p.store_id join public.retail_chains c on c.id=st.chain_id
 join public.store_snapshots s on s.id=p.snapshot_id join public.source_files f on f.id=s.source_file_id
 where p.product_id=any(p_product_ids[1:200]) and (p_store_ids is null or p.store_id=any(p_store_ids[1:100]));
$$;
create function public.search_catalog(p_query text default '',p_category text default null,p_cursor text default '',p_limit integer default 30)
returns jsonb language sql stable security definer set search_path='' as $$
 with page as (select p.* from public.catalog_products p where p.id>coalesce(p_cursor,'')
 and (p_category is null or p.category=p_category)
 and not exists(select 1 from regexp_split_to_table(lower(trim(left(coalesce(p_query,''),200))),'\s+') t
  where t<>'' and position(t in p.search_text)=0)
 order by p.id limit greatest(1,least(p_limit,200))), rows as (
 select p.id,p.payload || jsonb_build_object('image_status',coalesce(i.status,'pending'),'image_version',i.version,
 'image_url',case when i.status='ready' then 'https://simjxoshjprrexdixbso.supabase.co/storage/v1/object/public/product-images/'||i.storage_path end,
 'image_license',i.license,'image_source',i.source_url) as payload from page p left join public.product_images i on i.product_id=p.id)
 select jsonb_build_object('products',coalesce((select jsonb_agg(payload order by id) from rows),'[]'),
 'next_cursor',(select max(id) from rows),'offers',public.get_product_offers(array(select id from rows)));
$$;
create function public.get_active_promotions(p_chain_id text default null,p_cursor text default '',p_limit integer default 30)
returns jsonb language sql stable security definer set search_path='' as $$
 select jsonb_build_object('promotions',coalesce(jsonb_agg(payload order by id),'[]'),'next_cursor',max(id)) from (
 select pr.id,pr.payload from public.retail_promotions pr join public.retail_stores st on st.id=pr.store_id
 join public.store_snapshots s on s.id=pr.snapshot_id join public.source_files f on f.id=s.source_file_id
 where pr.id>coalesce(p_cursor,'') and pr.starts_at<=now() and pr.ends_at>=now()
 and f.published_at>=now()-interval '48 hours' and (p_chain_id is null or st.chain_id=p_chain_id)
 order by pr.id limit greatest(1,least(p_limit,100))) page;
$$;
create function public.get_catalog_health() returns jsonb language sql stable security definer set search_path='' as $$
 select jsonb_build_object('products',(select count(*) from public.catalog_products),
 'prices',(select count(*) from public.retail_prices_current where available),
 'images_ready',(select count(*) from public.product_images where status='ready'),
 'chains',(select jsonb_agg(jsonb_build_object('id',c.id,'name',c.name,'status',c.coverage_status,
 'last_checked_at',c.last_checked_at,'last_success_at',c.last_success_at,'stores',
 (select count(*) from public.retail_stores st where st.chain_id=c.id),'published_stores',
 (select count(distinct store_id) from public.store_snapshots s join public.retail_stores st on st.id=s.store_id
 where st.chain_id=c.id and s.state='published' and s.kind='prices')) order by c.name) from public.retail_chains c),
 'snapshots',(select jsonb_agg(jsonb_build_object('store_id',s.store_id,'kind',s.kind,'rows',s.row_count,
 'published_at',f.published_at,'fetched_at',f.fetched_at)) from public.store_snapshots s
 join public.source_files f on f.id=s.source_file_id where s.state='published'));
$$;

create function public.plan_catalog_jobs() returns integer language plpgsql security definer set search_path='' as $$
declare st record; k text; msg bigint; n integer:=0;
begin
 for st in select s.id,s.chain_id from public.retail_stores s join public.retail_chains c on c.id=s.chain_id where s.enabled and c.enabled loop
  k:=st.id||':'||floor(extract(epoch from now())/7200)::text;
  perform pg_advisory_xact_lock(hashtextextended(k,0));
  if not exists(select 1 from catalog_private.job_keys where key=k) then
   select pgmq.send('catalog_import',jsonb_build_object('store_id',st.id,'chain_id',st.chain_id)) into msg;
   insert into catalog_private.job_keys(key,message_id) values(k,msg); n:=n+1;
  end if;
 end loop;
 delete from catalog_private.job_keys where created_at<now()-interval '14 days';
 return n;
end $$;
create function public.claim_catalog_job() returns jsonb language plpgsql security definer set search_path='' as $$
declare m record;
begin
 for m in select * from pgmq.read('catalog_import',1800,1) loop
  if m.read_ct>5 then
   perform pgmq.send('catalog_dead_letters',m.message); perform pgmq.delete('catalog_import',m.msg_id); return null;
  end if;
  return jsonb_build_object('id',m.msg_id,'attempt',m.read_ct,'job',m.message);
 end loop;
 return null;
end $$;
create function public.finish_catalog_job(p_id bigint,p_success boolean,p_error text default null)
returns void language plpgsql security definer set search_path='' as $$
declare chain text;
begin
 select message->>'chain_id' into chain from pgmq.q_catalog_import where msg_id=p_id;
 update public.retail_chains set last_checked_at=now(),last_error=case when p_success then null else left(p_error,500) end where id=chain;
 if p_success then perform pgmq.delete('catalog_import',p_id);
 else perform pgmq.set_vt('catalog_import',p_id,900); end if;
end $$;

-- All writes and queue controls are server-only; public clients read bounded RPCs.
do $$ declare t text; begin
 foreach t in array array['retail_chains','retail_stores','catalog_products','retail_product_aliases','source_files','store_snapshots',
 'retail_prices_current','retail_price_changes','retail_promotions','retail_promotion_groups','product_images'] loop
 execute format('alter table public.%I enable row level security',t);
 execute format('revoke all on public.%I from anon,authenticated',t);
 execute format('grant all on public.%I to service_role',t);
 end loop;
end $$;
revoke all on all functions in schema public from public,anon,authenticated;
grant execute on all functions in schema public to service_role;
grant usage,select on all sequences in schema public to service_role;
grant execute on function public.search_catalog(text,text,text,integer),public.get_product_offers(text[],text[]),
 public.get_active_promotions(text,text,integer),public.get_catalog_health() to anon,authenticated;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types) values
 ('product-images','product-images',true,5242880,array['image/jpeg','image/png','image/webp']),
 ('catalog-raw','catalog-raw',false,52428800,array['application/gzip','application/octet-stream','application/xml']),
 ('catalog-releases','catalog-releases',true,52428800,array['application/json'])
on conflict(id) do nothing;
create policy catalog_images_read on storage.objects for select to anon,authenticated using(bucket_id in ('product-images','catalog-releases'));
select cron.schedule('catalog-discovery','17 */2 * * *','select public.plan_catalog_jobs()');
commit;
