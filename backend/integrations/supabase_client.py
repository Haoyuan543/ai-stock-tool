from __future__ import annotations

import json
import os
from urllib.parse import urlparse
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
    raw = _env("SUPABASE_URL").strip().strip('"').strip("'")
    if raw and not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw.rstrip("/")


def diagnostic_status() -> dict[str, Any]:
    base_url = _base_url()
    parsed = urlparse(base_url)
    return {
        "configured": is_supabase_configured(),
        "has_url": bool(_env("SUPABASE_URL")),
        "has_key": bool(_env("SUPABASE_SERVICE_ROLE_KEY")),
        "scheme": parsed.scheme or "",
        "host": parsed.netloc or parsed.path.split("/")[0],
        "host_looks_valid": (parsed.netloc or parsed.path).endswith(".supabase.co"),
    }


def print_diagnostic(prefix: str = "Supabase diagnostic") -> None:
    status = diagnostic_status()
    print(
        f"{prefix}: configured={status['configured']}, "
        f"has_url={status['has_url']}, has_key={status['has_key']}, "
        f"scheme={status['scheme'] or 'missing'}, host={status['host'] or 'missing'}, "
        f"host_looks_valid={status['host_looks_valid']}"
    )


def insert_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_supabase_configured():
        print("Supabase skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        print_diagnostic()
        return []
    if not rows:
        return []

    url = f"{_base_url()}/rest/v1/{table}"
    with httpx.Client(timeout=30) as client:
        try:
            response = client.post(url, headers=_headers(), content=json.dumps(rows, ensure_ascii=False))
        except httpx.RequestError:
            print_diagnostic("Supabase request failed")
            raise
        response.raise_for_status()
        return response.json()


def select_rows(table: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    if not is_supabase_configured():
        print("Supabase skipped: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not configured.")
        print_diagnostic()
        return []

    url = f"{_base_url()}/rest/v1/{table}"
    with httpx.Client(timeout=30) as client:
        try:
            response = client.get(url, headers=_headers(), params=params or {})
        except httpx.RequestError:
            print_diagnostic("Supabase request failed")
            raise
        response.raise_for_status()
        return response.json()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
