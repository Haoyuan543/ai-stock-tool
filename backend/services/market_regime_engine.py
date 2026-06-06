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

PROBES = {
    "taiwan_index": {"yahoo": "^TWII", "stooq": None, "name": "台股加權指數"},
    "vix": {"yahoo": "^VIX", "stooq": "^vix", "name": "VIX 恐慌指數"},
    "dxy": {"yahoo": "DX-Y.NYB", "stooq": "dx.f", "name": "美元指數"},
    "usd_twd": {"yahoo": "TWD=X", "stooq": "usdtwd", "name": "美元兌台幣"},
    "sp500": {"yahoo": "^GSPC", "stooq": "^spx", "name": "美股 S&P 500"},
}


def build_market_regime(stock: dict[str, Any]) -> dict[str, Any]:
    markets: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    missing_parts: list[str] = []

    for key, config in PROBES.items():
        row = _fetch_yahoo_series(config["yahoo"], config["name"])
        if not row and config.get("stooq"):
            row = _fetch_stooq_quote(config["stooq"], config["name"])
            if row:
                missing_parts.append(f"{config['name']}使用 Stooq 備援，僅有最新價，趨勢信心較低")
        if row:
            markets[key] = row
            sources.append({"name": f"{config['name']}（{row.get('source')}）", "url": row.get("url"), "as_of": row.get("date")})
        else:
            missing_parts.append(f"{config['name']}未取得")

    search = web_search(
        [
            "台股 加權指數 航運股 VIX 美元 台幣 風險偏好 最新",
            "Taiwan stock market shipping sector VIX USD TWD risk sentiment latest",
            "US stocks VIX dollar Taiwan risk sentiment latest",
        ],
        max_results_per_query=2,
    )
    sources.extend(search.get("sources", []))

    shipping = _shipping_signal(stock)
    taiwan = _index_signal(markets.get("taiwan_index"))
    sp500 = _index_signal(markets.get("sp500"))
    vix_risk = _vix_signal(markets.get("vix"))
    dxy_risk = _fx_risk_signal(markets.get("dxy"))
    twd_risk = _fx_risk_signal(markets.get("usd_twd"))
    search_bias = _search_bias(search.get("results", []))

    positive = 0
    negative = 0
    for item in (taiwan, sp500, shipping):
        positive += item == "bullish"
        negative += item == "bearish"
    for item in (vix_risk, dxy_risk, twd_risk):
        positive += item == "risk_on"
        negative += item == "risk_off"
    positive += search_bias == "risk_on"
    negative += search_bias == "risk_off"

    if negative >= positive + 2:
        regime = "risk_off"
    elif positive >= negative + 2:
        regime = "risk_on"
    elif positive or negative:
        regime = "neutral"
    else:
        regime = "unknown"

    confidence = 0.12 + min(len(markets), 5) * 0.09
    if shipping != "neutral":
        confidence += 0.12
    if search_bias != "neutral":
        confidence += 0.06
    confidence = min(confidence, 0.72)
    if regime == "unknown":
        confidence = min(confidence, 0.25)

    if len(markets) < 3:
        missing_reason = "資料限制：市場環境資料仍不完整；" + "；".join(_dedupe(missing_parts))
    elif missing_parts:
        missing_reason = "資料限制：部分市場環境資料使用備援或搜尋推論；" + "；".join(_dedupe(missing_parts[:3]))
    else:
        missing_reason = ""

    return {
        "market_regime": regime,
        "taiwan_market": taiwan,
        "shipping_sector": shipping,
        "us_market": sp500,
        "vix_signal": vix_risk,
        "dxy_signal": dxy_risk,
        "usd_twd_signal": twd_risk,
        "search_bias": search_bias,
        "confidence": round(confidence, 2),
        "coverage_credit": 0.5 if confidence >= 0.3 else 0.25 if confidence >= 0.2 else 0,
        "market_snapshot": markets,
        "sources": sources,
        "missing_reason": missing_reason,
    }


def _fetch_yahoo_series(symbol: str, name: str) -> dict[str, Any] | None:
    result = None
    encoded = quote(symbol, safe="")
    for host in YAHOO_HOSTS:
        try:
            response = httpx.get(
                f"https://{host}/v8/finance/chart/{encoded}",
                params={"range": "3mo", "interval": "1d"},
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
    for ts, close in zip(timestamps, closes):
        if close is not None:
            rows.append((datetime.utcfromtimestamp(ts).date().isoformat(), float(close)))
    if len(rows) < 5:
        return None
    latest_date, latest = rows[-1]
    previous = rows[-2][1] if len(rows) >= 2 else None
    five_ago = rows[-6][1] if len(rows) >= 6 else rows[0][1]
    ma20 = sum(value for _, value in rows[-20:]) / min(len(rows), 20)
    ma60 = sum(value for _, value in rows[-60:]) / min(len(rows), 60)
    return {
        "symbol": symbol,
        "name": name,
        "date": latest_date,
        "close": latest,
        "change_1d_pct": ((latest - previous) / previous * 100) if previous else None,
        "change_5d_pct": ((latest - five_ago) / five_ago * 100) if five_ago else None,
        "ma20": ma20,
        "ma60": ma60,
        "source": "Yahoo Finance",
        "url": f"https://finance.yahoo.com/quote/{symbol}",
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
        "ma20": None,
        "ma60": None,
        "source": "Stooq",
        "url": f"https://stooq.com/q/?s={symbol}",
    }


def _index_signal(row: dict[str, Any] | None) -> str:
    if not row:
        return "neutral"
    close = row.get("close")
    ma20 = row.get("ma20")
    ma60 = row.get("ma60")
    change_5d = row.get("change_5d_pct") or 0
    if close and ma20 and ma60 and close > ma20 >= ma60 and change_5d >= -1:
        return "bullish"
    if close and ma20 and ma60 and close < ma20 <= ma60:
        return "bearish"
    if change_5d <= -3:
        return "bearish"
    return "neutral"


def _shipping_signal(stock: dict[str, Any]) -> str:
    close, ma20, ma60 = stock.get("close"), stock.get("ma20"), stock.get("ma60")
    if close is None or ma20 is None or ma60 is None:
        return "neutral"
    if close > ma20 > ma60:
        return "bullish"
    if close < ma20 < ma60:
        return "bearish"
    return "neutral"


def _vix_signal(row: dict[str, Any] | None) -> str:
    if not row:
        return "neutral"
    close = row.get("close") or 0
    change_5d = row.get("change_5d_pct") or 0
    if close >= 22 or change_5d >= 12:
        return "risk_off"
    if close <= 16 and change_5d <= 5:
        return "risk_on"
    return "neutral"


def _fx_risk_signal(row: dict[str, Any] | None) -> str:
    if not row:
        return "neutral"
    close = row.get("close")
    ma20 = row.get("ma20")
    change_5d = row.get("change_5d_pct") or 0
    if close and ma20 and close > ma20 and change_5d > 1:
        return "risk_off"
    if close and ma20 and close < ma20 and change_5d < 0:
        return "risk_on"
    return "neutral"


def _search_bias(results: list[dict[str, Any]]) -> str:
    text = " ".join(f"{row.get('title') or ''} {row.get('snippet') or ''}" for row in results).lower()
    if any(word in text for word in ["risk-off", "risk off", "vix", "bearish", "避險", "風險趨避"]):
        return "risk_off"
    if any(word in text for word in ["risk-on", "risk on", "bullish", "風險偏好", "走強"]):
        return "risk_on"
    return "neutral"


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
