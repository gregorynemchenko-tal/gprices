"""Preflight every available retailer photo with HEAD. Does not copy photos.

Results are persisted in batches; each product is rechecked after 30 days.
Open-license photographs in our Storage take precedence and are never replaced.
"""
import concurrent.futures
import threading
import time
import urllib.request
import urllib.error
from catalog_worker import rpc
from catalog_images import photo_code

_lock=threading.Lock()
_next_start=0.0

def probe(identity, fetcher=None):
    global _next_start
    code=photo_code(identity)
    if not code: return {'product_id':identity,'found':False,'transient':False,'url':None}
    url=f'https://img.rami-levy.co.il/product/{code}/small.jpg'
    # A shared start-rate limit applies across all eight connections.
    with _lock:
        delay=max(0,_next_start-time.monotonic())
        _next_start=max(_next_start,time.monotonic())+.18
    if delay: time.sleep(delay)
    try:
        req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Kabalator/0.3 (+https://github.com/gregorynemchenko-tal/gprices)'})
        with (fetcher or urllib.request.urlopen)(req,timeout=3.5) as response:
            found=(response.status==200 and response.headers.get('Content-Type','').split(';')[0] in ('image/jpeg','image/png','image/webp')
                   and int(response.headers.get('Content-Length','0'))>0 and response.url==url)
        return {'product_id':identity,'url':url,'found':found,'transient':False}
    except urllib.error.HTTPError as error:
        return {'product_id':identity,'url':url,'found':False,'transient':error.code not in (403,404,410)}
    except Exception:
        return {'product_id':identity,'url':url,'found':False,'transient':True}

def prepare(db,limit=2500):
    checked=found=transient=0
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        while checked<limit:
            candidates=rpc(db,'claim_retailer_image_batch',p_limit=min(250,limit-checked))
            if not candidates: break
            rows=list(pool.map(lambda p:probe(p['product_id']),candidates))
            rpc(db,'save_retailer_image_checks',p_rows=rows)
            checked+=len(rows);found+=sum(r['found'] for r in rows);transient+=sum(r['transient'] for r in rows)
            print(f'Retailer photos: checked={checked} found={found} retry={transient}',flush=True)
    return {'checked':checked,'found':found,'retry':transient}
