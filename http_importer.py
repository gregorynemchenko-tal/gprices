#!/usr/bin/env python3
"""External importer for independent official retailer HTTP feeds."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
import time
from html.parser import HTMLParser
from collections.abc import Sequence

from importer import (
    Chain,
    Importer,
    ImportFailure,
    RemoteFile,
    SupabaseRest,
    digits,
)


HEADERS = {
    "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 FinanceIsrael/0.1 official-price-transparency-importer",
}

CARREFOUR = Chain(
    "carrefour",
    "7290055700007",
    "Carrefour",
    "",
    source_key_override="carrefour",
    feed_home_url="https://prices.carrefour.co.il/",
)
VICTORY = Chain(
    "victory",
    "7290696200003",
    "Victory",
    "",
    source_key_override="victory",
    feed_home_url="https://laibcatalog.co.il/victory/index.html",
)
HAZI_HINAM = Chain(
    "hazi_hinam", "7290700100008", "Hazi Hinam", "",
    source_key_override="hazi_hinam",
    feed_home_url="https://shop.hazi-hinam.co.il/Prices",
)
CHAINS = (CARREFOUR, VICTORY, HAZI_HINAM)
CHAINS_BY_KEY = {chain.key: chain for chain in CHAINS}


def request_bytes(url: str, referer: str) -> bytes:
    proxy = os.environ.get('CATALOG_FEED_PROXY_URL')
    if proxy:
        if urllib.parse.urlsplit(proxy).hostname != 'simjxoshjprrexdixbso.supabase.co':
            raise ImportFailure('Unexpected catalog bridge host')
        key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
        proxy += ('&' if '?' in proxy else '?') + 'forceFunctionRegion=eu-central-1'
        request = urllib.request.Request(proxy, data=json.dumps({'url': url}).encode(), method='POST', headers={
            'Content-Type': 'application/json', 'apikey': key, 'Authorization': 'Bearer ' + key, 'x-region': 'eu-central-1'})
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()
    request = urllib.request.Request(url, headers={**HEADERS, "Referer": referer})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def parse_carrefour_directory(html: str) -> tuple[str, list[RemoteFile]]:
    path_match = re.search(r"const\s+path\s*=\s*['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
    files_match = re.search(r"const\s+files\s*=\s*(\[[\s\S]*?\]);\s*(?:const|let)\s+", html, re.IGNORECASE)
    if not path_match or not files_match:
        raise ImportFailure("Carrefour directory format changed")
    try:
        entries = json.loads(files_match.group(1))
    except json.JSONDecodeError as error:
        raise ImportFailure("Carrefour directory JSON is invalid") from error
    files = [
        RemoteFile(str(entry.get("name", "")), entry.get("size"), entry.get("modified"))
        for entry in entries
        if entry.get("name")
    ]
    return path_match.group(1), files


class CarrefourClient:
    def __init__(self) -> None:
        self.chain = CARREFOUR
        self.path = ""

    def list(self) -> list[RemoteFile]:
        payload = request_bytes(self.chain.feed_home_url, self.chain.feed_home_url)
        self.path, files = parse_carrefour_directory(payload.decode("utf-8", errors="replace"))
        return files

    def source_url(self, remote: RemoteFile) -> str:
        if not self.path:
            raise ImportFailure("Carrefour directory must be listed before download")
        return f"https://prices.carrefour.co.il/{urllib.parse.quote(self.path)}/{urllib.parse.quote(remote.name)}"

    def download(self, remote: RemoteFile) -> bytes:
        return request_bytes(self.source_url(remote), self.chain.feed_home_url)


def parse_victory_directory(payload: bytes) -> list[RemoteFile]:
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ImportFailure("Victory directory JSON is invalid") from error
    if not isinstance(entries, list):
        raise ImportFailure("Victory directory format changed")
    return [
        RemoteFile(str(entry.get("fileName", "")), modified_at=str(entry.get("fileDate", "")))
        for entry in entries
        if entry.get("fileName")
    ]


class VictoryClient:
    directory_url = "https://laibcatalog.co.il/webapi/api/getfiles?edi=7290696200003"

    def __init__(self) -> None:
        self.chain = VICTORY

    def list(self) -> list[RemoteFile]:
        return parse_victory_directory(request_bytes(self.directory_url, self.chain.feed_home_url))

    def source_url(self, remote: RemoteFile) -> str:
        return f"https://laibcatalog.co.il/webapi/{self.chain.code}/{urllib.parse.quote(remote.name)}"

    def download(self, remote: RemoteFile) -> bytes:
        return request_bytes(self.source_url(remote), self.chain.feed_home_url)


class HaziDirectoryParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__()
        self.page_url = page_url
        self.files: dict[str, str] = {}
        self.pages: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urllib.parse.urljoin(self.page_url, href)
        parsed = urllib.parse.urlsplit(url)
        name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        if (parsed.scheme == "https" and parsed.hostname == "hazihinamprod01.blob.core.windows.net"
                and re.fullmatch(r"(?:Price(?:Full)?|Promo(?:Full)?|Stores?(?:Full)?)7290700100008[-_][\w-]+\.gz", name, re.I)):
            self.files[name] = url
        if parsed.hostname == "shop.hazi-hinam.co.il" and parsed.path.lower() == "/prices":
            page = urllib.parse.parse_qs(parsed.query).get("p", ["1"])[0]
            if page.isdigit() and int(page) > 1:
                self.pages.add(url)


class HaziHinamClient:
    """Read the chain's HTML directory, including pagination and store files."""
    def __init__(self):
        self.chain = HAZI_HINAM
        self.urls: dict[str, str] = {}

    def list(self) -> list[RemoteFile]:
        pending = [self.chain.feed_home_url, self.chain.feed_home_url + "?t=3"]
        visited: set[str] = set()
        self.urls = {}
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            if len(visited) >= 20:
                raise ImportFailure("Hazi Hinam directory page limit exceeded")
            if visited:
                time.sleep(1.2)
            visited.add(url)
            parser = HaziDirectoryParser(url)
            parser.feed(request_bytes(url, self.chain.feed_home_url).decode("utf-8-sig"))
            self.urls.update(parser.files)
            pending.extend(sorted(parser.pages - visited - set(pending)))
        if not self.urls:
            raise ImportFailure("Hazi Hinam directory contains no recognized files")
        return [RemoteFile(name) for name in self.urls]

    def source_url(self, remote: RemoteFile) -> str:
        return self.urls[remote.name]

    def download(self, remote: RemoteFile) -> bytes:
        return request_bytes(self.source_url(remote), self.chain.feed_home_url)


