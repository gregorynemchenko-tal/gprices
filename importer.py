#!/usr/bin/env python3
"""Incremental importer for Israel's publishedprices Cerberus FTPS feeds.

The worker intentionally has no AI dependency. It lists public transparency
files, downloads only the newest requested snapshots, filters exact barcodes
while parsing, and writes normalized rows through the Supabase REST API.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import ftplib
import gzip
import hashlib
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as element_tree
from collections.abc import Iterable, Iterator, Sequence
from typing import Any


# The regulator historically linked url.retail.publishedprices.co.il, but the
# server certificate only covers *.publishedprices.co.il. The one-label alias
# resolves to the same public server and lets us retain strict TLS validation.
FTP_HOST = os.environ.get("PUBLISHEDPRICES_FTP_HOST", "url.publishedprices.co.il")
FTP_TIMEOUT_SECONDS = 90
REST_BATCH_SIZE = 350
VALID_BARCODE = re.compile(r"^[0-9]{5,20}$")
WEIGHT_UNIT = re.compile(r"(?:^|\s)(?:kg|кг|килограмм|ק.?ג|קילוגרם)(?:\s|$)", re.IGNORECASE)
PRODUCE_EXCLUSIONS = re.compile(
    r"מיובש|צ.?יפס|גרעינ|מיץ|רסק|מחית|שימור|מוחמץ|כבוש|בטעם|סלט|לחם|"
    r"ארוז|אריזה|מארז|שקית|ברשת|конфет|чипс|суш|вялен|семеч|сок|пюре|марин|"
    r"сол[её]н|салат|вкус|хлеб|упаков|pack",
    re.IGNORECASE,
)
PRODUCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern, re.IGNORECASE))
    for key, pattern in (
        ("potato", r"תפוח\s+אדמה|תפודי\s+אדמה|картоф"),
        ("sweet_potato", r"בטטה|батат"),
        ("tomato_cherry", r"עגבנ.*שרי|שרי.*עגבנ|помидор.*черри|томат.*черри"),
        ("tomato", r"עגבנ|помидор|томат"),
        ("cucumber_baby", r"מלפפון.*בייבי|בייבי.*מלפפון|мини.*огур|огур.*мини"),
        ("cucumber", r"מלפפון|огур"),
        ("apple", r"תפוח\s+עץ|תפוח|яблок"),
        ("banana", r"בננה|банан"),
        ("avocado", r"אבוקדו|авокад"),
        ("green_onion", r"בצל.*ירוק|ירוק.*בצל|зел[её]н.*лук"),
        ("red_onion", r"בצל.*אדום|אדום.*בצל|красн.*лук"),
        ("onion", r"בצל|лук"),
        ("garlic", r"שום|чеснок"),
        ("carrot", r"גזר|морков"),
        ("lemon", r"לימון|лимон"),
        ("clementine", r"קלמנטינ|клементин|мандарин"),
        ("orange", r"תפוז|апельсин"),
        ("grapes_light", r"ענב.*(?:לבן|ירוק)|(?:бел|зел[её]н).*виноград"),
        ("grapes_dark", r"ענב.*(?:שחור|אדום)|(?:ч[её]рн|красн).*виноград"),
        ("grapes", r"ענב|виноград"),
        ("watermelon", r"אבטיח|арбуз"),
        ("melon", r"מלון|дын"),
        ("pear", r"אגס|груш"),
        ("peach", r"אפרסק|персик"),
        ("nectarine", r"נקטרינ|нектарин"),
        ("plum", r"שזיף|слив"),
        ("kiwi", r"קיווי|киви"),
        ("strawberry", r"תות\s*שדה|клубник"),
        ("cauliflower", r"כרובית|цветн.*капуст"),
        ("cabbage", r"כרוב|капуст"),
        ("broccoli", r"ברוקולי|брокколи"),
        ("zucchini", r"קישוא|кабач"),
        ("eggplant", r"חציל|баклаж"),
        ("mushroom", r"פטרי|гриб"),
        ("lettuce", r"חסה|латук|листья\s+салата"),
        ("celery", r"סלרי|сельдер"),
        ("parsley", r"פטרוזיל|петруш"),
        ("cilantro", r"כוסברה|кинз"),
        ("dill", r"שמיר|укроп"),
        ("mint", r"נענע|мят"),
        ("corn", r"תירס|кукуруз"),
        ("radish", r"צנון|צנונית|редис"),
        ("pepper_red", r"פלפל.*אדום|גמבה|красн.*перец"),
        ("pepper_green", r"פלפל.*ירוק|ירוק.*פלפל|зел[её]н.*перец"),
        ("pepper_yellow", r"פלפל.*צהוב|צהוב.*פלפל|ж[её]лт.*перец"),
        ("pepper_orange", r"פלפל.*כתום|כתום.*פלפל|оранж.*перец"),
        ("pepper", r"פלפל|перец"),
    )
)


@dataclasses.dataclass(frozen=True)
class Chain:
    key: str
    code: str
    name: str
    username: str
    aliases: tuple[str, ...] = ()
    path: str = "/"
    password_env: str | None = None
    source_key_override: str | None = None
    feed_home_url: str = "https://url.publishedprices.co.il/login"

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.code, *self.aliases)

    @property
    def source_key(self) -> str:
        return self.source_key_override or f"publishedprices_{self.key}"


# Usernames and blank-password access are publicly listed by the regulator.
# Non-empty passwords are never committed and must be provided via env vars.
CHAINS: tuple[Chain, ...] = (
    Chain("osher_ad", "7290103152017", "Osher Ad", "osherad"),
    Chain("rami_levy", "7290058140886", "Rami Levy", "RamiLevi"),
    Chain("yohananof", "7290803800003", "Yohananof", "yohananof"),
    Chain("tiv_taam", "7290873255550", "Tiv Taam", "TivTaam"),
    Chain("dor_alon", "7290492000005", "Dor Alon", "doralon", ("729049000005",)),
    Chain("keshet_teamim", "7290785400000", "Keshet Teamim", "Keshet"),
    Chain("politzer", "7291059100008", "Politzer", "politzer"),
    Chain("stop_market", "7290639000004", "Stop Market", "Stop_Market", ("72906390",)),
    Chain("freshmarket", "7290876100000", "Freshmarket", "freshmarket"),
    Chain("salah_dabah", "7290526500006", "Salah Dabah", "SalachD", password_env="PUBLISHEDPRICES_SALAH_DABAH_PASSWORD"),
    Chain("super_yuda", "7290058198450", "Super Yuda", "yuda_ho", ("7290058177776",), "/Yuda", "PUBLISHEDPRICES_SUPER_YUDA_PASSWORD"),
    Chain("yellow", "7290644700005", "Yellow", "Paz_bo", password_env="PUBLISHEDPRICES_YELLOW_PASSWORD"),
)

CHAINS_BY_KEY = {chain.key: chain for chain in CHAINS}
CHAINS_BY_CODE = {code: chain for chain in CHAINS for code in chain.codes}


class ImportFailure(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int | None = None
    modified_at: str | None = None


@dataclasses.dataclass(frozen=True)
class SnapshotImportResult:
    count: int
    selected_barcodes: frozenset[str] = frozenset()
    skipped: bool = False


def produce_match_key(name: str) -> str | None:
    value = text(name).lower()
    if not value or PRODUCE_EXCLUSIONS.search(value):
        return None
    for key, pattern in PRODUCE_PATTERNS:
        if pattern.search(value):
            return key
    return None


def is_weight_unit(unit: str) -> bool:
    return bool(WEIGHT_UNIT.search(text(unit)))


def is_weighted_produce(name: str, unit: str, reported_weighted: bool) -> bool:
    return bool(produce_match_key(name)) and (reported_weighted or is_weight_unit(unit))


def scoped_snapshot_url(source_url: str, barcodes: set[str], include_weighted_produce: bool) -> str:
    if not barcodes and not include_weighted_produce:
        return source_url
    scopes: list[str] = []
    if barcodes:
        digest = hashlib.sha256(",".join(sorted(barcodes)).encode("ascii")).hexdigest()[:16]
        scopes.append(f"barcodes-{digest}")
    if include_weighted_produce:
        scopes.append("weighted-produce-v1")
    return f"{source_url}#scope={'+'.join(scopes)}"


def log(message: str, **fields: Any) -> None:
    suffix = " " + json.dumps(fields, ensure_ascii=False, default=str) if fields else ""
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] {message}{suffix}", flush=True)


def chunks(values: Sequence[dict[str, Any]], size: int = REST_BATCH_SIZE) -> Iterator[list[dict[str, Any]]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def child_text(element: element_tree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element:
        if local_name(child.tag) in wanted:
            return text(child.text)
    return ""


def descendants(element: element_tree.Element, name: str) -> Iterator[element_tree.Element]:
    wanted = name.lower()
    for descendant in element.iter():
        if local_name(descendant.tag) == wanted:
            yield descendant


def decimal(value: Any) -> float | None:
    try:
        parsed = float(text(value).replace(",", "."))
    except ValueError:
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def minor_units(value: Any) -> int | None:
    parsed = decimal(value)
    return round(parsed * 100) if parsed is not None else None


def boolean_flag(value: Any) -> bool:
    return text(value).lower() in {"1", "true", "yes", "כן"}


def digits(value: Any) -> str:
    return re.sub(r"\D", "", text(value))


def iso_datetime(value: Any, secondary: Any = None) -> str | None:
    raw = text(value)
    extra = text(secondary)
    if not raw:
        return None
    raw = re.sub(r"^(\d{4})(\d{2})(\d{2})$", r"\1-\2-\3", raw)
    if extra and "T" not in raw and " " not in raw:
        raw = f"{raw}T{extra}"
    raw = raw.replace("/", "-").replace(" ", "T", 1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw += "T00:00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d-%m-%YT%H:%M:%S", "%d-%m-%Y"):
            try:
                parsed = dt.datetime.strptime(raw, pattern)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def source_timestamp_from_filename(filename: str) -> tuple[int, str]:
    matches = re.findall(r"(?<!\d)(20\d{6,12})(?!\d)", filename)
    stamp = matches[-1] if matches else ""
    return (len(stamp), stamp)


def filename_matches_chain(filename: str, chain: Chain) -> bool:
    lowered = filename.lower()
    return any(code in lowered for code in chain.codes)


def latest_store_file(files: Sequence[RemoteFile], chain: Chain) -> RemoteFile | None:
    candidates = [
        remote for remote in files
        if re.match(r"^stores?", remote.name, re.IGNORECASE)
        and filename_matches_chain(remote.name, chain)
        and remote.name.lower().endswith((".xml", ".gz"))
    ]
    return max(candidates, key=lambda item: (source_timestamp_from_filename(item.name), item.modified_at or "", item.name), default=None)


def latest_store_snapshot(
    files: Sequence[RemoteFile], chain: Chain, prefix: str, store_id: str
) -> RemoteFile | None:
    normalized_store = str(int(store_id)) if store_id.isdigit() else store_id.lstrip("0") or "0"
    candidates: list[RemoteFile] = []
    for remote in files:
        if not remote.name.lower().startswith(prefix.lower()) or not filename_matches_chain(remote.name, chain):
            continue
        if not remote.name.lower().endswith((".xml", ".gz")):
            continue
        stem = re.sub(r"\.(?:xml|gz)$", "", remote.name, flags=re.IGNORECASE)
        for code in sorted(chain.codes, key=len, reverse=True):
            match = re.match(rf"^{re.escape(prefix)}[-_]?{re.escape(code)}[-_]?(.*)$", stem, re.IGNORECASE)
            if not match:
                continue
            # Feeds use both chain-store-timestamp and
            # chain-subchain-store-timestamp filename layouts. The store is
            # therefore the last token immediately before the timestamp.
            tokens = [token for token in re.split(r"[-_]", match.group(1)) if token]
            timestamp_index = next(
                (index for index, token in enumerate(tokens) if re.fullmatch(r"20\d{6,12}", token)),
                -1,
            )
            if timestamp_index <= 0:
                break
            candidate_store = tokens[timestamp_index - 1]
            candidate_store = str(int(candidate_store)) if candidate_store.isdigit() else candidate_store.lstrip("0") or "0"
            if candidate_store == normalized_store:
                candidates.append(remote)
            break
    return max(candidates, key=lambda item: (source_timestamp_from_filename(item.name), item.modified_at or "", item.name), default=None)


def open_xml_payload(payload: bytes, filename: str) -> io.BytesIO | gzip.GzipFile:
    stream = io.BytesIO(payload)
    return gzip.GzipFile(fileobj=stream) if filename.lower().endswith(".gz") or payload[:2] == b"\x1f\x8b" else stream


def parse_stores(payload: bytes, filename: str, chain: Chain) -> list[dict[str, Any]]:
    with open_xml_payload(payload, filename) as stream:
        root = element_tree.parse(stream).getroot()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for subchain in descendants(root, "subchain"):
        subchain_code = child_text(subchain, "SubChainID", "SubChainId") or None
        subchain_name = child_text(subchain, "SubChainName") or None
        for store in descendants(subchain, "store"):
            external_id = child_text(store, "StoreID", "StoreId")
            name = child_text(store, "StoreName", "Name")
            if not external_id or not name or external_id in seen:
                continue
            seen.add(external_id)
            rows.append(store_row(store, chain, external_id, name, subchain_code, subchain_name))
    if not rows:
        for store in descendants(root, "store"):
            external_id = child_text(store, "StoreID", "StoreId")
            name = child_text(store, "StoreName", "Name")
            if external_id and name and external_id not in seen:
                seen.add(external_id)
                rows.append(store_row(store, chain, external_id, name, None, None))
    return rows


def store_row(
    store: element_tree.Element,
    chain: Chain,
    external_id: str,
    name: str,
    subchain_code: str | None,
    subchain_name: str | None,
) -> dict[str, Any]:
    return {
        "chain_code": chain.code,
        "external_id": external_id,
        "subchain_code": subchain_code,
        "subchain_name": subchain_name,
        "name": name,
        "address": child_text(store, "Address") or None,
        "city_code": child_text(store, "City", "CityCode") or None,
        "postal_code": digits(child_text(store, "ZIPCode", "ZipCode")) or None,
        "is_active": True,
    }


def iter_price_rows(
    payload: bytes,
    filename: str,
    store_uuid: str,
    requested_barcodes: set[str],
    fetched_at: str,
    run_id: str,
    include_weighted_produce: bool = False,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    with open_xml_payload(payload, filename) as stream:
        for _, item in element_tree.iterparse(stream, events=("end",)):
            if local_name(item.tag) != "item":
                continue
            barcode = digits(child_text(item, "ItemCode"))
            price_minor = minor_units(child_text(item, "ItemPrice"))
            name = child_text(item, "ItemName", "ManufactureItemDescription") or barcode
            unit = child_text(item, "UnitOfMeasure", "UnitQty") or ""
            unit_price_minor = minor_units(child_text(item, "UnitOfMeasurePrice"))
            reported_weighted = boolean_flag(child_text(item, "bIsWeighted"))
            weighted_produce = is_weighted_produce(name, unit, reported_weighted)
            selected = (
                (bool(requested_barcodes) and barcode in requested_barcodes)
                or (include_weighted_produce and weighted_produce)
                or (not requested_barcodes and not include_weighted_produce)
            )
            if VALID_BARCODE.fullmatch(barcode) and price_minor is not None and price_minor > 0 and selected:
                updated_at = iso_datetime(child_text(item, "PriceUpdateTime"))
                normalized_unit_price = unit_price_minor if unit_price_minor and unit_price_minor > 0 else None
                product = {
                    "barcode": barcode,
                    "name_original": name,
                    "manufacturer_name": child_text(item, "ManufactureName") or None,
                    "quantity": decimal(child_text(item, "Quantity")),
                    "unit": unit or None,
                    "is_weighted": reported_weighted or is_weight_unit(unit),
                    "source_updated_at": updated_at,
                }
                price = {
                    "store_id": store_uuid,
                    "barcode": barcode,
                    "price_minor": normalized_unit_price if weighted_produce and normalized_unit_price else price_minor,
                    "unit_price_minor": normalized_unit_price,
                    "currency": "ILS",
                    "source_updated_at": updated_at,
                    "source_fetched_at": fetched_at,
                    "ingestion_run_id": run_id,
                }
                yield product, price
            item.clear()


def promotion_datetime(promotion: element_tree.Element, prefix: str) -> str | None:
    direct = child_text(promotion, f"Promotion{prefix}DateTime")
    if direct:
        return iso_datetime(direct)
    return iso_datetime(
        child_text(promotion, f"Promotion{prefix}Date"),
        child_text(promotion, f"Promotion{prefix}Hour"),
    )


def iter_promotion_rows(
    payload: bytes,
    filename: str,
    store_uuid: str,
    requested_barcodes: set[str],
    fetched_at: str,
    run_id: str,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    with open_xml_payload(payload, filename) as stream:
        for _, promotion in element_tree.iterparse(stream, events=("end",)):
            if local_name(promotion.tag) != "promotion":
                continue
            promotion_code = child_text(promotion, "PromotionID")
            description = child_text(promotion, "PromotionDescription")
            starts_at = promotion_datetime(promotion, "Start")
            ends_at = promotion_datetime(promotion, "End")
            if not promotion_code or not starts_at or not ends_at:
                promotion.clear()
                continue
            club_id = child_text(promotion, "ClubID")
            member_only = bool(club_id) and not re.match(r"^0(?:\D|$)", club_id)
            coupon_required = boolean_flag(child_text(promotion, "AdditionalIsCoupon"))
            updated_at = iso_datetime(child_text(promotion, "PromotionUpdateTime"))
            default_min_quantity = decimal(child_text(promotion, "MinNoOfItemOffered")) or 1
            for group in descendants(promotion, "group"):
                min_purchase_minor = max(minor_units(child_text(group, "MinPurchaseAmount")) or 0, 0)
                for item in descendants(group, "promotionitem"):
                    barcode = digits(child_text(item, "ItemCode"))
                    if not VALID_BARCODE.fullmatch(barcode) or (requested_barcodes and barcode not in requested_barcodes):
                        continue
                    promotion_price_minor = minor_units(child_text(item, "DiscountedPrice"))
                    min_quantity = decimal(child_text(item, "MinQty")) or default_min_quantity
                    if min_quantity <= 0:
                        continue
                    product = {"barcode": barcode, "name_original": description or barcode}
                    row = {
                        "store_id": store_uuid,
                        "promotion_code": promotion_code,
                        "barcode": barcode,
                        "description": description or None,
                        "promotion_price_minor": promotion_price_minor if promotion_price_minor and promotion_price_minor > 0 else None,
                        "discount_rate": decimal(child_text(item, "DiscountRate")),
                        "min_quantity": min_quantity,
                        "min_purchase_minor": min_purchase_minor,
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "member_only": member_only,
                        "coupon_required": coupon_required,
                        "is_simple_price": bool(promotion_price_minor and promotion_price_minor > 0) and min_quantity <= 1 and min_purchase_minor <= 0 and not member_only and not coupon_required,
                        "source_updated_at": updated_at,
                        "source_fetched_at": fetched_at,
                        "ingestion_run_id": run_id,
                    }
                    yield product, row
            promotion.clear()


class SupabaseRest:
    def __init__(self, base_url: str, service_role_key: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.dry_run = dry_run

    def request(
        self,
        method: str,
        table: str,
        params: dict[str, str] | None = None,
        body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        if self.dry_run and method != "GET":
            return []
        query = urllib.parse.urlencode(params or {}, safe="(),.*")
        url = f"{self.base_url}/rest/v1/{table}" + (f"?{query}" if query else "")
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {
            "apikey": self.service_role_key,
            "authorization": f"Bearer {self.service_role_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        if prefer:
            headers["prefer"] = prefer
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ImportFailure(f"Supabase {method} {table} returned {error.code}: {detail[:500]}") from error

    def upsert(self, table: str, rows: Sequence[dict[str, Any]], on_conflict: str, ignore_duplicates: bool = False) -> None:
        if not rows:
            return
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        for batch in chunks(rows):
            self.request("POST", table, {"on_conflict": on_conflict}, batch, f"resolution={resolution},return=minimal")

    def start_run(self, chain: Chain, kind: str, source_url: str, store_id: str | None = None) -> str:
        if self.dry_run:
            return "00000000-0000-0000-0000-000000000000"
        rows = self.request("POST", "retail_ingestion_runs", body=[{
            "chain_code": chain.code,
            "store_external_id": store_id,
            "feed_kind": kind,
            "source_url": source_url,
        }], prefer="return=representation")
        if not rows:
            raise ImportFailure("Supabase did not return an ingestion run id")
        return rows[0]["id"]

    def finish_run(self, run_id: str, count: int, error: str | None = None) -> None:
        if self.dry_run:
            return
        self.request("PATCH", "retail_ingestion_runs", {"id": f"eq.{run_id}"}, {
            "status": "failed" if error else "completed",
            "record_count": count,
            "error_message": error,
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, "return=minimal")

    def completed(self, source_url: str) -> bool:
        if self.dry_run:
            return False
        rows = self.request("GET", "retail_ingestion_runs", {
            "select": "id",
            "status": "eq.completed",
            "source_url": f"eq.{source_url}",
            "limit": "1",
        })
        return bool(rows)

    def store(self, chain: Chain, external_id: str) -> dict[str, Any] | None:
        rows = self.request("GET", "retail_stores", {
            "select": "id,external_id",
            "chain_code": f"eq.{chain.code}",
            "external_id": f"eq.{external_id}",
            "limit": "1",
        })
        return rows[0] if rows else None


class CerberusClient:
    def __init__(self, chain: Chain, host: str = FTP_HOST):
        self.chain = chain
        self.host = host
        self.password = os.environ.get(chain.password_env, "") if chain.password_env else ""
        if chain.password_env and not self.password:
            raise ImportFailure(f"{chain.name} requires environment variable {chain.password_env}")

    def _connect(self) -> ftplib.FTP_TLS:
        context = ssl.create_default_context()
        ftp = ftplib.FTP_TLS(context=context, timeout=FTP_TIMEOUT_SECONDS)
        ftp.connect(self.host, 21)
        ftp.login(self.chain.username, self.password)
        ftp.prot_p()
        ftp.set_pasv(True)
        if hasattr(ftp, "trust_server_pasv_ipv4_address"):
            ftp.trust_server_pasv_ipv4_address = True
        ftp.cwd(self.chain.path)
        return ftp

    def list(self) -> list[RemoteFile]:
        def operation() -> list[RemoteFile]:
            ftp = self._connect()
            try:
                result: list[RemoteFile] = []
                try:
                    for name, facts in ftp.mlsd():
                        if facts.get("type") == "file":
                            result.append(RemoteFile(name, int(facts["size"]) if facts.get("size", "").isdigit() else None, facts.get("modify")))
                except ftplib.error_perm:
                    result.extend(RemoteFile(name) for name in ftp.nlst())
                return result
            finally:
                close_ftp(ftp)
        return retry(operation, f"list {self.chain.name}")

    def download(self, remote: RemoteFile) -> bytes:
        def operation() -> bytes:
            ftp = self._connect()
            output = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {remote.name}", output.write, blocksize=128 * 1024)
                return output.getvalue()
            finally:
                close_ftp(ftp)
        return retry(operation, f"download {self.chain.name}/{remote.name}")

    def source_url(self, remote: RemoteFile) -> str:
        return f"ftps://{self.host}{self.chain.path.rstrip('/')}/{remote.name}"


def retry(operation: Any, label: str, attempts: int = 4) -> Any:
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (OSError, EOFError, ftplib.Error, socket.timeout) as error:
            if attempt == attempts:
                raise ImportFailure(f"{label} failed after {attempts} attempts: {error}") from error
            log("Transient FTPS error", operation=label, attempt=attempt, error=str(error))
            time.sleep(delay)
            delay = min(delay * 2, 15)


def close_ftp(ftp: ftplib.FTP_TLS) -> None:
    try:
        ftp.quit()
    except Exception:
        ftp.close()


class Importer:
    def __init__(self, database: SupabaseRest, force: bool = False):
        self.database = database
        self.force = force

    def ensure_chain(self, chain: Chain) -> None:
        self.database.upsert("retail_chains", [{
            "code": chain.code,
            "name": chain.name,
            "source_key": chain.source_key,
            "feed_home_url": chain.feed_home_url,
            "is_active": True,
        }], "code")

    def import_stores(self, chain: Chain, client: CerberusClient, files: Sequence[RemoteFile]) -> int:
        self.ensure_chain(chain)
        remote = latest_store_file(files, chain)
        if remote is None:
            raise ImportFailure(f"{chain.name}: no Stores file found")
        source_url = client.source_url(remote)
        if not self.force and self.database.completed(source_url):
            log("Store directory already imported", chain=chain.name, file=remote.name)
            return 0
        payload = client.download(remote)
        rows = parse_stores(payload, remote.name, chain)
        if not rows:
            raise ImportFailure(f"{chain.name}: Stores file was empty")
        run_id = self.database.start_run(chain, "stores", source_url)
        try:
            if not self.database.dry_run:
                self.database.request("PATCH", "retail_stores", {"chain_code": f"eq.{chain.code}"}, {"is_active": False}, "return=minimal")
            self.database.upsert("retail_stores", rows, "chain_code,external_id")
            self.database.finish_run(run_id, len(rows))
            log("Imported stores", chain=chain.name, stores=len(rows), file=remote.name)
            return len(rows)
        except Exception as error:
            self.database.finish_run(run_id, 0, str(error))
            raise

    def import_snapshot(
        self,
        chain: Chain,
        client: CerberusClient,
        files: Sequence[RemoteFile],
        store_id: str,
        kind: str,
        barcodes: set[str],
        include_weighted_produce: bool = False,
    ) -> SnapshotImportResult:
        prefix = "PriceFull" if kind == "prices_full" else "PromoFull"
        remote = latest_store_snapshot(files, chain, prefix, store_id)
        if remote is None:
            raise ImportFailure(f"{chain.name}: no {prefix} file found for store {store_id}")
        download_url = client.source_url(remote)
        source_url = scoped_snapshot_url(download_url, barcodes, include_weighted_produce if kind == "prices_full" else False)
        if not self.force and self.database.completed(source_url):
            log("Snapshot already imported", chain=chain.name, store=store_id, kind=kind, file=remote.name)
            return SnapshotImportResult(0, skipped=True)
        store = self.database.store(chain, store_id)
        if not store:
            raise ImportFailure(f"{chain.name}: store {store_id} is not in retail_stores; import stores first")
        payload = client.download(remote)
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        run_id = self.database.start_run(chain, kind, source_url, store_id)
        count = 0
        selected_barcodes: set[str] = set()
        products: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        try:
            iterator = iter_price_rows(
                payload,
                remote.name,
                store["id"],
                barcodes,
                fetched_at,
                run_id,
                include_weighted_produce=include_weighted_produce,
            ) if kind == "prices_full" else iter_promotion_rows(payload, remote.name, store["id"], barcodes, fetched_at, run_id)
            for product, row in iterator:
                selected_barcodes.add(row["barcode"])
                products.append(product)
                rows.append(row)
                if len(rows) >= REST_BATCH_SIZE:
                    self.flush_snapshot(kind, products, rows)
                    count += len(rows)
                    products.clear()
                    rows.clear()
            self.flush_snapshot(kind, products, rows)
            count += len(rows)
            field = "prices_fetched_at" if kind == "prices_full" else "promos_fetched_at"
            self.database.request("PATCH", "retail_stores", {"id": f"eq.{store['id']}"}, {field: fetched_at}, "return=minimal")
            self.database.finish_run(run_id, count)
            log(
                "Imported snapshot",
                chain=chain.name,
                store=store_id,
                kind=kind,
                rows=count,
                filtered_barcodes=len(barcodes),
                weighted_produce=include_weighted_produce,
                file=remote.name,
            )
            return SnapshotImportResult(count, frozenset(selected_barcodes))
        except Exception as error:
            self.database.finish_run(run_id, count, str(error))
            raise

    def flush_snapshot(self, kind: str, products: Sequence[dict[str, Any]], rows: Sequence[dict[str, Any]]) -> None:
        unique_products = {row["barcode"]: row for row in products}
        self.database.upsert("retail_products", list(unique_products.values()), "barcode", ignore_duplicates=(kind == "promos_full"))
        conflict = "store_id,barcode" if kind == "prices_full" else "store_id,promotion_code,barcode"
        table = "retail_prices" if kind == "prices_full" else "retail_promotions"
        if kind == "prices_full":
            unique_rows = {
                (row["store_id"], row["barcode"]): row
                for row in rows
            }
        else:
            unique_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
            for row in rows:
                key = (row["store_id"], row["promotion_code"], row["barcode"])
                current = unique_rows.get(key)
                candidate_rank = (
                    0 if row["is_simple_price"] else 1,
                    row["promotion_price_minor"] if row["promotion_price_minor"] is not None else sys.maxsize,
                    row["min_purchase_minor"],
                    row["min_quantity"],
                )
                current_rank = (
                    0 if current and current["is_simple_price"] else 1,
                    current["promotion_price_minor"] if current and current["promotion_price_minor"] is not None else sys.maxsize,
                    current["min_purchase_minor"] if current else sys.maxsize,
                    current["min_quantity"] if current else sys.maxsize,
                )
                if current is None or candidate_rank < current_rank:
                    unique_rows[key] = row
        self.database.upsert(table, list(unique_rows.values()), conflict)


def parse_store_targets(values: Sequence[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        if ":" not in value:
            raise ImportFailure(f"Invalid --store '{value}'; use chain_key:store_id")
        chain_value, store_id = value.split(":", 1)
        chain = CHAINS_BY_KEY.get(chain_value) or CHAINS_BY_CODE.get(chain_value)
        if not chain or not re.fullmatch(r"\d{1,12}", store_id):
            raise ImportFailure(f"Invalid --store '{value}'")
        result.setdefault(chain.key, []).append(store_id)
    return result


def selected_chains(value: str | None) -> list[Chain]:
    if not value or value == "all":
        return list(CHAINS)
    result: list[Chain] = []
    for key in value.split(","):
        chain = CHAINS_BY_KEY.get(key.strip()) or CHAINS_BY_CODE.get(key.strip())
        if not chain:
            raise ImportFailure(f"Unknown chain '{key}'. Use --list-chains.")
        if chain not in result:
            result.append(chain)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import publishedprices FTPS snapshots into Supabase")
    parser.add_argument("--action", choices=("stores", "prices", "all"), default="stores")
    parser.add_argument("--chains", help="Comma-separated chain keys/codes; default: all")
    parser.add_argument("--store", action="append", default=[], help="Target chain_key:store_id (repeatable)")
    parser.add_argument("--barcodes", default="", help="Comma-separated exact barcodes; empty imports the full selected snapshots")
    parser.add_argument(
        "--weighted-produce",
        action="store_true",
        help="Also import fresh weighted vegetables and fruit by name; with no barcodes imports only that subset",
    )
    parser.add_argument("--force", action="store_true", help="Reprocess snapshots already completed in the ingestion log")
    parser.add_argument("--dry-run", action="store_true", help="Download and parse without writing to Supabase")
    parser.add_argument("--list-chains", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_chains:
        for chain in CHAINS:
            availability = f"requires {chain.password_env}" if chain.password_env else "public blank-password access"
            print(f"{chain.key:16} {chain.code} {chain.name:14} {availability}")
        return 0
    base_url = os.environ.get("SUPABASE_URL", "")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise ImportFailure("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    database = SupabaseRest(base_url, service_key, dry_run=args.dry_run)
    importer = Importer(database, force=args.force)
    targets = parse_store_targets(args.store)
    barcodes = {digits(value) for value in args.barcodes.split(",") if VALID_BARCODE.fullmatch(digits(value))}
    failures: list[str] = []
    for chain in selected_chains(args.chains):
        if chain.password_env and not os.environ.get(chain.password_env):
            log("Skipping chain without configured public portal password", chain=chain.name, variable=chain.password_env)
            continue
        try:
            client = CerberusClient(chain)
            files = client.list()
            if args.action in {"stores", "all"}:
                importer.import_stores(chain, client, files)
            if args.action in {"prices", "all"}:
                for store_id in targets.get(chain.key, []):
                    price_result = importer.import_snapshot(
                        chain,
                        client,
                        files,
                        store_id,
                        "prices_full",
                        barcodes,
                        include_weighted_produce=args.weighted_produce,
                    )
                    promotion_barcodes = barcodes | set(price_result.selected_barcodes)
                    if promotion_barcodes:
                        importer.import_snapshot(chain, client, files, store_id, "promos_full", promotion_barcodes)
        except Exception as error:
            message = f"{chain.name}: {error}"
            failures.append(message)
            log("Chain import failed", chain=chain.name, error=str(error))
    if failures:
        raise ImportFailure("; ".join(failures))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportFailure as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
