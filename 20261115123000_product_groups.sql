begin;
create table public.product_groups (
 id text primary key, name_ru text not null, name_he text not null,
 category text not null, unit text not null check(unit in ('кг','шт.')),
 criteria jsonb not null, rule_version text not null,
 origin text not null default 'rule' check(origin in ('rule','manual')),
 enabled boolean not null default true, updated_at timestamptz not null default now()
);
create table public.product_group_members (
 group_id text not null references public.product_groups,
 product_id text not null references public.catalog_products,
 evidence jsonb not null, rule_version text not null,
 origin text not null default 'rule' check(origin in ('rule','manual')),
 decision text not null default 'approved' check(decision in ('approved','rejected')),
 enabled boolean not null default true, updated_at timestamptz not null default now(),
 primary key(group_id,product_id)
);
create index product_group_reverse_idx on public.product_group_members(product_id,group_id) where enabled and decision='approved';
alter table public.product_groups enable row level security;
alter table public.product_group_members enable row level security;
revoke all on public.product_groups,public.product_group_members from anon,authenticated;
grant all on public.product_groups,public.product_group_members to service_role;

create function public.sync_product_groups(p_groups jsonb,p_members jsonb,p_rule_version text) returns jsonb
language plpgsql security definer set search_path='' set statement_timeout='30s' as $$
begin
 if coalesce(p_rule_version,'')='' or jsonb_typeof(p_groups)<>'array' or jsonb_typeof(p_members)<>'array'
 or jsonb_array_length(p_groups)>200 or jsonb_array_length(p_members)>10000 then raise exception 'Invalid group batch'; end if;
 if exists(select 1 from jsonb_array_elements(p_members) m where not exists(select 1 from jsonb_array_elements(p_groups) g where g->>'id'=m->>'group_id')) then raise exception 'Unknown group in batch'; end if;
 perform pg_advisory_xact_lock(hashtextextended('catalog-product-groups',0));
 update public.product_groups set enabled=false where origin='rule';
 insert into public.product_groups(id,name_ru,name_he,category,unit,criteria,rule_version)
 select g->>'id',g->>'name_ru',g->>'name_he',g->>'category',g->>'unit',g->'criteria',p_rule_version from jsonb_array_elements(p_groups) g
 on conflict(id) do update set name_ru=excluded.name_ru,name_he=excluded.name_he,category=excluded.category,unit=excluded.unit,
 criteria=excluded.criteria,rule_version=excluded.rule_version,enabled=true,updated_at=now() where product_groups.origin='rule';
 update public.product_group_members set enabled=false where origin='rule' and decision='approved'
 and group_id in(select id from public.product_groups where origin='rule');
 insert into public.product_group_members(group_id,product_id,evidence,rule_version)
 select m->>'group_id',m->>'product_id',m->'evidence',p_rule_version from jsonb_array_elements(p_members) m
 join public.product_groups g on g.id=m->>'group_id' and g.origin='rule'
 on conflict(group_id,product_id) do update set evidence=excluded.evidence,rule_version=excluded.rule_version,enabled=true,updated_at=now()
 where product_group_members.origin='rule' and product_group_members.decision<>'rejected';
 return jsonb_build_object('groups',jsonb_array_length(p_groups),'members',jsonb_array_length(p_members));
end $$;

create function public.get_product_groups() returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_agg(x.data order by x.id),'[]') from (
 select g.id,jsonb_build_object('id',g.id,'name',g.name_ru,'original',g.name_he,'category',g.category,'unit',g.unit,
 'criteria',g.criteria,'ruleVersion',g.rule_version,'memberIds',jsonb_agg(m.product_id order by m.product_id)) data
 from public.product_groups g join public.product_group_members m on m.group_id=g.id and m.enabled and m.decision='approved'
 where g.enabled group by g.id order by g.id limit 200) x;
$$;

create function public.get_product_group_offers(p_group_id text,p_store_ids text[] default null) returns jsonb
language sql stable security definer set search_path='' as $$
 with offers as (
 select g.id group_id,st.id store_id,st.chain_id,p.product_id,p.price_minor,cp.payload product,
 st.name store_name,st.address,f.published_at,f.fetched_at,
 row_number() over(partition by st.id order by p.price_minor,p.product_id) choice
 from public.product_groups g
 join public.product_group_members m on m.group_id=g.id and m.enabled and m.decision='approved'
 join public.catalog_products cp on cp.id=m.product_id and cp.payload->>'unit'=g.unit
 join public.retail_prices_current p on p.product_id=m.product_id and p.available
 join public.retail_stores st on st.id=p.store_id and st.enabled
 join public.store_snapshots s on s.id=p.snapshot_id and s.state='published'
 join public.source_files f on f.id=s.source_file_id and f.published_at>=now()-interval '48 hours' and f.published_at<=now()+interval '5 minutes'
 where g.enabled and g.id=p_group_id and (p_store_ids is null or st.id=any(p_store_ids[1:100])))
 select coalesce(jsonb_agg(to_jsonb(offers)-'choice' order by price_minor,store_id),'[]') from offers where choice=1;
$$;
revoke all on function public.sync_product_groups(jsonb,jsonb,text) from public,anon,authenticated;
grant execute on function public.sync_product_groups(jsonb,jsonb,text) to service_role;
revoke all on function public.get_product_groups(),public.get_product_group_offers(text,text[]) from public;
grant execute on function public.get_product_groups(),public.get_product_group_offers(text,text[]) to anon,authenticated,service_role;
notify pgrst,'reload schema';
commit;
