begin;
do $migration$
declare definition text; old_text text:='search_text=excluded.search_text,payload=excluded.payload,updated_at=now()';
merged text:='case when position(excluded.search_text in catalog_products.search_text)>0 then catalog_products.search_text else catalog_products.search_text||'' ''||excluded.search_text end';
begin
 select pg_get_functiondef('public.publish_catalog_snapshot(uuid,integer)'::regprocedure) into definition;
 if position(old_text in definition)=0 then raise exception 'Unexpected publication function definition'; end if;
 definition:=replace(definition,old_text,'search_text='||merged||',payload=jsonb_set(excluded.payload,''{searchText}'',to_jsonb('||merged||')),updated_at=now()');
 execute definition;
end $migration$;
create function public.enrich_catalog_search(p_rows jsonb) returns integer language plpgsql security definer set search_path='' as $$
declare n integer;
begin
 if jsonb_array_length(p_rows)>250 then raise exception 'Search enrichment batch limit 250'; end if;
 with incoming as(select r->>'id' id,lower(r->>'searchText') keywords from jsonb_array_elements(p_rows) r),
 merged as(select p.id,case when position(i.keywords in p.search_text)>0 then p.search_text else p.search_text||' '||i.keywords end keywords
 from public.catalog_products p join incoming i on p.id=i.id where coalesce(i.keywords,'')<>'')
 update public.catalog_products p set search_text=m.keywords,payload=jsonb_set(p.payload,'{searchText}',to_jsonb(m.keywords)) from merged m where p.id=m.id and p.search_text<>m.keywords;
 get diagnostics n=row_count;return n;
end $$;
revoke all on function public.enrich_catalog_search(jsonb) from public,anon,authenticated;
grant execute on function public.enrich_catalog_search(jsonb) to service_role;
notify pgrst,'reload schema';
commit;
