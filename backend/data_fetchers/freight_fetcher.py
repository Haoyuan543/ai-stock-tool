from __future__ import annotations

import base64
import csv
import html
import json
import os
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
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
from backend.services.freight_route_store import latest_route_row_from_supabase, upsert_route_row_to_supabase


ROOT = Path(__file__).resolve().parents[2]
SCFI_CSV = ROOT / "data" / "scfi_routes.csv"
SSE_SCFI_PAGE = "https://en.sse.net.cn/indices/scfinew.jsp"
SSE_SCFI_CHART = "https://www.sse.net.cn/index/indexImg?name=scfi&type=english"
SSE_SCFI_SINGLE_INDEX = "https://www.sse.net.cn/index/singleIndex?indexType=scfi"


class FreightFetcher:
    def fetch_scfi(self) -> dict[str, Any]:
        rows = _load_scfi_csv()
        return rows[-1] if rows else {}

    def fetch_route_rates(self) -> list[dict[str, Any]]:
        return _load_scfi_csv()


def fetch_freight_data(symbol: str, manual: dict[str, Any] | None = None, allow_ai_extraction: bool = True) -> dict[str, Any]:
    settings = get_settings()
    source = {"name": "Shanghai Shipping Exchange SCFI", "url": "https://www.sse.net.cn/indexIntro?indexName=scfi"}
    single_index_source = {"name": "SSE SCFI single index official table", "url": SSE_SCFI_SINGLE_INDEX}
    chart_source = {"name": "SSE SCFI latest chart image", "url": SSE_SCFI_CHART}
    csv_source = {"name": "Local SCFI route CSV", "url": str(SCFI_CSV)}
    supabase_route_source = {"name": "Supabase freight_routes", "url": "Supabase table: freight_routes"}
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
    supabase_latest = latest_route_row_from_supabase()
    if supabase_latest:
        rows = _merge_route_rows(rows, supabase_latest)
    latest = rows[-1] if rows else {}
    data = _empty_data(page_available)
    single_index = _fetch_sse_single_index(settings)
    if single_index.get("data"):
        sources.append(single_index_source)
        data.update({key: value for key, value in single_index["data"].items() if value is not None})
        page_available = True
        data["note"] = "SSE official single-index table was parsed first. Route fields are used only when the official table contains exact numbers."
    elif single_index.get("missing"):
        missing.extend(single_index["missing"])
    if latest:
        if latest.get("_storage_source") == "supabase_freight_routes":
            sources.append(supabase_route_source)
            data["supabase_route_used"] = True
            data["supabase_route_date"] = latest.get("date")
            data["route_storage_source"] = "supabase"
            data["route_storage_label"] = "Supabase 雲端航線資料庫"
        else:
            sources.append(csv_source)
            data["route_storage_source"] = "repo_csv"
            data["route_storage_label"] = "Repo 內建 CSV 航線資料"
        csv_data = _row_to_data(latest)
        csv_row_date = csv_data.get("latest_date")
        if data.get("latest_date") and csv_data.get("latest_date"):
            csv_data["official_latest_date"] = data.get("latest_date")
        csv_filled_fields: list[str] = []
        for key, value in csv_data.items():
            if value is not None and data.get(key) is None:
                data[key] = value
                csv_filled_fields.append(key)
        data["csv_filled_fields"] = csv_filled_fields
        data["csv_row_date"] = csv_row_date
        if csv_data.get("verified_route_source") and not data.get("verified_route_source"):
            data["verified_route_source"] = csv_data.get("verified_route_source")
        for field in ("history", "note"):
            data[field] = data.get(field) or csv_data.get(field)
        data["history"] = rows[-26:]
        if latest.get("_storage_source") == "supabase_freight_routes":
            data["note"] = (data.get("note", "") + " SCFI route data was supplemented from Supabase freight_routes when fresher than local CSV. Empty cells are not guessed.").strip()
        else:
            data["note"] = (data.get("note", "") + " SCFI route data may be supplemented from local CSV when still fresh. Empty cells are not guessed.").strip()
        _apply_csv_freshness_guard(data, missing)
        if latest.get("_storage_source") != "supabase_freight_routes":
            _sync_route_row_to_supabase_if_current(latest, data)
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
        search = _freight_search_fallback(symbol, allow_ai_extraction=allow_ai_extraction)
        sources.extend(search.get("sources", []))
        missing.extend(search.get("missing", []))
        data["page_extracts"] = search.get("page_extracts", [])
        data["search_screenshots"] = search.get("screenshots", [])
        extracted = _sanitize_search_intelligence(search.get("extracted") or {}, data)
        data["search_intelligence"] = extracted
        csv_update = _maybe_update_scfi_csv_from_search(extracted, data, latest)
        if csv_update.get("updated"):
            data["csv_auto_updated"] = True
            data["csv_update_note"] = csv_update.get("note")
            data["csv_data_date"] = csv_update.get("date")
            data["csv_row_date"] = csv_update.get("date")
            data["verified_route_source"] = csv_update.get("source")
            data["csv_exact_used"] = True
            data["csv_stale"] = False
            data["csv_filled_fields"] = [
                "scfi_latest",
                "weekly_change",
                "scfi_streak_weeks",
                "us_west",
                "us_west_weekly_change",
                "us_east",
                "us_east_weekly_change",
                "europe",
                "europe_weekly_change",
            ]
        elif csv_update.get("note"):
            data["csv_update_note"] = csv_update.get("note")
        route_rates = extracted.get("route_rates") or {}
        _assign_search_value(data, "us_west", route_rates.get("us_west"))
        _assign_search_value(data, "us_east", route_rates.get("us_east"))
        _assign_search_value(data, "europe", route_rates.get("europe"))
        scfi = extracted.get("scfi") or {}
        _assign_search_value(data, "scfi_latest", scfi.get("latest_value"))
        _assign_search_value(data, "weekly_change", scfi.get("weekly_change"))
        _assign_search_value(data, "scfi_streak_weeks", scfi.get("weeks_up_or_down"))
        route_weekly_change = extracted.get("route_weekly_change") or {}
        _assign_search_value(data, "us_west_weekly_change", route_weekly_change.get("us_west"))
        _assign_search_value(data, "us_east_weekly_change", route_weekly_change.get("us_east"))
        _assign_search_value(data, "europe_weekly_change", route_weekly_change.get("europe"))
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


