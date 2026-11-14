import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_worker import snapshot_rows, store_key
from catalog_images import allowed_image, photo_code
from retailer_images import probe
from unittest.mock import Mock,patch
import urllib.error


class CatalogWorkerTests(unittest.TestCase):
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
