from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from backend.integrations.supabase_client import insert_rows, is_supabase_configured


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _json(value: Any) -> Any:
    return value if value is not None else {}


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_analysis_run_row(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") or {}
    scores = (result.get("local_scores") or {}).get("revised_score") or {}
    freshness = result.get("data_freshness") or {}
    stock = (result.get("market_data") or {}).get("stock") or {}

    return {
        "analysis_id": _stable_hash(
            {
                "symbol": result.get("symbol"),
                "timestamp": result.get("timestamp"),
                "model_used": result.get("model_used"),
            }
        ),
        "analysis_time": result.get("timestamp"),
        "completed_at": result.get("completed_at"),
        "symbol": result.get("symbol"),
        "mode": result.get("mode"),
        "price": stock.get("close"),
        "price_date": freshness.get("price_data_date"),
        "is_realtime_price": freshness.get("is_realtime_price"),
        "market_state": summary.get("market_state"),
        "recommendation": summary.get("action"),
        "direction_score": scores.get("direction_score"),
        "timing_score": scores.get("timing_score"),
        "valuation_score": scores.get("valuation_score"),
        "risk_score": scores.get("risk_score"),
        "data_coverage": scores.get("data_coverage") or summary.get("data_coverage"),
        "truthfulness_score": scores.get("truthfulness_score") or (result.get("truthfulness") or {}).get("truthfulness_score"),
        "overall_score": scores.get("overall_score") or summary.get("conviction_score"),
        "analysis_mode": result.get("analysis_mode"),
        "model_used": result.get("model_used"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "summary_json": _json(summary),
        "scores_json": _json(scores),
        "market_data_json": _json(result.get("market_data")),
        "data_quality_json": _json(result.get("data_quality")),
        "truthfulness_json": _json(result.get("truthfulness")),
        "audit_json": _json(result.get("report_audit")),
        "report_markdown": result.get("report_markdown") or result.get("ai_report") or "",
        "warnings_json": _json(result.get("warnings")),
    }


def build_market_snapshot_row(result: dict[str, Any]) -> dict[str, Any]:
    market = result.get("market_data") or {}
    stock = market.get("stock") or {}
    freight = market.get("freight") or {}
    institutional = market.get("institutional") or {}
    fundamentals = market.get("fundamentals") or {}

    return {
        "snapshot_id": _stable_hash(
            {
                "symbol": result.get("symbol"),
                "timestamp": result.get("timestamp"),
                "price_date": (result.get("data_freshness") or {}).get("price_data_date"),
            }
        ),
        "analysis_time": result.get("timestamp"),
        "symbol": result.get("symbol"),
        "price_date": (result.get("data_freshness") or {}).get("price_data_date"),
        "close": stock.get("close"),
        "volume": stock.get("volume"),
        "ma20": stock.get("ma20"),
        "ma60": stock.get("ma60"),
        "scfi_latest": freight.get("scfi_latest"),
        "scfi_weekly_change": freight.get("weekly_change"),
        "freight_trend": (freight.get("intelligence") or {}).get("overall_trend"),
        "institutional_total": (institutional.get("latest") or {}).get("total"),
        "eps": fundamentals.get("eps"),
        "dividend_yield": fundamentals.get("dividend_yield"),
        "raw_json": _json(market),
    }


def write_history_to_supabase(result: dict[str, Any]) -> bool:
    if _env("UPDATE_SUPABASE", "true").lower() not in {"1", "true", "yes"}:
        print("Supabase skipped: UPDATE_SUPABASE is false.")
        return False
    if not is_supabase_configured():
        print("Supabase skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        return False

    try:
        insert_rows("analysis_runs", [build_analysis_run_row(result)])
        insert_rows("market_snapshots", [build_market_snapshot_row(result)])
        print("Supabase history written.")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"Supabase history write failed: {exc}")
        if _env("SUPABASE_REQUIRED", "false").lower() in {"1", "true", "yes"}:
            raise
        return False