def _row_route_value_count(row: dict[str, Any]) -> int:
    return sum(1 for route in ("us_west", "us_east", "europe") if _safe_float(row.get(route)) is not None)


def _merge_route_rows(local_rows: list[dict[str, Any]], cloud_row: dict[str, Any]) -> list[dict[str, Any]]:
    if not cloud_row or not cloud_row.get("date"):
        return local_rows
    rows = [dict(row) for row in local_rows]
    cloud_date = cloud_row.get("date")
    same_day = [row for row in rows if row.get("date") == cloud_date]
    rows = [row for row in rows if row.get("date") != cloud_date]
    if same_day:
        best_same_day = sorted(
            same_day + [cloud_row],
            key=lambda row: (_row_route_value_count(row), 1 if row.get("_storage_source") == "supabase_freight_routes" else 0),
            reverse=True,
        )[0]
        rows.append(best_same_day)
    else:
        rows.append(cloud_row)
    return sorted(rows, key=lambda item: item.get("date") or "")


def _sync_route_row_to_supabase_if_current(row: dict[str, Any], data: dict[str, Any]) -> None:
    row_date = row.get("date")
    age = _age_days(row_date)
    if age is None or age > _csv_max_age_days():
        return
    result = upsert_route_row_to_supabase(row)
    if result.get("written"):
        data["supabase_route_synced"] = True
        data["supabase_route_date"] = row_date
        sync_note = f"已將有效期限內的航線資料同步到 Supabase freight_routes，資料日期 {row_date}。"
        data["csv_update_note"] = f"{data.get('csv_update_note')} {sync_note}".strip() if data.get("csv_update_note") else sync_note


