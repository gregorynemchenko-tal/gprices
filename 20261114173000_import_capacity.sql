-- Only the service-only atomic publication needs a longer transaction.
alter function public.publish_catalog_snapshot(uuid,integer) set statement_timeout='60s';
notify pgrst, 'reload schema';
