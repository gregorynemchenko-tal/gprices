begin;
-- A private projection can be populated once receipt ownership is migrated.
-- Existing users and receipts are deliberately left in the original project.
create table public.user_product_purchases (
 user_id uuid not null references auth.users on delete cascade, receipt_id uuid not null,
 product_id text not null references public.catalog_products, purchased_at timestamptz not null,
 uploaded_at timestamptz not null, quantity numeric not null check(quantity>0),
 total_minor integer not null check(total_minor>=0), primary key(user_id,receipt_id,product_id)
);
alter table public.user_product_purchases enable row level security;
create policy own_purchases on public.user_product_purchases for select to authenticated using(user_id=(select auth.uid()));
revoke all on public.user_product_purchases from anon,authenticated;
grant select on public.user_product_purchases to authenticated;
grant all on public.user_product_purchases to service_role;
create function public.get_my_products(p_cursor text default '',p_limit integer default 30,p_store_ids text[] default null)
returns jsonb language sql stable security invoker set search_path='' as $$
 with history as (select *,count(*) over(partition by product_id) as receipt_count,
 row_number() over(partition by product_id order by purchased_at desc,uploaded_at desc,receipt_id desc) as recent
 from public.user_product_purchases where user_id=(select auth.uid())), page as (
 select * from history where recent=1 and product_id>coalesce(p_cursor,'') order by product_id limit greatest(1,least(p_limit,100)))
 select jsonb_build_object('products',coalesce((select jsonb_agg(to_jsonb(page) order by product_id) from page),'[]'),
 'next_cursor',(select max(product_id) from page),'offers',public.get_product_offers(array(select product_id from page),p_store_ids));
$$;
revoke all on function public.get_my_products(text,integer,text[]) from public,anon;
grant execute on function public.get_my_products(text,integer,text[]) to authenticated,service_role;

create table catalog_private.image_jobs(product_id text primary key references public.catalog_products,message_id bigint not null);
create function public.plan_catalog_images(p_limit integer default 20) returns integer language plpgsql security definer set search_path='' as $$
declare r record; msg bigint; n integer:=0;
begin
 for r in select p.id from public.catalog_products p join public.product_images i on i.product_id=p.id
 where p.gtin is not null and i.status='pending' and not exists(select 1 from catalog_private.image_jobs j where j.product_id=p.id)
 order by p.id limit greatest(0,least(p_limit,100)) for update of i skip locked loop
  select pgmq.send('catalog_images',jsonb_build_object('product_id',r.id)) into msg;
  insert into catalog_private.image_jobs(product_id,message_id) values(r.id,msg); n:=n+1;
 end loop;
 return n;
end $$;
create function public.claim_catalog_image() returns jsonb language plpgsql security definer set search_path='' as $$
declare m record;
begin
 for m in select * from pgmq.read('catalog_images',600,1) loop
  if m.read_ct>5 then
   perform pgmq.send('catalog_dead_letters',m.message); perform pgmq.delete('catalog_images',m.msg_id);
   delete from catalog_private.image_jobs where message_id=m.msg_id; return null;
  end if;
  return jsonb_build_object('id',m.msg_id,'job',m.message);
 end loop;
 return null;
end $$;
create function public.finish_catalog_image(p_id bigint,p_success boolean) returns void language plpgsql security definer set search_path='' as $$
begin
 if p_success then
  perform pgmq.delete('catalog_images',p_id); delete from catalog_private.image_jobs where message_id=p_id;
 else perform pgmq.set_vt('catalog_images',p_id,900); end if;
end $$;
revoke all on function public.plan_catalog_images(integer),public.claim_catalog_image(),public.finish_catalog_image(bigint,boolean) from public,anon,authenticated;
grant execute on function public.plan_catalog_images(integer),public.claim_catalog_image(),public.finish_catalog_image(bigint,boolean) to service_role;
select cron.schedule('catalog-photo-planning','41 2 * * *','select public.plan_catalog_images(100)');
-- Keep intermediate/history data bounded. Raw object cleanup is handled by worker.
select cron.schedule('catalog-retention','11 3 * * *',$job$
 delete from public.retail_price_changes where changed_at<now()-interval '90 days';
 delete from catalog_private.stage_rows r using public.store_snapshots s where r.snapshot_id=s.id and s.state='staging' and s.created_at<now()-interval '2 days';
$job$);
commit;
