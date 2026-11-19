import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from extra_feeds import FeedError, github_entries, possible_price, refresh_all, xml_entries
from tracker import ROOT, read_json

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
AGE = timedelta(days=21)
SOURCE = {"id": "test", "name": "Test", "url": "https://example.com/feed", "kind": "rss"}
RSS = """<rss xmlns:d="https://example.com/deals"><channel>
<item><title>55-inch TV for $299</title><link>https://example.com/tv</link><guid>1</guid>
<pubDate>Wed, 18 Nov 2026 20:00:00 GMT</pubDate>
<d:price currency="USD">299.00</d:price><d:retailer>Test Shop</d:retailer></item>
<item><title>TV deal last year</title><link>https://example.com/stale</link>
<pubDate>Wed, 19 Nov 2025 20:00:00 GMT</pubDate></item></channel></rss>"""


class ExtraFeedTests(unittest.TestCase):
    def test_explicit_prices_and_stale_entries(self):
        offer, = xml_entries(RSS, SOURCE, NOW, AGE)
        self.assertEqual((offer["price_cents"], offer["retailer"]), (29900, "Test Shop"))
        self.assertEqual(offer["price_source"], "publisher_structured_data")

    def test_atom_ignores_self_link_and_does_not_promote_headline_price(self):
        body = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>TV for $199.99</title><id>post</id>
        <link rel="self" href="https://example.com/self"/>
        <link rel="alternate" href="https://example.com/offer"/>
        </entry></feed>"""
        offer, = xml_entries(body, SOURCE, NOW, AGE)
        self.assertEqual(offer["url"], "https://example.com/offer")
        self.assertIsNone(offer["price_cents"])
        self.assertEqual(offer["headline_price_cents"], 19999)

    def test_excludes_dollar_off_regional_prices_ranges_and_financing(self):
        for title in ("save $200 on a TV", "TV for CA$300", "TVs from $100 to $200", "TV for $19 a month"):
            self.assertIsNone(possible_price(title))

    def test_github_drops_expired_and_ambiguous_price(self):
        body = """## Electronics
| Deal | Discount | Price | Expires | Source | Notes |
| [TV X](https://example.com/x) | — | $200 | 2026-11-22 | — | — |
| [TV Y](https://example.com/y) | — | $9 per month | — | — | — |
| [TV Z](https://example.com/z) | — | $100 | 2026-11-10 | — | — |
"""
        rows = github_entries(body, SOURCE, NOW, AGE)
        self.assertEqual([row["price_cents"] for row in rows], [20000, None])

    def test_a_failed_feed_preserves_snapshot_and_does_not_block_other_sources(self):
        other = dict(SOURCE, id="other", url="https://example.net/feed")
        manifest = {"max_age_days": 21, "sources": [SOURCE, other]}
        config = read_json(ROOT / "config.json")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with patch("extra_feeds.fetch", return_value=RSS):
                self.assertEqual(refresh_all(manifest, config, {}, output, NOW), 2)
            original = (output / "test/latest.json").read_bytes()

            def next_fetch(url):
                if url == SOURCE["url"]:
                    raise FeedError("HTTP 503")
                return RSS.replace("299.00", "199.00")

            with patch("extra_feeds.fetch", side_effect=next_fetch):
                self.assertEqual(refresh_all(manifest, config, {}, output, NOW + timedelta(hours=2)), 1)
            self.assertEqual((output / "test/latest.json").read_bytes(), original)
            index = json.loads((output / "index.json").read_bytes())
            self.assertEqual([item["status"] for item in index["sources"]], ["failed", "ok"])
            change, = read_json(output / "other/changes.json")["events"]
            self.assertEqual(change["types"], ["price_decreased"])
