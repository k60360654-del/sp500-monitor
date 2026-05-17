# S&P 500 Signal Monitor

An automated dashboard that scores every S&P 500 company on two signals derived
from SEC EDGAR filings: **discretionary insider transactions** (Form 4) and
**quarterly share count changes** (XBRL companyfacts).

Built to run for free on GitHub Actions + GitHub Pages. No server, no database,
no monthly cost.

---

## What it does

On a schedule (default: every 6 hours), the pipeline:

1. Pulls the current S&P 500 constituent list from Wikipedia and diffs it
   against the prior run to detect additions/removals.
2. For each company, fetches new Form 4 filings since the last run, parses
   the XML, and identifies **discretionary** insider transactions
   (transaction code `P` for buys, `S` for sells, with 10b5-1 plan trades
   filtered out).
3. For each company, fetches the XBRL companyfacts JSON and extracts the
   `EntityCommonStockSharesOutstanding` time series to compute quarter-over-
   quarter share count changes.
4. Applies your weights to compute a composite score per company.
5. Writes `data/companies.json`, which the static dashboard loads.
6. Commits and pushes the updated JSON. GitHub Pages serves the dashboard.

## Default scoring

```
Insider discretionary buy (last 90 days)       +1 each
Insider discretionary sell (last 90 days)      -1 each
Share count QoQ decrease > 1%                  +1
Share count QoQ increase > 1%                  -1
```

All weights are configurable on the command line (see `--help` on the pipeline).

---

## Setup

### 1. Fork or create the repo

Push this codebase to a new GitHub repo. **Private repos work too** — GitHub
Actions and Pages both support private repos under the free tier limits.

### 2. Set the `EDGAR_USER_AGENT` secret

SEC EDGAR requires a User-Agent header identifying you. Go to:

`Settings → Secrets and variables → Actions → New repository secret`

Name: `EDGAR_USER_AGENT`
Value: e.g. `Kumar Srinivasan kumar@example.com`

This is mandatory. EDGAR will reject requests without it.

### 3. Enable GitHub Pages

`Settings → Pages → Build and deployment → Source: GitHub Actions`

### 4. Run the workflow

`Actions → Update SP500 Signals → Run workflow`

For the first run, you can leave the defaults (full S&P 500, 1 year of
Form 4 backfill). This will take 30-60 minutes.

For testing, set `max_companies` to `20` to run a quick first pass.

After the run completes, your dashboard will be at:

`https://<your-github-username>.github.io/<repo-name>/`

### 5. (Optional) Adjust the schedule

In `.github/workflows/update.yml`, change the cron expression. Default is
`15 */6 * * *` (every 6 hours at :15). Form 4s have a 2-business-day filing
deadline so this is more than fast enough.

---

## Running locally

```bash
pip install -r requirements.txt
export EDGAR_USER_AGENT="Your Name your@email.com"

# First run — full universe, full backfill
python -m src.pipeline

# Quick test — 20 companies
python -m src.pipeline --max-companies 20

# Custom weights
python -m src.pipeline \
  --insider-lookback-days 180 \
  --share-count-pct-threshold 0.5 \
  --insider-buy-weight 2.0
```

Then serve `dashboard/` over any local HTTP server, e.g.:

```bash
cp data/companies.json dashboard/
cd dashboard && python -m http.server 8000
# open http://localhost:8000
```

---

## Architecture

```
src/
├── edgar.py       # Rate-limited HTTP client + URL builders
├── universe.py    # Wikipedia + SEC ticker→CIK mapping; add/remove tracking
├── form4.py       # Form 4 XML parsing, discretionary-transaction logic
├── shares.py      # XBRL companyfacts → quarterly share count series
├── scoring.py     # Composite score with configurable weights
└── pipeline.py    # Orchestrator + JSON export

data/              # Generated; checked into repo so dashboard can read it
├── universe.json       # S&P 500 membership state
├── transactions.json   # Cached Form 4 transactions (incremental updates)
├── share_counts.json   # Cached share count time series
└── companies.json      # The output file the dashboard reads

dashboard/         # Static site for GitHub Pages
└── index.html     # Single-file dashboard
```

## How "discretionary" is determined

A Form 4 transaction is counted as a discretionary buy or sell only if all
of these are true:

- Transaction code is `P` (buy) or `S` (sell). Codes `A` (grant), `M`
  (option exercise), `F` (tax withholding), `D` (disposition to issuer),
  `G` (gift), `X` (option exercise), and `C` (conversion) are excluded.
- Acquired/disposed code matches (`A` for buys, `D` for sells).
- The filing does **not** have the `<aff10b5One>` flag set, **and** no
  footnote references 10b5-1.

10b5-1 plans are pre-scheduled trades and don't carry the same signal value
as actively-decided open market transactions.

## Notes & limitations

- Share count from `dei:EntityCommonStockSharesOutstanding` is the cover-page
  "as of" count. For ~5% of filers this field is missing or stale; the code
  falls back to `us-gaap:CommonStockSharesOutstanding`.
- The "QoQ" change uses adjacent reported periods. For most companies these
  are calendar quarters, but some (e.g. Walmart, Cisco) have offset fiscal
  calendars — the comparison is still period-over-period and meaningful.
- GitHub Actions has a 6-hour job timeout. Even a full backfill of 500
  companies × 1 year stays well under that.
- EDGAR rate limit is 10 req/sec. The client targets ~8 req/sec to stay safely
  under it.
- GitHub Pages free tier: 100GB/month bandwidth, 1GB site size. This project
  uses < 1MB of bandwidth per day. Not a concern.

## Customizing

Common changes:

**Track a different universe** — replace `fetch_sp500_from_wikipedia()` in
`src/universe.py` with your own ticker list (Russell 1000, your portfolio,
etc.).

**Add a new signal** — e.g. 13D/G activist filings, or 10-Q restatements.
Add a parser module, add a component in `scoring.py`, expose it in the
dashboard.

**Change scoring math** — `src/scoring.py` accepts a `ScoreWeights` dataclass.
The `insider_mode` parameter switches between `count`, `net_count`, and
`net_value` modes.

**Send a daily email** — add a step in the workflow that diffs the new
`companies.json` against the prior commit and pipes top movers to a
mail-action like `dawidd6/action-send-mail`.
