# Black Friday Deal Tracker

GitHub Actions reads the live public spreadsheet every two hours at minute 17 UTC. On the default branch, open **Actions → Refresh Black Friday deals → Run workflow** for an on-demand refresh. The workflow needs permission to push its generated data commit to the default branch. Branch rules restricting that push must explicitly permit the automation. GitHub may pause scheduled workflows in inactive public repositories.

## Read-only source

The supplied spreadsheet's direct CSV and XLSX downloads returned HTTP 401 Unauthorized anonymously. Its public HTML view exposes visible tab IDs and Google Visualization serves each tab as CSV without a Google account or API key. The tracker discovers tabs on each run and tries the public HTML grid for an individual tab if its Visualization CSV cannot be fetched or parsed. The two reader formats can differ. In particular, Visualization may omit formatted cell hyperlinks and mixed-type prices. The tracker keeps a source tab link when no product link is available, and leaves ambiguous prices unpriced.

All included tabs must return usable data before publishing. The tracker checks the minimum inventory, rejects sudden count losses across the workbook or in any continuing tab with at least ten prior offers, and leaves existing files intact on retrieval or validation errors. Review the source and adjust the loss threshold for a verified large removal. If Google withdraws anonymous access, ask the publisher to restore the public view and query access, or arrange a separately authorized read-only mirror maintained by the publisher. A mirror with a new sheet ID needs a new output directory or a fresh baseline. Public workbook tabs with unfamiliar headers can be mapped in `source.layouts` by tab ID with zero-based column indexes.

## Analyze the data

| File | Contents |
| --- | --- |
| `data/latest.json` | Latest verified snapshot, retrieval timestamp, source tabs, offers, and ranking |
| `data/changes.json` | New and removed offers, price increases and decreases, newly available or unavailable prices, and offers newly qualifying for the shortlist |
| `data/price_history.json` | First observed price and subsequent distinct numeric price observations, including offers that have since disappeared |

Prices are USD in integer cents. A stable source deal ID is preferred when available. Otherwise, an offer is identified by normalized title and retailer. Mirror entries are combined, and conflicting numeric prices become unavailable for ranking. With title-based IDs, a title or retailer edit can appear as one removal and one addition. The initial refresh labels existing offers as new and establishes the baseline. Git history preserves prior versions of the change feed.

## Ranking and alerts

Edit `config.json` to add categories, change budget caps and preferred prices, tune matching and exclusion patterns, or adjust score weights. TVs cap at $500, soundbars at $100, and robot vacuums and mops at $350 with a preference at $250. The starting air fryer watch thresholds are $75 for standard models and $130 for dual-basket or dual-zone models. An applicable comparison is required to shortlist air fryers and other categories. The default threshold for those categories is at least 20% and $10 below the most conservative available comparator. Advertised discounts do not enter the score.

Tracked price history can supply a comparison after three distinct price observations. An optional `evidence.json` keyed by offer ID accepts `independent_price_cents`, `competing_price_cents`, `review_score` on a 0–1 scale, and provenance fields such as source URLs and observation dates. Use comparable offers and current evidence. The tracker has no external review or retailer feed enabled.

To enable notifications, add a repository Actions secret named `DEAL_ALERT_WEBHOOK` containing an HTTPS endpoint that accepts JSON with `retrieved_at` and `deals`. For Slack or email, point the secret at a relay that formats the JSON for that destination. The first successful run seeds the baseline without sending notifications. Later runs send qualifying changes after the data commit. Webhook errors are logged separately from retrieval and leave the commit available for replay. To replay an alert batch, run `python3 tracker.py --alerts-only` against the data from the affected commit with that secret supplied in the environment.

## Local use

Run `python3 -m unittest discover -v`, then `python3 tracker.py --output data` with Python 3.10 or newer. No third-party packages are required.
