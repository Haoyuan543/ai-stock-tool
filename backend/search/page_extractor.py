from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from backend.config import get_settings
from backend.search.ai_extractor import EMPTY_EXTRACTION, merge_extractions


ROOT = Path(__file__).resolve().parents[2]
EXTRACT_DIR = ROOT / "data" / "page_extracts"


def extract_pages_with_browser(results: list[dict[str, Any]], max_pages: int = 1, allow_ai: bool = True) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"extracted": None, "pages": [], "missing": ["Data Missing: Playwright is not installed; DOM/network extraction is disabled."]}

    candidates = [row for row in results if _is_http_url(row.get("url"))][:max_pages]
    if not candidates:
        return {"extracted": None, "pages": [], "missing": ["Data Missing: no DOM/network-capable URLs found."]}

    pages: list[dict[str, Any]] = []
    missing: list[str] = []
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    page_timeout_ms = max(12000, min(45000, int(settings.request_timeout * 1000)))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1365, "height": 900}, locale="zh-TW")
        page = context.new_page()
        for index, row in enumerate(candidates, start=1):
            json_payloads: list[dict[str, Any]] = []

            def on_response(response: Any) -> None:
                if len(json_payloads) >= 5:
                    return
                content_type = response.headers.get("content-type", "")
                url = response.url
                if "json" not in content_type and not re.search(r"(api|ajax|json|index|quote|freight|scfi)", url, re.I):
                    return
                try:
                    body = response.json()
                except Exception:
                    return
                compact = _compact_json(body)
                if compact:
                    json_payloads.append({"url": url, "data": compact})

            page.on("response", on_response)
            try:
                page.goto(row["url"], wait_until="domcontentloaded", timeout=page_timeout_ms)
                page.wait_for_timeout(1500)
                dom = page.evaluate(
                    """() => {
                        const tables = Array.from(document.querySelectorAll('table')).slice(0, 6).map((table) => table.innerText);
                        const text = document.body ? document.body.innerText : '';
                        return { title: document.title, text: text.slice(0, 12000), tables };
                    }"""
                )
                page_record = {
                    "url": row["url"],
                    "title": dom.get("title") or row.get("title"),
                    "text": dom.get("text") or "",
                    "tables": dom.get("tables") or [],
                    "network_json": json_payloads,
                }
                path = EXTRACT_DIR / f"page_extract_{index}.json"
                path.write_text(json.dumps(page_record, ensure_ascii=False, indent=2), encoding="utf-8")
                page_record["path"] = str(path)
                pages.append(page_record)
            except Exception as exc:
                missing.append(f"Data Limitation: DOM/network extraction failed for {row.get('url')}: {exc}")
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
        browser.close()

    extracted = extract_from_page_records(pages, allow_ai=allow_ai)
    missing.extend(extracted.get("missing", []))
    return {"extracted": extracted.get("data"), "pages": _metadata_only(pages), "missing": missing}


def extract_from_page_records(pages: list[dict[str, Any]], allow_ai: bool = True) -> dict[str, Any]:
    if not pages:
        return {"data": None, "missing": ["Data Limitation: no page DOM/network records were captured."]}
    local = _local_extract_from_pages(pages)
    settings = get_settings()
    if not allow_ai or not settings.openai_api_key:
        return {"data": local, "missing": ["Data Limitation: OPENAI_API_KEY is unavailable for DOM/network AI extraction; local extraction was used."]}

    compact_pages = [
        {
            "url": page.get("url"),
            "title": page.get("title"),
            "text": page.get("text", "")[:6000],
            "tables": page.get("tables", [])[:4],
            "network_json": page.get("network_json", [])[:4],
        }
        for page in pages
    ]
    prompt = f"""
Extract freight and shipping data from DOM text, tables, and captured network JSON.
Return strict minified JSON only using this schema:
{json.dumps(EMPTY_EXTRACTION, ensure_ascii=False, indent=2)}

Rules:
- Only fill us_west, us_east, europe, or asia if the exact number is present.
- If only direction is present, fill scfi.trend or evidence_type.inferred_trend with confidence <= 0.7.
- Do not invent route-level rates.

Page records:
{json.dumps(compact_pages, ensure_ascii=False, indent=2)}
"""
    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json={"model": settings.openai_model, "input": prompt, "max_output_tokens": 600},
            timeout=httpx.Timeout(settings.openai_timeout_seconds, connect=20.0, read=settings.openai_timeout_seconds, write=20.0, pool=20.0),
        )
        response.raise_for_status()
        text = _extract_output_text(response.json())
        return {"data": merge_extractions(local, _loads_json_object(text)), "missing": []}
    except Exception as exc:
        return {"data": local, "missing": [f"Data Limitation: OpenAI DOM/network extraction failed; local DOM extraction was used. Error: {exc}"]}


def _local_extract_from_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    data = json.loads(json.dumps(EMPTY_EXTRACTION))
    text = "\n".join((page.get("title") or "") + "\n" + (page.get("text") or "") + "\n".join(page.get("tables") or []) for page in pages)
    normalized = text.replace(",", "")
    lower = normalized.lower()
    pct = re.search(r"(?:SCFI|freight|運價|指數).{0,80}?([0-9]+(?:\.[0-9]+)?)\s*%", normalized, flags=re.I | re.S)
    if pct:
        data["scfi"]["weekly_change"] = float(pct.group(1))
        data["scfi"]["trend"] = "up" if any(word in lower for word in ["上漲", "週漲", "連漲", "走升", "rise", "higher", "increase"]) else "unknown"
        data["scfi"]["confidence"] = 0.55
        data["evidence_type"]["inferred_trend"].append(f"DOM text mentions SCFI/freight percentage change: {pct.group(1)}%.")
    if any(word in lower for word in ["連三漲", "連續三週", "three-week", "3 weeks"]):
        data["scfi"]["weeks_up_or_down"] = 3
        data["evidence_type"]["inferred_trend"].append("DOM text mentions SCFI rose for three consecutive periods.")
    if any(word in lower for word in ["四航線齊揚", "四大航線齊揚", "航線齊揚", "routes rose", "route rates rose", "multiple route rates rising"]):
        data["evidence_type"]["inferred_trend"].append("DOM text mentions multiple route rates rising, but exact route mapping requires confirmation.")
    if "紅海" in normalized or "red sea" in lower:
        data["red_sea"]["status"] = "stable"
        data["red_sea"]["confidence"] = 0.4
        data["red_sea"]["summary"] = "DOM text references Red Sea shipping context."
    if not any(data["route_rates"].values()):
        data["evidence_type"]["missing_data"].append("DOM/network extraction did not identify exact route-level rates.")
    return data


def _compact_json(value: Any, max_chars: int = 4000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        return None
    return text[:max_chars] if len(text) > max_chars else value


def _metadata_only(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "url": page.get("url"),
            "title": page.get("title"),
            "path": page.get("path"),
            "table_count": len(page.get("tables") or []),
            "network_json_count": len(page.get("network_json") or []),
            "text_chars": len(page.get("text") or ""),
        }
        for page in pages
    ]


def _is_http_url(url: str | None) -> bool:
    return bool(url and url.startswith(("http://", "https://")))


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
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        return json.loads(match.group(0)) if match else {}
