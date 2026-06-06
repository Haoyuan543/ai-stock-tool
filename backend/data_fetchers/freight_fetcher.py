from __future__ import annotations

import base64
import csv
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.config import get_settings
from backend.search.ai_extractor import extract_market_intelligence, merge_extractions
from backend.search.page_extractor import extract_pages_with_browser
from backend.search.search_queries import freight_queries
from backend.search.screenshot_analyzer import analyze_search_result_screenshots
from backend.search.web_search import web_search
from backend.services.freight_cache import apply_last_successful_freight_cache


ROOT = Path(__file__).resolve().parents[2]
SCFI_CSV = ROOT / "data" / "scfi_routes.csv"
SSE_SCFI_PAGE = "https://en.sse.net.cn/indices/scfinew.jsp"
SSE_SCFI_CHART = "https://www.sse.net.cn/index/indexImg?name=scfi&type=english"


class FreightFetcher:
    def fetch_scfi(self) -> dict[str, Any]:
        rows = _load_scfi_csv()
        return rows[-1] if rows else {}

    def fetch_route_rates(self) -> list[dict[str, Any]]:
        return _load_scfi_csv()


def fetch_freight_data(symbol: str, manual: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    source = {"name": "Shanghai Shipping Exchange SCFI", "url": "https://www.sse.net.cn/indexIntro?indexName=scfi"}
    chart_source = {"name": "SSE SCFI latest chart image", "url": SSE_SCFI_CHART}
    csv_source = {"name": "Local SCFI route CSV", "url": str(SCFI_CSV)}
    manual_source = {"name": "Manual freight supplement", "url": "frontend advanced freight fields"}
    sources = [source]
    missing: list[str] = []
    page_available = False

    try:
        response = httpx.get(source["url"], timeout=settings.request_timeout)
        response.raise_for_status()
        page_available = True
    except Exception as exc:
        missing.append(f"Data Missing: SCFI public page fetch failed: {exc}")

    rows = FreightFetcher().fetch_route_rates()
    latest = rows[-1] if rows else {}
    data = _empty_data(page_available)
    if latest:
        sources.append(csv_source)
        data.update(_row_to_data(latest))
        data["history"] = rows[-26:]
        data["note"] = "SCFI route data was loaded from local CSV. Empty cells are not guessed."
        _apply_csv_freshness_guard(data, missing)
    if data.get("scfi_latest") is None:
        official = _fetch_official_scfi_latest(settings)
        if official.get("scfi_latest") is not None:
            sources.append(chart_source)
            data.update({key: value for key, value in official.items() if value is not None})
            data["note"] = "SCFI composite latest value was parsed from the official SSE chart image. Route-level values are not included in the public chart."
        elif official.get("missing"):
            missing.extend(official["missing"])
    if manual:
        cleaned = _manual_to_data(manual)
        if any(value is not None for value in cleaned.values()):
            sources.append(manual_source)
            data.update({key: value for key, value in cleaned.items() if value is not None})
            data["note"] = "Manual freight supplement was used for missing route data."
    if _needs_search_fallback(data):
        search = _freight_search_fallback(symbol)
        sources.extend(search.get("sources", []))
        missing.extend(search.get("missing", []))
        data["search_intelligence"] = search.get("extracted")
        data["page_extracts"] = search.get("page_extracts", [])
        data["search_screenshots"] = search.get("screenshots", [])
        extracted = search.get("extracted") or {}
        route_rates = extracted.get("route_rates") or {}
        if data.get("us_west") is None:
            data["us_west"] = _safe_float(route_rates.get("us_west"))
        if data.get("us_east") is None:
            data["us_east"] = _safe_float(route_rates.get("us_east"))
        if data.get("europe") is None:
            data["europe"] = _safe_float(route_rates.get("europe"))
        scfi = extracted.get("scfi") or {}
        if data.get("scfi_latest") is None:
            data["scfi_latest"] = _safe_float(scfi.get("latest_value"))
        if data.get("weekly_change") is None:
            data["weekly_change"] = _safe_float(scfi.get("weekly_change"))
        if data.get("scfi_streak_weeks") is None:
            data["scfi_streak_weeks"] = _safe_float(scfi.get("weeks_up_or_down"))
        route_weekly_change = extracted.get("route_weekly_change") or {}
        if data.get("us_west_weekly_change") is None:
            data["us_west_weekly_change"] = _safe_float(route_weekly_change.get("us_west"))
        if data.get("us_east_weekly_change") is None:
            data["us_east_weekly_change"] = _safe_float(route_weekly_change.get("us_east"))
        if data.get("europe_weekly_change") is None:
            data["europe_weekly_change"] = _safe_float(route_weekly_change.get("europe"))
        if data.get("red_sea_status") is None:
            red_sea = extracted.get("red_sea") or {}
            data["red_sea_status"] = red_sea.get("status") if red_sea.get("status") != "unknown" else None
        if extracted:
            data["note"] = data.get("note", "") + " Web search intelligence was used for inferred context; exact values remain Data Missing unless explicitly extracted."
        _downgrade_weak_search_route_exactness(data, missing)

    if _route_or_scfi_missing(data) or data.get("weekly_change") is None:
        data = apply_last_successful_freight_cache(symbol, data, missing)

    if data.get("scfi_latest") is None:
        missing.append("Data Missing: SCFI latest value unavailable. Use data/scfi_routes.csv or manual freight supplement.")
    if data.get("us_west") is None or data.get("us_east") is None or data.get("europe") is None:
        missing.append("Data Missing: US West / US East / Europe route freight rates unavailable.")
    if not rows and not manual and _route_or_scfi_missing(data):
        missing.append("Data Missing: data/scfi_routes.csv not found or empty.")

    if not missing:
        status = "ok"
    elif data.get("search_intelligence"):
        status = "inferred_from_search"
    else:
        status = "partial" if page_available or rows or manual or data.get("scfi_latest") is not None else "missing"
    return {"status": status, "data": data, "sources": sources, "missing": missing}


def _needs_search_fallback(data: dict[str, Any]) -> bool:
    return data.get("us_west") is None or data.get("us_east") is None or data.get("europe") is None or data.get("red_sea_status") is None


def _route_or_scfi_missing(data: dict[str, Any]) -> bool:
    return data.get("scfi_latest") is None or data.get("us_west") is None or data.get("us_east") is None or data.get("europe") is None


def _route_value_count(data: dict[str, Any]) -> int:
    return sum(1 for route in ("us_west", "us_east", "europe") if data.get(route) is not None)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _csv_max_age_days() -> int:
    try:
        return int(_env("SCFI_CSV_MAX_AGE_DAYS", "7") or "7")
    except ValueError:
        return 7


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _age_days(value: Any) -> int | None:
    parsed = _parse_date(value)
    if not parsed:
        return None
    return max(0, (datetime.now(timezone.utc).date() - parsed).days)


def _clear_csv_exact_fields(data: dict[str, Any]) -> None:
    for field in (
        "scfi_latest",
        "weekly_change",
        "scfi_streak_weeks",
        "us_west",
        "us_west_weekly_change",
        "us_east",
        "us_east_weekly_change",
        "europe",
        "europe_weekly_change",
        "mediterranean",
        "asia_regional",
    ):
        data[field] = None


def _apply_csv_freshness_guard(data: dict[str, Any], missing: list[str]) -> None:
    age = _age_days(data.get("latest_date"))
    max_age = _csv_max_age_days()
    data["csv_data_date"] = data.get("latest_date")
    data["csv_age_days"] = age
    data["csv_max_age_days"] = max_age
    data["csv_stale"] = age is None or age > max_age
    data["csv_exact_used"] = not data["csv_stale"]
    if not data["csv_stale"]:
        return
    stale_date = data.get("latest_date") or "日期不明"
    _clear_csv_exact_fields(data)
    data["csv_exact_used"] = False
    data["note"] = (
        data.get("note", "")
        + f" CSV route data date {stale_date} is stale or invalid, so it was not used as exact current freight data."
    )
    missing.append(
        f"Data Limitation: data/scfi_routes.csv 日期 {stale_date} 已超過可用期限或無法判讀，未列為本次精確航線資料。"
    )


def _downgrade_weak_search_route_exactness(data: dict[str, Any], missing: list[str]) -> None:
    """Avoid treating a single search-extracted route number as reliable exact data.

    Public snippets often contain rounded boundaries, target ranges, or unrelated
    freight examples. If search fallback finds only one main route, keep the
    freight trend/intelligence but remove that route from exact-data fields so
    the report does not overstate route coverage.
    """

    route_count = _route_value_count(data)
    if route_count != 1:
        return
    route = next((name for name in ("us_west", "us_east", "europe") if data.get(name) is not None), "")
    value = _safe_float(data.get(route))
    suspicious_boundary = value is not None and value >= 5900
    source_note = "search_single_route"
    if suspicious_boundary:
        source_note = "search_single_route_boundary_value"
    data.setdefault("route_quality_warnings", []).append(
        {
            "route": route,
            "value": value,
            "reason": source_note,
            "message": "搜尋 fallback 只取得單一航線數字，可信度不足；已降級為趨勢推論，不列為精確航線資料。",
        }
    )
    data[route] = None
    data[f"{route}_weekly_change"] = None
    missing.append(
        "Data Limitation: 搜尋 fallback 只取得單一航線數字，已降級為趨勢推論；需三大航線交叉確認或快取補齊。"
    )


def _freight_search_fallback(symbol: str) -> dict[str, Any]:
    search = web_search(freight_queries(symbol), max_results_per_query=5)
    extracted = extract_market_intelligence(search.get("results", []))
    news_numbers = _extract_scfi_numbers_from_news(search.get("results", []))
    if news_numbers:
        extracted = merge_extractions(extracted, news_numbers)
    page_extract = None
    if _extraction_needs_page_extract(extracted):
        page_extract = extract_pages_with_browser(search.get("results", []), max_pages=3)
        page_numbers = _extract_scfi_numbers_from_pages((page_extract or {}).get("pages", []))
        if page_numbers:
            extracted = merge_extractions(extracted, page_numbers)
        if page_extract.get("extracted"):
            extracted = merge_extractions(extracted, page_extract["extracted"])
    screenshot = None
    if _extraction_needs_screenshots(extracted):
        screenshot = analyze_search_result_screenshots(search.get("results", []), max_pages=3)
        if screenshot.get("extracted"):
            extracted = merge_extractions(extracted, screenshot["extracted"])
    return {
        "results": search.get("results", []),
        "sources": search.get("sources", []),
        "missing": search.get("missing", []) + ((page_extract or {}).get("missing", [])) + ((screenshot or {}).get("missing", [])),
        "page_extracts": (page_extract or {}).get("pages", []),
        "screenshots": (screenshot or {}).get("screenshots", []),
        "extracted": extracted,
    }


def _extract_scfi_numbers_from_news(results: list[dict[str, Any]]) -> dict[str, Any]:
    texts = []
    for row in results[:12]:
        title = row.get("title") or ""
        snippet = row.get("snippet") or ""
        url = row.get("url") or ""
        texts.append(f"{title}\n{snippet}\n{url}")
        page_text = _fetch_public_article_text(url)
        if page_text:
            texts.append(page_text)
    return _extract_scfi_numbers_from_text("\n".join(texts), results)


def _extract_scfi_numbers_from_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(f"{row.get('title') or ''}\n{row.get('text') or ''}\n{row.get('url') or ''}" for row in pages)
    return _extract_scfi_numbers_from_text(text, pages)


def _fetch_public_article_text(url: str) -> str:
    if not url or "news.google.com" in url:
        return ""
    allowed = ("money.udn.com", "udn.com", "nownews.com", "ctee.com.tw", "moneydj.com", "anue")
    if not any(domain in url.lower() for domain in allowed):
        return ""
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=8.0,
        )
        response.raise_for_status()
    except Exception:
        return ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", response.text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:20000]