def _assign_search_value(data: dict[str, Any], key: str, raw_value: Any) -> None:
    value = _safe_float(raw_value)
    if value is None:
        return
    if data.get(key) is None or data.get("csv_auto_updated"):
        data[key] = value


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
        pass
    try:
        return parsedate_to_datetime(text).date()
    except Exception:
        return None


def _age_days(value: Any) -> int | None:
    parsed = _parse_date(value)
    if not parsed:
        return None
    return max(0, (datetime.now(timezone.utc).date() - parsed).days)


def _date_after(left: Any, right: Any) -> bool:
    left_date = _parse_date(left)
    right_date = _parse_date(right)
    return bool(left_date and right_date and left_date > right_date)


def _same_date(left: Any, right: Any) -> bool:
    left_date = _parse_date(left)
    right_date = _parse_date(right)
    return bool(left_date and right_date and left_date == right_date)


def _clear_csv_exact_fields(data: dict[str, Any], fields: list[str] | None = None, keep_official_scfi: bool = False) -> None:
    if fields is not None:
        for field in fields:
            if keep_official_scfi and field in {"scfi_latest", "weekly_change", "scfi_streak_weeks"}:
                continue
            data[field] = None
        return
    for field in (
        "scfi_latest",
        "weekly_change",
        "scfi_streak_weeks",
        "mediterranean",
        "asia_regional",
    ):
        if keep_official_scfi and field in {"scfi_latest", "weekly_change", "scfi_streak_weeks"}:
            continue
        data[field] = None
    for field in (
        "us_west",
        "us_west_weekly_change",
        "us_east",
        "us_east_weekly_change",
        "europe",
        "europe_weekly_change",
    ):
        data[field] = None


def _apply_csv_freshness_guard(data: dict[str, Any], missing: list[str]) -> None:
    csv_date = data.get("csv_row_date") or data.get("latest_date")
    age = _age_days(csv_date)
    max_age = _csv_max_age_days()
    data["csv_data_date"] = csv_date
    official_latest_date = data.get("official_latest_date")
    data["csv_age_days"] = age
    data["csv_max_age_days"] = max_age
    data["csv_stale"] = age is None or age > max_age or _date_after(official_latest_date, csv_date)
    csv_filled_fields = data.get("csv_filled_fields") or []
    data["csv_exact_used"] = (not data["csv_stale"]) and any(field in csv_filled_fields for field in {
        "us_west",
        "us_west_weekly_change",
        "us_east",
        "us_east_weekly_change",
        "europe",
        "europe_weekly_change",
    })
    if not data["csv_stale"]:
        return
    stale_date = csv_date or "日期不明"
    _clear_csv_exact_fields(data, fields=list(csv_filled_fields), keep_official_scfi=bool(official_latest_date))
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


def _maybe_update_scfi_csv_from_search(
    extracted: dict[str, Any],
    current_data: dict[str, Any],
    latest_csv_row: dict[str, Any] | None,
) -> dict[str, Any]:
    if not extracted:
        return {"updated": False, "note": ""}
    if _env("SCFI_AUTO_UPDATE_CSV", "true").lower() not in {"1", "true", "yes", "on"}:
        return {"updated": False, "note": "SCFI CSV auto-update disabled by SCFI_AUTO_UPDATE_CSV."}

    row = _csv_row_from_search_extraction(extracted)
    if not row:
        return {"updated": False, "note": "搜尋資料未取得三條主要航線完整數字，不更新 CSV。"}

    search_date = row.get("date")
    csv_date = (latest_csv_row or {}).get("date") or current_data.get("csv_row_date")
    if not search_date:
        return {"updated": False, "note": "搜尋資料沒有可驗證資料日期，不更新 CSV。"}
    if csv_date and not (_date_after(search_date, csv_date) or _same_date(search_date, csv_date)):
        return {"updated": False, "note": f"搜尋資料日期 {search_date} 未比 CSV 日期 {csv_date} 新，不更新 CSV。"}
    if _same_date(search_date, csv_date) and latest_csv_row and not _csv_row_materially_changed(row, latest_csv_row):
        return {"updated": False, "note": f"搜尋資料日期 {search_date} 與 CSV 相同且數值一致，沿用 CSV。"}

    if not _write_scfi_csv_row(row):
        return {"updated": False, "note": "搜尋資料符合更新條件，但寫入 data/scfi_routes.csv 失敗。"}

    source = row.get("source") or "auto_updated_from_public_news"
    action = "覆寫" if _same_date(search_date, csv_date) else "新增"
    return {
        "updated": True,
        "date": search_date,
        "source": source,
        "note": f"{action} data/scfi_routes.csv：使用公開新聞/搜尋抽取的較新航線資料，資料日 {search_date}。",
    }


