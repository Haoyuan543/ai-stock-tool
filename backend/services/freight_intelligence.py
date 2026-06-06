from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


ROUTES = ("us_west", "us_east", "europe")
UP_WORDS = (
    "上漲",
    "走揚",
    "調漲",
    "揚升",
    "連漲",
    "大漲",
    "higher",
    "rise",
    "rising",
    "increase",
    "increased",
    "surge",
    "up",
)
DOWN_WORDS = (
    "下跌",
    "走跌",
    "轉弱",
    "跌破",
    "回落",
    "lower",
    "fall",
    "falling",
    "decrease",
    "decreased",
    "down",
)
FLAT_WORDS = ("持平", "高檔震盪", "穩定", "stable", "flat", "unchanged")
ROUTE_WORDS = {
    "us_west": ("美西", "us west", "west coast", "fbx01"),
    "us_east": ("美東", "us east", "east coast", "fbx03"),
    "europe": ("歐洲", "北歐", "europe", "north europe", "mediterranean", "fbx11"),
}


def build_freight_intelligence(freight: dict[str, Any], news: dict[str, Any] | None = None) -> dict[str, Any]:
    signals = _collect_signals(freight, news or {})
    overall = _resolve_trend(signals)
    route_data = {route: _route_intelligence(route, freight, signals, overall["trend"]) for route in ROUTES}
    independent_sources = _independent_sources(signals)
    confidence = _confidence(overall["trend"], signals, route_data, independent_sources)
    strength = _strength(freight, overall["trend"], confidence, signals)
    weeks = _weeks_up_or_down(freight, signals)
    exact_route_count = sum(1 for route in ROUTES if _exact_route_available(freight, route))

    if overall["trend"] == "unknown":
        status = "missing"
    elif exact_route_count:
        status = "partial_exact"
    elif confidence >= 0.75 and len(independent_sources) >= 3:
        status = "inferred_from_multiple_independent_sources"
    elif confidence >= 0.55:
        status = "inferred"
    else:
        status = "low_confidence_inferred"

    return {
        "overall_trend": overall["trend"],
        "strength": strength,
        "weeks_up_or_down": weeks,
        "confidence": round(confidence, 2),
        "source_count": len(independent_sources),
        "raw_signal_count": len(signals),
        "exact_route_count": exact_route_count,
        "status": status,
        "us_west": route_data["us_west"],
        "us_east": route_data["us_east"],
        "europe": route_data["europe"],
        "summary": _summary(overall["trend"], strength, confidence, len(independent_sources), weeks, exact_route_count),
        "signals": signals[:20],
        "independent_sources": sorted(independent_sources),
    }


