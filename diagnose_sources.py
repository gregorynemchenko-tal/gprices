"""Read-only connection diagnostics; no credentials and no certificate bypass."""
import concurrent.futures
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

URLS = [
 'https://prices.carrefour.co.il/',
 'https://laibcatalog.co.il/webapi/api/getfiles?edi=7290696200003',
 'https://shop.hazi-hinam.co.il/Prices',
 'https://hazihinamprod01.blob.core.windows.net/regulatories/PriceFull7290700100008-000-201-20261114-005710.gz',
]

def probe(url):
    host=urllib.parse.urlsplit(url).hostname
    result={'host':host}
    try:
        result['addresses']=sorted(set(x[4][0] for x in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)))
        start=time.monotonic()
        with socket.create_connection((host,443),timeout=8) as raw:
            result['tcp_seconds']=round(time.monotonic()-start,2)
        for version in ['default','TLS1.2']:
            context=ssl.create_default_context()
            if version=='TLS1.2': context.maximum_version=ssl.TLSVersion.TLSv1_2
            start=time.monotonic()
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 Kabalator/0.2','Accept':'*/*'})
                with urllib.request.urlopen(req,timeout=15,context=context) as response:
                    result[version]={'status':response.status,'bytes_sampled':len(response.read(1024)),'seconds':round(time.monotonic()-start,2)}
            except urllib.error.HTTPError as error:
                result[version]={'status':error.code,'server':error.headers.get('server'),'body':error.read(180).decode(errors='replace')}
            except Exception as error: result[version]={'error':str(error),'seconds':round(time.monotonic()-start,2)}
    except Exception as error: result['connection_error']=str(error)
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    with concurrent.futures.ThreadPoolExecutor(4) as pool: list(pool.map(probe,URLS))