def _csv_row_from_search_extraction(extracted: dict[str, Any]) -> dict[str, str] | None:
    route_rates = extracted.get("route_rates") or {}
    route_weekly = extracted.get("route_weekly_change") or {}
    scfi = extracted.get("scfi") or {}
    required = [
        route_rates.get("us_west"),
        route_rates.get("us_east"),
        route_rates.get("europe"),
        route_weekly.get("us_west"),
        route_weekly.get("us_east"),
        route_weekly.get("europe"),
    ]
    if any(_safe_float(value) is None for value in required):
        return None
    data_date = _extracted_data_date(extracted)
    if not data_date:
        return None
    source = _extracted_source_label(extracted)
    return {
        "date": data_date,
        "scfi": _csv_number(scfi.get("latest_value")),
        "weekly_change": _csv_number(scfi.get("weekly_change")),
        "scfi_streak_weeks": _csv_number(scfi.get("weeks_up_or_down"), decimals=0),
        "us_west": _csv_number(route_rates.get("us_west")),
        "us_west_weekly_change": _csv_number(route_weekly.get("us_west")),
        "us_east": _csv_number(route_rates.get("us_east")),
        "us_east_weekly_change": _csv_number(route_weekly.get("us_east")),
        "europe": _csv_number(route_rates.get("europe")),
        "europe_weekly_change": _csv_number(route_weekly.get("europe")),
        "mediterranean": "",
        "asia_regional": "",
        "monthly_change": "",
        "source": source,
    }


def _extracted_data_date(extracted: dict[str, Any]) -> str:
    for key in ("data_date", "latest_date", "as_of"):
        parsed = _parse_date(extracted.get(key))
        if parsed:
            return parsed.isoformat()
    scfi = extracted.get("scfi") or {}
    for key in ("data_date", "latest_date", "as_of"):
        parsed = _parse_date(scfi.get(key))
        if parsed:
            return parsed.isoformat()
    dates: list[date] = []
    for section in ("evidence_type", "scfi"):
        raw = extracted.get(section) or {}
        candidates = json.dumps(raw, ensure_ascii=False)
        dates.extend(_dates_from_text(candidates))
    for url in (scfi.get("sources") or [])[:5]:
        dates.extend(_dates_from_text(str(url)))
    return max(dates).isoformat() if dates else ""


def _dates_from_text(text: str) -> list[date]:
    out: list[date] = []
    for match in re.findall(r"20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}", text or ""):
        normalized = re.sub(r"[年月/.]", "-", match).rstrip("-")
        parsed = _parse_date(normalized)
        if parsed:
            out.append(parsed)
    current_year = datetime.now(timezone.utc).year
    for month, day in re.findall(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)", text or ""):
        try:
            parsed = date(current_year, int(month), int(day))
        except ValueError:
            continue
        out.append(parsed)
    return out


def _extracted_source_label(extracted: dict[str, Any]) -> str:
    scfi = extracted.get("scfi") or {}
    sources = [str(item) for item in scfi.get("sources") or [] if item]
    if sources:
        return "auto_updated_from_public_news:" + sources[0]
    return "auto_updated_from_public_news"


