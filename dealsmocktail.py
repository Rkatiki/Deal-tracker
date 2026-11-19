"""Read public DealsMocktail US listing data embedded in the page HTML."""

import json
import logging
import re
import subprocess
from decimal import Decimal
from urllib.parse import urlencode
from urllib.robotparser import RobotFileParser

from sheet_source import SourceError

LOG = logging.getLogger(__name__)
ORIGIN = "https://dealsmocktail.com"
AGENT = "DealTracker/1.0"
PUSH = re.compile(r"<script[^>]*>\s*self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.S)
ID = re.compile(r"[0-9a-fA-F-]{36}")
SLUG = re.compile(r"[a-z0-9_-]+", re.I)
DECODER = json.JSONDecoder(parse_float=Decimal)


def download(url):
    result = subprocess.run(
        [
            "curl", "--silent", "--show-error", "--fail", "--retry", "2",
            "--retry-max-time", "35", "--max-time", "20", "--max-filesize", "10000000",
            "--user-agent", AGENT, "--proto", "=https",
            "--write-out", "\n%{http_code} %{content_type}", url,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=65, check=False,
    )
    body, _, status = result.stdout.rpartition("\n")
    if result.returncode or not status.startswith("200 "):
        raise SourceError(f"DealsMocktail returned {status or 'no HTTP status'} for {url}")
    if not status.startswith(("200 text/html", "200 text/plain")):
        raise SourceError(f"DealsMocktail returned unexpected content type for {url}: {status}")
    return body


def parse_listing(page):
    chunks = []
    for match in PUSH.finditer(page):
        packet = json.loads(match[1])
        if len(packet) == 2 and packet[0] == 1 and isinstance(packet[1], str):
            chunks.append(packet[1])
    stream = "".join(chunks)
    marker = re.search(r'"initialDeals"\s*:\s*(?=\[)', stream)
    if not marker:
        raise SourceError("DealsMocktail listing has no embedded initialDeals array")
    records, _ = DECODER.raw_decode(stream, marker.end())
    if not isinstance(records, list) or not records:
        raise SourceError("DealsMocktail listing returned no deal records")
    rows = []
    for item in records:
        deal_id, title, slug = item.get("id"), item.get("title"), item.get("slug")
        if not isinstance(deal_id, str) or not ID.fullmatch(deal_id):
            raise SourceError("DealsMocktail listing record has an invalid ID")
        if not (isinstance(title, str) and title.strip() and isinstance(slug, str) and SLUG.fullmatch(slug)):
            raise SourceError("DealsMocktail listing record has no usable title or URL")
        if item.get("country") not in (None, "USA"):
            raise SourceError("DealsMocktail US listing returned an unexpected country")
        amount = item.get("salePrice")
        price = str(amount) if isinstance(amount, (int, Decimal)) and not isinstance(amount, bool) else ""
        rows.append({
            "id": deal_id, "title": title.strip(), "price": price,
            "retailer": str(item.get("store") or ""), "url": f"{ORIGIN}/us/deal/{slug}",
            "source_details": {
                "category": item.get("category"), "posted_at": item.get("postedAt"),
                "expires_at": item.get("expiresAt"), "advertised_price": item.get("originalPrice"),
            },
        })
        if isinstance(rows[-1]["source_details"]["advertised_price"], Decimal):
            rows[-1]["source_details"]["advertised_price"] = str(item["originalPrice"])
    return rows


def retrieve_website(config):
    robots = RobotFileParser()
    robots.parse(download(f"{ORIGIN}/robots.txt").splitlines())
    listings = config["dealsmocktail"]["listings"]
    tabs = []
    for name, category in listings.items():
        query = "?" + urlencode({"category": category}) if category else ""
        url = f"{ORIGIN}/us/deals{query}"
        if not robots.can_fetch(AGENT, url):
            raise SourceError(f"DealsMocktail robots.txt disallows {url}")
        rows = parse_listing(download(url))
        LOG.info("DealsMocktail %s: %d public listing offers", name, len(rows))
        tabs.append(({
            "gid": f"dealsmocktail:{category or 'latest'}", "name": name,
            "method": "public_listing_html", "count": len(rows), "url": url,
        }, rows))
    return tabs
