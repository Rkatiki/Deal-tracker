"""Refresh independent public deal feeds and build a cross-source discovery index."""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from sheet_source import deal_links, retailer_kind, safe_link
from tracker import ROOT, cents, changes, publish, rank, read_json

LOG = logging.getLogger(__name__)
DOLLARS = re.compile(r"(?<![\w$])(?:US\s*)?\$\s*(\d[\d,]*(?:\.\d{1,2})?)(?![\w.])", re.I)
PROMOTIONAL = re.compile(r"\b(?:off|savings|save|from|under|up to|was|msrp|reg\.?|cashback|cash back|credit|gift card|a month|per month|per year|monthly|annual(?:ly)?|per item)\b|/\s*(?:mo|yr|ea)\b", re.I)
DEAL_HEADLINE = re.compile(r"\b(?:deal[s]?|black friday|cyber monday|coupon|discount|freebie|clearance|sale|free shipping|save|price drop)\b|\$\d", re.I)
NON_US = re.compile(r"[£€]|\b(?:CA\$|AU\$|NZ\$|C\$|A\$|CAD\b|AUD\b)", re.I)
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
HREF = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.I)
URL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class FeedError(Exception):
    pass


def text_only(value):
    return " ".join(re.sub(r"</?[A-Za-z][^>]*>", "", html.unescape(value)).split())


def link_only(value):
    return safe_link(value) or ""


def fetch(url):
    if not link_only(url) or urlsplit(url).scheme != "https":
        raise FeedError("feed URL must be HTTPS")
    result = subprocess.run(
        [
            "curl", "--silent", "--show-error", "--fail", "--location", "--max-redirs", "4", "--compressed",
            "--max-time", "15", "--retry", "1", "--retry-max-time", "25", "--max-filesize", "6000000",
            "--proto", "=https", "--proto-redir", "=https", "--user-agent", "DealTracker/1.0",
            "--write-out", "\n%{http_code}", url,
        ],
        text=True, encoding="utf-8", capture_output=True, timeout=45, check=False,
    )
    body, _, status = result.stdout.rpartition("\n")
    if result.returncode or status != "200":
        raise FeedError(f"HTTP {status or 'unavailable'} (curl exit {result.returncode})")
    if len(body) > 6_000_000:
        raise FeedError("feed exceeded the response size limit")
    return body


