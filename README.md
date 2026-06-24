# Local stock screener and paper trader

This project builds a morning pre-breakout watchlist, checks it against 9:45 AM
price action, paper-buys the top 20 confirmed setups at 100 shares each, and
creates a daily HTML performance report. It is a research tool, not financial
advice, and market data can be delayed or incomplete.

## Setup

Requires Python 3.9 or newer:

```bash
cd /Users/dadon003/code/stock
python3 -m pip install pandas yfinance finvizfinance "urllib3<2"
mkdir -p exports logs
```

The `urllib3<2` constraint avoids the common LibreSSL warning with Apple's
Xcode Python. By default all files live beside these scripts. Override the root
with `STOCK_SCREENER_HOME=/another/path`; individual output paths can also be
set with the environment variables documented in `scanner_config.py`.

## Daily workflow

Run each command independently after the time shown (America/New_York):

```bash
python3 morning_candidates.py  # around 8:45 AM
python3 confirm_945.py          # at/after 9:45 AM
python3 daily_report.py         # around 9:50 AM
```

The morning job saves raw and scored CSV/HTML files. Confirmation saves the
9:45 CSV/HTML pair. The report updates `paper_trades.csv`, writes a performance
CSV, creates a daily report, and refreshes the cumulative `exports/dashboard.html`.
The live paper account begins with $25,000; buys remain at 100 shares and are
skipped when their cost would exceed available paper cash.

The Finviz universe is restricted to stocks only; ETFs and ETNs are excluded by
both the upstream industry filter and a local validation safeguard.

Example crontab (`crontab -e`; ensure the Mac timezone is Eastern):

```cron
45 8 * * 1-5 cd /Users/dadon003/code/stock && python3 morning_candidates.py >> logs/morning.log 2>&1
45 9 * * 1-5 cd /Users/dadon003/code/stock && python3 confirm_945.py >> logs/confirm.log 2>&1
50 9 * * 1-5 cd /Users/dadon003/code/stock && python3 daily_report.py >> logs/report.log 2>&1
```

Finviz and Yahoo can occasionally reject or omit symbols. Each stage skips bad
symbols without losing the usable results. Re-running the report is safe: the
same ticker will not be added twice on the same trade date.

## Historical replay

Replay the confirmation and top-20 rules for a saved candidate universe:

```bash
python3 backtest.py --date 2026-06-23 --candidates exports/finviz_raw_2026-06-23.csv
```

If `--candidates` is omitted, the newest raw snapshot is used. A snapshot from
a different date introduces look-ahead bias, which is prominently disclosed in
the generated CSV/HTML report.

## Risk controls and continuous improvement

The live paper ledger uses realistic 10-basis-point slippage on entries and
exits. For a 100-share position it sells 50 shares at +10%, 25 at +20%, and the
last 25 at +30%. After +20% is reached, that final lot is protected by a +10%
fallback exit. A -8% stop or 10-session time limit closes all shares still held,
and every partial sale immediately recycles cash. No sector may consume more
than 25% of starting capital.
When a target and stop appear inside the same five-minute bar, the back office
conservatively records the stop first.

Each entry records sector, RSI, market regime, confirmation volume, five-minute
spread proxy, score band, and every scoring component that fired. Daily exports
include score-band and component-level performance. `strategy_baseline.json` is
the immutable baseline; tunable live values are held separately in
`strategy_settings.json`. This separation makes experiments reversible and
prevents later changes from rewriting the comparison strategy.
