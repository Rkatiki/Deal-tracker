import unittest
from datetime import datetime, timedelta, timezone

from extra_feeds import xml_entries
from sheet_source import deal_links, enrich_links, grid_rows, parse_rows
from tracker import consolidate, changes


class OfferLinkTests(unittest.TestCase):
    def test_sheet_uses_tab_when_missing_offer_link(self):
        deal, = consolidate([
            ({"gid": "7", "name": "Deals"}, [{"title": "TV", "price": "$300", "url": None}])
        ], "sheet").values()
        self.assertEqual(deal["url"], "https://docs.google.com/spreadsheets/d/sheet/htmlview#gid=7")
        self.assertEqual(deal["link_type"], "source")
        self.assertIsNone(deal["retailer_url"])

    def test_sheet_enrichment_keeps_csv_prices_and_rejects_ambiguous_links(self):
        original = [{"title": "TV", "price": "$300", "retailer": "Shop", "url": None}]
        html = [{"title": " TV ", "price": "$500", "retailer": "shop",
                 "url": "https://www.walmart.com/ip/tv/1"}]
        enrich_links(original, html + [dict(html[0], url="https://www.walmart.com/ip/tv/2")])
        self.assertIsNone(original[0]["url"])
        enrich_links(original, html)
        self.assertEqual(original[0]["price"], "$300")
        self.assertEqual(original[0]["url"], html[0]["url"])

    def test_sheet_uses_hyperlinked_store_cells(self):
        page = """<table class="waffle"><tr><td>Product</td><td>Price</td><td>Store</td></tr>
        <tr><td>TV</td><td>$300</td><td><a href="https://mavely.app.link/test">Walmart</a></td></tr>
        </table>"""
        row, = parse_rows(*grid_rows(page), None)
        self.assertEqual(row["retailer"], "Walmart")
        self.assertEqual(row["url"], "https://mavely.app.link/test")

    def test_retailer_and_publisher_link_survive(self):
        links = deal_links("https://example.com/offer", retailer_url="https://go.magik.ly/ml/test/")
        self.assertEqual(links["link_type"], "retailer_redirect")
        self.assertEqual(links["url"], links["retailer_url"])
        self.assertEqual(links["source_url"], "https://example.com/offer")
        self.assertEqual(
            deal_links("https://example.com/offer", retailer_url="javascript:alert(1)")["link_type"],
            "source",
        )

    def test_woot_rss_preserves_retailer_and_forum_urls(self):
        body = """<rss><channel><item><title>A TV</title>
        <link>https://forums.woot.com/t/tv/12</link>
        <description><![CDATA[<a href="https://www.woot.com/offers/tv">View offer</a>]]></description>
        </item></channel></rss>"""
        source = {"id": "woot-daily", "name": "Woot", "retailer": "Woot"}
        offer, = xml_entries(body, source, datetime.now(timezone.utc), timedelta(days=21))
        self.assertEqual(offer["url"], "https://www.woot.com/offers/tv")
        self.assertEqual(offer["source_url"], "https://forums.woot.com/t/tv/12")
        change, = changes({}, {offer["id"]: dict(offer, rank={})})
        self.assertEqual(change["retailer_url"], offer["url"])
