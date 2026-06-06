from __future__ import annotations

import json
import re
from typing import Any

import httpx

from backend.config import get_settings


EMPTY_EXTRACTION = {
    "scfi": {"latest_value": None, "weekly_change": None, "trend": "unknown", "weeks_up_or_down": None, "confidence": 0.0, "sources": []},
    "route_rates": {"us_west": None, "us_east": None, "europe": None, "asia": None},
    "route_weekly_change": {"us_west": None, "us_east": None, "europe": None},
    "red_sea": {"status": "unknown", "summary": "", "confidence": 0.0, "sources": []},
    "institutional_context": {"foreign": "", "investment_trust": "", "etf_flow": "", "confidence": 0.0},
    "evidence_type": {"exact_data": [], "inferred_trend": [], "missing_data": []},
}


def extract_market_intelligence(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        data = _copy_empty()
        data["evidence_type"]["missing_data"].append("No web search results were available.")
        return data

    settings = get_settings()
    if not settings.openai_api_key:
        return _rule_based_extract(results)

    prompt = _build_prompt(results)
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json={"model": settings.openai_model, "input": prompt, "max_output_tokens": 900},
            timeout=httpx.Timeout(settings.openai_timeout_seconds, connect=20.0, read=settings.openai_timeout_seconds, write=20.0, pool=20.0),
        )
        response.raise_for_status()
        text = _extract_output_text(response.json())
        return _normalize(_loads_json_object(text))
    except Exception as exc:
        data = _rule_based_extract(results)
        data["evidence_type"]["missing_data"].append(f"OpenAI web extraction failed: {exc}")
        return data


def merge_extractions(primary: dict[str, Any] | None, secondary: dict[str, Any] | None) -> dict[str, Any]:
    primary = primary or {}
    secondary = secondary or {}
    merged = _copy_empty()
    merged.update(primary)
    for key in ("scfi", "route_rates", "route_weekly_change", "red_sea", "institutional_context"):
        base = dict(primary.get(key) or {})
        extra = secondary.get(key) or {}
        for subkey, value in extra.items():
            if _is_empty_value(base.get(subkey)) and not _is_empty_value(value):
                base[subkey] = value
        merged[key] = base
    evidence = primary.get("evidence_type") or {}
    extra_evidence = secondary.get("evidence_type") or {}
    merged["evidence_type"] = {
        "exact_data": _dedupe((evidence.get("exact_data") or []) + (extra_evidence.get("exact_data") or [])),
        "inferred_trend": _dedupe((evidence.get("inferred_trend") or []) + (extra_evidence.get("inferred_trend") or [])),
        "missing_data": _dedupe((evidence.get("missing_data") or []) + (extra_evidence.get("missing_data") or [])),
    }
    route_rates = merged.get("route_rates") or {}
    if any(not _is_empty_value(route_rates.get(route)) for route in ("us_west", "us_east", "europe")):
        stale_messages = {
            "Exact route rates not found in extracted evidence.",
            "Exact SCFI route rates were not found in public search snippets.",
        }
        merged["evidence_type"]["missing_data"] = [
            item for item in merged["evidence_type"]["missing_data"] if item not in stale_messages
        ]
    return merged


def _build_prompt(results: list[dict[str, Any]]) -> str:
    compact = [
        {
            "title": row.get("title"),
            "snippet": row.get("snippet"),
            "url": row.get("url"),
            "published_at": row.get("published_at"),
            "source": row.get("source"),
        }
        for row in results[:15]
    ]
    return f"""
Extract freight and market intelligence from these web search results.
Return JSON only. Do not invent exact numbers. If a number is not directly present, keep it null.
You may infer trend only from repeated wording across sources, and then confidence must be <= 0.7.

Schema:
{json.dumps(EMPTY_EXTRACTION, ensure_ascii=False, indent=2)}

Search results:
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""


def _rule_based_extract(results: list[dict[str, Any]]) -> dict[str, Any]:
    data = _copy_empty()
    joined = " ".join(f"{row.get('title') or ''} {row.get('snippet') or ''}" for row in results).lower()
    up_words = ["\u4e0a\u6f32", "\u6f32", "\u8d70\u63da", "\u5347", "rise", "increase", "higher"]
    down_words = ["\u4e0b\u8dcc", "\u8dcc", "\u8d70\u5f31", "\u964d", "fall", "decrease", "lower"]
    if any(word in joined for word in up_words):
        data["scfi"]["trend"] = "up"
        data["scfi"]["confidence"] = 0.45
        data["evidence_type"]["inferred_trend"].append("Search snippets mention rising freight or SCFI.")
    elif any(word in joined for word in down_words):
        data["scfi"]["trend"] = "down"
        data["scfi"]["confidence"] = 0.45
        data["evidence_type"]["inferred_trend"].append("Search snippets mention falling freight or SCFI.")
    if "\u7d05\u6d77" in joined or "red sea" in joined:
        data["red_sea"]["status"] = "stable"
        data["red_sea"]["summary"] = "Red Sea shipping context was mentioned in search snippets; exact severity requires source review."
        data["red_sea"]["confidence"] = 0.4
        data["red_sea"]["sources"] = [row.get("url") for row in results[:5] if row.get("url")]
    data["evidence_type"]["missing_data"].append("Exact SCFI route rates were not found in public search snippets.")
    return data


def _normalize(parsed: dict[str, Any]) -> dict[str, Any]:
    data = _copy_empty()
    for section, value in (parsed or {}).items():
        if isinstance(value, dict) and isinstance(data.get(section), dict):
            data[section].update(value)
    if not data["evidence_type"]["missing_data"] and not any(data["route_rates"].values()):
        data["evidence_type"]["missing_data"].append("Exact route rates not found in extracted evidence.")
    return data


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value in {"", "unknown"}:
        return True
    if isinstance(value, (int, float)) and value == 0:
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _dedupe(items: list[Any]) -> list[Any]:
    output: list[Any] = []
    for item in items:
        if item not in output:
            output.append(item)
    return output


def _copy_empty() -> dict[str, Any]:
    return json.loads(json.dumps(EMPTY_EXTRACTION))


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
