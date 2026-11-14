"""Public PromoFull snapshots for the prototype; no database or AI calls."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo
from build_catalog import ROOT, DEFAULT_TARGETS, product_key, published_at
from http_importer import CHAINS_BY_KEY, client_for_chain
from importer import (RemoteFile, latest_store_snapshot, open_xml_payload,
                      local_name, child_text, descendants, boolean_flag, decimal, minor_units)


def local_datetime(node, prefix):
    raw = child_text(node, f'Promotion{prefix}DateTime')
    if not raw:
        date = child_text(node, f'Promotion{prefix}Date')
        hour = child_text(node, f'Promotion{prefix}Hour') or ('23:59:59' if prefix == 'End' else '00:00:00')
        raw = date + 'T' + hour
    try:
        value = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.tzinfo is None: value = value.replace(tzinfo=ZoneInfo('Asia/Jerusalem'))
        return value.astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        return None


def parse_promotions(payload, filename, chain, branch, catalog, fetched_at):
    valid_products = {p['id']: p for p in catalog['products'] if p['id'] in branch['prices']}
    seen = set()
    with open_xml_payload(payload, filename) as stream:
        for _, promotion in ET.iterparse(stream, events=('end',)):
            if local_name(promotion.tag) != 'promotion': continue
            code = child_text(promotion, 'PromotionID')
            start, end = local_datetime(promotion, 'Start'), local_datetime(promotion, 'End')
            if not code or not start or not end or start > end:
                promotion.clear(); continue
            description = child_text(promotion, 'PromotionDescription')
            club = child_text(promotion, 'ClubID')
            member = (bool(club) and club != '0') or bool(re.search(r'מועדון|club', description, re.I))
            coupon = boolean_flag(child_text(promotion, 'AdditionalIsCoupon')) or bool(re.search(r'קופון|coupon', description, re.I))
            gift = boolean_flag(child_text(promotion, 'IsGiftItem'))
            groups = list(descendants(promotion, 'group'))
            if not groups:
                yield from parse_flat_promotion(promotion, chain, branch, valid_products, filename, fetched_at)
                promotion.clear()
                continue
            for group in groups:
                group_id = child_text(group, 'GroupID')
                purchase = max(minor_units(child_text(group, 'MinPurchaseAmount')) or 0, 0)
                for item in descendants(group, 'promotionitem'):
                    barcode = child_text(item, 'ItemCode').strip()
                    weighted = boolean_flag(child_text(item, 'bIsWeighted'))
                    identity = product_key(barcode, chain.code, weighted)
                    if identity not in valid_products: continue
                    price = minor_units(child_text(item, 'DiscountedPrice'))
                    quantity = decimal(child_text(item, 'MinQty'))
                    if quantity is not None and quantity <= 0: continue
                    reward = child_text(item, 'RewardType')
                    restrictions = child_text(promotion, 'AdditionalRestrictions')
                    hours = ' — '.join(filter(None, [child_text(promotion, 'PromotionStartHour'), child_text(promotion, 'PromotionEndHour')]))
                    days = child_text(promotion, 'PromotionDays')
                    simple = (len(groups) == 1 and reward == '3' and quantity == 1 and price is not None and price > 0
                              and not member and not coupon and not gift and purchase == 0 and not hours and not days and not restrictions)
                    unique = f'{chain.key}:{branch["storeId"]}:{code}:{group_id}:{barcode}:{reward}:{price}:{quantity}'
                    if unique in seen: continue
                    seen.add(unique)
                    yield {'id': unique, 'chainId': chain.key, 'productId': identity, 'description': description,
                           'startsAt': start, 'endsAt': end, 'memberOnly': member, 'clubId': club,
                           'couponRequired': coupon, 'gift': gift, 'minQuantity': quantity,
                           'minPurchase': purchase, 'maxQuantity': decimal(child_text(item, 'MaxQty')),
                           'rewardType': reward, 'offerPrice': price if price is not None and price > 0 else None, 'groupCount': len(groups), 'simplePrice': price if simple else None,
                           'discountRate': decimal(child_text(item, 'DiscountRate')),
                           'restrictions': restrictions, 'remarks': child_text(promotion, 'Remarks'),
                           'hours': hours, 'days': days, 'redemptionLimit': child_text(promotion, 'RedemptionLimit'),
                           'sourceFile': filename, 'publishedAt': published_at(filename), 'fetchedAt': fetched_at}
            promotion.clear()


def parse_flat_promotion(promotion, chain, branch, products, filename, fetched_at):
    """MOF flat PromotionItems/Item layout (Keshet); never infer bundle unit price."""
    restriction_nodes = list(descendants(promotion, 'additionalrestrictions'))
    flags = restriction_nodes[0] if restriction_nodes else promotion
    if child_text(flags, 'AdditionalIsActive') == '0': return
    description = child_text(promotion, 'PromotionDescription')
    club_ids = [child_text(c,'ClubId') for c in descendants(promotion,'clubs')]
    club = next((c for c in club_ids if c and c != '0'), '0')
    member = club != '0' or bool(re.search(r'מועדון|club',description,re.I))
    coupon = boolean_flag(child_text(flags,'AdditionalIsCoupon')) or bool(re.search(r'קופון|coupon',description,re.I))
    quantity = decimal(child_text(promotion,'MinQty'))
    price = minor_units(child_text(promotion,'DiscountedPrice'))
    reward = child_text(promotion,'RewardType')
    hours = ' — '.join(filter(None,[child_text(promotion,'PromotionStartHour'),child_text(promotion,'PromotionEndHour')]))
    purchase = max(minor_units(child_text(promotion,'MinPurchaseAmount')) or 0,0)
    restrictions = '; '.join(f'{e.tag}={e.text.strip()}' for e in flags
        if e.text and e.text.strip() not in ('0','') and local_name(e.tag) != 'additionalisactive')
    code = child_text(promotion,'PromotionId')
    seen = set()
    for container in descendants(promotion,'promotionitems'):
        for item in container:
            if local_name(item.tag) != 'item': continue
            barcode = child_text(item,'ItemCode')
            identity = product_key(barcode,chain.code,False)
            if identity not in products or identity in seen: continue
            seen.add(identity)
            gift = boolean_flag(child_text(item,'IsGiftItem')) or boolean_flag(child_text(flags,'AdditionalGiftCount'))
            yield {'id':f'{chain.key}:{branch["storeId"]}:{code}:flat:{barcode}:{reward}:{price}:{quantity}',
                'chainId':chain.key,'productId':identity,'description':description,
                'startsAt':local_datetime(promotion,'Start'),'endsAt':local_datetime(promotion,'End'),
                'memberOnly':member,'clubId':club,'couponRequired':coupon,'gift':gift,
                'minQuantity':quantity,'minPurchase':purchase,'maxQuantity':decimal(child_text(promotion,'MaxQty')),
                'rewardType':reward,'offerPrice':price if price is not None and price>0 else None,
                'groupCount':1,'simplePrice':None,'discountRate':decimal(child_text(promotion,'DiscountRate')),
                'restrictions':restrictions,'remarks':child_text(promotion,'Remarks'),'hours':hours,
                'days':child_text(promotion,'PromotionDays'),'redemptionLimit':child_text(promotion,'RedemptionLimit'),
                'sourceFile':filename,'publishedAt':published_at(filename),'fetchedAt':fetched_at}


def build(offline=False, targets=None, output=None, catalog=None):
    catalog = catalog or json.loads((ROOT / 'apps/mobile/src/prototype/catalog-data.json').read_text(encoding='utf-8'))
    cache = ROOT / 'tmp/catalog-cache'
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    promotions, sources, failures = [], [], []
    for target in targets or DEFAULT_TARGETS:
        key, store = target.split(':')
        chain = CHAINS_BY_KEY[key]
        try:
            branch = next(c for c in catalog['chains'] if c['id'] == key and int(c['storeId']) == int(store))
            if offline:
                files = [RemoteFile(p.name[len(key)+1:]) for p in cache.glob(key + '-PromoFull*.gz')]
            else:
                client = client_for_chain(chain)
                files = client.list()
            remote = latest_store_snapshot(files, chain, 'PromoFull', store)
            if not remote: raise ValueError('No full promotion snapshot')
            file = cache / (key + '-' + remote.name)
            if not file.exists(): file.write_bytes(client.download(remote))
            rows = list(parse_promotions(file.read_bytes(), remote.name, chain, branch, catalog, now))
            promotions.extend(rows)
            sources.append({'chainId': key, 'sourceFile': remote.name, 'publishedAt': published_at(remote.name), 'count': len(rows)})
            print(json.dumps({'chain': key, 'promotions': len(rows), 'simple': sum(p['simplePrice'] is not None for p in rows)}), flush=True)
        except Exception as error:
            failures.append({'chainId': key, 'error': type(error).__name__})
            print(json.dumps({'chain': key, 'error': str(error)[:150]}), flush=True)
    if not sources: raise RuntimeError('No sources loaded; previous snapshot preserved')
    data = {'generatedAt': now, 'sources': sources, 'failures': failures, 'promotions': promotions}
    target = output or ROOT / 'apps/mobile/src/prototype/promotions-data.json'
    staging = target.with_suffix('.staging.json')
    staging.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    staging.replace(target)
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--store', action='append')
    args = parser.parse_args()
    build(args.offline, args.store)
