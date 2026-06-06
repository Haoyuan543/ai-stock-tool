from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
from typing import Any
from urllib.parse import quote

import httpx

from backend.search.web_search import web_search


YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
HEADERS = {"User-Agent": "Mozilla/5.0"}
STOOQ_QUOTE = "https://stooq.com/q/l/"
QUERIES = [
    "US tariff policy shipping trade latest",
    "US China trade policy tariff shipping latest",
    "Brent WTI oil price shipping cost latest",
    "Middle East conflict shipping oil price latest",
    "Russia Ukraine war shipping oil price latest",
    "US sanctions shipping trade latest",
    "Fed interest rate latest risk assets Asia stocks",
]


def build_international_events() -> dict[str, Any]:
    oil_missing: list[str] = []
    oil_prices = {
        "wti": _fetch_oil_series("CL=F", "cl.f", "WTI 原油", oil_missing),
        "brent": _fetch_oil_series("BZ=F", "sc.f", "Brent 原油參考價", oil_missing),
    }
    search = web_search(QUERIES, max_results_per_query=3)
    rows = search.get("results", [])
    text = " ".join(f"{row.get('title') or ''} {row.get('snippet') or ''}" for row in rows).lower()

    us_policy = _classify_policy(text)
    war_geopolitics = _classify_war(text)
    oil = _classify_oil(oil_prices)
    overall_risk = _overall_risk(us_policy, war_geopolitics, oil)
    unique_urls = {row.get("url") for row in rows if row.get("url")}
    confidence = min(0.8, 0.25 + 0.08 * len(unique_urls) + (0.12 if oil_prices["wti"] or oil_prices["brent"] else 0))

    if oil_missing:
        missing_reason = "資料限制：" + "；".join(_dedupe(oil_missing))
    elif not rows and not oil_prices["wti"] and not oil_prices["brent"]:
        missing_reason = "資料限制：國際事件資料未取得，需補充新聞或市場資料來源。"
    else:
        missing_reason = ""

    return {
        "overall_risk": overall_risk,
        "us_policy": us_policy,
        "war_geopolitics": war_geopolitics,
        "oil": oil,
        "oil_prices": oil_prices,
        "confidence": round(confidence, 2),
        "summary": _summary(us_policy, war_geopolitics, oil, overall_risk),
        "sources": search.get("sources", []) + _oil_sources(oil_prices),
        "events": rows[:10],
        "missing_reason": missing_reason,
    }


def _fetch_oil_series(yahoo_symbol: str, stooq_symbol: str, name: str, missing: list[str]) -> dict[str, Any] | None:
    yahoo = _fetch_yahoo_series(yahoo_symbol, name)
    if yahoo:
        yahoo["source"] = "Yahoo Finance"
        return yahoo
    missing.append(f"{name} Yahoo Finance 暫時無法取得，已改用備援來源")

    stooq = _fetch_stooq_quote(stooq_symbol, name)
    if stooq:
        stooq["source"] = "Stooq"
        missing.append(f"{name} 備援來源只有最新價，近 5 日變化暫無法計算")
        return stooq
    missing.append(f"{name} 備援來源也未回傳有效報價")
    return None


def _fetch_yahoo_series(symbol: str, name: str) -> dict[str, Any] | None:
    result = None
    encoded = quote(symbol, safe="")
    for host in YAHOO_HOSTS:
        try:
            response = httpx.get(
                f"https://{host}/v8/finance/chart/{encoded}",
                params={"range": "1mo", "interval": "1d"},
                headers=HEADERS,
                timeout=httpx.Timeout(30.0, connect=15.0, read=30.0, write=15.0, pool=15.0),
            )
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
            break
        except Exception:
            result = None
    if not result:
        return None
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []

    rows: list[tuple[str, float]] = []
    for timestamp, close in zip(timestamps, closes):
        if close is not None:
            rows.append((datetime.utcfromtimestamp(timestamp).date().isoformat(), float(close)))
    if len(rows) < 2:
        return None
    latest_date, latest = rows[-1]
    previous = rows[-2][1]
    five_ago = rows[-6][1] if len(rows) >= 6 else rows[0][1]
    return {
        "symbol": symbol,
        "name": name,
        "date": latest_date,
        "close": latest,
        "change_1d_pct": (latest - previous) / previous * 100 if previous else None,
        "change_5d_pct": (latest - five_ago) / five_ago * 100 if five_ago else None,
    }