def _extract_scfi_numbers_from_text(text: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not text:
        return {}
    normalized = _normalize_number_text(text)
    if not any(token.lower() in normalized.lower() for token in ("scfi", "美西", "美東", "欧洲", "歐洲", "us west", "us east")):
        return {}

    scfi_latest = _scfi_latest_value(normalized)
    weekly_change = _scfi_weekly_change(normalized)
    weeks = _first_number(
        normalized,
        [
            r"連續(?:第)?\s*([0-9]+)\s*週(?:上漲|走揚|漲)",
            r"連\s*([0-9]+)\s*漲",
        ],
        min_value=1,
        max_value=20,
    )
    if weeks is None:
        weeks = _chinese_streak_weeks(normalized)

    us_west = _route_rate(normalized, ("美西", "US West", "U.S. West", "West Coast"))
    us_east = _route_rate(normalized, ("美東", "美东", "US East", "U.S. East", "East Coast"))
    europe = _route_rate(normalized, ("歐洲", "欧洲", "Europe", "North Europe"))
    us_west_change = _route_change(normalized, ("美西", "US West", "U.S. West", "West Coast"))
    us_east_change = _route_change(normalized, ("美東", "美东", "US East", "U.S. East", "East Coast"))
    europe_change = _route_change(normalized, ("歐洲", "欧洲", "Europe", "North Europe"))

    exact_notes = []
    if scfi_latest is not None:
        exact_notes.append(f"SCFI latest {scfi_latest}")
    for label, value in (("US West", us_west), ("US East", us_east), ("Europe", europe)):
        if value is not None:
            exact_notes.append(f"{label} route rate {value}")
    for label, value in (("SCFI weekly change", weekly_change), ("US West weekly change", us_west_change), ("US East weekly change", us_east_change), ("Europe weekly change", europe_change)):
        if value is not None:
            exact_notes.append(f"{label} {value}%")
    if not exact_notes:
        return {}

    sources = [row.get("url") for row in rows if isinstance(row, dict) and row.get("url")]
    return {
        "scfi": {
            "latest_value": scfi_latest,
            "weekly_change": weekly_change,
            "trend": "up" if (weekly_change or 0) > 0 else "down" if (weekly_change or 0) < 0 else "unknown",
            "weeks_up_or_down": weeks,
            "confidence": 0.82,
            "sources": sources[:5],
        },
        "route_rates": {"us_west": us_west, "us_east": us_east, "europe": europe, "asia": None},
        "route_weekly_change": {"us_west": us_west_change, "us_east": us_east_change, "europe": europe_change},
        "evidence_type": {"exact_data": exact_notes, "inferred_trend": [], "missing_data": []},
    }


def _normalize_number_text(text: str) -> str:
    full_width = str.maketrans({
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "．": ".",
        "％": "%",
        "，": ",",
    })
    return text.translate(full_width).replace(",", "")


def _first_number(text: str, patterns: list[str], min_value: float, max_value: float) -> float | None:
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = _safe_float(match.group(1))
            if value is not None and min_value <= value <= max_value:
                return value
    return None


def _scfi_latest_value(text: str) -> float | None:
    candidates: list[float] = []
    sentences = re.split(r"[。\n；;]", text)
    for sentence in sentences:
        upper = sentence.upper()
        if "SCFI" not in upper and "上海集裝箱" not in sentence and "上海集装箱" not in sentence and "運價指數" not in sentence and "运价指数" not in sentence:
            continue
        if "http" in sentence.lower() or "/story/" in sentence.lower():
            continue
        for match in re.finditer(r"([0-9]{4}(?:\.[0-9]+)?)\s*(?:點|点)", sentence):
            value = _safe_float(match.group(1))
            if value is not None and 1000 <= value <= 6000:
                candidates.append(value)
        for match in re.finditer(r"(?:至|為|为|報|报|來到|来到)\s*([0-9]{4}(?:\.[0-9]+)?)", sentence):
            window = sentence[max(0, match.start() - 40) : match.end() + 20]
            if "美元" in window or "USD" in window.upper():
                continue
            value = _safe_float(match.group(1))
            if value is not None and 1000 <= value <= 6000:
                candidates.append(value)
    if candidates:
        return max(candidates)
    return _first_number(
        text,
        [
            r"(?:SCFI|上海出口集裝箱運價指數|上海出口集装箱运价指数)[^。\n；;]{0,120}([0-9]{4}(?:\.[0-9]+)?)",
        ],
        min_value=1000,
        max_value=6000,
    )


def _scfi_weekly_change(text: str) -> float | None:
    sentences = re.split(r"[。\n；;]", text)
    for sentence in sentences:
        if "SCFI" not in sentence.upper() and "指數" not in sentence and "指数" not in sentence:
            continue
        value = _first_number(
            sentence,
            [
                r"(?:約|漲幅|週漲|周漲|上漲)?[^0-9+\-]{0,8}([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%",
            ],
            min_value=-50,
            max_value=80,
        )
        if value is not None:
            return value
    return _first_number(
        text,
        [r"(?:SCFI|指數|指数)[^。\n；;]{0,80}(?:週漲|周漲|上漲|漲幅)[^0-9+\-]{0,8}([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%"],
        min_value=-50,
        max_value=80,
    )


def _route_rate(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        patterns = [
            rf"{re.escape(label)}[^。\n]{{0,60}}?(?:運價|線|航線|報價)?[^0-9]{{0,12}}([0-9]{{3,5}}(?:\.[0-9]+)?)\s*(?:美元|USD|美金)?",
            rf"(?:遠東|上海|亞洲)[^。\n]{{0,20}}{re.escape(label)}[^。\n]{{0,80}}?([0-9]{{3,5}}(?:\.[0-9]+)?)",
        ]
        value = _first_number(text, patterns, 500, 12000)
        if value is not None:
            return value
    return None


def _route_change(text: str, labels: tuple[str, ...]) -> float | None:
    candidates: list[tuple[int, float]] = []
    for label in labels:
        patterns = [
            rf"{re.escape(label)}[^。\n]{{0,80}}?(?:週漲|周漲|上漲|漲幅)[^0-9+\-]{{0,8}}([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%",
            rf"{re.escape(label)}[^。\n]{{0,80}}?([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                value = _safe_float(match.group(1))
                if value is None or not -50 <= value <= 80:
                    continue
                precision = len(match.group(1).split(".", 1)[1]) if "." in match.group(1) else 0
                candidates.append((precision, value))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[0][1]


def _chinese_streak_weeks(text: str) -> float | None:
    numerals = {
        "一": 1,
        "二": 2,
        "兩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    match = re.search(r"連(?:續)?([一二兩三四五六七八九十])(?:週)?(?:上漲|走揚|漲|彈)", text)
    if not match:
        return None
    return float(numerals.get(match.group(1), 0) or 0) or None


def _extraction_needs_page_extract(extracted: dict[str, Any]) -> bool:
    return _extraction_needs_screenshots(extracted)


def _extraction_needs_screenshots(extracted: dict[str, Any]) -> bool:
    route_rates = (extracted or {}).get("route_rates") or {}
    return route_rates.get("us_west") is None or route_rates.get("us_east") is None or route_rates.get("europe") is None


def _fetch_official_scfi_latest(settings: Any) -> dict[str, Any]:
    missing: list[str] = []
    try:
        image = httpx.get(
            SSE_SCFI_CHART,
            headers={"Referer": SSE_SCFI_PAGE, "User-Agent": "Mozilla/5.0"},
            timeout=30.0,
        )
        image.raise_for_status()
    except Exception as exc:
        return {"missing": [f"Data Missing: SSE SCFI chart image fetch failed: {exc}"]}

    raw = _parse_scfi_image_with_openai(image.content, settings)
    if raw.get("scfi_latest") is not None:
        return raw

    missing.extend(raw.get("missing", []))
    missing.append("Data Missing: SSE SCFI chart image was fetched but could not be parsed automatically.")
    return {"missing": missing}


def _parse_scfi_image_with_openai(image_bytes: bytes, settings: Any) -> dict[str, Any]:
    if not settings.openai_api_key:
        return {"missing": ["Data Missing: OPENAI_API_KEY is required to OCR the SSE SCFI chart image."]}
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Read the orange label in this Shanghai Containerized Freight Index chart. "
        "Return JSON only with keys latest_date and scfi_latest. "
        "If unreadable, return {\"latest_date\": null, \"scfi_latest\": null}. "
        "Do not infer any route-level rates."
    )
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json={
                "model": settings.openai_model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
                "max_output_tokens": 120,
            },
            timeout=httpx.Timeout(settings.openai_timeout_seconds, connect=20.0, read=settings.openai_timeout_seconds, write=20.0, pool=20.0),
        )
        response.raise_for_status()
        text = _extract_output_text(response.json())
        parsed = _loads_json_object(text)
        return {
            "scfi_latest": _safe_float(parsed.get("scfi_latest")),
            "latest_date": parsed.get("latest_date"),
            "official_chart_parsed": True,
        }
    except Exception as exc:
        return {"missing": [f"Data Missing: OpenAI OCR for SSE SCFI chart failed: {exc}"]}


def _extract_output_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        return json.loads(match.group(0)) if match else {}


def _empty_data(page_available: bool) -> dict[str, Any]:
    return {
        "scfi_public_page_available": page_available,
        "scfi_latest": None,
        "us_west": None,
        "us_east": None,
        "europe": None,
        "mediterranean": None,
        "asia_regional": None,
        "weekly_change": None,
        "monthly_change": None,
        "scfi_streak_weeks": None,
        "us_west_weekly_change": None,
        "us_east_weekly_change": None,
        "europe_weekly_change": None,
        "red_sea_status": None,
        "latest_date": None,
        "history": [],
        "note": "Public SCFI page is recorded as a source. Route-level numeric values are not guessed.",
    }


def _row_to_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scfi_latest": _safe_float(row.get("scfi")),
        "us_west": _safe_float(row.get("us_west")),
        "us_east": _safe_float(row.get("us_east")),
        "europe": _safe_float(row.get("europe")),
        "mediterranean": _safe_float(row.get("mediterranean")),
        "asia_regional": _safe_float(row.get("asia_regional")),
        "weekly_change": _safe_float(row.get("weekly_change")),
        "scfi_streak_weeks": _safe_float(row.get("scfi_streak_weeks")),
        "us_west_weekly_change": _safe_float(row.get("us_west_weekly_change")),
        "us_east_weekly_change": _safe_float(row.get("us_east_weekly_change")),
        "europe_weekly_change": _safe_float(row.get("europe_weekly_change")),
        "monthly_change": _safe_float(row.get("monthly_change")),
        "latest_date": row.get("date"),
        "verified_route_source": row.get("source") or "data/scfi_routes.csv",
    }


def _manual_to_data(manual: dict[str, Any]) -> dict[str, Any]:
    return {
        "scfi_latest": _safe_float(manual.get("scfi_latest")),
        "us_west": _safe_float(manual.get("us_west")),
        "us_east": _safe_float(manual.get("us_east")),
        "europe": _safe_float(manual.get("europe")),
        "mediterranean": _safe_float(manual.get("mediterranean")),
        "asia_regional": _safe_float(manual.get("asia_regional")),
        "weekly_change": _safe_float(manual.get("scfi_weekly_change")),
        "scfi_streak_weeks": _safe_float(manual.get("scfi_streak_weeks")),
        "us_west_weekly_change": _safe_float(manual.get("us_west_weekly_change")),
        "us_east_weekly_change": _safe_float(manual.get("us_east_weekly_change")),
        "europe_weekly_change": _safe_float(manual.get("europe_weekly_change")),
        "red_sea_status": manual.get("red_sea_status") or None,
    }


def _load_scfi_csv() -> list[dict[str, Any]]:
    if not SCFI_CSV.exists():
        return []
    try:
        with SCFI_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.DictReader(handle) if row.get("date")]
    except Exception:
        return []
    return sorted(rows, key=lambda item: item.get("date") or "")


def _safe_float(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
