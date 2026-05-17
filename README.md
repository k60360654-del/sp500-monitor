# S&P 500 Signal Monitor

Automated forensic-style dashboard that scores every S&P 500 company across
**12 signal categories** derived from SEC EDGAR filings.

Built to run free on GitHub Actions + GitHub Pages. No server, no database,
no monthly cost.

---

## Signal Model

| Category | Signal | Weight |
|---|---|---|
| **Insider** | Discretionary buy (non-10b5-1, code P) | **+1 each** |
| | Discretionary sell (non-10b5-1, code S) | **-0.25 each** |
| **Share Count** | QoQ decrease >1% (buyback) | **+1** |
| | QoQ decrease >3% (aggressive buyback) | **+2** (replaces +1) |
| | QoQ increase >1% (dilution) | **-1** |
| | QoQ increase >5% (major dilution) | **-2** (replaces -1) |
| **8-K Events** | Item 4.02 non-reliance on prior financials | **-3** |
| | Item 4.01 auditor change | **-2** |
| | Director resignation w/o "no disagreement" language | **-2** |
| | Buyback authorization announcement | **+1** |
| **Late Filings** | NT-10K / NT-10Q late-filing notification | **-2 each** |
| **Governance** | Say-on-pay vote <70% | **-1** |
| | Say-on-pay vote <50% | **-2** (replaces -1) |
| | Auditor ratification vote <95% | **-1** |
| | Any individual director withhold vote >20% | **-1** |
| **13D / 13G** | 13G amendment showing increased stake | **+1** |
| | 13D amendment showing increased stake (activist) | **+2** |
| **Financials (TTM YoY)** | Dividend per share increase | **+1** |
| | Dividend initiation ($0 → positive) | **+2** |
| | Dividend cut | **-2** |
| | Gross margin expansion >100 bps | **+1** |
| | Gross margin compression >100 bps | **-1** |
| | Operating cash flow growth >5% | **+1** |
| | Operating cash flow decline >5% | **-1** |

The asymmetric insider weighting (+1 buy vs. -0.25 sell) reflects academic findings that insider buys are far more predictive than sells — sells happen for many non-informational reasons (diversification, taxes, divorce, mansion), while buys mainly happen when the insider thinks the stock is going up.

## What clean signals look like

- **Quality compounder**: typically +3 to +6 (modest buybacks + growing dividend + expanding margins + OCF growth)
- **Activist target**: +3 to +6 (insider buys + 13D accumulation)
- **Distress / accounting issue**: -5 to -12 (4.02 + dividend cut + margin compression + insider sells)
- **Forensic outlier**: -10+ (multiple red flags compounding)

## Setup

### 1. Push to GitHub (public repo for free Pages hosting)

### 2. Set `EDGAR_USER_AGENT` secret

`Settings → Secrets and variables → Actions → New repository secret`

Name: `EDGAR_USER_AGENT`
Value: `Your Name your@email.com` (SEC requires a real contact email)

### 3. Enable GitHub Pages

`Settings → Pages → Build and deployment → Source: GitHub Actions`

### 4. Run the workflow

`Actions → Update SP500 Signals → Run workflow`

**For the first run**:
- Set `max_companies` to `20` for a quick smoke test (~10 min)
- Keep `skip_13dg` **true** for the first few runs (13D/G fetching adds 30-60 min)
- Set `backfill_years` to `1`

Once that works, re-run with `max_companies=0` for the full S&P 500. Expect 60-90 minutes with skip_13dg, or 3-4 hours with 13D/G enabled. Subsequent runs are much faster thanks to incremental caching.

### 5. Open the dashboard

`https://<your-username>.github.io/<repo-name>/`

## Dashboard Features

- **Sortable table** with all 7 score components per row, plus a flags column highlighting concerning (4.02, NT filing, director resignation, dividend cut) and bullish items (13D accumulation, dividend initiation, buyback announcement)
- **Filter pills**: All / Positive / Negative / Insider Buys / 4.02-4.01 / Late Filings / 13D Activist / Div ↑ / Div ↓ / Buybacks
- **Click any row** to expand a drill-down with:
  - Insider activity (buys/sells, $ values, 10b5-1 filtered counts)
  - Share count history and QoQ change
  - 8-K events and late filings
  - Governance vote tallies
  - 13D/G activity
  - Financial signal values
  - **Score contributions list** — every signal that fired with its weight
  - **Recent Form 4 transactions table** — every transaction in the 90d lookback with its classification (counted buy, counted sell, 10b5-1 filtered, or noise), and a direct link to the filing on SEC.gov
  - EDGAR drill-down links

## Methodology Notes

**Why insider buys are weighted 4x sells**: Academic research (Jeng-Metrick-Zeckhauser 2003, Cohen-Malloy-Pomorski 2012) consistently finds insider purchases predict future returns much more strongly than insider sales. Sales happen for many non-informational reasons; purchases mainly happen when the insider thinks the stock will go up.

**Why 10b5-1 transactions are filtered out**: 10b5-1 plans are pre-scheduled trades that by definition aren't discretionary — the trade was set before any new information. The parser checks both the form-level `<aff10b5One>` flag and per-transaction footnote references.

**Why share count uses XBRL, not 8-K announcements**: The 8-K signal captures when a buyback *program is authorized*. The XBRL signal captures actual share count *changes*. Companies announce programs they don't execute, and execute (offset by stock-based comp) without headlines.

**Why dividends use per-share TTM, not total dollars**: Total dividends grow mechanically with share count (M&A, secondaries). DPS-TTM reflects board capital-allocation decisions. Initiation is bonus-weighted because it signals a major shift in capital strategy.

## Limitations

- **Gross margin for financials**: Banks/insurers don't report "gross profit" in the traditional sense. Their scores skip this signal rather than fire spuriously. NIM and combined ratio would be the meaningful substitutes — not yet implemented.
- **8-K 5.02 text parsing**: Director resignation detection uses regex matching. False positives possible. Cross-check the underlying filing.
- **13D/G is expensive**: The EFTS full-text search adds significant runtime. Recommend `--skip-13dg` until the rest of the system is stable.
- **EDGAR rate limit**: 10 req/sec. Client targets 8 req/sec. First full run with all signals takes 2-4 hours.

## Architecture

```
src/
├── edgar.py        # Rate-limited HTTP client
├── universe.py     # S&P 500 membership tracking
├── form4.py        # Form 4 XML parsing + 10b5-1 filtering
├── shares.py       # XBRL share count
├── eightk.py       # 8-K item parsing + NT filings
├── thirteendg.py   # 13D/G EFTS search
├── financials.py   # XBRL TTM dividends, margins, OCF
├── scoring.py      # Composite scoring with contribution breakdown
└── pipeline.py     # Orchestrator
```

## Customization

**Change weights**: edit `ScoreWeights` defaults in `src/scoring.py`.

**Track a different universe**: replace `fetch_sp500_from_wikipedia()` in `src/universe.py` with your own ticker list (Russell 1000, your portfolio).

**Add a signal**: write a parser module, add a component function to `scoring.py`, wire through `pipeline.py`, surface in the dashboard.
