"""Conservative, versioned equivalence rules. No fuzzy or AI price matching."""
import re
import unicodedata

RULE_VERSION = 'grocery-groups-1'
GROUPS = [
    ('milk-3-1l','Молоко 3%, 1 л','חלב 3% 1 ליטר','Молочное','шт.',{'kind':'milk','fat':3,'amount':1000,'measure':'ml'}),
    ('milk-1-1l','Молоко 1%, 1 л','חלב 1% 1 ליטר','Молочное','шт.',{'kind':'milk','fat':1,'amount':1000,'measure':'ml'}),
    ('cottage-5-250g','Коттедж 5%, 250 г',"קוטג׳ 5% 250 גרם",'Молочное','шт.',{'kind':'cottage','fat':5,'amount':250,'measure':'g'}),
    ('cottage-3-250g','Коттедж 3%, 250 г',"קוטג׳ 3% 250 גרם",'Молочное','шт.',{'kind':'cottage','fat':3,'amount':250,'measure':'g'}),
    ('sour-cream-15-200ml','Сметана 15%, 200 мл','שמנת חמוצה 15% 200 מל','Молочное','шт.',{'kind':'sour_cream','fat':15,'amount':200,'measure':'ml'}),
    ('sugar-white-1kg','Сахар белый, 1 кг','סוכר לבן 1 קג','Бакалея','шт.',{'kind':'sugar','amount':1000,'measure':'g'}),
    ('flour-white-1kg','Мука пшеничная белая, 1 кг','קמח חיטה לבן 1 קג','Бакалея','шт.',{'kind':'flour','amount':1000,'measure':'g'}),
    ('rice-basmati-1kg','Рис басмати белый, 1 кг','אורז בסמטי לבן 1 קג','Бакалея','шт.',{'kind':'basmati','amount':1000,'measure':'g'}),
    ('eggs-l-12','Яйца L, 12 штук','ביצים L 12 יחידות','Другое','шт.',{'kind':'eggs','size':'L','count':12}),
    ('cucumber-kg','Огурцы обычные, на вес','מלפפון רגיל במשקל','Овощи и фрукты','кг',{'kind':'cucumber','weighted':True,'organic':False}),
    ('tomato-kg','Помидоры обычные, на вес','עגבניות רגילות במשקל','Овощи и фрукты','кг',{'kind':'tomato','weighted':True,'organic':False}),
    ('banana-kg','Бананы, на вес','בננות במשקל','Овощи и фрукты','кг',{'kind':'banana','weighted':True,'organic':False}),
    ('potato-white-kg','Картофель белый, на вес','תפוח אדמה לבן במשקל','Овощи и фрукты','кг',{'kind':'potato_white','weighted':True,'organic':False}),
    ('apple-gala-kg','Яблоки Гала, на вес','תפוח עץ גאלה במשקל','Овощи и фрукты','кг',{'kind':'apple_gala','weighted':True,'variety':'gala','organic':False}),
]

def clean(value):
    return unicodedata.normalize('NFKC',value or '').lower().replace('״','"').replace('׳',"'").replace('’',"'")

def word(value):
    return r'(?<![א-תa-z])'+value+r'(?![א-תa-z])'

