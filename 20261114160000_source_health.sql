begin;
create or replace function public.finish_catalog_job(p_id bigint,p_success boolean,p_error text default null)
returns void language plpgsql security definer set search_path='' as $$
declare chain text;
begin
 select message->>'chain_id' into chain from pgmq.q_catalog_import where msg_id=p_id;
 update public.retail_chains set last_checked_at=now(),
  last_error=case when p_success then null else left(p_error,500) end,
  coverage_status=case when p_success then 'partial' else 'source_unavailable' end where id=chain;
 if p_success then perform pgmq.delete('catalog_import',p_id);
 else perform pgmq.set_vt('catalog_import',p_id,900); end if;
end $$;
update public.retail_chains set coverage_status='source_unavailable' where enabled and last_error is not null;
create or replace function public.plan_catalog_jobs() returns integer language plpgsql security definer set search_path='' as $$
declare st record; k text; msg bigint; n integer:=0;
begin
 for st in select s.id,s.chain_id from public.retail_stores s join public.retail_chains c on c.id=s.chain_id where s.enabled and c.enabled loop
  k:=st.id||':'||floor(extract(epoch from now())/7200)::text;
  perform pg_advisory_xact_lock(hashtextextended(st.id,0));
  if not exists(select 1 from catalog_private.job_keys where key=k)
   and not exists(select 1 from pgmq.q_catalog_import where message->>'store_id'=st.id) then
   select pgmq.send('catalog_import',jsonb_build_object('store_id',st.id,'chain_id',st.chain_id)) into msg;
   insert into catalog_private.job_keys(key,message_id) values(k,msg); n:=n+1;
  end if;
 end loop;
 delete from catalog_private.job_keys where created_at<now()-interval '14 days';
 return n;
end $$;
commit;
