-- Live integration checks, completely rolled back (including test rows).
begin;
do $$
declare s uuid; s2 uuid; result jsonb; row_data jsonb;
begin
 if has_function_privilege('anon','public.publish_catalog_snapshot(uuid,integer)','execute') then raise exception 'Anonymous publishing allowed'; end if;
 if has_table_privilege('anon','public.catalog_products','insert') then raise exception 'Anonymous catalog write allowed'; end if;
 if has_function_privilege('anon','public.claim_catalog_job()','execute') then raise exception 'Anonymous queue access allowed'; end if;
 insert into public.retail_chains(id,name,code,source_url,adapter) values('integration-test','QA','integration-test','https://example.com','test');
 insert into public.retail_stores(id,chain_id,external_id,name) values('integration-test:1','integration-test','1','QA');
 row_data:='[{"id":"integration-test:product","barcode":"123","original":"QA","name":"QA","category":"QA","searchText":"qa","price":100}]'::jsonb;
 s:=public.begin_catalog_snapshot('integration-test:1','prices','first',now()-interval '2 hours',now());
 perform public.stage_catalog_rows(s,row_data);
 if exists(select 1 from public.retail_prices_current where store_id='integration-test:1') then raise exception 'Staging leaked before publication'; end if;
 begin
  perform public.publish_catalog_snapshot(s,2);
  raise exception 'Incomplete publication was accepted';
 exception when others then
  if sqlerrm='Incomplete publication was accepted' then raise; end if;
 end;
 perform public.publish_catalog_snapshot(s,1);
 perform public.publish_catalog_snapshot(s,1);
 if (select count(*) from public.retail_price_changes where store_id='integration-test:1')<>1 then raise exception 'Duplicate history'; end if;
 s2:=public.begin_catalog_snapshot('integration-test:1','prices','second',now()-interval '1 hour',now());
 perform public.stage_catalog_rows(s2,jsonb_set(row_data,'{0,price}','120'));
 if (select price_minor from public.retail_prices_current where store_id='integration-test:1')<>100 then raise exception 'Interrupted import changed published price'; end if;
 perform public.publish_catalog_snapshot(s2,1);
 s:=public.begin_catalog_snapshot('integration-test:1','prices','older',now()-interval '3 hours',now());
 perform public.stage_catalog_rows(s,jsonb_set(row_data,'{0,price}','90'));
 result:=public.publish_catalog_snapshot(s,1);
 if result->>'status'<>'older_version' or (select price_minor from public.retail_prices_current where store_id='integration-test:1')<>120 then raise exception 'Older file overwrote new price'; end if;
 s:=public.begin_catalog_snapshot('integration-test:1','prices','unchanged',now()-interval '30 minutes',now());
 perform public.stage_catalog_rows(s,jsonb_set(row_data,'{0,price}','120'));
 perform public.publish_catalog_snapshot(s,1);
 if (select count(*) from public.retail_price_changes where store_id='integration-test:1')<>2 then raise exception 'Unchanged price duplicated history'; end if;
end $$;
select 'PASS: permissions, atomic publication, incomplete import rejection, idempotence, ordering, change-only history' as checks;
rollback;
