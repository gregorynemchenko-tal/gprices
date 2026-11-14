-- Process local packaged products first; all other GTINs remain eligible.
create or replace function public.claim_retailer_image_batch(p_limit integer default 250) returns jsonb
language sql security definer set search_path='' as $$
 with chosen as (select i.product_id,p.gtin from public.product_images i join public.catalog_products p on p.id=i.product_id
 where p.gtin is not null and i.storage_path is null
 and (i.retailer_checked_at is null or i.retailer_checked_at<now()-interval '30 days')
 and (i.retailer_lease_until is null or i.retailer_lease_until<now())
 order by (p.gtin like '0729%') desc,i.retailer_checked_at nulls first,i.product_id
 limit greatest(1,least(p_limit,500)) for update of i skip locked),
 leased as (update public.product_images i set retailer_lease_until=now()+interval '15 minutes'
 from chosen c where i.product_id=c.product_id returning i.product_id,c.gtin)
 select coalesce(jsonb_agg(to_jsonb(leased)),'[]') from leased;
$$;
notify pgrst,'reload schema';
