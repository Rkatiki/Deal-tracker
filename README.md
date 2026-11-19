# Deal Tracker

A multi-source deal tracker for Black Friday and year-round shopping. GitHub Actions reads public spreadsheets, deal listings, publisher feeds, and community-maintained lists. It keeps source links, tracks price changes, and publishes JSON for browsing or analysis.

## Find deals and track prices

Start with the [deal index](data/feeds/index.json) for publisher status, ranked offers with structured prices, and leads with prices mentioned in headlines. The spreadsheet and public listing snapshots are available separately.

| Source | What it covers | Output |
| --- | --- | --- |
| Public Google Sheet | Offers across the workbook’s visible tabs | [Latest snapshot](data/latest.json), [changes](data/changes.json), [price history](data/price_history.json) |
| DealsMocktail US | Latest, Electronics, and Home and Kitchen listings | [Snapshot, changes, and price history](data/dealsmocktail/) |
| Additional public sources | Deal communities, shopping blogs, tech publications, game-price trackers, and GitHub lists | [Source index](data/feeds/index.json), [publisher snapshots](data/feeds/), [configured sources](feeds.json) |

- `latest.json` contains the most recent successful snapshot for a source.
- `changes.json` records additions, removals, price changes, and offers newly qualifying for the shortlist. The first snapshot establishes a baseline.
- `price_history.json` stores the first observed numeric price and later changes, including for offers that disappear from a listing.

Every offer has a recommended `url` and an original `source_url`. `retailer_url` is included when a merchant page or click-through is available. `link_type` identifies direct retailer links, retailer redirects, other offer links, and source fallbacks. The spreadsheet reader also checks public HTML for product hyperlinks that CSV omits.

Prices use integer USD cents. Headline prices are discovery leads. Structured prices feed the ranking and price history. A removal means an offer left a monitored source or aged out of a rolling feed. Check the linked offer for current price and availability. Multiple publishers can report the same offer.

## Automatic and manual refreshes

| Workflow | Schedule |
| --- | --- |
| [Refresh Black Friday deals](https://github.com/Rkatiki/Deal-tracker/actions/workflows/deals.yml) | Every two hours at minute 17 UTC |
| [Refresh additional deal sources](https://github.com/Rkatiki/Deal-tracker/actions/workflows/feeds.yml) | Every two hours at minute 47 UTC |

To refresh manually, open the workflow in **Actions**, select **Run workflow**, and choose the default branch. The workflows need permission to push generated snapshots to that branch.

The spreadsheet, public listings, and individual feeds publish independently. A failed retrieval preserves the affected source’s last successful snapshot. Additional feed failures appear in the index. If all additional feeds fail in one run, the workflow reports failure and preserves the previous published index.

## Configure sources and ranking

- Edit [`config.json`](config.json) for workbook tab settings, public listing categories, price caps, and ranking rules. The spreadsheet reader discovers visible tabs and uses Google Visualization CSV, with public HTML as a fallback when a tab’s CSV fails. Direct Google CSV and XLSX downloads returned HTTP 401 during setup. Use `source.layouts` to map unfamiliar tab headers by zero-based column index. The listing reader checks the site’s crawler rules before fetching configured pages.
- Edit [`feeds.json`](feeds.json) for publisher URLs and feed freshness. The tracker supports RSS, Atom, and adapters for the configured game-price API and GitHub deal table. Replace seasonal sources with a current edition as needed.
- The default caps are **$500 for TVs**, **$100 for soundbars**, and **$350 for robot vacuums and mops**, with preference at **$250**. Air-fryer watch thresholds are **$75 for standard models** and **$130 for dual-basket or dual-zone models**. Air fryers and other categories need a qualifying price comparison to make the shortlist. Advertised discount percentages carry no ranking weight.
- Optional `evidence.json` entries keyed by offer ID can supply `independent_price_cents`, `competing_price_cents`, `review_score` on a 0–1 scale, and source links or observation dates. The tracker can also compare against its own price history.

<a id="ranking-and-alerts"></a>

## Optional alerts

Add a repository Actions secret named `DEAL_ALERT_WEBHOOK` with an HTTPS endpoint that accepts JSON containing `retrieved_at` and `deals`. Notifications cover qualifying changes from the spreadsheet and public listings after each source’s baseline is established. The additional feeds are available through the discovery index. For Slack or email, use a relay that formats the webhook payload for the destination. Webhook failures are logged after the data commit. To replay an alert batch, check out that commit and run `python3 tracker.py --alerts-only` with the secret set in your environment. Add `--source dealsmocktail` to replay the public listing batch.

## Run locally

Use Python 3.10 or newer and `curl` for the website and feed adapters.

```sh
python3 -m unittest discover -v
python3 tracker.py
python3 tracker.py --source dealsmocktail
python3 extra_feeds.py
```

Use `--output` to write snapshots to a separate directory during local testing.
