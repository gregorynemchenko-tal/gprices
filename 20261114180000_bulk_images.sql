begin;
alter table public.product_images add column external_url text;
alter table public.product_images add column provider text not null default 'Open Food Facts';
alter table public.product_images add column retailer_checked_at timestamptz;
alter table public.product_images add column retailer_lease_until timestamptz;
alter table public.product_images drop constraint product_images_check;
alter table public.product_images add constraint product_images_ready_check check(status<>'ready' or
 (storage_path is not null and license is not null and sha256 is not null) or
 (coalesce(external_url ~ '^https://img[.]rami-levy[.]co[.]il/product/[0-9]{8,14}/small[.]jpg$',false) and provider='Rami Levy'));
create index product_images_retailer_idx on public.product_images(retailer_checked_at,product_id) where storage_path is null;

create function public.claim_retailer_image_batch(p_limit integer default 250) returns jsonb
language sql security definer set search_path='' as $$
 with chosen as (select i.product_id,p.gtin from public.product_images i join public.catalog_products p on p.id=i.product_id
 where p.gtin is not null and i.storage_path is null
 and (i.retailer_checked_at is null or i.retailer_checked_at<now()-interval '30 days')
 and (i.retailer_lease_until is null or i.retailer_lease_until<now())
 order by i.retailer_checked_at nulls first,i.product_id limit greatest(1,least(p_limit,500)) for update of i skip locked),
 leased as (update public.product_images i set retailer_lease_until=now()+interval '15 minutes'
 from chosen c where i.product_id=c.product_id returning i.product_id,c.gtin)
 select coalesce(jsonb_agg(to_jsonb(leased)),'[]') from leased;
$$;
create function public.save_retailer_image_checks(p_rows jsonb) returns integer
language plpgsql security definer set search_path='' as $$
declare n integer;
begin
 if jsonb_array_length(p_rows)>500 then raise exception 'Image batch limit 500'; end if;
 if exists(select 1 from jsonb_array_elements(p_rows) r join public.catalog_products p on p.id=r->>'product_id'
  where (r->>'found')::boolean and r->>'url' is distinct from
  'https://img.rami-levy.co.il/product/'||lpad(ltrim(p.gtin,'0'),case when length(ltrim(p.gtin,'0'))<=8 then 8 when length(ltrim(p.gtin,'0'))<=13 then 13 else 14 end,'0')||'/small.jpg')
 then raise exception 'Image URL does not match product GTIN'; end if;
 update public.product_images i set retailer_checked_at=case when (r->>'transient')::boolean then i.retailer_checked_at else now() end,
 retailer_lease_until=case when (r->>'transient')::boolean then now()+interval '15 minutes' else null end,
 external_url=case when i.storage_path is not null or (r->>'transient')::boolean then i.external_url when (r->>'found')::boolean then r->>'url' else null end,
 status=case when i.storage_path is not null or (r->>'transient')::boolean then i.status when (r->>'found')::boolean then 'ready' when i.external_url is not null then 'pending' else i.status end,
 provider=case when i.storage_path is null and (r->>'found')::boolean then 'Rami Levy' else i.provider end,
 version=case when i.storage_path is null and (r->>'found')::boolean then to_char(now(),'YYYYMMDD') else i.version end
 from jsonb_array_elements(p_rows) r where i.product_id=r->>'product_id';
 get diagnostics n=row_count;return n;
end $$;
revoke all on function public.claim_retailer_image_batch(integer),public.save_retailer_image_checks(jsonb) from public,anon,authenticated;
grant execute on function public.claim_retailer_image_batch(integer),public.save_retailer_image_checks(jsonb) to service_role;

create or replace function public.search_catalog(p_query text default '',p_category text default null,p_cursor text default '',p_limit integer default 30)
returns jsonb language sql stable security definer set search_path='' as $$
 with page as (select p.* from public.catalog_products p where p.id>coalesce(p_cursor,'')
 and (p_category is null or p.category=p_category)
 and not exists(select 1 from regexp_split_to_table(lower(trim(left(coalesce(p_query,''),200))),'\s+') t
 where t<>'' and position(t in p.search_text)=0)
 order by p.id limit greatest(1,least(p_limit,200))), rows as (
 select p.id,p.payload || jsonb_build_object('image_status',coalesce(i.status,'pending'),'image_version',i.version,
 'image_url',case when i.status='ready' then coalesce('https://simjxoshjprrexdixbso.supabase.co/storage/v1/object/public/product-images/'||i.storage_path,i.external_url) end,
 'image_license',case when i.storage_path is not null then i.license end,'image_source',case when i.external_url is not null then i.external_url else i.source_url end,
 'image_provider',i.provider) as payload from page p left join public.product_images i on i.product_id=p.id)
 select jsonb_build_object('products',coalesce((select jsonb_agg(payload order by id) from rows),'[]'),
 'next_cursor',(select max(id) from rows),'offers',public.get_product_offers(array(select id from rows)));
$$;

create function public.claim_catalog_chain_job(p_chain_id text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare m record;
begin
 with chosen as(select msg_id from pgmq.q_catalog_import where vt<=now() and message->>'chain_id'=p_chain_id order by msg_id limit 1 for update skip locked)
 update pgmq.q_catalog_import q set vt=now()+interval '30 minutes',read_ct=q.read_ct+1 from chosen c where q.msg_id=c.msg_id returning q.* into m;
 if not found then return null; end if;
 if m.read_ct>5 then perform pgmq.send('catalog_dead_letters',m.message);perform pgmq.delete('catalog_import',m.msg_id);return null; end if;
 return jsonb_build_object('id',m.msg_id,'attempt',m.read_ct,'job',m.message);
end $$;
revoke all on function public.claim_catalog_chain_job(text) from public,anon,authenticated;
grant execute on function public.claim_catalog_chain_job(text) to service_role;
notify pgrst, 'reload schema';
commit;
