"""Canonical catalog publisher and durable queue consumer (no AI calls).

Runs on a scheduled Linux runner or a persistent container. Personal receipts
remain in their original project. All credentials are read from server env.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

from importer import CHAINS as FTPS_CHAINS, SupabaseRest
from http_importer import CHAINS, CHAINS_BY_KEY, client_for_chain
from build_catalog import ROOT, build as build_catalog
from build_promotions import build as build_promotions

TARGET_PROJECT = 'simjxoshjprrexdixbso'


def load_environment():
    path = ROOT / '.env.catalog-worker'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key, value)
    url, key = os.environ.get('SUPABASE_URL', ''), os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if urllib.parse.urlsplit(url).hostname != f'{TARGET_PROJECT}.supabase.co' or not key:
        raise ValueError('Expected target catalog project and server-only service key')
    return SupabaseRest(url, key)


def rpc(db, name, **body):
    return db.request('POST', 'rpc/' + name, body=body)


def store_key(branch):
    return branch['id'] + ':' + str(int(branch['storeId']))


def snapshot_rows(catalog, branch):
    """A shared product's display price must never leak into another store."""
    products = {p['id']: p for p in catalog['products']}
    for identity, price in branch['prices'].items():
        product = copy.deepcopy(products[identity])
        product['price'] = price
        yield product


def publish(db, branch, kind, filename, published_at, fetched_at, rows):
    cache = ROOT / 'tmp/catalog-cache' / (branch['id'] + '-' + filename)
    digest = hashlib.sha256(cache.read_bytes()).hexdigest() if cache.exists() else None
    snapshot = rpc(db, 'begin_catalog_snapshot', p_store_id=store_key(branch), p_kind=kind,
                   p_filename=filename, p_published_at=published_at, p_fetched_at=fetched_at, p_sha256=digest)
    state = db.request('GET', 'store_snapshots', {'id': 'eq.' + snapshot, 'select': 'state'})[0]['state']
    if state != 'staging':
        return {'status': 'already_processed'}
    if cache.exists():
        raw_path = f"{branch['id']}/{int(branch['storeId'])}/{digest}.gz"
        upload(db, 'catalog-raw', raw_path, cache.read_bytes(), 'application/gzip')
        db.request('PATCH', 'source_files', {'id': 'eq.' + get_snapshot_file(db, snapshot)}, {'raw_path': raw_path})
    for offset in range(0, len(rows), 250):
        rpc(db, 'stage_catalog_rows', p_snapshot_id=snapshot, p_rows=rows[offset:offset+250])
    result = rpc(db, 'publish_catalog_snapshot', p_snapshot_id=snapshot, p_expected_count=len(rows))
    print(json.dumps({'store': store_key(branch), 'kind': kind, **result}), flush=True)
    return result


def get_snapshot_file(db, snapshot):
    return db.request('GET', 'store_snapshots', {'id': 'eq.' + snapshot, 'select': 'source_file_id'})[0]['source_file_id']


def upload(db, bucket, path, payload, mime):
    url = db.base_url + '/storage/v1/object/' + bucket + '/' + urllib.parse.quote(path, safe='/')
    req = urllib.request.Request(url, data=payload, method='POST', headers={
        'apikey': db.service_role_key, 'Authorization': 'Bearer ' + db.service_role_key,
        'Content-Type': mime, 'x-upsert': 'true', 'cache-control': '31536000' if bucket == 'product-images' else '60'})
    with urllib.request.urlopen(req, timeout=90) as response:
        response.read()


def register(db, catalog):
    db.upsert('retail_chains', [{'id': c.key, 'name': c.name, 'code': c.code, 'source_url': c.feed_home_url,
        'adapter': 'https' if c in CHAINS else 'ftps_external', 'enabled': c in CHAINS,
        'coverage_status': 'not_imported' if c in CHAINS else 'not_connected'} for c in {c.key:c for c in (*FTPS_CHAINS, *CHAINS)}.values()], 'id')
    stores = []
    selected = {store_key(c) for c in catalog['chains']}
    for c in CHAINS:
        file = ROOT / f'tmp/catalog-{c.key}-stores.json'
        if file.exists():
            for s in json.loads(file.read_text(encoding='utf-8')):
                identity = c.key + ':' + str(int(s['external_id']))
                stores.append({'id': identity, 'chain_id': c.key, 'external_id': str(int(s['external_id'])),
                    'name': s['name'], 'address': s.get('address'), 'city_code': s.get('city_code'),
                    'enabled': identity in selected})
    db.upsert('retail_stores', stores, 'id')


