import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sheet_source import SourceError, discover, get_tab, grid_rows, parse_rows
from tracker import cents, consolidate, rank, refresh


CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())


def row(title, price, retailer="Shop", **fields):
    return {"title": title, "price": price, "retailer": retailer, "url": None, **fields}


def tab(rows, gid="1"):
    return {"name": "Deals", "gid": gid, "method": "test", "count": len(rows)}, rows


class SourceTests(unittest.TestCase):
    def test_discovery_decodes_google_labels(self):
        page = r'items.push({name: "Sports \x26 Outdoors", pageUrl: "x", gid: "87"});'
        self.assertEqual(discover(page), {"87": "Sports & Outdoors"})
        with self.assertRaises(SourceError):
            discover("<html>Sign in</html>")

    def test_gviz_missing_price_header_and_unpriced_deal(self):
        source = [
            ["Master deals", "", "", ""], ["Item/Product", "", "Store", "Notes"],
            ["Soundbar", "$90.00", "Store A", "Details"], ["TV", "", "Store A", ""],
            ["Group header", "", "", ""],
        ]
        self.assertEqual([deal["price"] for deal in parse_rows(source, {}, None)], ["$90.00", ""])

    def test_csv_failure_uses_html_and_extracts_link(self):
        html = (
            '<table class="waffle"><tr><td>Product</td><td>Price</td></tr>'
            '<tr><td><a href="https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Foffer">'
            'TV</a></td><td>$90.00</td></tr></table>'
        )
        with patch("sheet_source.request", side_effect=[SourceError("HTTP 401"), html]):
            info, offers = get_tab("https://example.com", "1", "Deals", None)
        self.assertEqual(info["method"], "html")
        self.assertEqual(offers[0]["url"], "https://example.com/offer")
        with self.assertRaises(SourceError):
            grid_rows("<html>Sign in</html>")

    def test_invalid_tab_fails_after_both_readers(self):
        with patch("sheet_source.request", side_effect=SourceError("HTTP 401")):
            with self.assertRaisesRegex(SourceError, "gviz_csv.*html"):
                get_tab("https://example.com", "1", "Deals", None)


class TrackerTests(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(cents("$1,299.95"), 129995)
        for value in ("", "from $20", "$5–$10", "CA$50"):
            self.assertIsNone(cents(value))

    def test_mirror_dedup_and_conflicting_prices(self):
        tabs = [tab([row("Great TV", "$200")], "0"), tab([row("great TV", "$210")])]
        offer, = consolidate(tabs, "sheet").values()
        self.assertIsNone(offer["price_cents"])
        self.assertTrue(offer["price_conflict"])
        self.assertEqual(len(offer["sources"]), 2)

    def test_rank_budget_preferences_and_independent_comparison(self):
        def evaluate(title, price, evidence=None):
            return rank({"title": title, "price_cents": price}, [], evidence or {}, CONFIG)
        self.assertEqual(evaluate("Smart TV", 50001)["status"], "over_budget")
        self.assertEqual(evaluate("Soundbar", 10000)["status"], "candidate")
        self.assertGreater(evaluate("Robot Vacuum", 25000)["score"], evaluate("Robot Vacuum", 26000)["score"])
        self.assertEqual(evaluate("Air Fryer Paper Liner", 500)["category"], "other")
        self.assertEqual(evaluate("DualZone Air Fryer", 12900)["status"], "watch")
        self.assertEqual(
            evaluate("DualZone Air Fryer", 12900, {"competing_price_cents": 18000})["status"],
            "candidate",
        )
        self.assertEqual(evaluate("Router", 3000)["status"], "watch")
        self.assertEqual(
            evaluate("Router", 3000, {"independent_price_cents": 5000})["status"], "candidate",
        )

    def test_refresh_deltas_history_and_failure_protection(self):
        first = [tab([row("TV A", "$300"), row("TV B", "$150")])]
        second = [tab([row("TV A", "$250"), row("TV C", "$100")])]
        third = [tab([row("TV A", "$250"), row("TV C", "$100")])]
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp)
            with patch("tracker.retrieve", side_effect=[first, second, third, SourceError("blocked")]):
                refresh(CONFIG, location, {})
                self.assertTrue(json.loads((location / "changes.json").read_text())["baseline"])
                refresh(CONFIG, location, {})
                feed = json.loads((location / "changes.json").read_text())
                self.assertEqual(
                    {event["title"]: event["types"] for event in feed["events"]},
                    {"TV A": ["price_decreased"], "TV B": ["removed"], "TV C": ["new"]},
                )
                refresh(CONFIG, location, {})
                saved = {path.name: path.read_bytes() for path in location.iterdir()}
                with self.assertRaises(SourceError):
                    refresh(CONFIG, location, {})
                self.assertEqual(saved, {path.name: path.read_bytes() for path in location.iterdir()})
            history = {entry["title"]: entry for entry in json.loads(saved["price_history.json"]).values()}
            self.assertEqual(
                [point["price_cents"] for point in history["TV A"]["observations"]], [30000, 25000]
            )
            self.assertIn("TV B", history)

    def test_sudden_loss_and_invalid_price_protect_existing_snapshot(self):
        config = copy.deepcopy(CONFIG)
        previous = [tab([row(f"TV {i}", "$300") for i in range(12)])]
        for invalid in (previous[0][1][:2], [row(f"TV {i}", "") for i in range(12)]):
            with tempfile.TemporaryDirectory() as tmp:
                location = Path(tmp)
                with patch("tracker.retrieve", side_effect=[previous, [tab(invalid)]]):
                    refresh(config, location, {})
                    original = (location / "latest.json").read_bytes()
                    with self.assertRaises(SourceError):
                        refresh(config, location, {})
                self.assertEqual((location / "latest.json").read_bytes(), original)