def _csv_row_materially_changed(new_row: dict[str, str], old_row: dict[str, Any]) -> bool:
    fields = (
        "scfi",
        "weekly_change",
        "scfi_streak_weeks",
        "us_west",
        "us_west_weekly_change",
        "us_east",
        "us_east_weekly_change",
        "europe",
        "europe_weekly_change",
    )
    for field in fields:
        left = _safe_float(new_row.get(field))
        right = _safe_float(old_row.get(field))
        if left is None and right is None:
            continue
        if left is None or right is None:
            return True
        tolerance = 0.01 if "weekly_change" in field else 1.0
        if abs(left - right) > tolerance:
            return True
    return False


def _write_scfi_csv_row(row: dict[str, str]) -> bool:
    fieldnames = [
        "date",
        "scfi",
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
        "monthly_change",
        "source",
    ]
    try:
        SCFI_CSV.parent.mkdir(parents=True, exist_ok=True)
        rows = _load_scfi_csv()
        rows = [existing for existing in rows if existing.get("date") != row.get("date")]
        rows.append({key: row.get(key, "") for key in fieldnames})
        rows = sorted(rows, key=lambda item: item.get("date") or "")
        with SCFI_CSV.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return True
    except Exception:
        return False


def _csv_number(value: Any, decimals: int = 2) -> str:
    number = _safe_float(value)
    if number is None:
        return ""
    if decimals == 0 or abs(number - round(number)) < 0.000001:
        return str(int(round(number)))
    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