def client_for_chain(chain: Chain):
    return {CARREFOUR.code: CarrefourClient, VICTORY.code: VictoryClient,
            HAZI_HINAM.code: HaziHinamClient}[chain.code]()


def selected_chains(value: str | None) -> list[Chain]:
    if not value or value == "all":
        return list(CHAINS)
    result: list[Chain] = []
    for key in value.split(","):
        chain = CHAINS_BY_KEY.get(key.strip())
        if not chain:
            raise ImportFailure(f"Unknown HTTP chain '{key}'")
        if chain not in result:
            result.append(chain)
    return result


def parse_targets(values: Sequence[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        if ":" not in value:
            raise ImportFailure(f"Invalid --store '{value}'")
        key, store_id = value.split(":", 1)
        if key not in CHAINS_BY_KEY or not re.fullmatch(r"\d{1,8}", store_id):
            raise ImportFailure(f"Invalid --store '{value}'")
        result.setdefault(key, []).append(store_id)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Carrefour, Victory and Hazi Hinam official HTTP feeds")
    parser.add_argument("--action", choices=("stores", "prices", "all"), default="stores")
    parser.add_argument("--chains", default="all")
    parser.add_argument("--store", action="append", default=[])
    parser.add_argument("--barcodes", default="")
    parser.add_argument(
        "--weighted-produce",
        action="store_true",
        help="Also import fresh weighted vegetables and fruit by name; with no barcodes imports only that subset",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list-chains", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_chains:
        for chain in CHAINS:
            print(f"{chain.key:12} {chain.code} {chain.name}")
        return 0
    base_url = os.environ.get("SUPABASE_URL", "")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base_url or not service_key:
        raise ImportFailure("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    importer = Importer(SupabaseRest(base_url, service_key), force=args.force)
    targets = parse_targets(args.store)
    barcodes = {digits(value) for value in args.barcodes.split(",") if re.fullmatch(r"\d{5,20}", digits(value))}
    failures: list[str] = []
    for chain in selected_chains(args.chains):
        try:
            client = client_for_chain(chain)
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
            failures.append(f"{chain.name}: {error}")
    if failures:
        raise ImportFailure("; ".join(failures))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportFailure as error:
        print(f"ERROR: {error}")
        raise SystemExit(1)
