"""Bootstrap the eight additional networks from verified official snapshots."""
import argparse
import datetime as dt
import json
from pathlib import Path
from build_catalog import ROOT, build
from build_promotions import build as build_promotions
from catalog_worker import load_environment, import_catalog, publish_releases
from http_importer import CHAINS_BY_KEY

TARGETS = ['rami_levy:043', 'osher_ad:025', 'yohananof:028', 'tiv_taam:014',
           'freshmarket:051', 'shufersal:113', 'mahsanei_hashuk:277', 'super_bareket:007']

def connect(db, target, from_cache=False):
    key, number = target.split(':')
    chain = CHAINS_BY_KEY[key]
    catalog_path = ROOT / f'tmp/new-{key}-catalog.json'
    promotion_path = ROOT / f'tmp/new-{key}-promotions.json'
    if from_cache:
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        promotions = json.loads(promotion_path.read_text(encoding='utf-8'))
    else:
        catalog = build([target], catalog_path)
        promotions = build_promotions(False, [target], promotion_path, catalog=catalog)
    if len(catalog['chains']) != 1 or catalog['chains'][0]['id'] != key or int(catalog['chains'][0]['storeId']) != int(number):
        raise ValueError('Snapshot does not match requested branch')
    branch = catalog['chains'][0]
    if not promotions['sources'] or any(p['chainId'] != key for p in promotions['promotions']):
        raise ValueError('Promotions do not match requested chain')
    if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(branch['publishedAt']) > dt.timedelta(days=2):
        raise ValueError('Initial snapshot is not fresh')
    db.upsert('retail_chains', [{'id':key, 'code':chain.code, 'name':chain.name,
        'source_url':chain.feed_home_url, 'adapter':'publishedprices_https' if chain.username else 'https',
        'enabled':True}], 'id')
    stores = json.loads((ROOT / f'tmp/catalog-{key}-stores.json').read_text(encoding='utf-8'))
    db.upsert('retail_stores', [{'id':key+':'+str(int(s['external_id'])), 'chain_id':key,
        'external_id':str(int(s['external_id'])), 'name':s['name'], 'address':s.get('address'),
        'city_code':s.get('city_code'), 'enabled':int(s['external_id'])==int(number)} for s in stores], 'id')
    import_catalog(db, catalog, promotions)
    return {'chain':key,'store':number,'products':len(catalog['products']),'promotions':len(promotions['promotions'])}

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--from-cache',action='store_true')
    parser.add_argument('--chain',action='append',choices=[t.split(':')[0] for t in TARGETS])
    args=parser.parse_args()
    db=load_environment()
    results=[]
    for target in TARGETS:
        if args.chain and target.split(':')[0] not in args.chain: continue
        result=connect(db,target,args.from_cache)
        results.append(result)
        print(json.dumps(result),flush=True)
    publish_releases(db)
    print(json.dumps({'connected':results}),flush=True)