def utc_date(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (ValueError, TypeError, OverflowError):
            return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def possible_price(title):
    found = DOLLARS.findall(title)
    if len(found) == 1 and not PROMOTIONAL.search(title) and not NON_US.search(title):
        return cents("$" + found[0])
    return None


def record(source, identity, title, url, *, price=None, retailer=None, published=None,
           expires=None, category=None, retailer_url=None, retailer_type=None, source_url=None):
    key = f"{source['id']}:{identity}".encode()
    deal_id = hashlib.sha256(key).hexdigest()[:24]
    return {
        "id": deal_id, "title": title, **deal_links(source_url or url, url, retailer_url, retailer_type),
        "retailer": retailer or source.get("retailer"),
        "price_cents": price, "headline_price_cents": possible_price(title) if price is None else None,
        "price_source": "publisher_structured_data" if price is not None else None,
        "currency": "USD" if price is not None else None,
        "source_id": source["id"], "publisher": source["name"],
        "published_at": published.isoformat() if published else None,
        "expires_at": expires.isoformat() if expires else None,
        "publication_category": category,
    }


def xml_entries(body, source, now, max_age):
    if "<!DOCTYPE" in body.upper():
        raise FeedError("XML declarations with a document type are unsupported")
    root = ElementTree.fromstring(body)
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    if not entries:
        raise FeedError("RSS/Atom feed contained no entries")
    offers = []
    recognized = 0
    for item in entries:
        fields = {}
        for child in item:
            key = child.tag.rsplit("}", 1)[-1]
            if key != "link" or child.get("rel", "alternate") == "alternate":
                fields.setdefault(key, child)

        def value(key):
            node = fields.get(key)
            return "".join(node.itertext()).strip() if node is not None else ""

        title = text_only(value("title"))
        href = fields["link"].get("href", "") if "link" in fields else ""
        url = link_only(value("link") or href)
        if not title or not url:
            continue
        recognized += 1
        published = utc_date(value("published") or value("pubDate") or value("updated"))
        expires = utc_date(value("expires"))
        if expires and expires < now:
            continue
        if published and published < now - max_age:
            continue
        if NON_US.search(title) or re.search(r"/(?:uk|au|ca)(?:/|$)", urlsplit(url).path):
            continue
        if source.get("deal_headlines_only") and not DEAL_HEADLINE.search(title):
            continue
        price_node = fields.get("price")
        price = cents(value("price")) if price_node is not None and price_node.get("currency") == "USD" else None
        retailer_url = link_only(value("retailerUrl") or value("affiliateLink") or value("affiliateUrl"))
        if not retailer_url and source["id"] in {"9to5toys", "9to5mac"}:
            links = {link_only(html.unescape(href)) for href in HREF.findall(value("description"))}
            merchant_links = {link for link in links if retailer_kind(link)}
            if len(merchant_links) == 1:
                retailer_url = merchant_links.pop()
        if not retailer_url and source.get("retailer") == "Woot":
            woot_links = {
                html.unescape(link) for link in HREF.findall(value("description") or value("encoded"))
                if re.fullmatch(r"https://(?:www\.)?woot\.com/offers/[\w-]+(?:\?[^\s]*)?", html.unescape(link))
            }
            if len(woot_links) == 1:
                retailer_url = woot_links.pop()
        offers.append(record(
            source, value("id") or value("guid") or url, title, url, price=price,
            retailer=value("retailer") or None, published=published, expires=expires,
            category=value("category") or None, retailer_url=retailer_url,
        ))
    if recognized < max(1, (len(entries) + 1) // 2):
        raise FeedError("most XML entries are missing a title or usable URL")
    return offers


def github_entries(body, source, now, _max_age):
    if "| Deal | Discount | Price | Expires | Source | Notes |" not in body:
        raise FeedError("GitHub list no longer exposes its expected deal table")
    offers = []
    recognized = 0
    category = None
    for line in body.splitlines():
        if line.startswith("## "):
            category = text_only(line[3:])
        if not line.startswith("| ["):
            continue
        columns = re.split(r"(?<!\\)\|", line.strip("|"))
        if len(columns) != 6:
            continue
        link = LINK.search(columns[0])
        if not link or not link_only(link[2]):
            continue
        recognized += 1
        title, url = text_only(link[1]), link[2]
        expires = utc_date(columns[3].strip()) if re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", columns[3]) else None
        if expires and expires.date() < now.date():
            continue
        price_cell = columns[2].strip()
        price = cents(price_cell) if price_cell.startswith(("$", "US$")) else None
        offers.append(record(
            source, url, title, url, price=price, expires=expires, category=category,
            source_url=source.get("homepage", source["url"]),
        ))
    if not recognized:
        raise FeedError("GitHub list contained no readable deal rows")
    return offers


def cheapshark_entries(body, source, _now, _max_age):
    items = json.loads(body)
    if not isinstance(items, list) or not items:
        raise FeedError("CheapShark returned no offers")
    offers = []
    for item in items:
        if item["storeID"] != "1":
            raise FeedError("CheapShark returned an unexpected retailer")
        if item["isOnSale"] != "1":
            continue
        url = f"https://www.cheapshark.com/redirect?dealID={quote(item['dealID'], safe='%')}"
        updated = datetime.fromtimestamp(item["lastChange"], tz=timezone.utc)
        offers.append(record(
            source, item["gameID"], item["title"], url, price=cents(item["salePrice"]),
            published=updated, category="PC games",
            retailer_url=f"https://store.steampowered.com/app/{item['steamAppID']}/"
            if str(item.get("steamAppID", "")).isdigit() else url,
            retailer_type="retailer" if str(item.get("steamAppID", "")).isdigit() else "retailer_redirect",
        ))
    return offers


PARSERS = {"rss": xml_entries, "github": github_entries, "cheapshark": cheapshark_entries}


def process(source, output, config, evidence, now, max_age):
    directory = output / source["id"]
    rows = PARSERS[source["kind"]](fetch(source["url"]), source, now, max_age)
    before = read_json(directory / "latest.json", {})
    history = read_json(directory / "price_history.json", {})
    if bool(before) != (directory / "price_history.json").exists():
        raise FeedError("snapshot and price history must be restored together")
    before_offers = {deal["id"]: deal for deal in before.get("deals", [])}
    offers = {deal["id"]: deal for deal in rows}
    for deal_id, deal in offers.items():
        price = deal["price_cents"]
        observations = history.get(deal_id, {}).get("observations", [])
        deal["rank"] = rank(deal, [o["price_cents"] for o in observations], evidence.get(deal_id, {}), config)
        if price is not None and (not observations or observations[-1]["price_cents"] != price):
            observations.append({"at": now.isoformat(), "price_cents": price})
            history[deal_id] = {
                "title": deal["title"], "retailer": deal["retailer"], "currency": "USD",
                "observations": observations,
            }
    payload = {"schema_version": 1, "retrieved_at": now.isoformat(), "source": source}
    snapshot = dict(payload, deals=sorted(offers.values(), key=lambda deal: (
        deal["rank"]["status"] != "candidate", -deal["rank"]["score"], deal["id"],
    )))
    publish(directory, {
        "price_history.json": history,
        "changes.json": dict(payload, baseline=not before, events=changes(before_offers, offers)),
        "latest.json": snapshot,
    })
    return snapshot


def refresh_all(manifest, config, evidence, output, now):
    sources = manifest["sources"]
    ids = [source["id"] for source in sources]
    if not ids or len(set(ids)) != len(ids) or any(not URL_ID.fullmatch(key) for key in ids):
        raise FeedError("configure unique source IDs using lowercase letters, numbers, and hyphens")
    max_age = timedelta(days=manifest["max_age_days"])
    status = []
    shortlist = []
    headline_leads = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = [pool.submit(process, source, output, config, evidence, now, max_age) for source in sources]
        for source, task in zip(sources, tasks):
            item = {
                "id": source["id"], "name": source["name"], "url": source.get("homepage", source["url"]),
                "snapshot": f"{source['id']}/latest.json", "status": "ok",
            }
            try:
                snapshot = task.result()
            except (FeedError, OSError, KeyError, ValueError, TypeError, ElementTree.ParseError, subprocess.SubprocessError) as exc:
                item.update(status="failed", error=str(exc))
                LOG.warning("%s: %s", source["name"], exc)
                snapshot = read_json(output / item["snapshot"], {})
            item["last_success_at"] = snapshot.get("retrieved_at")
            deals = snapshot.get("deals", [])
            candidates = [deal for deal in deals if deal["rank"]["status"] == "candidate"]
            item.update(offer_count=len(deals), candidate_count=len(candidates))
            if item["status"] == "ok":
                shortlist.extend(candidates)
                for deal in deals:
                    if deal["headline_price_cents"] is None:
                        continue
                    hypothetical = dict(deal, price_cents=deal["headline_price_cents"])
                    evaluation = rank(hypothetical, [], {}, config)
                    if evaluation["category"] != "other" and evaluation["within_budget"]:
                        headline_leads.append(dict(deal, headline_rank=evaluation))
                LOG.info("%s: %d entries, %d ranked candidates", source["name"], len(deals), len(candidates))
            status.append(item)
    shortlist.sort(key=lambda deal: (-deal["rank"]["score"], deal["id"]))
    headline_leads.sort(key=lambda deal: (-deal["headline_rank"]["score"], deal["id"]))
    publish(output, {"index.json": {
        "schema_version": 1, "checked_at": now.isoformat(), "sources": status,
        "candidates": shortlist[:100], "headline_leads": headline_leads[:100],
    }})
    ok = sum(item["status"] == "ok" for item in status)
    discovered = sum(item["offer_count"] for item in status if item["status"] == "ok")
    message = f"{ok}/{len(status)} sources refreshed, {discovered} current entries, {len(shortlist)} priced candidates."
    LOG.info(message)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            print(message, file=stream)
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/feeds")
    parser.add_argument("--manifest", type=Path, default=ROOT / "feeds.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        ok = refresh_all(
            read_json(args.manifest), read_json(ROOT / "config.json"),
            read_json(ROOT / "evidence.json", {}), args.output, datetime.now(timezone.utc),
        )
    except (FeedError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