def _fetch_sse_single_index(settings: Any) -> dict[str, Any]:
    try:
        response = httpx.get(
            SSE_SCFI_SINGLE_INDEX,
            timeout=settings.request_timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.net.cn/"},
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        return {"missing": [f"Data Limitation: SSE SCFI single-index official table fetch failed: {exc}"]}

    data = _parse_sse_single_index_html(response.text)
    if not data.get("scfi_latest"):
        return {"missing": ["Data Limitation: SSE SCFI single-index official table did not expose parsable SCFI values."]}
    data["official_single_index_used"] = True
    data["official_latest_date"] = data.get("latest_date")
    data["official_route_exact_used"] = _route_value_count(data) >= 2
    data["verified_route_source"] = "SSE official single-index table" if _route_value_count(data) >= 2 else None
    return {"data": data}


def _parse_sse_single_index_html(text: str) -> dict[str, Any]:
    rows = _html_table_rows(text)
    data: dict[str, Any] = {
        "scfi_latest": None,
        "weekly_change": None,
        "scfi_streak_weeks": None,
        "us_west": None,
        "us_west_weekly_change": None,
        "us_east": None,
        "us_east_weekly_change": None,
        "europe": None,
        "europe_weekly_change": None,
        "latest_date": None,
    }
    for cells in rows:
        joined = " ".join(cells)
        if not cells:
            continue
        if "航线" in joined and "本期" in joined:
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", joined)
            if dates:
                data["latest_date"] = dates[-1]
            continue
        if "综合指数" in joined or "Comprehensive Index" in joined:
            prev, current, _delta = _last_three_numbers(cells)
            data["scfi_latest"] = current
            if prev is not None and current is not None and prev:
                data["weekly_change"] = round((current - prev) / prev * 100, 2)
            continue
        route = None
        if "美西" in joined or "USWC" in joined:
            route = "us_west"
        elif "美东" in joined or "USEC" in joined:
            route = "us_east"
        elif "欧洲" in joined or "Europe" in joined:
            route = "europe"
        if route:
            prev, current, _delta = _last_three_numbers(cells)
            if current is not None:
                data[route] = current
            if prev is not None and current is not None and prev:
                data[f"{route}_weekly_change"] = round((current - prev) / prev * 100, 2)
    return data


def _html_table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_match in re.finditer(r"<tr[\s\S]*?</tr>", text, flags=re.I):
        row_html = row_match.group(0)
        cells: list[str] = []
        for cell_match in re.finditer(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", row_html, flags=re.I):
            raw = cell_match.group(1)
            clean = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
            clean = re.sub(r"<[^>]+>", " ", clean)
            clean = html.unescape(clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            cells.append(clean)
        if cells:
            rows.append(cells)
    return rows


def _last_three_numbers(cells: list[str]) -> tuple[float | None, float | None, float | None]:
    values: list[float | None] = []
    for cell in cells[-3:]:
        values.append(_safe_float(cell))
    while len(values) < 3:
        values.insert(0, None)  # type: ignore[arg-type]
    return values[-3], values[-2], values[-1]


def _freight_search_fallback(symbol: str, allow_ai_extraction: bool = True) -> dict[str, Any]:
    search = web_search(freight_queries(symbol), max_results_per_query=5)
    extracted = extract_market_intelligence(search.get("results", []), allow_ai=allow_ai_extraction)
    news_numbers = _extract_scfi_numbers_from_news(search.get("results", []))
    if news_numbers:
        extracted = merge_extractions(extracted, news_numbers)
    page_extract = None
    if _extraction_needs_page_extract(extracted):
        page_extract = extract_pages_with_browser(search.get("results", []), max_pages=3, allow_ai=allow_ai_extraction)
        page_numbers = _extract_scfi_numbers_from_pages((page_extract or {}).get("pages", []))
        if page_numbers:
            extracted = merge_extractions(extracted, page_numbers)
        if page_extract.get("extracted"):
            extracted = merge_extractions(extracted, page_extract["extracted"])
    screenshot = None
    if allow_ai_extraction and _extraction_needs_screenshots(extracted):
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


def _sanitize_search_intelligence(extracted: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any]:
    if not extracted:
        return extracted
    cleaned = json.loads(json.dumps(extracted))
    search_date = _extracted_data_date(cleaned)
    accepted_date = accepted.get("csv_data_date") or accepted.get("csv_row_date") or accepted.get("latest_date")
    search_is_newer = _date_after(search_date, accepted_date)
    scfi = cleaned.get("scfi") or {}
    search_scfi = _safe_float(scfi.get("latest_value"))
    accepted_scfi = _safe_float(accepted.get("scfi_latest"))
    if (
        not search_is_newer
        and search_scfi is not None
        and accepted_scfi is not None
        and _materially_different(search_scfi, accepted_scfi, tolerance_pct=3.0)
    ):
        scfi["latest_value"] = None
        evidence = cleaned.setdefault("evidence_type", {})
        evidence["exact_data"] = [
            item for item in evidence.get("exact_data", []) or []
            if "SCFI latest" not in str(item)
        ]
        evidence.setdefault("missing_data", []).append(
            f"Search-extracted SCFI value conflicted with official SSE value {accepted_scfi}; it was ignored."
        )
        cleaned["scfi"] = scfi

    route_rates = cleaned.get("route_rates") or {}
    route_weekly = cleaned.get("route_weekly_change") or {}
    for route in ("us_west", "us_east", "europe"):
        accepted_rate = _safe_float(accepted.get(route))
        search_rate = _safe_float(route_rates.get(route))
        if (
            not search_is_newer
            and accepted_rate is not None
            and search_rate is not None
            and _materially_different(search_rate, accepted_rate, tolerance_pct=5.0)
        ):
            route_rates[route] = None
            _remove_route_evidence(cleaned, route)
            cleaned.setdefault("evidence_type", {}).setdefault("missing_data", []).append(
                f"Search-extracted {route} rate conflicted with accepted route data {accepted_rate}; it was ignored."
            )

        accepted_change = _safe_float(accepted.get(f"{route}_weekly_change"))
        search_change = _safe_float(route_weekly.get(route))
        if (
            not search_is_newer
            and accepted_change is not None
            and search_change is not None
            and abs(search_change - accepted_change) > 3.0
        ):
            route_weekly[route] = None
            _remove_route_evidence(cleaned, route)
            cleaned.setdefault("evidence_type", {}).setdefault("missing_data", []).append(
                f"Search-extracted {route} weekly change conflicted with accepted route data {accepted_change}; it was ignored."
            )
    cleaned["route_rates"] = route_rates
    cleaned["route_weekly_change"] = route_weekly
    return cleaned


def _materially_different(left: float, right: float, tolerance_pct: float) -> bool:
    if right == 0:
        return abs(left - right) > tolerance_pct
    return abs(left - right) / abs(right) * 100 > tolerance_pct


def _remove_route_evidence(cleaned: dict[str, Any], route: str) -> None:
    labels = {
        "us_west": ("US West", "美西"),
        "us_east": ("US East", "美東"),
        "europe": ("Europe", "歐洲"),
    }.get(route, (route,))
    evidence = cleaned.setdefault("evidence_type", {})
    evidence["exact_data"] = [
        item for item in evidence.get("exact_data", []) or []
        if not any(label in str(item) for label in labels)
    ]


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
    data_date = _infer_freight_data_date(normalized, rows)
    return {
        "data_date": data_date,
        "scfi": {
            "latest_value": scfi_latest,
            "weekly_change": weekly_change,
            "trend": "up" if (weekly_change or 0) > 0 else "down" if (weekly_change or 0) < 0 else "unknown",
            "weeks_up_or_down": weeks,
            "confidence": 0.82,
            "sources": sources[:5],
            "data_date": data_date,
        },
        "route_rates": {"us_west": us_west, "us_east": us_east, "europe": europe, "asia": None},
        "route_weekly_change": {"us_west": us_west_change, "us_east": us_east_change, "europe": europe_change},
        "evidence_type": {"exact_data": exact_notes, "inferred_trend": [], "missing_data": []},
    }


def _infer_freight_data_date(text: str, rows: list[dict[str, Any]]) -> str:
    explicit_dates = _dates_from_text(text)
    if explicit_dates:
        return max(explicit_dates).isoformat()
    published_dates: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _parse_date(row.get("published_at"))
        if parsed:
            published_dates.append(parsed)
    return max(published_dates).isoformat() if published_dates else ""


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


def _maybe_update_scfi_csv_from_search(
    extracted: dict[str, Any],
    current_data: dict[str, Any],
    latest_csv_row: dict[str, Any] | None,
) -> dict[str, Any]:
    if not extracted:
        return {"updated": False, "note": ""}
    if _env("SCFI_AUTO_UPDATE_CSV", "true").lower() not in {"1", "true", "yes", "on"}:
        return {"updated": False, "note": "航線資料自動更新已關閉。"}

    row = _csv_row_from_search_extraction(extracted)
    if not row:
        return {"updated": False, "note": "公開搜尋未取得完整美西、美東、歐洲線數字，未更新航線資料庫。"}

    search_date = row.get("date")
    csv_date = (latest_csv_row or {}).get("date") or current_data.get("csv_row_date")
    if not search_date:
        return {"updated": False, "note": "公開搜尋資料缺少資料日期，未更新航線資料庫。"}
    if csv_date and not (_date_after(search_date, csv_date) or _same_date(search_date, csv_date)):
        return {"updated": False, "note": f"公開搜尋資料日期 {search_date} 早於既有資料 {csv_date}，未更新航線資料庫。"}
    if _same_date(search_date, csv_date) and latest_csv_row and not _csv_row_materially_changed(row, latest_csv_row):
        return {"updated": False, "note": f"公開搜尋資料日期 {search_date} 與既有資料相同且數值未變，未更新航線資料庫。"}

    csv_written = _write_scfi_csv_row(row)
    supabase_result = upsert_route_row_to_supabase(row)
    supabase_written = bool(supabase_result.get("written"))
    if not csv_written and not supabase_written:
        return {
            "updated": False,
            "note": f"航線資料更新失敗：CSV 未寫入；Supabase 未寫入：{supabase_result.get('note')}",
        }

    source = row.get("source") or "auto_updated_from_public_news"
    targets = []
    if csv_written:
        targets.append("本機 CSV")
    if supabase_written:
        targets.append("Supabase freight_routes")
    action = "更新" if _date_after(search_date, csv_date) else "覆寫同日"
    return {
        "updated": True,
        "date": search_date,
        "source": source,
        "note": f"{action}航線資料至 {'、'.join(targets)}，資料日期 {search_date}。",
        "csv_written": csv_written,
        "supabase_written": supabase_written,
    }
