"""Refresh the deal snapshot, change feed, and price history from the public sheet."""

import argparse
import hashlib
import json
import logging
import os
import re
import statistics
import tempfile
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dealsmocktail import retrieve_website
from sheet_source import SourceError, retrieve

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
USD = re.compile(r"(?:(?:US\s*)?\$\s*)?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?", re.I)


def read_json(path, default=None):
    if default is not None and not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def cents(raw):
    raw = raw.strip()
    if USD.fullmatch(raw):
        amount = int(Decimal(re.sub(r"[^0-9.]", "", raw)) * 100)
        return amount if amount > 0 else None
    return None


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def consolidate(tabs, sheet_id):
    offers = {}
    # Prefer detailed category tabs for retail notes and prices when mirrored in the master list.
    for tab, rows in sorted(tabs, key=lambda pair: pair[0]["gid"] == "0"):
        gid = tab["gid"]
        for row in rows:
            title, retailer = row["title"], row.get("retailer", "")
            basis = ["source", row["id"]] if row.get("id") else [
                "title_retailer", normalize(title), normalize(retailer)
            ]
            deal_id = hashlib.sha256(json.dumps(basis).encode()).hexdigest()[:24]
            price = cents(row["price"])
            source = {"gid": gid, "tab": tab["name"], "price_text": row["price"]}
            if row.get("source_details"):
                source.update(row["source_details"])
            if deal_id not in offers:
                offers[deal_id] = {
                    "id": deal_id, "title": title, "retailer": retailer or None,
                    "price_cents": price, "currency": "USD", "coupon": row.get("coupon") or None,
                    "notes": row.get("notes") or None, "url": row["url"],
                    "sheet_url": tab.get("url") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview#gid={gid}",
                    "sources": [],
                }
            deal = offers[deal_id]
            if source not in deal["sources"]:
                deal["sources"].append(source)
            if not deal["url"]:
                deal["url"] = row["url"]

    conflicts = 0
    for deal in offers.values():
        observed = {cents(source["price_text"]) for source in deal["sources"]} - {None}
        deal["price_conflict"] = len(observed) > 1
        if deal["price_conflict"]:
            conflicts += 1
            deal["price_cents"] = None
        elif observed:
            deal["price_cents"] = observed.pop()
    if conflicts:
        LOG.warning("%d offers have conflicting mirrored prices", conflicts)
    return offers


def matches(pattern, title):
    return bool(pattern and re.search(pattern, title, re.I))


def rank(deal, prior, evidence, config):
    settings = config["ranking"]
    title = deal["title"]
    price = deal["price_cents"]
    rule = next((
        rule for rule in config["categories"]
        if matches(rule["match"], title) and not matches(rule.get("exclude"), title)
    ), None)
    variant = next((
        v for v in (rule or {}).get("variants", []) if matches(v["match"], title)
    ), None)
    category = rule["id"] if rule else "other"
    maximum = (variant or {}).get("max_usd", (rule or {}).get("max_usd"))
    preferred = (variant or {}).get("preferred_usd", (rule or {}).get("preferred_usd"))
    history_minimum = settings["previous_observations_minimum"]
    reference = {}
    if len(prior) >= history_minimum:
        reference["tracked_median_cents"] = round(statistics.median(prior))
    for field in ("independent_price_cents", "competing_price_cents"):
        if evidence.get(field, 0) > 0:
            reference[field] = evidence[field]
    baseline = min(reference.values(), default=None)
    savings = max(0, baseline - price) if baseline and price is not None else 0
    fraction = savings / baseline if baseline else 0
    exceptional = (
        fraction >= settings["other_minimum_savings_fraction"]
        and savings >= settings["other_minimum_savings_usd"] * 100
    )
    in_budget = price <= maximum * 100 if price is not None and maximum else None
    status = "watch"
    score = 0
    if price is None:
        status = "price_unavailable"
    elif in_budget is False:
        status = "over_budget"
    elif rule:
        requires_evidence = category in settings["evidence_required_for_categories"]
        status = "candidate" if exceptional or not requires_evidence else "watch"
        headroom = 1 - price / (maximum * 100) if maximum else 0
        score = rule["priority"] + (variant or {}).get("boost", 0)
        score += settings["budget_headroom_points"] * headroom
        if preferred and price <= preferred * 100:
            score += settings["preferred_price_points"]
    elif exceptional:
        status = "candidate"
    if status in {"candidate", "watch"}:
        score += settings["comparison_points"] * fraction
        score += settings["review_points"] * evidence.get("review_score", 0)
    return {
        "category": category, "variant": (variant or {}).get("id"), "status": status,
        "score": round(score, 2), "max_usd": maximum, "preferred_usd": preferred,
        "within_budget": in_budget, "comparisons": reference,
        "savings_vs_comparison_cents": savings if baseline else None,
        "evidence": evidence,
    }


def validate(tabs, offers, last, config):
    if len(offers) < config["minimum_deals"] or not any(
        deal["price_cents"] is not None for deal in offers.values()
    ):
        raise SourceError("source did not return the minimum usable offers and a valid price")
    fraction = config["maximum_loss_fraction"]
    if last and len(offers) < len(last["deals"]) * (1 - fraction):
        raise SourceError(f"offer count dropped more than {fraction:.0%}; review the source")
    old_tabs = {tab["gid"]: tab["count"] for tab in last.get("tabs", [])}
    for tab, _ in tabs:
        previous = old_tabs.get(tab["gid"], 0)
        if previous >= 10 and tab["count"] < previous * (1 - fraction):
            raise SourceError(f"tab {tab['name']} offer count dropped more than {fraction:.0%}")


