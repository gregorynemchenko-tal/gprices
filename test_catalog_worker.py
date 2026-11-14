import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).parents[1]))
from catalog_worker import snapshot_rows, store_key
from catalog_images import allowed_image, photo_code


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
