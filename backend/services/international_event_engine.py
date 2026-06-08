from __future__ import annotations

import csv
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import StringIO
from typing import Any
from urllib.parse import quote

import httpx

from backend.config import get_settings
from backend.search.web_search import web_search
from backend.services.source_registry import now_taipei, source_stamp


YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
HEADERS = {"User-Agent": "Mozilla/5.0"}
STOOQ_QUOTE = "https://stooq.com/q/l/"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
EIA_BASE = "https://api.eia.gov/v2/petroleum/pri/spt/data/"

EVENT_QUERIES = [
    "US tariff policy shipping trade latest",
    "US China trade policy tariff shipping latest",
    "Brent WTI oil price shipping cost latest",
    "Middle East conflict shipping oil price latest",
    "Russia Ukraine war shipping oil price latest",
    "US sanctions shipping trade latest",
    "Fed interest rate latest risk assets Asia stocks",
]


def build_international_events() -> dict[str, Any]:
    settings = get_settings()
    fetched_at = now_taipei()
    missing: list[str] = []
    sources: list[dict[str, Any]] = []

    oil_prices = {
        "wti": _fetch_oil_price("wti", settings.eia_api_key, fetched_at, missing, sources),
        "brent": _fetch_oil_price("brent", settings.eia_api_key, fetched_at, missing, sources),
    }

    rss_events = _fetch_google_news_events(EVENT_QUERIES, fetched_at, sources, missing)
    search = web_search(EVENT_QUERIES, max_results_per_query=2)
    search_events = [
        {
            "title": row.get("title"),
            "url": row.get("url"),
            "source": row.get("source") or "公開搜尋",
            "published_at": row.get("published_at") or "",
            "summary": row.get("snippet") or "",
            "method": "search",
        }
        for row in search.get("results", [])[:12]
    ]
    sources.extend(search.get("sources", []))

    events = _dedupe_events(rss_events + search_events)
    text = " ".join(f"{row.get('title') or ''} {row.get('summary') or ''}" for row in events).lower()

    us_policy = _classify_policy(text)
    war_geopolitics = _classify_war(text)
    oil = _classify_oil(oil_prices)
    overall_risk = _overall_risk(us_policy, war_geopolitics, oil)
    confidence = _confidence(oil_prices, events, sources)

    if not events:
        missing.append("資料不足：Google News RSS 與搜尋備援都沒有取得可用的國際事件。")

    source_stamps = _dedupe_sources(sources)
    return {
        "overall_risk": overall_risk,
        "us_policy": us_policy,
        "war_geopolitics": war_geopolitics,
        "oil": oil,
        "oil_prices": oil_prices,
        "confidence": round(confidence, 2),
        "summary": _summary(us_policy, war_geopolitics, oil, overall_risk),
        "sources": source_stamps,
        "source_stamps": source_stamps,
        "events": events[:12],
        "missing_reason": "；".join(_dedupe(missing)),
    }


