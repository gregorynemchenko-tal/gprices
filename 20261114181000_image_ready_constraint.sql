alter table public.product_images drop constraint product_images_ready_check;
alter table public.product_images add constraint product_images_ready_check check(status<>'ready' or
 (storage_path is not null and license is not null and sha256 is not null) or
 (coalesce(external_url ~ '^https://img[.]rami-levy[.]co[.]il/product/[0-9]{8,14}/small[.]jpg$',false) and provider='Rami Levy'));