def _collect_signals(freight: dict[str, Any], news: dict[str, Any]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    weekly = _safe_float(freight.get("weekly_change"))
    if weekly is not None:
        signals.append(_signal("SCFI", "official/index", _trend_from_number(weekly), 0.9, f"SCFI weekly change {weekly}%", exact=True))
    if freight.get("scfi_streak_weeks") is not None:
        trend = "up" if (weekly or 0) >= 0 else "down"
        signals.append(_signal("SCFI", "official/index", trend, 0.78, f"SCFI streak weeks {freight.get('scfi_streak_weeks')}", exact=True))
    if freight.get("official_chart_parsed") and freight.get("scfi_latest") is not None:
        signals.append(_signal("SSE official chart", "sse.net.cn", "flat", 0.55, f"Official SCFI latest {freight.get('scfi_latest')}", exact=True))

    for route in ROUTES:
        rate = _safe_float(freight.get(route))
        change = _safe_float(freight.get(f"{route}_weekly_change"))
        if rate is not None:
            signals.append(_signal(_route_source(route), route, "flat", 0.62, f"{route} exact rate {rate}", route=route, exact=True))
        if change is not None:
            signals.append(_signal(_route_source(route), route, _trend_from_number(change), 0.84, f"{route} weekly change {change}%", route=route, exact=True))

    extracted = freight.get("search_intelligence") or {}
    scfi = extracted.get("scfi") or {}
    trend = _clean_trend(scfi.get("trend"))
    if trend != "unknown":
        signals.append(_signal("Web search extraction", "search", trend, _safe_float(scfi.get("confidence")) or 0.55, "Search extraction SCFI trend"))
    value = _safe_float(scfi.get("weekly_change"))
    if value is not None:
        signals.append(_signal("Web search extraction", "search", _trend_from_number(value), 0.66, f"Search extraction SCFI weekly change {value}%"))

    for note in (extracted.get("evidence_type") or {}).get("inferred_trend") or []:
        signals.extend(_signals_from_text(note, "Search evidence", "search"))
    for page in freight.get("page_extracts") or []:
        domain = _domain(page.get("url"))
        signals.extend(_signals_from_text(f"{page.get('title') or ''} {page.get('text') or ''}", "Playwright DOM/Network", domain))
    for article in (news.get("articles") or [])[:40]:
        source = article.get("source") or _domain(article.get("url")) or "News"
        signals.extend(_signals_from_text(f"{article.get('title') or ''} {article.get('description') or ''}", source, _domain(article.get("url")) or source))
    return _dedupe_signals(signals)


def _signals_from_text(text: str, source: str, source_key: str) -> list[dict[str, Any]]:
    clean = (text or "").lower()
    if not clean.strip():
        return []
    route = _route_from_text(clean)
    signals: list[dict[str, Any]] = []
    if any(token in clean for token in ("主要航線", "多數航線", "all major routes", "multiple route rates rising")):
        if any(word in clean for word in UP_WORDS):
            for route_name in ROUTES:
                signals.append(_signal(source, source_key, "up", 0.58, "Text indicates major routes rising", route=route_name))
            return signals
    if any(word in clean for word in UP_WORDS):
        signals.append(_signal(source, source_key, "up", 0.52, text[:180], route=route))
    elif any(word in clean for word in DOWN_WORDS):
        signals.append(_signal(source, source_key, "down", 0.52, text[:180], route=route))
    elif any(word in clean for word in FLAT_WORDS):
        signals.append(_signal(source, source_key, "flat", 0.45, text[:180], route=route))
    pct = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", clean)
    if pct and any(token in clean for token in ("scfi", "freight", "運價", "wci")):
        value = _safe_float(pct.group(1))
        if value is not None:
            signals.append(_signal(source, source_key, _trend_from_number(value), 0.6, f"Text mentions freight percentage {value}%", route=route))
    if any(token in clean for token in ("連三漲", "連續三週", "three consecutive", "3 weeks")):
        signals.append(_signal(source, source_key, "up", 0.62, "Text mentions three consecutive increases", route=route))
    return signals


def _resolve_trend(signals: list[dict[str, Any]]) -> dict[str, Any]:
    weights = {"up": 0.0, "down": 0.0, "flat": 0.0}
    for signal in signals:
        trend = signal.get("trend")
        if trend in weights:
            weights[trend] += float(signal.get("weight") or 0.0)
    if not any(weights.values()):
        return {"trend": "unknown", "weights": weights}
    trend = max(weights, key=weights.get)
    if weights[trend] < 0.4:
        trend = "unknown"
    return {"trend": trend, "weights": weights}


def _route_intelligence(route: str, freight: dict[str, Any], signals: list[dict[str, Any]], overall_trend: str) -> dict[str, Any]:
    change = _safe_float(freight.get(f"{route}_weekly_change"))
    rate = _safe_float(freight.get(route))
    route_signals = [signal for signal in signals if signal.get("route") == route]
    if change is not None:
        return {"trend": _trend_from_number(change), "confidence": 0.85, "status": "exact_change"}
    if rate is not None:
        return {"trend": "flat", "confidence": 0.62, "status": "exact_rate_only"}
    resolved = _resolve_trend(route_signals)
    sources = _independent_sources(route_signals)
    confidence = min(0.68, 0.3 + 0.12 * len(sources))
    if resolved["trend"] == "unknown" and overall_trend in {"up", "down", "flat"}:
        return {"trend": overall_trend, "confidence": 0.42, "status": "inferred_from_scfi_composite"}
    return {
        "trend": resolved["trend"],
        "confidence": round(confidence if resolved["trend"] != "unknown" else 0.0, 2),
        "status": "inferred" if resolved["trend"] != "unknown" else "missing",
    }


def _confidence(overall_trend: str, signals: list[dict[str, Any]], route_data: dict[str, dict[str, Any]], independent_sources: set[str]) -> float:
    if overall_trend == "unknown":
        return 0.0
    agreeing = {
        signal.get("source_key") or signal.get("source")
        for signal in signals
        if signal.get("trend") == overall_trend and signal.get("source")
    }
    exact_bonus = 0.1 if any(signal.get("exact") and signal.get("trend") in {overall_trend, "flat"} for signal in signals) else 0.0
    route_bonus = 0.03 * sum(1 for item in route_data.values() if item.get("trend") == overall_trend)
    if len(agreeing) >= 3:
        base = 0.72
    elif len(agreeing) == 2:
        base = 0.58
    elif len(agreeing) == 1:
        base = 0.42
    else:
        base = 0.35
    if not any(signal.get("exact") for signal in signals):
        base = min(base, 0.65)
    return min(0.9, base + exact_bonus + route_bonus)


def _strength(freight: dict[str, Any], trend: str, confidence: float, signals: list[dict[str, Any]]) -> str:
    weekly = _safe_float(freight.get("weekly_change"))
    weeks = _weeks_up_or_down(freight, signals)
    if trend == "unknown" or confidence < 0.45:
        return "weak"
    if weekly is not None and abs(weekly) >= 7:
        return "strong"
    if weeks and weeks >= 3 and confidence >= 0.68:
        return "strong"
    if confidence >= 0.6:
        return "moderate"
    return "weak"


def _weeks_up_or_down(freight: dict[str, Any], signals: list[dict[str, Any]]) -> int | None:
    value = _safe_float(freight.get("scfi_streak_weeks"))
    if value is not None:
        return int(value)
    extracted = freight.get("search_intelligence") or {}
    value = _safe_float((extracted.get("scfi") or {}).get("weeks_up_or_down"))
    if value is not None:
        return int(value)
    for signal in signals:
        note = str(signal.get("note") or "").lower()
        if "three consecutive" in note or "連三漲" in note or "連續三週" in note:
            return 3
    return None


def _summary(trend: str, strength: str, confidence: float, source_count: int, weeks: int | None, exact_route_count: int) -> str:
    if trend == "unknown":
        return "目前無法可靠判斷運價方向，需要更多 freight/news sources。"
    direction = {"up": "上升", "down": "下降", "flat": "持平"}.get(trend, trend)
    exact = f"細分航線精確資料 {exact_route_count} 筆" if exact_route_count else "沒有細分航線精確資料，改用多來源趨勢推論"
    streak = f"，連續週數 {weeks}" if weeks else ""
    return f"運價方向偏{direction}，強度 {strength}，信心 {confidence:.2f}，獨立來源 {source_count} 個{streak}；{exact}。"


def _signal(source: str, source_key: str, trend: str, weight: float, note: str, route: str | None = None, exact: bool = False) -> dict[str, Any]:
    return {
        "source": source,
        "source_key": source_key or source,
        "trend": _clean_trend(trend),
        "weight": round(float(weight), 2),
        "note": note,
        "route": route or "",
        "exact": exact,
    }


def _trend_from_number(value: float) -> str:
    if value > 0.1:
        return "up"
    if value < -0.1:
        return "down"
    return "flat"


def _clean_trend(value: Any) -> str:
    value = str(value or "").lower()
    return value if value in {"up", "down", "flat"} else "unknown"


def _safe_float(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _route_source(route: str) -> str:
    return {"us_west": "US West route", "us_east": "US East route", "europe": "Europe route"}.get(route, route)


def _route_from_text(text: str) -> str | None:
    for route, words in ROUTE_WORDS.items():
        if any(token in text for token in words):
            return route
    return None


def _domain(url: Any) -> str:
    try:
        return urlparse(str(url)).netloc.lower() or "unknown"
    except Exception:
        return "unknown"


def _independent_sources(signals: list[dict[str, Any]]) -> set[str]:
    return {str(signal.get("source_key") or signal.get("source")) for signal in signals if signal.get("source")}


def _exact_route_available(freight: dict[str, Any], route: str) -> bool:
    return _safe_float(freight.get(route)) is not None and _safe_float(freight.get(f"{route}_weekly_change")) is not None


def _dedupe_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for signal in signals:
        key = (signal.get("source_key"), signal.get("trend"), signal.get("route"), signal.get("note"))
        if key in seen or signal.get("trend") == "unknown":
            continue
        seen.add(key)
        output.append(signal)
    return output