def _fetch_stooq_quote(symbol: str, name: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(STOOQ_QUOTE, params={"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"}, headers=HEADERS, timeout=20.0)
        response.raise_for_status()
        if "<html" in response.text.lower():
            return None
        rows = list(csv.DictReader(StringIO(response.text)))
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    close_raw = row.get("Close")
    if not close_raw or close_raw == "N/D":
        return None
    try:
        close = float(close_raw)
    except ValueError:
        return None
    return {
        "symbol": symbol.upper(),
        "name": name,
        "date": row.get("Date"),
        "close": close,
        "change_1d_pct": None,
        "change_5d_pct": None,
    }


def _classify_policy(text: str) -> dict[str, Any]:
    negative = ("tariff", "sanction", "export control", "trade war", "restriction")
    positive = ("deal", "cut tariff", "agreement", "easing")
    if any(word in text for word in negative):
        return {"status": "watch", "impact": "negative", "summary": "美國關稅、制裁或貿易限制相關新聞需要納入風險觀察。"}
    if any(word in text for word in positive):
        return {"status": "improving", "impact": "positive", "summary": "若貿易緊張降溫，可能降低部分總體風險。"}
    return {"status": "neutral", "impact": "neutral", "summary": "目前沒有明確新的美國政策訊號。"}


def _classify_war(text: str) -> dict[str, Any]:
    high_words = ("war", "attack", "missile", "strike", "escalation", "houthi", "red sea", "middle east")
    easing_words = ("ceasefire", "truce", "peace talks")
    if any(word in text for word in easing_words):
        return {"status": "improving", "impact": "mixed", "summary": "若地緣風險降溫，可能削弱繞航與運價支撐。"}
    if any(word in text for word in high_words):
        return {"status": "elevated", "impact": "freight_supportive", "summary": "戰爭或紅海風險仍可能支撐繞航需求與貨櫃運價。"}
    return {"status": "neutral", "impact": "neutral", "summary": "目前沒有明確新的戰爭或地緣風險訊號。"}


def _classify_oil(oil_prices: dict[str, Any]) -> dict[str, Any]:
    values = [row for row in oil_prices.values() if row]
    if not values:
        return {"status": "unknown", "impact": "unknown", "summary": "油價資料未取得，燃油成本影響需保留。"}
    changes = [row.get("change_5d_pct") for row in values if row.get("change_5d_pct") is not None]
    if not changes:
        return {"status": "known_price_only", "impact": "watch", "summary": "已取得部分油價最新報價，但缺少可比較序列，暫不把油價當成主要多空因子。"}
    avg_5d = sum(changes) / len(changes)
    if avg_5d >= 5:
        return {"status": "rising", "impact": "cost_pressure", "summary": "油價近 5 日明顯上升，可能提高燃油成本壓力。"}
    if avg_5d <= -5:
        return {"status": "falling", "impact": "cost_relief", "summary": "油價近 5 日明顯下降，可能緩解燃油成本壓力。"}
    return {"status": "stable", "impact": "neutral", "summary": "油價近 5 日變動不大，暫不構成主要多空因子。"}


def _overall_risk(policy: dict[str, Any], war: dict[str, Any], oil: dict[str, Any]) -> str:
    negatives = int(policy.get("impact") == "negative") + int(war.get("status") == "elevated") + int(oil.get("impact") == "cost_pressure")
    positives = int(policy.get("impact") == "positive") + int(war.get("status") == "improving") + int(oil.get("impact") == "cost_relief")
    if negatives >= positives + 2:
        return "high"
    if negatives > positives:
        return "medium"
    if positives > negatives:
        return "low"
    return "neutral"


def _summary(policy: dict[str, Any], war: dict[str, Any], oil: dict[str, Any], risk: str) -> str:
    risk_text = {"high": "高", "medium": "中", "neutral": "中性", "low": "低"}.get(risk, "未知")
    return f"國際事件整體風險為{risk_text}。{policy.get('summary')} {war.get('summary')} {oil.get('summary')}"


def _oil_sources(oil_prices: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in oil_prices.values():
        if row:
            symbol = row.get("symbol")
            source = row.get("source")
            url = f"https://finance.yahoo.com/quote/{symbol}" if source == "Yahoo Finance" else f"https://stooq.com/q/?s={symbol}"
            out.append({"name": f"{row['name']}（{source}）", "url": url, "as_of": row.get("date")})
    return out


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
