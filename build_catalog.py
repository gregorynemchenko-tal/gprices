"""Build the prototype catalogue from complete official branch price files.

No Supabase credentials or database writes. Downloaded public source files are
cached in tmp; the app receives product metadata and ordinary shelf prices.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from http_importer import CHAINS_BY_KEY, client_for_chain
from importer import (latest_store_snapshot, latest_store_file, parse_stores,
                      open_xml_payload, local_name, child_text, minor_units,
                      boolean_flag, decimal, RemoteFile)

ROOT = Path(__file__).resolve().parents[2] if Path(__file__).parent.name == 'retail-importer' else Path(__file__).resolve().parent
DEFAULT_TARGETS = ['carrefour:081', 'victory:023', 'hazi_hinam:201']
# Search vocabulary, not asserted complete/contextual translations. Hebrew
# source names remain authoritative and are always displayed above these hints.
VOCAB = [
 ('חלב', 'молоко молочный milk'), ('קוטג', 'коттедж творог cottage'),
 ('יוגורט', 'йогурт yogurt'), ('גבינה', 'сыр'), ('גבינות', 'сыр'),
 ('שמנת', 'сливки сметана'), ('חמאה', 'масло сливочное'), ('לבן', 'белый'),
 ('ביצים', 'яйца яйцо'), ('לחם', 'хлеб'), ('לחמניה', 'булочка'), ('לחמניות', 'булочки'),
 ('פיתה', 'пита'), ('פיתות', 'пита'), ('חלה', 'хала'), ('טורטיה', 'тортилья'),
 ('אורז', 'рис'), ('פסטה', 'паста макароны'), ('ספגטי', 'спагетти макароны'),
 ('קמח', 'мука'), ('סוכר', 'сахар'), ('מלח', 'соль'), ('פלפל', 'перец'),
 ('שמן זית', 'оливковое масло'), ('שמן', 'масло растительное'), ('שיבולת שועל', 'овсянка овсяные хлопья'),
 ('כוסמת', 'гречка'), ('עדשים', 'чечевица'), ('שעועית', 'фасоль'), ('חומוס', 'хумус нут'),
 ('טחינה', 'тхина тахини'), ('תירס', 'кукуруза'), ('אפונה', 'горошек горох'),
 ('רסק עגבניות', 'томатная паста'), ('עגבניות', 'помидор помидоры томат'), ('עגבניה', 'помидор помидоры'),
 ('מלפפון', 'огурец огурцы'), ('מלפפונים', 'огурцы огурец'), ('תפוח אדמה', 'картофель картошка'),
 ('תפוחי אדמה', 'картофель картошка'), ('תפוח עץ', 'яблоко яблоки'), ('תפוחים', 'яблоки яблоко'),
 ('גאלה', 'гала яблоки'), ('בננה', 'банан бананы'), ('בננות', 'бананы банан'),
 ('גזר', 'морковь'), ('בצל', 'лук'), ('שום', 'чеснок'), ('לימון', 'лимон'),
 ('תפוז', 'апельсин'), ('אבוקדו', 'авокадо'), ('ענבים', 'виноград'), ('תות', 'клубника'),
 ('אגס', 'груша'), ('אפרסק', 'персик'), ('אבטיח', 'арбуз'), ('מלון', 'дыня'),
 ('כרוב', 'капуста'), ('כרובית', 'цветная капуста'), ('ברוקולי', 'брокколи'),
 ('חסה', 'салат латук'), ('פטריות', 'грибы'), ('קישוא', 'кабачок'), ('חציל', 'баклажан'),
 ('עוף', 'курица куриный'), ('עופות', 'курица'), ('בקר', 'говядина'), ('הודו', 'индейка'),
 ('בשר', 'мясо'), ('טחון', 'фарш'), ('חזה', 'грудка'), ('כנפיים', 'крылышки'),
 ('נקניק', 'колбаса сосиски'), ('נקניקיות', 'сосиски'), ('פסטרמה', 'пастрома'),
 ('דג', 'рыба'), ('דגים', 'рыба'), ('סלמון', 'лосось семга'), ('טונה', 'тунец'),
 ('קפה', 'кофе'), ('תה', 'чай'), ('מים', 'вода'), ('מינרליים', 'минеральная'),
 ('סודה', 'газированная вода'), ('מיץ', 'сок'), ('קולה', 'кола'), ('בירה', 'пиво'), ('יין', 'вино'),
 ('שוקולד', 'шоколад'), ('עוגיות', 'печенье'), ('עוגה', 'торт кекс пирог'),
 ('וופל', 'вафли'), ('ופל', 'вафли'), ('סוכריות', 'конфеты'), ('דבש', 'мед'),
 ('ריבה', 'варенье джем'), ('בוטנים', 'арахис'), ('שקדים', 'миндаль'), ('אגוזים', 'орехи'),
 ('צימוקים', 'изюм'), ('תמרים', 'финики'), ('חטיף', 'снэк снек батончик'),
 ('במבה', 'бамба'), ('ביסלי', 'бисли'), ('ציפס', 'чипсы картофель фри'),
 ('קורנפלקס', 'кукурузные хлопья'), ('דגני בוקר', 'хлопья сухой завтрак'),
 ('גלידה', 'мороженое'), ('פיצה', 'пицца'), ('בורקס', 'бурекас'), ('קפוא', 'замороженный'),
 ('קטשופ', 'кетчуп'), ('מיונז', 'майонез'), ('חרדל', 'горчица'), ('רוטב', 'соус'),
 ('סויה', 'соя соевый'), ('שניצל', 'шницель'), ('טבעוני', 'веганский'),
 ('ללא גלוטן', 'без глютена'), ('ללא סוכר', 'без сахара'), ('ללא לקטוז', 'без лактозы'),
 ('דל לקטוז', 'низколактозный'), ('אורגני', 'органический'), ('מלא', 'цельный'),
 ('משחת שיניים', 'зубная паста'), ('מברשת שיניים', 'зубная щетка'),
 ('שמפו', 'шампунь'), ('סבון', 'мыло'), ('מרכך', 'кондиционер'), ('דאודורנט', 'дезодорант'),
 ('נייר טואלט', 'туалетная бумага'), ('מגבות נייר', 'бумажные полотенца'),
 ('אבקת כביסה', 'стиральный порошок'), ('כביסה', 'стирка'), ('ניקוי', 'чистящее уборка'),
 ('כלים', 'посуда'), ('שקיות', 'пакеты'), ('חיתולים', 'подгузники'),
 ('תנובה', 'тнува tnuva'), ('טרה', 'тара tara'), ('יטבתה', 'йотвата yotvata'),
 ('שטראוס', 'штраус strauss'), ('דנונה', 'данон danone'), ('אסם', 'осем osem'),
 ('תלמה', 'тельма telma'), ('עלית', 'элит elite'), ('אנג', 'анжел angel'),
 ('קוקה קולה', 'кока кола coca cola'), ('פפסי', 'пепси pepsi'), ('ספרייט', 'спрайт sprite'),
 ('פנטה', 'фанта fanta'), ('נביעות', 'невиот neviot'), ('מי עדן', 'мей эден'),
 ('סנו', 'сано sano'), ('קולגייט', 'колгейт colgate'), ('הד אנד שולדרס', 'хед энд шолдерс'),
 ('נסטלה', 'нестле nestle'), ('נוטלה', 'нутелла nutella'), ('היינץ', 'хайнц heinz'),
 ('ליטר', 'литр л'), ('גרם', 'грамм г'), ('יחידות', 'штук шт упаковка'),
]
CATEGORIES = [
 ('Уход и гигиена', '🧴', '#F0EAF5', r'שמפו|סבון|שיניים|דאודורנט|חיתולים|היגיינה'),
 ('Для дома', '🧹', '#EAF0E6', r'ניקוי|כביסה|טואלט|מגבות נייר|כלים|שקיות אשפה'),
 ('Напитки', '🧃', '#E7EEF8', r'משקה|קולה|מיץ|סודה|מינרליים|בירה|יין |קפה|תה |פפסי|ספרייט'),
 ('Сладости и снеки', '🍫', '#F4E5DF', r'שוקולד|עוגיות|וופל|ופל|סוכריות|חטיף|במבה|ביסלי'),
 ('Мясо и рыба', '🐟', '#F5E9E5', r'בשר|עוף|עופות|הודו|נקניק|פסטרמה|סלמון|טונה|דגים'),
 ('Хлеб и выпечка', '🍞', '#F7EBDD', r'לחם|לחמני|פיתה|פיתות|חלה|טורטיה'),
 ('Молочное', '🥛', '#EAF0F6', r'חלב|גבינ|קוטג|יוגורט|שמנת|חמאה'),
 ('Бакалея', '🍚', '#F3EDDB', r'אורז|פסטה|ספגטי|קמח|סוכר|מלח|שמן|כוסמת|עדשים|שעועית|חומוס|טחינה|קטשופ|מיונז|חרדל|רוטב'),
]

def normalize(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c))).replace('ё', 'е').split())

def product_key(code, chain, weighted):
    # Short/in-store identifiers cannot be equated between unrelated chains.
    valid = len(code) in (8, 12, 13, 14) and code.isdigit()
    if valid:
        total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(code[:-1])))
        valid = (10 - total % 10) % 10 == int(code[-1])
    base = code[-13:] if len(code) >= 13 else code
    prefix = int(base[:3]) if len(base) >= 3 and base[:3].isdigit() else -1
    restricted = 20 <= prefix <= 29 or 40 <= prefix <= 49 or 200 <= prefix <= 299
    return 'gtin:' + code.zfill(14) if valid and not weighted and not restricted else chain + ':' + code

def classify(name, weighted):
    if weighted:
        if re.search(r'בשר|עוף|בקר|דג|סלמון|הודו', name):
            return 'Мясо и рыба', '🐟', '#F5E9E5'
        if re.search(r'תפוח|עגבני|מלפפ|בננ|גזר|בצל|אבוקדו|ענבים|כרוב|חסה|פטריות|קישוא|חציל|לימון|תפוז|אגס|תות|אבטיח', name):
            return 'Овощи и фрукты', '🥬', '#E8EEDC'
    for category, symbol, tint, pattern in CATEGORIES:
        if re.search(pattern, name): return category, symbol, tint
    return 'Другое', '🛍', '#F0EEE7'

def published_at(filename):
    match = re.search(r'(20\d{6})[-_]?(\d{6})', filename)
    if not match: return None
    # Feed timestamps are local Israel times. zoneinfo is available in the
    # standard runtime; Windows installations may need the tzdata package.
    from zoneinfo import ZoneInfo
    value = dt.datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Jerusalem'))
    return value.astimezone(dt.timezone.utc).isoformat()

def build(targets, output, refresh=False, offline=False):
    products, chains, failures = {}, [], []
    cache = ROOT / 'tmp/catalog-cache'; cache.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for target in targets:
        key, store_id = target.split(':', 1)
        chain = CHAINS_BY_KEY[key]
        try:
            client = client_for_chain(chain)
            stores_cache = ROOT / ('tmp/catalog-' + key + '-stores.json')
            if offline:
                files = [RemoteFile(p.name[len(key)+1:]) for p in cache.glob(key + '-*.gz')]
                stores = json.loads(stores_cache.read_text(encoding='utf-8'))
            else:
                files = client.list()
                stores_file = latest_store_file(files, chain)
                if not stores_file: raise RuntimeError('Missing store directory')
                stores = parse_stores(client.download(stores_file), stores_file.name, chain)
                stores_cache.write_text(json.dumps(stores, ensure_ascii=False), encoding='utf-8')
            remote = latest_store_snapshot(files, chain, 'PriceFull', store_id)
            if not remote: raise RuntimeError('Missing full price file')
            store = next(s for s in stores if int(s['external_id']) == int(store_id))
            file = cache / (key + '-' + remote.name)
            if refresh or not file.exists(): file.write_bytes(client.download(remote))
            fetched = dt.datetime.fromtimestamp(file.stat().st_mtime, dt.timezone.utc).isoformat()
            prices, count = {}, 0
            with open_xml_payload(file.read_bytes(), remote.name) as stream:
                for _, item in ET.iterparse(stream, events=('end',)):
                    if local_name(item.tag) != 'item': continue
                    code = child_text(item, 'ItemCode').strip()
                    name = child_text(item, 'ItemName', 'ManufactureItemDescription').strip()
                    price = minor_units(child_text(item, 'ItemPrice'))
                    weighted = boolean_flag(child_text(item, 'bIsWeighted'))
                    if not re.fullmatch(r'\d{1,20}', code) or not name or price is None or price <= 0:
                        item.clear(); continue
                    identity = product_key(code, chain.code, weighted)
                    category, symbol, tint = classify(name, weighted)
                    matched_words = [ru for he, ru in VOCAB if re.search(r'(?<![א-ת])' + re.escape(he) + r'(?![א-ת])', name)]
                    keywords = ' '.join(matched_words)
                    hint = ', '.join(dict.fromkeys(words.split()[0] for words in matched_words))
                    display = 'Поиск: ' + hint if hint else 'Русское название пока не добавлено'
                    manufacturer = child_text(item, 'ManufactureName')
                    package_quantity = decimal(child_text(item, 'Quantity'))
                    package_unit = child_text(item, 'UnitOfMeasure', 'UnitQty')
                    packaging = f'{package_quantity:g} {package_unit}' if package_quantity and package_quantity > 0 else ''
                    detail = ' · '.join(part for part in [manufacturer, packaging, 'за кг' if weighted else ''] if part)
                    if identity not in products:
                        products[identity] = {'id': identity, 'original': name, 'name': display,
                            'detail': detail or 'Точная упаковка из каталога',
                            'category': category, 'symbol': symbol, 'tint': tint, 'quantity': 1,
                            'unit': 'кг' if weighted else 'шт.', 'price': price, 'frequency': 0,
                            'source': 'official', 'barcode': code, 'approximate': weighted, 'searchText': normalize(name + ' ' + manufacturer + ' ' + keywords + ' ' + code)}
                    if len(name) > len(products[identity]['original']):
                        products[identity]['original'] = name
                        products[identity]['name'] = display
                        products[identity]['detail'] = detail or products[identity]['detail']
                    variant = normalize(name + ' ' + manufacturer + ' ' + keywords + ' ' + code)
                    if variant not in products[identity]['searchText']:
                        products[identity]['searchText'] += ' ' + variant
                    prices[identity] = price; count += 1
                    item.clear()
            if not prices: raise RuntimeError('Empty price snapshot')
            chains.append({'id': key, 'name': chain.name, 'branch': store['name'], 'address': store['address'],
                'storeId': store['external_id'], 'cityCode': store['city_code'], 'distance': 'Расстояние не рассчитано',
                'color': {'carrefour': '#326CAC', 'victory': '#306EB2', 'hazi_hinam': '#9A5135'}[key],
                'prices': prices, 'sourceUrl': chain.feed_home_url, 'sourceFile': remote.name,
                'fetchedAt': fetched, 'publishedAt': published_at(remote.name), 'productCount': len(prices)})
            print(json.dumps({'chain': key, 'store': store['external_id'], 'rows': count, 'products': len(prices)}, ensure_ascii=True), flush=True)
        except Exception as error:
            failures.append({'chain': key, 'error': type(error).__name__})
            print(json.dumps({'chain': key, 'error': str(error)[:200]}, ensure_ascii=True), flush=True)
    if not chains: raise RuntimeError('No catalogue sources succeeded; previous output preserved')
    covered = set().union(*(set(c['prices']) for c in chains))
    products = {key: value for key, value in products.items() if key in covered}
    data = {'generatedAt': now, 'city': 'Ришон-ле-Цион', 'products': list(products.values()), 'chains': chains, 'failures': failures}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix('.staging.json')
    staging.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    staging.replace(output)
    print(json.dumps({'catalog_products': len(products), 'branches': len(chains), 'failed': len(failures)}), flush=True)
    return data

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--store', action='append', default=[])
    parser.add_argument('--output', type=Path, default=ROOT / 'apps/mobile/src/prototype/catalog-data.json')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--offline', action='store_true', help='Rebuild from already downloaded official files')
    args = parser.parse_args()
    build(args.store or DEFAULT_TARGETS, args.output, args.refresh, args.offline)
