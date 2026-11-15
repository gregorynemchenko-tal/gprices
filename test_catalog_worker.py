import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_worker import snapshot_rows, store_key, publish
from catalog_images import allowed_image, photo_code
from retailer_images import probe
from unittest.mock import Mock,patch
import urllib.error
import json
from catalog_worker import publish_releases


class CatalogWorkerTests(unittest.TestCase):
    def test_release_keeps_every_page_of_products_prices_and_ready_photos(self):
        products=[{'id':f'gtin:{i:014d}','payload':{'id':f'gtin:{i:014d}','original':str(i)+'x'*800,'category':'Молочное'}} for i in range(5501)]
        tables={'catalog_products':products,
            'store_snapshots':[{'id':'snapshot','state':'published','store_id':'test:1','source_file_id':'file','kind':'prices'}],
            'source_files':[{'id':'file','filename':'PriceFull.gz','published_at':'2026-11-15T00:00:00Z','fetched_at':'2026-11-15T01:00:00Z'}],
            'retail_stores':[{'id':'test:1','chain_id':'test','external_id':'1','name':'Branch','address':'Address','city_code':'8300'}],
            'retail_chains':[{'id':'test','name':'Test','source_url':'https://example.test/'}],
            'retail_prices_current':[{'product_id':p['id'],'price_minor':700} for p in products],
            'product_images':[{'product_id':p['id'],'storage_path':None,'external_url':'https://img.rami-levy.co.il/product/7290004131074/small.jpg','source_url':None,'license':None,'version':'1'} for p in products[:1201]]}
        db=Mock(base_url='https://simjxoshjprrexdixbso.supabase.co')
        def read(method,table,params):
            key=params['order']
            self.assertNotEqual(params.get(key),'gt.','Empty cursors must not be sent to UUID columns')
            return [p for p in tables[table] if p[key]>params.get(key,'gt.')[3:]][:int(params['limit'])]
        db.request.side_effect=read
        uploads={}
        with patch('catalog_worker.rpc',side_effect=lambda _db,name,**body: [] if name=='get_product_groups' else {'groups':0,'members':0}) as group_rpc, patch('catalog_worker.upload',side_effect=lambda _db,bucket,path,data,mime:uploads.update({path:json.loads(data)})):
            publish_releases(db)
        self.assertEqual([c.args[1] for c in group_rpc.call_args_list],['sync_product_groups','get_product_groups'])
        manifest=uploads['manifest.json'];release=uploads[manifest['catalog']]
        rows=[p for name in release['productParts'] for p in uploads[name]]
        self.assertEqual(manifest['format'],2)
        self.assertEqual(release['productGroups'],[])
        self.assertGreaterEqual(len(release['productParts']),2)
        self.assertTrue(all(len(json.dumps(uploads[name],ensure_ascii=False,separators=(',',':')).encode())<=4*1024*1024 for name in release['productParts']))
        self.assertEqual(len(rows),5501)
        self.assertEqual(len(release['chains'][0]['prices']),5501)
        self.assertEqual(sum(p['image_status']=='ready' for p in rows),1201)

    def test_publication_waits_for_all_bounded_staging_batches(self):
        db=Mock()
        db.request.return_value=[{'state':'staging'}]
        batches=[]
        def call(_db,name,**body):
            if name=='begin_catalog_snapshot': return 'test-snapshot'
            if name=='stage_catalog_rows': batches.append(body['p_rows']);return len(body['p_rows'])
            if name=='publish_catalog_snapshot':
                self.assertEqual(sum(map(len,batches)),1001)
                self.assertEqual(body['p_expected_count'],1001)
                return {'status':'published'}
        with patch('catalog_worker.rpc',side_effect=call):
            publish(db,{'id':'test','storeId':'1'},'prices','test-missing-file.gz',None,None,[{'id':str(i)} for i in range(1001)])
        self.assertEqual(sorted(map(len,batches)),[1,500,500])

    def test_failed_staging_never_publishes_partial_snapshot(self):
        db=Mock();db.request.return_value=[{'state':'staging'}]
        def call(_db,name,**body):
            if name=='begin_catalog_snapshot':return 'test-snapshot'
            if name=='stage_catalog_rows':raise RuntimeError('network failure')
            raise AssertionError('Partial snapshot must not be published')
        with patch('catalog_worker.rpc',side_effect=call):
            with self.assertRaises(RuntimeError):
                publish(db,{'id':'test','storeId':'1'},'prices','test-missing-file.gz',None,None,[{'id':'1'}])

    def test_store_specific_price_never_uses_shared_display_price(self):
        product = {'id': 'gtin:07290004131074', 'price': 700}
        branch = {'id':'victory', 'storeId':'023', 'prices':{product['id']:735}}
        rows = list(snapshot_rows({'products':[product]},branch))
        self.assertEqual(rows[0]['price'],735)
        self.assertEqual(product['price'],700)
        self.assertEqual(store_key(branch),'victory:23')

    def test_only_explicit_licensed_host_for_cached_images(self):
        self.assertTrue(allowed_image('https://images.openfoodfacts.org/images/products/729/000/413/1074/front_en.jpg'))
        self.assertFalse(allowed_image('https://img.rami-levy.co.il/product/7290004131074/small.jpg'))
        self.assertFalse(allowed_image('http://images.openfoodfacts.org/images/products/x.jpg'))
        self.assertFalse(allowed_image('https://images.openfoodfacts.org.evil.test/images/products/x.jpg'))
        self.assertIsNone(photo_code('7290785400000:146'))
        self.assertEqual(photo_code('gtin:07290004131074'),'7290004131074')

    @patch('retailer_images.time.sleep')
    def test_retailer_photo_uses_head_and_rejects_html_placeholder(self,_sleep):
        response=Mock(status=200,url='https://img.rami-levy.co.il/product/7290004131074/small.jpg')
        response.headers={'Content-Type':'image/jpeg','Content-Length':'100'}
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        fetcher=Mock(return_value=response)
        self.assertTrue(probe('gtin:07290004131074',fetcher)['found'])
        self.assertEqual(fetcher.call_args.args[0].method,'HEAD')
        response.headers['Content-Type']='text/html'
        self.assertFalse(probe('gtin:07290004131074',fetcher)['found'])
