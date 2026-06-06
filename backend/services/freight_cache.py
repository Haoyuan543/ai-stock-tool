from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.integrations.supabase_client import insert_rows, is_supabase_configured, select_rows


ROOT = Path(__file__).resolve().parents[2]
CACHE_FILE = ROOT / "data" / "freight_cache.json"
FREIGHT_FIELDS = [
    "scfi_latest",
    "weekly_change",
    "scfi_streak_weeks",
    "us_west",
    "us_west_weekly_change",
    "us_east",
    "us_east_weekly_change",
    "europe",
    "europe_weekly_change",
    "red_sea_status",
    "latest_date",
]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == "unknown"


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _age_days(value: Any) -> int | None:
    parsed = _parse_date(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).days)


def _max_cache_days() -> int:
    try:
        return int(_env("FREIGHT_CACHE_MAX_DAYS", "21") or "21")
    except ValueError:
        return 21


def _cache_payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("freight_json") or record.get("raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if "freight" in raw:
        raw = raw.get("freight") or {}
    return raw if isinstance(raw, dict) else {}


def _local_cache_rows(symbol: str) -> list[dict[str, Any]]:
    if not CACHE_FILE.exists():
        return []
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload if isinstance(payload, list) else payload.get("rows", [])
    return [row for row in rows if row.get("symbol") == symbol]


def _supabase_cache_rows(symbol: str) -> list[dict[str, Any]]:
    if not is_supabase_configured():
        return []
    rows: list[dict[str, Any]] = []
    try:
        rows.extend(
            select_rows(
                "freight_cache",
                {
                    "select": "*",
                    "symbol": f"eq.{symbol}",
                    "order": "data_date.desc,fetched_at.desc",
                    "limit": "3",
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Freight cache Supabase read skipped: {exc}")
    if not rows:
        try:
            snapshots = select_rows(
                "market_snapshots",
                {
                    "select": "symbol,analysis_time,raw_json",
                    "symbol": f"eq.{symbol}",
                    "order": "analysis_time.desc",
                    "limit": "3",
                },
            )
            for row in snapshots:
                freight = ((row.get("raw_json") or {}).get("freight") or {}) if isinstance(row.get("raw_json"), dict) else {}
                if freight:
                    rows.append(
                        {
                            "symbol": symbol,
                            "fetched_at": row.get("analysis_time"),
                            "data_date": freight.get("latest_date"),
                            "freight_json": freight,
                            "source": "supabase_market_snapshots",
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"Freight cache market snapshot read skipped: {exc}")
    return rows


def _best_cache_row(symbol: str) -> dict[str, Any]:
    rows = _supabase_cache_rows(symbol) + _local_cache_rows(symbol)
    valid: list[dict[str, Any]] = []
    for row in rows:
        freight = _cache_payload(row)
        if not freight:
            continue
        has_core = any(not _is_missing(freight.get(field)) for field in ("scfi_latest", "us_west", "us_east", "europe"))
        if not has_core:
            continue
        data_date = row.get("data_date") or freight.get("latest_date")
        age = _age_days(data_date)
        if age is not None and age > _max_cache_days():
            continue
        valid.append(row | {"_freight": freight, "_age_days": age})
    if not valid:
        return {}
    return sorted(
        valid,
        key=lambda row: (
            str(row.get("data_date") or (row.get("_freight") or {}).get("latest_date") or ""),
            str(row.get("fetched_at") or ""),
        ),
        reverse=True,
    )[0]


def apply_last_successful_freight_cache(symbol: str, data: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    cache_row = _best_cache_row(symbol)
    if not cache_row:
        return data
    cached = cache_row.get("_freight") or _cache_payload(cache_row)
    filled: list[str] = []
    for field in FREIGHT_FIELDS:
        if _is_missing(data.get(field)) and not _is_missing(cached.get(field)):
            data[field] = cached.get(field)
            filled.append(field)
    if not filled:
        return data

    data["cache_used"] = True
    data["cache_filled_fields"] = filled
    data["cache_data_date"] = cache_row.get("data_date") or cached.get("latest_date")
    data["cache_fetched_at"] = cache_row.get("fetched_at")
    data["cache_age_days"] = cache_row.get("_age_days")
    data["cache_source"] = cache_row.get("source") or "last_successful_freight_cache"
    data["cache_note"] = "本次即時抓取缺少部分航運欄位，已用上次成功資料補齊；報告須以快取日期解讀。"

    if all(not _is_missing(data.get(route)) for route in ("us_west", "us_east", "europe")):
        stale_messages = {
            "Data Missing: US West / US East / Europe route freight rates unavailable.",
            "Data Missing: data/scfi_routes.csv not found or empty.",
        }
        missing[:] = [item for item in missing if item not in stale_messages]
    return data


def build_freight_cache_row(symbol: str, freight: dict[str, Any], analysis_time: str | None = None) -> dict[str, Any] | None:
    if not freight:
        return None
    has_value = any(not _is_missing(freight.get(field)) for field in ("scfi_latest", "us_west", "us_east", "europe"))
    if not has_value:
        return None
    freight_json = {field: freight.get(field) for field in FREIGHT_FIELDS if field in freight}
    freight_json.update(
        {
            "intelligence": freight.get("intelligence"),
            "search_intelligence": freight.get("search_intelligence"),
            "official_chart_parsed": freight.get("official_chart_parsed"),
            "note": freight.get("note"),
        }
    )
    fetched_at = analysis_time or datetime.now(timezone.utc).isoformat(timespec="seconds")
    data_date = freight.get("latest_date")
    return {
        "cache_id": _stable_hash({"symbol": symbol, "data_date": data_date, "fetched_at": fetched_at, "freight": freight_json}),
        "symbol": symbol,
        "data_date": data_date,
        "fetched_at": fetched_at,
        "scfi_latest": freight.get("scfi_latest"),
        "weekly_change": freight.get("weekly_change"),
        "scfi_streak_weeks": freight.get("scfi_streak_weeks"),
        "us_west": freight.get("us_west"),
        "us_west_weekly_change": freight.get("us_west_weekly_change"),
        "us_east": freight.get("us_east"),
        "us_east_weekly_change": freight.get("us_east_weekly_change"),
        "europe": freight.get("europe"),
        "europe_weekly_change": freight.get("europe_weekly_change"),
        "source": "analysis_success",
        "freight_json": freight_json,
    }


def write_freight_cache(symbol: str, freight: dict[str, Any], analysis_time: str | None = None) -> bool:
    row = build_freight_cache_row(symbol, freight, analysis_time)
    if not row:
        return False
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = _local_cache_rows(symbol)
    rows = [item for item in rows if item.get("cache_id") != row["cache_id"]]
    rows.append(row)
    rows = sorted(rows, key=lambda item: (str(item.get("data_date") or ""), str(item.get("fetched_at") or "")), reverse=True)[:20]
    existing_other = []
    if CACHE_FILE.exists():
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            all_rows = payload if isinstance(payload, list) else payload.get("rows", [])
            existing_other = [item for item in all_rows if item.get("symbol") != symbol]
        except Exception:
            existing_other = []
    CACHE_FILE.write_text(json.dumps({"rows": existing_other + rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    if _env("UPDATE_SUPABASE", "true").lower() in {"1", "true", "yes"} and is_supabase_configured():
        try:
            insert_rows("freight_cache", [row])
        except Exception as exc:  # noqa: BLE001
            print(f"Freight cache Supabase write skipped: {exc}")
    return True