def changes(before, after):
    events = []
    for deal_id in sorted(before.keys() | after.keys()):
        old, new = before.get(deal_id), after.get(deal_id)
        kinds = []
        if not old:
            kinds.append("new")
        elif not new:
            kinds.append("removed")
        else:
            old_price, new_price = old["price_cents"], new["price_cents"]
            if old_price != new_price:
                if new_price is None:
                    kinds.append("price_unavailable")
                elif old_price is None:
                    kinds.append("price_available")
                else:
                    kinds.append("price_decreased" if new_price < old_price else "price_increased")
            if old["rank"]["status"] != "candidate" and new["rank"]["status"] == "candidate":
                kinds.append("became_candidate")
        if kinds:
            deal = new or old
            events.append({
                "id": deal_id, "types": kinds, "title": deal["title"],
                "retailer": deal["retailer"],
                "previous_price_cents": old["price_cents"] if old else None,
                "current_price_cents": new["price_cents"] if new else None,
                "rank": deal["rank"], "url": deal["url"] or deal["sheet_url"],
            })
    return events


def publish(directory, documents):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as stage:
        for name, value in documents.items():
            Path(stage, name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        # The workflow publishes all files in one Git commit after successful staging.
        for name in documents:
            Path(stage, name).replace(directory / name)


def refresh(config, directory, evidence, source="sheet"):
    latest = read_json(directory / "latest.json", {})
    history = read_json(directory / "price_history.json", {})
    if bool(latest) != bool(history):
        raise SourceError("latest.json and price_history.json must be restored together")
    source_info = {"sheet_id": config["sheet_id"]} if source == "sheet" else {
        "website": "https://dealsmocktail.com/us"
    }
    if latest and any(latest.get(key) != value for key, value in source_info.items()):
        raise SourceError("start a new output directory when changing sources")
    tabs = retrieve(config) if source == "sheet" else retrieve_website(config)
    offers = consolidate(tabs, config["sheet_id"])
    validate(tabs, offers, latest, config)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for deal_id, deal in offers.items():
        entry = history.setdefault(deal_id, {
            "title": deal["title"], "retailer": deal["retailer"],
            "currency": "USD", "observations": [],
        })
        observations = entry["observations"]
        prior = [observation["price_cents"] for observation in observations]
        deal["rank"] = rank(deal, prior, evidence.get(deal_id, {}), config)
        price = deal["price_cents"]
        if price is not None and (
            not observations or observations[-1]["price_cents"] != price
        ):
            observations.append({"at": now, "price_cents": price})
    old_offers = {deal["id"]: deal for deal in latest.get("deals", [])}
    events = changes(old_offers, offers)
    ranked = sorted(offers.values(), key=lambda deal: (
        deal["rank"]["status"] != "candidate", -deal["rank"]["score"],
        deal["price_cents"] is None, deal["price_cents"] or 0, deal["id"],
    ))
    snapshot = {
        "schema_version": 1, "retrieved_at": now, **source_info,
        "tabs": [tab for tab, _ in tabs], "deals": ranked,
    }
    feed = {"schema_version": 1, "retrieved_at": now, "baseline": not latest, "events": events}
    publish(directory, {
        "price_history.json": history, "changes.json": feed, "latest.json": snapshot,
    })
    candidates = sum(deal["rank"]["status"] == "candidate" for deal in ranked)
    message = (
        f"Retrieved {len(tabs)} tabs and {len(offers)} unique offers. "
        f"{candidates} ranked candidates, {len(events)} change events."
    )
    LOG.info(message)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as output:
            print(message, file=output)


def send_alerts(config, directory):
    webhook = os.environ.get("DEAL_ALERT_WEBHOOK", "")
    if not webhook:
        LOG.info("No deal alert webhook configured")
        return
    if urlsplit(webhook).scheme != "https":
        raise SourceError("Deal alert webhook must use HTTPS")
    feed = read_json(directory / "changes.json")
    if feed["baseline"]:
        LOG.info("First snapshot recorded as baseline")
        return
    settings = config["alerts"]
    types = set(settings["event_types"])
    selected = sorted((
        event for event in feed["events"]
        if event["rank"]["status"] == "candidate" and types.intersection(event["types"])
    ), key=lambda event: -event["rank"]["score"])[:settings["max_deals"]]
    if not selected:
        LOG.info("No qualifying changes to send")
        return
    payload = json.dumps({"retrieved_at": feed["retrieved_at"], "deals": selected}).encode()
    request = Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=15):
            LOG.info("Sent %d deal alerts", len(selected))
    except HTTPError as exc:
        raise SourceError(f"Deal alert webhook returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SourceError("Could not reach the deal alert webhook") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", choices=("sheet", "dealsmocktail"), default="sheet")
    parser.add_argument("--evidence", type=Path, default=ROOT / "evidence.json")
    parser.add_argument("--alerts-only", action="store_true")
    args = parser.parse_args()
    output = args.output or ROOT / ("data" if args.source == "sheet" else "data/dealsmocktail")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = read_json(args.config)
        if args.alerts_only:
            send_alerts(config, output)
        else:
            refresh(config, output, read_json(args.evidence, {}), args.source)
    except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
