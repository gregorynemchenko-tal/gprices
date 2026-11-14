# Kabalator catalog deployment

Target: `simjxoshjprrexdixbso` (independent catalog project).
The existing `yivimpjpcufdpxyfycsz` project continues to own authentication and receipts.
Do not run the root financial migrations against this catalog project.

## Deployed 2026-11-14

- Canonical product IDs and chain-scoped aliases.
- Staged, atomic branch publication; source versions; price-change history.
- Bounded public search/offers/promotions/health RPCs; server-only import and queue RPCs.
- PGMQ queues: catalog_import, catalog_images, catalog_dead_letters.
- Cron plans imports every two hours, photos daily, retention daily.
- Private raw bucket; public licensed images and versioned catalog releases.
- Initial 17,318 products; 26,934 branch prices; 9,079 promotion-product rows.
- Three published branches: Carrefour 81, Victory 23, Hazi Hinam 201.
- Other registered chains are explicitly not connected; directory presence does not imply imported coverage.

The personal-purchase projection is ready but empty in the new project.
It is not connected to the old receipt database yet. No private users or receipts
were copied. All live publication integration checks run in a rolled-back transaction.

## Updating schema

This directory is separately linked with Supabase CLI. Apply its SQL files with
`supabase db query --linked --workdir deploy/catalog --file supabase/migrations/<file>`.
Paths passed to the CLI are relative to its workdir. Never use root `db push` here.
These initial migrations were applied through the Management API, not root migration history.

## Worker and schedule

`apps/retail-importer/catalog_worker.py` reads SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY on a server. Local ignored `.env.catalog-worker` is
supported; never publish it. Linux Python 3.12 needs no extra packages.

`--seed` imports the checked local prototype fixtures (initial bootstrap only).
`--max-jobs 6 --images 5` claims queued store jobs, fetches complete official
snapshots, publishes independent branches, and prepares up to five licensed photos.
`--release-only` assembles the Expo compatibility snapshot from published DB rows.

GitHub Actions runs every two hours, without a permanent VM. GitHub scheduled
runs can be delayed and do not provide an exact-time SLA. One run at a time,
25-minute timeout, 30-minute message lease, up to five attempts then dead letter.
Failures preserve the last published data. A prepared workflow alone does not
mean the scheduled runner is enabled; verify a successful live workflow run.

The worker consumes the three currently enabled branches. Other branches are
registered but disabled until their import and resource usage are checked.
FTPS adapters are not yet integrated into the new canonical worker. These are
explicit remaining rollout steps, not promised full-market coverage.

## Images

Only matching Open Food Facts images with CC BY-SA attribution are cached in
Supabase, at content-hashed paths. Retailer photographs whose reuse rights are
unverified are not copied. Missing images remain pending/missing, not fabricated.
Current worker saves the original JPEG; thumbnail generation is a later step.

## Mobile transition

Expo loads a shared versioned manifest from Supabase public Storage and caches
the completed release. The manifest is published last, so catalog and promotions
come from the same completed release. Source file dates remain independent.
This is a compatibility bridge for existing screens: the server paginated RPCs
are deployed, but the prototype still reads the complete release. Moving each
screen to server pagination and removing direct retailer photo fallback remain.

Public catalog configuration is separate from receipt auth configuration.
Only the public key is added to mobile; the import key stays in server settings.

## Operations still to extend

- Import all branch directories/snapshots and remaining network adapters.
- Full raw promotion-group preservation, including products absent from prices.
- Licensed photo coverage, thumbnails, missing-photo rechecks and monitoring.
- Raw Storage and old release object cleanup (DB history retention is active).
- Receipt-owned projection migration; paginated mobile UI; operator alerts.