MEASURE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(קילוגרמים|קילוגרם|ק["\']?ג|kg|גרמים|גרם|גר|g|מיליליטר|מ["\']?ל|ml|ליטרים|ליטר|ליט|ל)(?![א-תa-z])',re.I)
MULTIPACK = re.compile(r'מארז|שליש|רביעי|שישי|ששיה|זוג|\d\s*[x×*]\s*\d|multipack',re.I)
SPECIAL = re.compile(r'אורגני|ללא|נטול|לקטוז|דל\s*לקטוז|עזים|עיזים|כבשים|טבעוני|צמחי|organic',re.I)

def measurements(text):
    result=set()
    for number,unit in MEASURE.findall(text):
        amount=float(number.replace(',','.'))
        if unit in ('kg','קג','ק"ג',"ק'ג",'קילוגרם','קילוגרמים'): amount*=1000; dimension='g'
        elif unit in ('g','גר','גרם','גרמים'): dimension='g'
        elif unit in ('ל','ליט','ליטר','ליטרים'): amount*=1000; dimension='ml'
        else: dimension='ml'
        result.add((round(amount,3),dimension))
    return result

def matches(product, criteria):
    name=clean(product.get('original',''))
    text=name+' '+clean(product.get('detail',''))
    kind=criteria['kind']
    if product.get('source')!='official' or SPECIAL.search(text): return False
    if criteria.get('weighted'):
        if product.get('unit')!='кг' or not product.get('approximate'): return False
        if re.search(r'ארוז|אריזה|מארז|שקית|ברשת|קפוא|מיובש|רסק|מיץ|מחית|שימור|כבוש|סלט|מוחמץ|חתוך|קלוף|פרוס|מוכן|בטעם|שרי|בייבי|מיני|ציפ|צ.?יפ|גבינ|בשר|עוף',name): return False
        # A weight product with a stated fixed package is not loose produce.
        if measurements(name): return False
        patterns={
            'cucumber':r'^(?:מלפפונים|מלפפון)(?:\s|$)',
            'tomato':r'^(?:עגבניה|עגבנייה|עגבניות)(?:\s|$)',
            'banana':r'^(?:בננה|בננות)(?:\s|$)',
            'potato_white':r'^תפוח(?:י)?\s+אדמה.*לבנ|^תפוח(?:י)?\s+אדמה.*לבן',
            'apple_gala':r'^תפוח.*(?:גאלה|גלה)',
        }
        if not re.search(patterns[kind],name): return False
        if kind in ('cucumber','tomato','banana'):
            # Closed qualifiers avoid pickles, mixed vegetable PLUs and vague
            # truncated names even when the feed marks them as weighted.
            simple=re.sub(r'[.,]','',name).strip()
            simple=re.sub(r'\s+(?:ישראל|רגיל|רגילים|רגילות|טרי|טריים|טריות|מובחר|מובחרת|מובחרות|טבעית|במשקל|בתפזורת)','',simple).strip()
            allowed={'cucumber':{'מלפפון','מלפפונים'},'tomato':{'עגבניה','עגבנייה','עגבניות'},'banana':{'בננה','בננות'}}
            if simple not in allowed[kind]: return False
        if kind=='tomato' and re.search(r'מגי|תמר|אשכול|לב.?שור|טייגר|כתום|צהוב|ירוק|שחור',name): return False
        if kind=='cucumber' and re.search(r'בלאדי|בלדי|חממה|פרסי|שדה',name): return False
        if kind=='potato_white' and re.search(r'אדום|סגול|בטטה|דזירה|גורמה',name): return False
        if kind=='apple_gala' and re.search(r'סמיט|זהוב|פוג|פינק|חרמון|קריפס',name): return False
        return True
    if product.get('unit')!='шт.' or product.get('approximate'): return False
    if kind=='eggs':
        return bool(re.search(word('ביצים'),name) and re.search(r'(?<![a-z])l(?![a-z])',name)
            and re.search(r'(?<!\d)12(?!\d)',name) and not re.search(r'חופש|אומגה|מועשר|xl|אורגני|30|18|24',name))
    if MULTIPACK.search(name): return False
    if measurements(text)!={(criteria['amount'],criteria['measure'])}: return False
    if 'fat' in criteria:
        fats={float(x.replace(',','.')) for x in re.findall(r'(\d+(?:[.,]\d+)?)\s*%',name)}
        if fats!={criteria['fat']}: return False
    if kind=='milk':
        reviewed={'gtin:07290000042442','gtin:07290110328887','gtin:07290102398072','gtin:07290004131074','gtin:07290000042008','gtin:07290000042015','gtin:07290000042435'}
        return bool(re.search(word('חלב'),name) and (product['id'] in reviewed or 'טרי' in name)
            and not re.search(r'משקה|עמיד|שוקו|בטעם|מועשר|סויה|שקדים|שיבולת|אורז|קוקוס|מרוכז|בריסטה|אבקה|להקצפה',name))
    if kind=='cottage':
        return bool(re.search(word('קוטג'),name) and not re.search(r'בצל|זית|שמיר|חריף|בטעם|תיבול|מתובל|מלוח',name))
    if kind=='sour_cream': return bool(re.search(r'שמנת\s+חמוצה',name))
    if kind=='sugar':
        return bool(re.search(r'^סוכר\s+לבן',name) and not re.search(r'חום|דמררה|קנים|אבק|תחליף|ממתיק|וניל|קוקוס|שקיות|קוביות|גולדן|ענבי|פרי',name))
    if kind=='flour':
        return bool(re.search(word('קמח'),name) and re.search(r'לבן',name)
            and not re.search(r'מלא|כוסמין|שיפון|תופח|קורנפלור|תירס|אורז|חומוס|שקדים|פיצה|לחם|עוג|פסטה|מניטובה|תערובת|00',name))
    if kind=='basmati':
        return bool(re.search(word('אורז'),name) and 'בסמטי' in name
            and not re.search(r'מלא|תערובת|מוכן|תבשיל|מהיר|פריכיות|מתובל|חום|מוזהב',name))
    return False

def build_groups(products):
    groups=[];members=[]
    for identity,ru,he,category,unit,criteria in GROUPS:
        selected=[p for p in products if matches(p,criteria)]
        if len(selected)<2: continue
        groups.append({'id':identity,'name_ru':ru,'name_he':he,'category':category,'unit':unit,
            'criteria':criteria,'rule_version':RULE_VERSION})
        for product in selected:
            members.append({'group_id':identity,'product_id':product['id'],
                'evidence':{'method':'strict_attributes','rule_version':RULE_VERSION,'original':product['original'],
                    'detail':product.get('detail',''),'unit':product['unit'],'criteria':criteria}})
    return groups,members
