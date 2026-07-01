"""Shadow-only news intelligence for morning candidates."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
POSITIVE_TERMS = (
    "approval", "approved", "contract", "partnership", "beats estimates",
    "raises guidance", "record revenue", "positive results", "award",
)
RISK_TERMS = (
    "public offering", "registered direct", "shelf registration", "dilution",
    "bankruptcy", "going concern", "delisting", "trading halt", "investigation",
)


def configured() -> bool:
    return bool(os.getenv("ALPACA_API_KEY_ID", "").strip() and os.getenv("ALPACA_API_SECRET_KEY", "").strip())


def _request_news(symbols: list[str], hours: int = 72) -> list[dict]:
    if not configured() or not symbols:
        return []
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    params = urlencode({
        "symbols": ",".join(symbols), "start": start, "limit": 50,
        "sort": "desc", "include_content": "false",
    })
    request = Request(
        f"{NEWS_URL}?{params}",
        headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET_KEY"],
            "Accept": "application/json", "User-Agent": "stock-strategy-app/2.4",
        },
    )
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return list(payload.get("news") or [])


def _classify(headline: str, summary: str = "") -> tuple[int, list[str]]:
    text = f"{headline} {summary}".lower()
    positive = [term for term in POSITIVE_TERMS if term in text]
    risks = [term for term in RISK_TERMS if term in text]
    score = min(2, len(positive)) - min(2, len(risks)) * 2
    flags = [f"positive:{term}" for term in positive] + [f"risk:{term}" for term in risks]
    return score, flags


def enrich_candidates(candidates: pd.DataFrame, ticker_column: str = "Ticker") -> pd.DataFrame:
    """Attach non-trading shadow features; failures never fail the morning scan."""
    result = candidates.copy()
    defaults = {
        "catalyst_mode": "SHADOW", "catalyst_configured": configured(),
        "news_count_72h": 0, "catalyst_positive_count": 0, "catalyst_risk_count": 0,
        "catalyst_shadow_score": 0, "catalyst_flags": "", "latest_news_at": pd.NA,
        "latest_headline": "", "latest_news_url": "", "catalyst_source": "Alpaca News",
    }
    for column, value in defaults.items():
        result[column] = value
    if result.empty or ticker_column not in result or not configured():
        return result

    symbols = result[ticker_column].dropna().astype(str).str.upper().unique().tolist()
    articles: list[dict] = []
    try:
        for offset in range(0, len(symbols), 50):
            articles.extend(_request_news(symbols[offset:offset + 50]))
    except Exception as exc:
        result["catalyst_source"] = f"Alpaca unavailable: {type(exc).__name__}"
        return result

    by_symbol: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    for article in articles:
        for symbol in article.get("symbols") or []:
            symbol = str(symbol).upper()
            if symbol in by_symbol:
                by_symbol[symbol].append(article)
    for index, row in result.iterrows():
        symbol = str(row[ticker_column]).upper()
        news = by_symbol.get(symbol, [])
        if not news:
            continue
        scored = []
        all_flags = []
        for article in news:
            score, flags = _classify(str(article.get("headline", "")), str(article.get("summary", "")))
            scored.append(score)
            all_flags.extend(flags)
        latest = news[0]
        result.at[index, "news_count_72h"] = len(news)
        result.at[index, "catalyst_positive_count"] = sum(score > 0 for score in scored)
        result.at[index, "catalyst_risk_count"] = sum(score < 0 for score in scored)
        result.at[index, "catalyst_shadow_score"] = max(-4, min(4, sum(scored)))
        result.at[index, "catalyst_flags"] = ", ".join(sorted(set(all_flags)))
        result.at[index, "latest_news_at"] = latest.get("created_at", pd.NA)
        result.at[index, "latest_headline"] = latest.get("headline", "")
        result.at[index, "latest_news_url"] = latest.get("url", "")
    return result

