# Pre-Breakout Paper Trading System

A local Python workflow that finds controlled swing-trade setups, confirms them
after the opening volatility, manages a $25,000 paper account, and improves the
strategy using resolved outcomes instead of intuition.

> Paper trading only — not investment advice. Finviz and Yahoo data may be
> delayed, incomplete, or unavailable.

![Strategy and process overview](assets/overview.png)

## Daily workflow

All times are America/New_York. The jobs run automatically Monday through
Friday and can also be run independently.

| Time | Job | Result |
|---|---|---|
| 8:45 AM | `morning_candidates.py` | Raw and scored stock universe |
| 9:45 AM | `confirm_945.py` | VWAP, trend, and breakout confirmation |
| 9:50 AM | `daily_report.py` | Entries, exits, account report, and dashboard |
| 10:00 AM | Strategy optimizer | Measurement and guarded parameter review |
| 10:30 AM | Daily infographic | Visual summary posted in Codex |

![Sample daily message](assets/daily_message.png)

## 1. Morning stock scan

Finviz supplies the starting universe:

- U.S. stocks only; ETFs and ETNs are excluded twice for safety.
- Price under $20, average volume above 1 million, relative volume above 1.
- Positive weekly performance.

Yahoo daily history then scores each stock for early weekly momentum, a
controlled daily move, healthy RSI, 20/50-day trend, compression, prior
accumulation, proximity to a 20-day breakout, manageable downside, and useful
volume. Exhausted moves, RSI above 75, weekly overextension, and volume mania
receive explicit penalties.

| Score | Morning signal |
|---:|---|
| 42+ | 🔥 Buy next session |
| 35–41 | 👀 Pre-breakout watch |
| 26–34 | 🟡 Monitor |
| Below 26 | 🔴 Pass |

## 2. 9:45 confirmation

Five-minute data confirms whether a setup is behaving correctly after the
open. The score rewards price above the open, prior close, and VWAP; a break of
the first 15-minute high; and remaining within 5% of the open. Moves above 8%
are penalized as overextended.

| Score | Confirmation signal |
|---:|---|
| 40+ | 🔥 Buy today |
| 25–39 | 👀 Wait |
| Below 25 | 🔴 Pass |

Each confirmation also records sector, RSI, market regime, SPY five-day return,
confirmation volume, five-minute spread proxy, score band, and every scoring
component that fired.

## 3. Paper-account rules

- Starting capital: **$25,000**
- Entry size: **100 shares**
- Maximum: **20 new confirmed trades per day**, subject to cash
- Sector ceiling: **25% of starting capital**
- Simulated slippage: **10 basis points on every entry and exit**
- Same ticker/date duplicates are rejected
- Partial-sale proceeds immediately return to available paper cash

Trades remain open from day to day. A 100-share position scales out as follows:

1. Sell 50 shares at **+10%**.
2. Sell 25 shares at **+20%**.
3. Sell the final 25 shares at **+30%**.
4. After +20% is reached, protect the final lot with a fallback exit at **+10%**.

An **−8% stop** or **10-session time stop** closes every share still held. If a
target and stop occur inside the same five-minute bar, the system conservatively
records the stop first. Exit processing is idempotent, so rerunning a report
cannot sell the same tranche twice.

## 4. Reporting and dashboard

`daily_report.py` maintains the complete lot-aware ledger in
`paper_trades.csv`. It writes dated CSV/HTML reports and refreshes the permanent
light-theme dashboard at:

```text
exports/dashboard.html
```

The dashboard shows account equity, cash, deployed capital, realized and
unrealized P/L, open and resolved trades, staged exits, daily equity history,
score-band performance, signal contribution, and active risk controls.

## 5. Continuous improvement

`strategy_baseline.json` is immutable baseline v1. Active, reversible settings
live in `strategy_settings.json`, and every evaluated change belongs in
`strategy_changelog.csv`.

The 10:00 AM optimizer:

- Defines success as reaching the +10% scale-out before the −8% stop.
- Waits for at least 30 resolved trades before tuning.
- Uses chronological walk-forward testing without look-ahead leakage.
- Evaluates hit rate, expectancy, profit factor, drawdown, regime, sector,
  spread, score band, and signal components.
- Adopts at most one evidence-backed setting change per day.
- Never weakens capital, slippage, stop, concentration, or sample safeguards to
  inflate the hit rate.
- Stops tuning after at least 80% success over the latest 50 resolved trades,
  while continuing to monitor performance.

## Project layout

```text
stock/
├── scanner_config.py          # Paths, account values, active strategy settings
├── morning_candidates.py      # Finviz universe and daily technical scoring
├── confirm_945.py             # Intraday confirmation and entry telemetry
├── daily_report.py            # Entries, staged exits, ledger, and reports
├── dashboard.py               # Permanent account dashboard
├── backtest.py                # Historical confirmation replay
├── strategy_baseline.json     # Frozen baseline v1
├── strategy_settings.json     # Active reversible parameters
├── strategy_changelog.csv     # Optimization audit trail
├── paper_trades.csv           # Live paper-account ledger
├── assets/
│   ├── overview.png
│   └── daily_message.png
├── exports/                   # Dated CSV, HTML, dashboard, and infographic output
└── logs/
```

## Setup

Requires Python 3.9 or newer:

```bash
cd /Users/dadon003/code/stock
python3 -m pip install pandas yfinance finvizfinance "urllib3<2"
mkdir -p exports logs
```

The `urllib3<2` constraint avoids the common LibreSSL warning with Apple's
Xcode Python. Override the project root with
`STOCK_SCREENER_HOME=/another/path`; individual paths and account values can be
overridden using the environment variables in `scanner_config.py`.

Run the core workflow manually:

```bash
python3 morning_candidates.py
python3 confirm_945.py
python3 daily_report.py
```

## Historical replay

Use a candidate snapshot from the requested date whenever possible:

```bash
python3 backtest.py \
  --date 2026-06-23 \
  --candidates exports/finviz_raw_2026-06-23.csv
```

If `--candidates` is omitted, the newest snapshot is used and the report is
clearly marked as look-ahead biased. Historical results are research evidence,
not a prediction of future performance.
