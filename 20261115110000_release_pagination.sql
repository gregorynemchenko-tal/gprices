begin;
create index if not exists catalog_promos_store_id_idx on public.retail_promotions(store_id,id);
analyze public.catalog_products;
analyze public.retail_promotions;
analyze public.retail_prices_current;
commit;
