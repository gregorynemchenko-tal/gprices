#!/usr/bin/env python3
"""Daily bounded official-feed refresh followed by basket price checks."""
from __future__ import annotations
import html
import os
import re
import sys
import time
import urllib.parse
from importer import Chain, CerberusClient, CHAINS_BY_CODE, Importer, ImportFailure, RemoteFile, SupabaseRest
from http_importer import CHAINS as HTTP_CHAINS, client_for_chain, request_bytes

SHUFERSAL = Chain('shufersal', '7290027600007', 'Shufersal', '', source_key_override='shufersal', feed_home_url='https://prices.shufersal.co.il/')

class ShufersalClient:
    def __init__(self, store_id):
        self.urls = {}
        self.store_id = store_id

    def list(self):
        files = []
        for prefix, category in [('PriceFull', 2), ('PromoFull', 4)]:
            url = f'https://prices.shufersal.co.il/FileObject/UpdateCategory?catID={category}&storeId={urllib.parse.quote(self.store_id)}'
            page = request_bytes(url, SHUFERSAL.feed_home_url).decode('utf-8')
            match = re.search(r'href="(https://pricesprodpublic\.blob\.core\.windows\.net/[^\"]+\.gz\?[^\"]+)"', page, re.I)
            if not match:
                raise ImportFailure(f'Shufersal did not publish {prefix}')
            source = html.unescape(match.group(1))
            name = urllib.parse.unquote(urllib.parse.urlparse(source).path.rsplit('/', 1)[-1])
            self.urls[name] = source
            files.append(RemoteFile(name))
        return files

    def source_url(self, remote):
        return self.urls[remote.name]

    def download(self, remote):
        return request_bytes(self.source_url(remote), SHUFERSAL.feed_home_url)

def import_target(database, target):
    code, store = target['chain_code'], target['external_id']
    barcodes = set(target['barcodes'])
    if len(barcodes) > 3000:
        raise ImportFailure('Barcode target limit exceeded')
    if not barcodes:
        return  # Never accidentally request a full unfiltered catalogue.
    if code == SHUFERSAL.code:
        chain, client = SHUFERSAL, ShufersalClient(store)
    elif code in {chain.code for chain in HTTP_CHAINS}:
        chain = next(chain for chain in HTTP_CHAINS if chain.code == code)
        client = client_for_chain(chain)
    else:
        chain = CHAINS_BY_CODE.get(code)
        if chain is None:
            raise ImportFailure('Unsupported chain')
        if chain.password_env and not os.environ.get(chain.password_env):
            raise ImportFailure('Portal password not configured')
        client = CerberusClient(chain)
    files = client.list()
    importer = Importer(database, force=True)
    # Force rereads the selected slice: an unchanged filename must not leave
    # source_fetched_at stale forever. Existing importer records ingestion health.
    errors = []
    for kind in ('prices_full', 'promos_full'):
        for attempt in range(3):
            try:
                importer.import_snapshot(chain, client, files, store, kind, barcodes)
                break
            except Exception:
                if attempt == 2:
                    errors.append(kind)
                else:
                    time.sleep(2 ** attempt)
    if errors:
        raise ImportFailure('Feed refresh failed: ' + ', '.join(errors))

def run(database, max_stores=100):
    targets = database.request('POST', 'rpc/get_basket_import_targets', body={})
    if len(targets) > max_stores:
        raise ImportFailure('Target limit exceeded; split worker workload before continuing')
    failures = 0
    for target in targets:
        try:
            import_target(database, target)
        except Exception:
            failures += 1
            # No provider URLs, credentials, receipt names or barcodes in logs.
            print(f"Refresh failed for chain {target['chain_code']}, store {target['external_id']}", file=sys.stderr)
    if targets and failures < len(targets):
        database.request('POST', 'rpc/refresh_basket_price_checks', body={})
    print(f'Daily basket check: {len(targets)} stores; {failures} failed')
    return 1 if failures else 0

if __name__ == '__main__':
    url, key = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise SystemExit('Server Supabase configuration is required')
    raise SystemExit(run(SupabaseRest(url, key)))
