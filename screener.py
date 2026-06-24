import yfinance as yf
import pandas as pd

TICKERS = [
    "RZLV", "TENX", "FWRD", "ONEG", "URG", "RDW",
    "DGXX", "GPRO", "WEN", "RKT", "PFE", "UBER"
]

def scalar(value):
    return float(value.iloc[0]) if hasattr(value, "iloc") else float(value)

def interpret_score(score):
    if score >= 42:
        return "🔥 BUY NEXT SESSION"
    elif score >= 35:
        return "👀 PRE-BREAKOUT WATCH"
    elif score >= 26:
        return "🟡 MONITOR"
    else:
        return "🔴 PASS"

def score_stock(ticker):
    df = yf.download(
        ticker,
        period="3mo",
        interval="1d",
        auto_adjust=False,
        progress=False
    )

    if df.empty or len(df) < 30:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close = df["Close"]
    volume = df["Volume"]

    price = scalar(close.iloc[-1])
    prev_close = scalar(close.iloc[-2])
    close6 = scalar(close.iloc[-6])

    day_change = (price / prev_close - 1) * 100
    week_change = (price / close6 - 1) * 100

    sma20 = scalar(close.rolling(20).mean().iloc[-1])
    sma50 = scalar(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None

    avg_vol20 = scalar(volume.rolling(20).mean().iloc[-1])
    today_vol = scalar(volume.iloc[-1])
    rvol = today_vol / avg_vol20 if avg_vol20 else 0

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss))
    rsi_latest = scalar(rsi.iloc[-1])

    last5_high = scalar(close.tail(5).max())
    last5_low = scalar(close.tail(5).min())
    compression = ((last5_high - last5_low) / last5_low) * 100

    high20 = scalar(close.tail(20).max())
    distance_from_20d_high = ((high20 - price) / high20) * 100

    downside_to_5d_low = ((price - last5_low) / price) * 100

    daily_pct = close.pct_change() * 100
    avg_vol20_series = volume.rolling(20).mean()

    prior_accumulation = (
        (
            (daily_pct.tail(10) > 4)
            &
            (volume.tail(10) > avg_vol20_series.tail(10) * 1.75)
        )
    ).any()

    target_10 = price * 1.10
    target_20 = price * 1.20
    target_30 = price * 1.30

    score = 0
    notes = []

    # Basic tradeable range
    if 1 <= price <= 25:
        score += 4
        notes.append("tradeable price")

    # Early weekly momentum, not exhausted
    if -2 <= week_change <= 15:
        score += 6
        notes.append("early weekly momentum")

    # Quiet current day, not already chasing
    if -2 <= day_change <= 5:
        score += 8
        notes.append("not chasing today")

    # RSI: healthy but not overheated
    if 45 <= rsi_latest <= 68:
        score += 6
        notes.append("healthy RSI")

    # Trend support
    if price > sma20:
        score += 5
        notes.append("above 20SMA")

    if sma50 and price > sma50:
        score += 3
        notes.append("above 50SMA")

    # Prior buying before breakout
    if prior_accumulation:
        score += 10
        notes.append("prior accumulation")

    # Tight range = coiled
    if compression <= 5:
        score += 12
        notes.append("tight squeeze")
    elif compression <= 8:
        score += 6
        notes.append("moderate compression")

    # Sitting under breakout, not already gone
    if 1 <= distance_from_20d_high <= 5:
        score += 12
        notes.append("coiled under breakout")
    elif 0 <= distance_from_20d_high <= 8:
        score += 6
        notes.append("near breakout")

    # Manageable risk
    if downside_to_5d_low <= 8:
        score += 6
        notes.append("risk under 8%")

    # Volume present but not mania
    if 1.2 <= rvol <= 4:
        score += 6
        notes.append("buying volume present")

    # Penalties
    if day_change > 8:
        score -= 15
        notes.append("penalty: too late today")

    if week_change > 18:
        score -= 10
        notes.append("penalty: already running")

    if rsi_latest > 75:
        score -= 8
        notes.append("penalty: RSI overheated")

    signal = interpret_score(score)

    entry_ready = (
        score >= 42
        and day_change < 5
        and compression < 6
        and 1 <= distance_from_20d_high <= 5
    )

    if entry_ready:
        signal = "🔥 BUY NEXT SESSION"

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "day_%": round(day_change, 2),
        "week_%": round(week_change, 2),
        "rvol": round(rvol, 2),
        "rsi": round(rsi_latest, 1),
        "compression_%": round(compression, 2),
        "dist_20d_high_%": round(distance_from_20d_high, 2),
        "risk_to_5d_low_%": round(downside_to_5d_low, 2),
        "target_10%": round(target_10, 2),
        "target_20%": round(target_20, 2),
        "target_30%": round(target_30, 2),
        "score": score,
        "signal": signal,
        "why": ", ".join(notes)
    }

results = []

for ticker in TICKERS:
    result = score_stock(ticker)
    if result:
        results.append(result)

df = pd.DataFrame(results).sort_values("score", ascending=False)

print("\n=== PRE-BREAKOUT 10–30% TRADE SCREENER ===\n")
print(df.to_string(index=False))

df.to_csv("pre_breakout_trade_screener.csv", index=False)

print("\nSaved → pre_breakout_trade_screener.csv")
