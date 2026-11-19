"""Anonymous Google Sheets readers. No Google credentials are required."""

import csv
import html
import io
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
TAB = re.compile(
    r'items\.push\(\{name:\s*("(?:[^"\\]|\\.)*").*?\bgid:\s*"(-?\d+)"',
    re.DOTALL,
)
FIELDS = {
    "title": {"item product", "item", "product", "product name", "deal", "title"},
    "price": {"price", "deal price", "sale price", "current price"},
    "retailer": {"store", "retailer", "merchant"},
    "notes": {"notes", "details"},
    "coupon": {"coupon", "coupon code", "promo code"},
    "url": {"link", "url", "product link", "deal link"},
    "id": {"deal id", "offer id"},
}


class SourceError(Exception):
    pass


def request(url, accepted):
    for attempt in range(3):
        try:
            headers = {"User-Agent": "DealTracker/1.0", "Cache-Control": "no-cache"}
            with urlopen(Request(url, headers=headers), timeout=25) as response:
                mime = response.headers.get_content_type()
                if mime not in accepted:
                    raise SourceError(f"unexpected content type {mime} from {url}")
                data = response.read(25_000_001)
                if len(data) > 25_000_000:
                    raise SourceError(f"response exceeded 25 MB from {url}")
                return data.decode("utf-8-sig")
        except HTTPError as exc:
            reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (URLError, TimeoutError, OSError) as exc:
            reason = str(exc)
        if attempt < 2:
            LOG.warning("Retrying %s after %s", url, reason)
            time.sleep(2 ** attempt)
    raise SourceError(f"{reason} from {url}")


def discover(page):
    tabs = {}
    for name, gid in TAB.findall(page):
        name = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), name)
        tabs[gid] = html.unescape(json.loads(name))
    if not tabs:
        raise SourceError("public HTML view did not expose a tab manifest")
    return tabs


class SheetGrid(HTMLParser):
    def __init__(self):
        super().__init__()
        self.on_grid = False
        self.found_grid = False
        self.row = None
        self.cell = None
        self.rows = []
        self.links = {}
        self.href = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and "waffle" in attrs.get("class", "").split():
            self.found_grid = self.on_grid = True
        if not self.on_grid:
            return
        if tag == "tr":
            self.row = []
        if tag == "td" and self.row is not None:
            self.cell = []
            self.href = None
        if tag == "a" and self.cell is not None and not self.href:
            self.href = attrs.get("href")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            if self.href:
                self.links[(len(self.rows), len(self.row))] = self.href
            self.row.append("".join(self.cell))
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        if tag == "table":
            self.on_grid = False


def grid_rows(page):
    grid = SheetGrid()
    grid.feed(page)
    if not grid.found_grid or not grid.rows:
        raise SourceError("public tab HTML did not contain a spreadsheet grid")
    return grid.rows, grid.links


def safe_link(value):
    parsed = urlsplit(value.strip())
    if parsed.hostname == "www.google.com" and parsed.path == "/url":
        parsed = urlsplit(parse_qs(parsed.query).get("q", [""])[0])
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
        return parsed.geturl()
    return None


def parse_rows(rows, links, layout):
    if not rows:
        raise SourceError("response contained no rows")
    fields = None
    first_data_row = 0
    for index, row in enumerate(rows[:40]):
        labels = [re.sub(r"[^a-z0-9]+", " ", cell.lower()).strip() for cell in row]
        candidate = {
            field: labels.index(label)
            for field, aliases in FIELDS.items()
            for label in labels
            if label in aliases
        }
        if "title" in candidate and ("price" in candidate or "retailer" in candidate):
            # GViz can omit a text header in a predominantly numeric column.
            if "price" not in candidate and candidate["title"] == 0 and len(row) > 1 and not row[1]:
                candidate["price"] = 1
            if "price" in candidate:
                fields, first_data_row = candidate, index + 1
                break
    if fields is None:
        fields = layout
    if not fields:
        raise SourceError("could not recognize product and price columns")

    deals = []
    for index in range(first_data_row, len(rows)):
        row = rows[index]
        values = {
            key: row[column].strip() if column < len(row) else ""
            for key, column in fields.items()
        }
        title = values["title"]
        url = safe_link(values.get("url") or links.get((index, fields["title"]), ""))
        notes = values.get("notes", "")
        if not url and notes.startswith(("http://", "https://")) and len(notes.split()) == 1:
            url = safe_link(notes)
        if not title or not (values["price"] or values.get("retailer") or url):
            continue
        deals.append(dict(values, url=url))
    return deals


def get_tab(base, gid, name, layout):
    query = urlencode({"tqx": "out:csv", "headers": 0, "gid": gid})
    readers = (
        ("gviz_csv", lambda: (
            list(csv.reader(io.StringIO(request(
                f"{base}/gviz/tq?{query}", {"text/csv", "application/csv"}
            )), strict=True)), {}
        )),
        ("html", lambda: grid_rows(request(
            f"{base}/htmlview/sheet?headers=true&gid={gid}", {"text/html"}
        ))),
    )
    failures = []
    for method, read in readers:
        try:
            deals = parse_rows(*read(), layout)
            LOG.info("Tab %s (%s) via %s: %s offers", name, gid, method, len(deals))
            return {"gid": gid, "name": name, "method": method, "count": len(deals)}, deals
        except (SourceError, ValueError, csv.Error) as exc:
            failures.append(f"{method}: {exc}")
            LOG.warning("Tab %s (%s) %s", name, gid, failures[-1])
    raise SourceError(f"Tab {name} ({gid}) failed: {'; '.join(failures)}")


def retrieve(config):
    sheet_id = config["sheet_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
        raise SourceError("sheet_id is invalid")
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    manifest = discover(request(f"{base}/htmlview", {"text/html"}))
    settings = config["source"]
    tabs = [(gid, name) for gid, name in manifest.items() if gid not in settings["ignored_tabs"]]
    if not tabs:
        raise SourceError("there are no included spreadsheet tabs")
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(get_tab, base, gid, name, settings["layouts"].get(gid))
            for gid, name in tabs
        ]
        return [future.result() for future in futures]
