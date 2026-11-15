import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]))
from product_groups import matches,GROUPS,measurements,build_groups

CRITERIA={g[0]:g[-1] for g in GROUPS}
def product(name,detail='',unit='шт.',**extra):
    return {'id':'test','original':name,'detail':detail,'unit':unit,'source':'official',**extra}

class ProductGroupTests(unittest.TestCase):
    def check(self,group,yes,no,unit='шт.'):
        for name in yes:
            with self.subTest(accept=name):self.assertTrue(matches(product(name,unit=unit,approximate=unit=='кг'),CRITERIA[group]))
        for name in no:
            with self.subTest(reject=name):self.assertFalse(matches(product(name,unit=unit,approximate=unit=='кг'),CRITERIA[group]))
    def test_milk_requires_fresh_same_fat_size_without_special_diets(self):
        self.check('milk-3-1l',['חלב טרי תנובה 3% 1 ליטר'],['חלב 3% 1 ליטר','חלב טרי 1% 1 ליטר','חלב טרי 3% 2 ליטר','חלב טרי 3% ללא לקטוז 1 ליטר','חלב טרי 3% עמיד 1 ליטר','חלב טרי 3% 2x1 ליטר'])
    def test_volume_never_converted_to_weight(self):
        self.check('sour-cream-15-200ml',['שמנת חמוצה 15% 200 מל'],['שמנת חמוצה 15% 200 גרם','שמנת חמוצה 15% 200 מל 200 גרם','שמנת להקצפה 15% 200 מל'])
        self.assertEqual(measurements('1 ליטר 1000 מל'),{(1000,'ml')})
    def test_special_sugar_flour_rice_not_standard(self):
        self.check('sugar-white-1kg',['סוכר לבן 1 קג'],['אבקת סוכר לבן 1 קג','סוכר לבן ענבים 1 קג','מעדן פרי סוכר לבן 1 קג','סוכר גולדן 1 קג'])
        self.check('flour-white-1kg',['קמח חיטה לבן 1 קג'],['קמח חיטה 80% 1 קג','קמח לבן תופח 1 קג','קמח אורז לבן 1 קג','קמח לבן פיצה 1 קג'])
        self.check('rice-basmati-1kg',['אורז בסמטי 1 קג'],['אורז בסמטי מלא 1 קג','אורז בסמטי מוזהב 1 קג','אורז בסמטי 5 קג'])
    def test_eggs_size_count_and_cottage(self):
        self.check('eggs-l-12',['ביצים L 12 יחידות'],['ביצים XL 12 יחידות','ביצים M 12 יחידות','ביצים L 30 יחידות','ביצים L חופש 12 יחידות'])
        self.check('cottage-5-250g',["קוטג׳ תנובה 5% 250 גרם"],["קוטג׳ 5% זיתים 250 גרם","קוטג׳ 3% 250 גרם","קוטג׳ 5% 100 גרם"])
    def test_weighted_excludes_plu_false_positives_and_different_cultivars(self):
        self.check('cucumber-kg',['מלפפון','מלפפונים ישראל'],['מלפפון בלאדי','מלפפון במלח','מלפפון ירקות','מלפפון 1 קג','מלפפון ארוז'],unit='кг')
        self.check('tomato-kg',['עגבניה','עגבניות'],['עגבניות שרי','עגבניות מגי','עגבניות גבינה','עגבניות מיובשות'],unit='кг')
        self.check('banana-kg',['בננה','בננות'],['בננה ציפס','בננות מיובשות'],unit='кг')
        self.check('apple-gala-kg',['תפוח עץ גאלה'],['תפוח עץ פינק','תפוח עץ גאלה אורגני','תפוח עץ גאלה 1 קג'],unit='кг')
    def test_requires_official_correct_unit_and_two_members(self):
        p=product('סוכר לבן 1 קג')
        self.assertFalse(matches({**p,'source':None},CRITERIA['sugar-white-1kg']))
        self.assertFalse(matches({**p,'unit':'кг'},CRITERIA['sugar-white-1kg']))
        self.assertEqual(build_groups([p]),([],[]))
        groups,members=build_groups([p,{**p,'id':'other'}])
        self.assertEqual(len(groups),1);self.assertEqual(len(members),2)
        self.assertEqual(members[0]['evidence']['method'],'strict_attributes')
