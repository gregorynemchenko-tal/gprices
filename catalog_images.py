"""Prepare publicly licensed photos before the mobile application opens.

Only Open Food Facts CC BY-SA photos are copied. Retailer image rights are not
assumed from a publicly accessible URL. Never sends receipts or user IDs.
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from catalog_worker import rpc, upload


def photo_code(identity):
    if not identity.startswith('gtin:'): return None
    code = identity[5:]
    if len(code) != 14 or not code.isdigit(): return None
    significant = code.lstrip('0') or '0'
    return significant.zfill(8 if len(significant)<=8 else 13 if len(significant)<=13 else 14)


def allowed_image(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password and not parsed.port
            and parsed.hostname == 'images.openfoodfacts.org' and parsed.path.startswith('/images/products/'))


def prepare(db, limit=5):
    rpc(db, 'plan_catalog_images', p_limit=limit)
    for index in range(limit):
        claim = rpc(db, 'claim_catalog_image')
        if not claim: break
        identity = claim['job']['product_id']
        code = photo_code(identity)
        try:
            if not code: raise ValueError('Not a global barcode')
            if index: time.sleep(5.1)  # Open Food Facts API: at most 12 requests/minute.
            headers = {'User-Agent': 'Kabalator/0.2 (Israeli grocery catalog test)'}
            url = f'https://world.openfoodfacts.org/api/v3.6/product/{code}?fields=code,image_front_url,image_url'
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                data = json.load(response)
            product = data.get('product') or {}
            returned = str(product.get('code') or data.get('code') or '')
            if returned and returned.zfill(14) != code.zfill(14): raise ValueError('Image barcode mismatch')
            source = product.get('image_front_url') or product.get('image_url')
            if data.get('status') != 1 or not source or not allowed_image(source):
                db.request('PATCH', 'product_images', {'product_id': 'eq.' + identity},
                    {'status': 'missing', 'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
                rpc(db, 'finish_catalog_image', p_id=claim['id'], p_success=True)
                continue
            # Refuse unexpected formats, oversize objects, redirects to unrelated hosts.
            with urllib.request.urlopen(urllib.request.Request(source, headers=headers), timeout=30) as response:
                if not allowed_image(response.url): raise ValueError('Unexpected image redirect')
                image = response.read(5*1024*1024+1)
            if len(image)>5*1024*1024 or not image.startswith(b'\xff\xd8\xff'): raise ValueError('Expected JPEG within 5 MB')
            digest = hashlib.sha256(image).hexdigest()
            path = f'{code}/{digest}.jpg'
            upload(db, 'product-images', path, image, 'image/jpeg')
            db.request('PATCH', 'product_images', {'product_id': 'eq.' + identity}, {'status': 'ready',
                'storage_path': path, 'source_url': 'https://world.openfoodfacts.org/product/' + code,
                'license': 'CC BY-SA 3.0', 'sha256': digest, 'version': digest[:20],
                'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            rpc(db, 'finish_catalog_image', p_id=claim['id'], p_success=True)
            print(json.dumps({'image_ready': identity}), flush=True)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                db.request('PATCH', 'product_images', {'product_id': 'eq.' + identity},
                    {'status': 'missing', 'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
                rpc(db, 'finish_catalog_image', p_id=claim['id'], p_success=True)
            else:
                rpc(db, 'finish_catalog_image', p_id=claim['id'], p_success=False)
                print(json.dumps({'image_error': identity, 'status': error.code}), flush=True)
        except Exception as error:
            rpc(db, 'finish_catalog_image', p_id=claim['id'], p_success=False)
            print(json.dumps({'image_error': identity, 'error': type(error).__name__}), flush=True)
