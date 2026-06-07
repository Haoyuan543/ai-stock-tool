from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from backend.integrations.supabase_client import is_supabase_configured, select_rows, upsert_rows


ROUTE_FIELDS = [
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
    "source_url",
    "quality",
]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _enabled(name: str, default: str = "true") -> bool:
    return _env(name, default).lower() in {"1", "true", "yes", "on"}


def _num(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_route_row(row: dict[str, Any], *, storage_source: str = "") -> dict[str, Any]:
    normalized = {field: row.get(field) for field in ROUTE_FIELDS if field in row}
    normalized["date"] = _text(row.get("date") or row.get("data_date") or row.get("latest_date"))
    normalized["scfi"] = _text(row.get("scfi") if row.get("scfi") is not None else row.get("scfi_latest"))
    normalized["weekly_change"] = _text(row.get("weekly_change"))
    normalized["scfi_streak_weeks"] = _text(row.get("scfi_streak_weeks"))
    normalized["us_west"] = _text(row.get("us_west"))
    normalized["us_west_weekly_change"] = _text(row.get("us_west_weekly_change"))
    normalized["us_east"] = _text(row.get("us_east"))
    normalized["us_east_weekly_change"] = _text(row.get("us_east_weekly_change"))
    normalized["europe"] = _text(row.get("europe"))
    normalized["europe_weekly_change"] = _text(row.get("europe_weekly_change"))
    normalized["mediterranean"] = _text(row.get("mediterranean"))
    normalized["asia_regional"] = _text(row.get("asia_regional"))
    normalized["monthly_change"] = _text(row.get("monthly_change"))
    normalized["source"] = _text(row.get("source") or row.get("verified_route_source") or storage_source)
    normalized["source_url"] = _text(row.get("source_url"))
    normalized["quality"] = _text(row.get("quality") or "verified_public_route_data")
    if storage_source:
        normalized["_storage_source"] = storage_source
    return normalized


def latest_route_row_from_supabase() -> dict[str, Any]:
    if not _enabled("READ_SUPABASE_FREIGHT", "true"):
        return {}
    if not is_supabase_configured():
        return {}
    try:
        rows = select_rows(
            "freight_routes",
            {
                "select": "*",
                "order": "date.desc,updated_at.desc",
                "limit": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Supabase freight_routes read skipped: {exc}")
        return {}
    if not rows:
        return {}
    return normalize_route_row(rows[0], storage_source="supabase_freight_routes")


def upsert_route_row_to_supabase(row: dict[str, Any]) -> dict[str, Any]:
    if not _enabled("UPDATE_SUPABASE", "true"):
        return {"written": False, "note": "UPDATE_SUPABASE is false."}
    if not is_supabase_configured():
        return {"written": False, "note": "Supabase is not configured."}

    payload = normalize_route_row(row, storage_source=row.get("_storage_source") or "auto_update")
    if not payload.get("date"):
        return {"written": False, "note": "route date is missing."}

    db_row = {
        "date": payload["date"],
        "scfi": _num(payload.get("scfi")),
        "weekly_change": _num(payload.get("weekly_change")),
        "scfi_streak_weeks": _num(payload.get("scfi_streak_weeks")),
        "us_west": _num(payload.get("us_west")),
        "us_west_weekly_change": _num(payload.get("us_west_weekly_change")),
        "us_east": _num(payload.get("us_east")),
        "us_east_weekly_change": _num(payload.get("us_east_weekly_change")),
        "europe": _num(payload.get("europe")),
        "europe_weekly_change": _num(payload.get("europe_weekly_change")),
        "mediterranean": _num(payload.get("mediterranean")),
        "asia_regional": _num(payload.get("asia_regional")),
        "monthly_change": _num(payload.get("monthly_change")),
        "source": payload.get("source"),
        "source_url": payload.get("source_url"),
        "quality": payload.get("quality") or "verified_public_route_data",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        upsert_rows("freight_routes", [db_row], on_conflict="date")
        return {"written": True, "note": f"freight_routes updated for {payload['date']}."}
    except Exception as exc:  # noqa: BLE001
        print(f"Supabase freight_routes write skipped: {exc}")
        return {"written": False, "note": str(exc)}