def import_catalog(db, catalog, promotions):
    for branch in catalog['chains']:
        publish(db, branch, 'prices', branch['sourceFile'], branch['publishedAt'], branch['fetchedAt'], list(snapshot_rows(catalog, branch)))
        source = next((s for s in promotions['sources'] if s['chainId'] == branch['id']), None)
        if source:
            rows = [p for p in promotions['promotions'] if p['chainId'] == branch['id']]
            publish(db, branch, 'promotions', source['sourceFile'], source['publishedAt'], promotions['generatedAt'], rows)


def publish_releases(db):
    """Compatibility snapshot generated ONLY from published DB versions.

    The paginated RPC is the canonical API. This snapshot lets the existing Expo
    prototype move off bundled files before its screen-by-screen pagination work.
    A versioned manifest is replaced last, after both immutable files are uploaded.
    """
    def all_rows(table, select='*', order='id', filters=None):
        result = []
        offset = 0
        while True:
            page = db.request('GET', table, {'select': select, 'order': order, 'offset': str(offset), 'limit': '1000',**(filters or {})})
            result.extend(page)
            if len(page) < 1000: return result
            offset += len(page)
    products = [p['payload'] for p in all_rows('catalog_products')]
    snapshots = all_rows('store_snapshots')
    files = {f['id']: f for f in all_rows('source_files')}
    stores = {s['id']: s for s in all_rows('retail_stores')}
    chains = {c['id']: c for c in all_rows('retail_chains')}
    images = all_rows('product_images',order='product_id',filters={'status':'eq.ready'})
    image_map = {i['product_id']: i for i in images}
    for p in products:
        i = image_map.get(p['id'])
        p['image_status'] = 'ready' if i else 'pending'
        if i:
            p.update(image_url=(db.base_url + '/storage/v1/object/public/product-images/' + i['storage_path']) if i['storage_path'] else i.get('external_url'),
                     image_source=i.get('external_url') or i['source_url'], image_license=i['license'] if i['storage_path'] else None,
                     image_provider=i.get('provider','Open Food Facts'),image_version=i['version'])
    branches, sources, promos = [], [], []
    for s in snapshots:
        if s['state'] != 'published': continue
        st, f = stores[s['store_id']], files[s['source_file_id']]
        chain = chains[st['chain_id']]
        if s['kind'] == 'prices':
            prices, offset = {}, 0
            while True:
                page = db.request('GET', 'retail_prices_current', {'store_id': 'eq.'+st['id'], 'available': 'eq.true',
                    'select': 'product_id,price_minor', 'order': 'product_id', 'offset': str(offset), 'limit': '1000'})
                prices.update({p['product_id']: p['price_minor'] for p in page})
                if len(page) < 1000: break
                offset += len(page)
            branches.append({'id': chain['id'], 'name': chain['name'], 'branch': st['name'], 'address': st['address'],
                'storeId': st['external_id'], 'cityCode': st['city_code'], 'distance': 'Расстояние не рассчитано',
                'color': '#326CAC', 'prices': prices, 'sourceUrl': chain['source_url'], 'sourceFile': f['filename'],
                'publishedAt': f['published_at'], 'fetchedAt': f['fetched_at'], 'productCount': len(prices)})
        else:
            sources.append({'chainId': chain['id'], 'sourceFile': f['filename'], 'publishedAt': f['published_at'], 'count': s['row_count']})
            offset = 0
            while True:
                page = db.request('GET', 'retail_promotions', {'store_id': 'eq.'+st['id'], 'select': 'payload',
                    'order': 'id', 'offset': str(offset), 'limit': '1000'})
                promos.extend(p['payload'] for p in page)
                if len(page) < 1000: break
                offset += len(page)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    products.sort(key=lambda p:(p.get('image_status')!='ready',p.get('category')=='Другое',not p['id'].startswith('gtin:'),p.get('original','')))
    catalog = {'generatedAt': now, 'city': 'Ришон-ле-Цион', 'products': products, 'chains': branches, 'failures': []}
    promotion_data = {'generatedAt': now, 'sources': sources, 'promotions': promos, 'failures': []}
    catalog_bytes = json.dumps(catalog, ensure_ascii=False, separators=(',', ':')).encode()
    promo_bytes = json.dumps(promotion_data, ensure_ascii=False, separators=(',', ':')).encode()
    version = hashlib.sha256(catalog_bytes + promo_bytes).hexdigest()[:20]
    for kind, payload in [('catalog', catalog_bytes), ('promotions', promo_bytes)]:
        upload(db, 'catalog-releases', f'{version}/{kind}.json', payload, 'application/json')
    manifest = {'version': version, 'generatedAt': now, 'catalog': f'{version}/catalog.json', 'promotions': f'{version}/promotions.json'}
    upload(db, 'catalog-releases', 'manifest.json', json.dumps(manifest).encode(), 'application/json')
    print(json.dumps({'release': version, 'products': len(products), 'branches': len(branches), 'promotions': len(promos)}), flush=True)