def _fetch_oil_price(
    kind: str,
    eia_api_key: str,
    fetched_at: str,
    missing: list[str],
    sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    config = {
        "wti": {
            "name": "WTI 油價",
            "eia_series": "RWTC",
            "yahoo": "CL=F",
            "stooq": "cl.f",
        },
        "brent": {
            "name": "Brent 油價",
            "eia_series": "RBRTE",
            "yahoo": "BZ=F",
            "stooq": "sc.f",
        },
    }[kind]

    if eia_api_key:
        eia = _fetch_eia_spot_price(config["eia_series"], config["name"], eia_api_key, fetched_at)
        if eia:
            sources.append(eia["source_stamp"])
            return eia["data"]
        missing.append(f"資料提醒：{config['name']} EIA API 未成功，改用 Yahoo / Stooq 備援。")
    else:
        missing.append(f"資料提醒：未設定 EIA_API_KEY，{config['name']} 改用 Yahoo / Stooq 備援。")

    yahoo = _fetch_yahoo_series(config["yahoo"], config["name"], fetched_at)
    if yahoo:
        sources.append(yahoo["source_stamp"])
        return yahoo["data"]

    stooq = _fetch_stooq_quote(config["stooq"], config["name"], fetched_at)
    if stooq:
        sources.append(stooq["source_stamp"])
        missing.append(f"資料提醒：{config['name']} 使用 Stooq 備援，可能缺少近 5 日變化。")
        return stooq["data"]

    missing.append(f"資料不足：{config['name']} 無法從 EIA / Yahoo / Stooq 取得。")
    return None


def _fetch_eia_spot_price(series: str, name: str, api_key: str, fetched_at: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(
            EIA_BASE,
            params={
                "api_key": api_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[series][]": series,
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                "length": 8,
            },
            headers=HEADERS,
            timeout=20.0,
        )
        response.raise_for_status()
        rows = response.json().get("response", {}).get("data") or []
    except Exception:
        return None
    parsed = []
    for row in rows:
        value = _safe_float(row.get("value"))
        if value is not None:
            parsed.append((row.get("period"), value))
    if len(parsed) < 2:
        return None
    latest_date, latest = parsed[0]
    previous = parsed[1][1]
    five_ago = parsed[5][1] if len(parsed) > 5 else parsed[-1][1]
    data = {
        "symbol": series,
        "name": name,
        "date": latest_date,
        "close": latest,
        "change_1d_pct": _pct_change(latest, previous),
        "change_5d_pct": _pct_change(latest, five_ago),
        "source": "EIA Open Data API",
        "method": "official_api",
    }
    return {
        "data": data,
        "source_stamp": source_stamp(
            f"{name}｜EIA Open Data API",
            "https://www.eia.gov/opendata/",
            data_as_of=latest_date,
            method="official_api",
            fetched_at=fetched_at,
            confidence=0.92,
            is_exact=True,
        ),
    }


def _fetch_yahoo_series(symbol: str, name: str, fetched_at: str) -> dict[str, Any] | None:
    result = None
    encoded = quote(symbol, safe="")
    for host in YAHOO_HOSTS:
        try:
            response = httpx.get(
                f"https://{host}/v8/finance/chart/{encoded}",
                params={"range": "1mo", "interval": "1d"},
                headers=HEADERS,
                timeout=httpx.Timeout(25.0, connect=15.0, read=25.0, write=15.0, pool=15.0),
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
            rows.append((datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat(), float(close)))
    if len(rows) < 2:
        return None
    latest_date, latest = rows[-1]
    previous = rows[-2][1]
    five_ago = rows[-6][1] if len(rows) >= 6 else rows[0][1]
    data = {
        "symbol": symbol,
        "name": name,
        "date": latest_date,
        "close": latest,
        "change_1d_pct": _pct_change(latest, previous),
        "change_5d_pct": _pct_change(latest, five_ago),
        "source": "Yahoo Finance",
        "method": "public_chart_api",
    }
    return {
        "data": data,
        "source_stamp": source_stamp(
            f"{name}｜Yahoo Finance",
            f"https://finance.yahoo.com/quote/{symbol}",
            data_as_of=latest_date,
            method="public_chart_api",
            fetched_at=fetched_at,
            confidence=0.72,
            is_exact=False,
            note="公開圖表資料，作為 EIA 不可用時的油價備援。",
        ),
    }


def _fetch_stooq_quote(symbol: str, name: str, fetched_at: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(
            STOOQ_QUOTE,
            params={"s": symbol, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            headers=HEADERS,
            timeout=20.0,
        )
        response.raise_for_status()
        if "<html" in response.text.lower():
            return None
        rows = list(csv.DictReader(StringIO(response.text)))
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    close = _safe_float(row.get("Close"))
    if close is None:
        return None
    data = {
        "symbol": symbol.upper(),
        "name": name,
        "date": row.get("Date"),
        "close": close,
        "change_1d_pct": None,
        "change_5d_pct": None,
        "source": "Stooq",
        "method": "public_csv",
    }
    return {
        "data": data,
        "source_stamp": source_stamp(
            f"{name}｜Stooq",
            f"https://stooq.com/q/?s={symbol}",
            data_as_of=row.get("Date"),
            method="public_csv",
            fetched_at=fetched_at,
            confidence=0.62,
            is_exact=False,
            note="免費 CSV 備援，近 5 日變化可能不足。",
        ),
    }


def _fetch_google_news_events(
    queries: list[str],
    fetched_at: str,
    sources: list[dict[str, Any]],
    missing: list[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for query in queries[:5]:
        try:
            response = httpx.get(
                GOOGLE_NEWS_RSS,
                params={"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"},
                headers=HEADERS,
                timeout=20.0,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except Exception:
            missing.append(f"資料提醒：Google News RSS 取得失敗：{query}")
            continue
        sources.append(
            source_stamp(
                f"Google News RSS｜{query}",
                str(response.url),
                data_as_of=fetched_at,
                method="rss",
                fetched_at=fetched_at,
                confidence=0.65,
                is_exact=False,
                note="新聞標題與摘要，不等同於官方統計數字。",
            )
        )
        for item in root.findall(".//item")[:4]:
            title = _xml_text(item, "title")
            link = _xml_text(item, "link")
            pub_date = _parse_rss_date(_xml_text(item, "pubDate"))
            description = _xml_text(item, "description")
            events.append(
                {
                    "title": title,
                    "url": link,
                    "source": "Google News RSS",
                    "published_at": pub_date,
                    "summary": _strip_html(description),
                    "method": "rss",
                    "fetched_at": fetched_at,
                }
            )
    return events


def _xml_text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _parse_rss_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    except Exception:
        return value


def _strip_html(value: str) -> str:
    text = value.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    output = []
    in_tag = False
    for char in text:
        if char == "<":
            in_tag = True
            continue
        if char == ">":
            in_tag = False
            continue
        if not in_tag:
            output.append(char)
    return " ".join("".join(output).split())


def _classify_policy(text: str) -> dict[str, Any]:
    negative = ("tariff", "sanction", "export control", "trade war", "restriction", "關稅", "制裁", "出口管制")
    positive = ("deal", "cut tariff", "agreement", "easing", "降稅", "協議", "緩和")
    if any(word in text for word in negative):
        return {"status": "watch", "impact": "negative", "summary": "美國政策或貿易限制仍需觀察，可能壓抑風險偏好與航運需求預期。"}
    if any(word in text for word in positive):
        return {"status": "improving", "impact": "positive", "summary": "貿易政策若有緩和，對亞洲出口鏈與航運需求預期偏正面。"}
    return {"status": "neutral", "impact": "neutral", "summary": "目前未偵測到明確改變航運需求的美國政策訊號。"}


def _classify_war(text: str) -> dict[str, Any]:
    high_words = ("war", "attack", "missile", "strike", "escalation", "houthi", "red sea", "middle east", "戰爭", "攻擊", "紅海", "中東")
    easing_words = ("ceasefire", "truce", "peace talks", "停火", "和談")
    if any(word in text for word in easing_words):
        return {"status": "improving", "impact": "mixed", "summary": "地緣風險若緩和，可能降低繞航支撐，但也改善整體風險偏好。"}
    if any(word in text for word in high_words):
        return {"status": "elevated", "impact": "freight_supportive", "summary": "地緣事件仍偏緊，可能支撐繞航與運價，但也提高油價和總體風險。"}
    return {"status": "neutral", "impact": "neutral", "summary": "目前未偵測到明確升溫或降溫的戰爭事件訊號。"}


def _classify_oil(oil_prices: dict[str, Any]) -> dict[str, Any]:
    values = [row for row in oil_prices.values() if row]
    if not values:
        return {"status": "unknown", "impact": "unknown", "summary": "油價資料無法取得，燃油成本影響需保留。"}
    changes = [row.get("change_5d_pct") for row in values if row.get("change_5d_pct") is not None]
    if not changes:
        return {"status": "known_price_only", "impact": "watch", "summary": "已取得油價價格，但近 5 日變化不足，燃油成本影響需觀察。"}
    avg_5d = sum(changes) / len(changes)
    if avg_5d >= 5:
        return {"status": "rising", "impact": "cost_pressure", "summary": "油價近 5 日上升，對航運燃油成本偏負面。"}
    if avg_5d <= -5:
        return {"status": "falling", "impact": "cost_relief", "summary": "油價近 5 日下降，對燃油成本壓力有緩和效果。"}
    return {"status": "stable", "impact": "neutral", "summary": "油價近 5 日變化相對溫和，對航運成本影響中性。"}


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
    return f"國際事件風險為{risk_text}。{policy.get('summary')} {war.get('summary')} {oil.get('summary')}"


def _confidence(oil_prices: dict[str, Any], events: list[dict[str, Any]], sources: list[dict[str, Any]]) -> float:
    score = 0.18
    score += 0.16 if oil_prices.get("wti") else 0
    score += 0.16 if oil_prices.get("brent") else 0
    score += min(len(events), 8) * 0.045
    score += min(len(sources), 8) * 0.025
    return min(score, 0.86)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_change(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous in (None, 0):
        return None
    return (latest - previous) / previous * 100


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    cutoff = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=7)
    for event in events:
        url = event.get("url") or event.get("title") or ""
        if not url or url in seen:
            continue
        published = event.get("published_at") or ""
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if dt < cutoff:
                    continue
            except Exception:
                pass
        seen.add(url)
        output.append(event)
    return output


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()
    for source in sources:
        key = (source.get("name"), source.get("url"), source.get("as_of") or source.get("data_as_of"), source.get("method"))
        if key in seen:
            continue
        seen.add(key)
        output.append(source)
    return output


def _dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return output
