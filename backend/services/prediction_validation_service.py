from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.data_fetchers.stock_fetcher import fetch_stock_data
from backend.integrations.supabase_client import insert_rows, is_supabase_configured, select_rows


HORIZONS = [(7, "7d"), (30, "30d"), (90, "90d")]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _price_history(symbol: str) -> list[dict[str, Any]]:
    data = fetch_stock_data(symbol).get("data", {})
    return data.get("bars") or data.get("history") or []


def _row_date(row: dict[str, Any]) -> str:
    return str(row.get("date") or "")


def _validation_id(analysis_id: str, horizon: str) -> str:
    return _stable_hash({"analysis_id": analysis_id, "horizon": horizon})


def _verdict_correct(direction_score: float | None, actual_return: float | None) -> bool | None:
    if actual_return is None:
        return None
    direction = float(direction_score or 50)
    if direction >= 60:
        return actual_return > 0
    if direction <= 40:
        return actual_return < 0
    return abs(actual_return) <= 0.03


def build_validation(row: dict[str, Any], horizon_days: int, horizon: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    analysis_id = row.get("analysis_id")
    symbol = row.get("symbol")
    base_price = row.get("price")
    price_date = str(row.get("price_date") or "")
    analysis_time = _parse_dt(row.get("analysis_time"))
    if not analysis_id or not symbol or not base_price or not price_date or not analysis_time:
        return None

    if datetime.now(timezone.utc) < analysis_time.astimezone(timezone.utc) + timedelta(days=horizon_days):
        return None

    window = [item for item in history if _row_date(item) >= price_date and item.get("close") is not None]
    if len(window) <= horizon_days:
        return None

    base = float(base_price)
    future = float(window[min(len(window) - 1, horizon_days)]["close"])
    sample = window[: horizon_days + 1]
    lows = [float(item["low"]) for item in sample if item.get("low") is not None]
    actual_return = (future - base) / base
    max_drawdown = (min(lows) - base) / base if lows else None
    correct = _verdict_correct(row.get("direction_score"), actual_return)
    return {
        "validation_id": _validation_id(str(analysis_id), horizon),
        "prediction_id": analysis_id,
        "symbol": symbol,
        "horizon": horizon,
        "base_price": base,
        "future_price": future,
        "actual_return": round(actual_return, 6),
        "max_drawdown": round(max_drawdown, 6) if max_drawdown is not None else None,
        "correct": correct,
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "details_json": {
            "price_date": price_date,
            "future_date": window[min(len(window) - 1, horizon_days)].get("date"),
            "market_state": row.get("market_state"),
            "recommendation": row.get("recommendation"),
            "direction_score": row.get("direction_score"),
            "overall_score": row.get("overall_score"),
        },
    }


def validate_due_predictions(limit: int = 200) -> list[dict[str, Any]]:
    if _env("VALIDATE_PREDICTIONS", "true").lower() not in {"1", "true", "yes"}:
        print("Prediction validation skipped: VALIDATE_PREDICTIONS is false.")
        return []
    if not is_supabase_configured():
        print("Prediction validation skipped: Supabase is not configured.")
        return []

    rows = select_rows(
        "analysis_runs",
        {
            "select": "analysis_id,analysis_time,symbol,price,price_date,market_state,recommendation,direction_score,overall_score",
            "order": "analysis_time.asc",
            "limit": str(limit),
        },
    )
    existing = select_rows("prediction_validations", {"select": "validation_id", "limit": "10000"})
    existing_ids = {item.get("validation_id") for item in existing}
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_symbol.setdefault(str(row.get("symbol")), []).append(row)

    validations: list[dict[str, Any]] = []
    for symbol, symbol_rows in by_symbol.items():
        history = _price_history(symbol)
        if not history:
            continue
        for row in symbol_rows:
            analysis_id = str(row.get("analysis_id"))
            for days, label in HORIZONS:
                vid = _validation_id(analysis_id, label)
                if vid in existing_ids:
                    continue
                validation = build_validation(row, days, label, history)
                if validation:
                    validations.append(validation)

    if validations:
        insert_rows("prediction_validations", validations)
        print(f"Prediction validations written: {len(validations)}")
    else:
        print("Prediction validation: no due predictions.")
    return validations