def consume(db, max_jobs, chain=None):
    failed = 0
    if chain: rpc(db,'plan_catalog_jobs')
    for _ in range(max_jobs):
        claimed = rpc(db,'claim_catalog_chain_job',p_chain_id=chain) if chain else rpc(db, 'claim_catalog_job')
        if not claimed: break
        job = claimed['job']
        target = job['store_id']
        try:
            output = ROOT / 'tmp/worker-catalog.json'
            catalog = build_catalog([target], output, refresh=False)
            branch = catalog['chains'][0]
            publish(db, branch, 'prices', branch['sourceFile'], branch['publishedAt'], branch['fetchedAt'], list(snapshot_rows(catalog, branch)))
            promotions = build_promotions(False, [target], ROOT / 'tmp/worker-promotions.json', catalog=catalog)
            import_catalog(db, catalog, promotions)
            rpc(db, 'finish_catalog_job', p_id=claimed['id'], p_success=True)
        except Exception as error:
            failed += 1
            rpc(db, 'finish_catalog_job', p_id=claimed['id'], p_success=False, p_error=str(error)[:500])
            print(json.dumps({'store': target, 'error': str(error)[:200]}), flush=True)
    publish_releases(db)
    if failed: raise RuntimeError(f'{failed} catalog job(s) failed; previous published versions preserved')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--max-jobs', type=int, default=3)
    parser.add_argument('--release-only', action='store_true')
    parser.add_argument('--images', type=int, default=0)
    parser.add_argument('--retailer-images', type=int, default=0)
    parser.add_argument('--chain', choices=list(CHAINS_BY_KEY))
    args = parser.parse_args()
    db = load_environment()
    if args.retailer_images:
        from retailer_images import prepare
        prepare(db,max(0,min(args.retailer_images,5000)))
    if args.images:
        from catalog_images import prepare
        prepare(db, max(0, min(args.images, 20)))
    if args.seed:
        catalog = json.loads((ROOT / 'apps/mobile/src/prototype/catalog-data.json').read_text(encoding='utf-8'))
        promotions = json.loads((ROOT / 'apps/mobile/src/prototype/promotions-data.json').read_text(encoding='utf-8'))
        register(db, catalog)
        import_catalog(db, catalog, promotions)
        publish_releases(db)
        print(json.dumps({'planned_jobs': rpc(db, 'plan_catalog_jobs')}), flush=True)
    elif args.release_only: publish_releases(db)
    else: consume(db, max(1, min(args.max_jobs, 12)),args.chain)


if __name__ == '__main__':
    main()
