from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_supabase_configured() -> bool:
    return bool(_env("SUPABASE_URL") and _env("SUPABASE_SERVICE_ROLE_KEY"))


def _headers() -> dict[str, str]:
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _base_url() -> str:
    return _env("SUPABASE_URL").rstrip("/")


def insert_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_supabase_configured():
        print("Supabase skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        return []
    if not rows:
        return []

    url = f"{_base_url()}/rest/v1/{table}"
    with httpx.Client(timeout=30) as client:
        response = client.post(url, headers=_headers(), content=json.dumps(rows, ensure_ascii=False))
        response.raise_for_status()
        return response.json()


def select_rows(table: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    if not is_supabase_configured():
        print("Supabase skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        return []

    url = f"{_base_url()}/rest/v1/{table}"
    with httpx.Client(timeout=30) as client:
        response = client.get(url, headers=_headers(), params=params or {})
        response.raise_for_status()
        return response.json()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
