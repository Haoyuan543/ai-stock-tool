from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_APPEND_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_name}:append"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _load_service_account_info(raw: str) -> dict[str, Any]:
    value = raw.strip()
    if not value:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is empty.")

    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    if value.startswith("{"):
        return json.loads(value)

    try:
        decoded = base64.b64decode(value).decode("utf-8")
        return json.loads(decoded)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be JSON, base64 JSON, or a file path.") from exc


def _authorized_session(service_account_json: str):
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-auth is not installed. Run pip install -r requirements.txt.") from exc

    info = _load_service_account_info(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
    return AuthorizedSession(credentials)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def build_summary_row(result: dict[str, Any], files: dict[str, Path] | None = None) -> list[str]:
    summary = result.get("summary") or {}
    scores = (result.get("local_scores") or {}).get("revised_score") or {}
    stock = (result.get("market_data") or {}).get("stock") or {}
    freshness = result.get("data_freshness") or {}
    truth = result.get("truthfulness") or {}
    action = result.get("action_plan") or {}
    audit = result.get("report_audit") or {}
    files = files or {}

    limitations = result.get("data_limitations") or result.get("warnings") or []
    top_limitations = "；".join(str(item) for item in limitations[:3])
    html_report = files.get("html").name if files.get("html") else ""

    return [
        _fmt(result.get("timestamp")),
        _fmt(result.get("completed_at")),
        _fmt(result.get("symbol")),
        _fmt(result.get("mode")),
        _fmt(freshness.get("price_data_date")),
        _fmt(freshness.get("is_realtime_price")),
        _fmt(stock.get("close")),
        _fmt(stock.get("volume")),
        _fmt(summary.get("market_state")),
        _fmt(summary.get("action")),
        _fmt(summary.get("buy_advice")),
        _fmt(summary.get("sell_advice")),
        _fmt(action.get("next_sell_point")),
        _fmt(action.get("next_buyback_point")),
        _fmt(scores.get("direction_score")),
        _fmt(scores.get("timing_score")),
        _fmt(scores.get("valuation_score")),
        _fmt(scores.get("risk_score")),
        _fmt(scores.get("data_coverage") or summary.get("data_coverage")),
        _fmt(scores.get("truthfulness_score") or truth.get("truthfulness_score")),
        _fmt(scores.get("overall_score") or summary.get("conviction_score")),
        _fmt(result.get("analysis_mode")),
        _fmt(result.get("model_used")),
        _fmt(result.get("elapsed_seconds")),
        _fmt(audit.get("audit_score")),
        _fmt(audit.get("needs_revision")),
        _fmt(top_limitations),
        _fmt(html_report),
    ]


def header_row() -> list[str]:
    return [
        "analysis_time",
        "completed_at",
        "symbol",
        "mode",
        "price_data_date",
        "is_realtime_price",
        "close",
        "volume",
        "market_state",
        "action",
        "buy_advice",
        "sell_advice",
        "next_sell_point",
        "next_buyback_point",
        "direction_score",
        "timing_score",
        "valuation_score",
        "risk_score",
        "data_coverage",
        "truthfulness_score",
        "overall_score",
        "analysis_mode",
        "model_used",
        "elapsed_seconds",
        "audit_score",
        "needs_revision",
        "top_data_limitations",
        "html_report_file",
    ]


def append_analysis_summary(result: dict[str, Any], files: dict[str, Path] | None = None) -> bool:
    service_account_json = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = _env("GOOGLE_SHEET_ID")
    worksheet = _env("GOOGLE_SHEET_WORKSHEET", "analysis_log")

    if not service_account_json or not sheet_id:
        print("Google Sheet skipped: GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SHEET_ID is not configured.")
        return False

    session = _authorized_session(service_account_json)
    range_name = f"{worksheet}!A:Z"
    url = SHEETS_APPEND_URL.format(sheet_id=sheet_id, range_name=range_name)
    params = {
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "INSERT_ROWS",
    }
    body = {"values": [build_summary_row(result, files)]}
    response = session.post(url, params=params, json=body, timeout=30)
    response.raise_for_status()
    print(f"Google Sheet appended: {worksheet}")
    return True
