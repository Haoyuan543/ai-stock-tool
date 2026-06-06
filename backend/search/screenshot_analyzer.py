from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx

from backend.config import get_settings
from backend.search.ai_extractor import EMPTY_EXTRACTION


ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_DIR = ROOT / "data" / "screenshots"


def analyze_search_result_screenshots(results: list[dict[str, Any]], max_pages: int = 2) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {
            "extracted": None,
            "screenshots": [],
            "missing": ["Data Missing: Playwright is not installed; webpage screenshot analysis is disabled."],
        }

    candidates = [row for row in results if _is_http_url(row.get("url"))][:max_pages]
    if not candidates:
        return {"extracted": None, "screenshots": [], "missing": ["Data Missing: no screenshot-capable web URLs found."]}

    screenshots: list[dict[str, Any]] = []
    missing: list[str] = []
    settings = get_settings()
    page_timeout_ms = max(12000, min(45000, int(settings.request_timeout * 1000)))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1365, "height": 900}, locale="zh-TW")
        for index, row in enumerate(candidates, start=1):
            try:
                page.goto(row["url"], wait_until="domcontentloaded", timeout=page_timeout_ms)
                page.wait_for_timeout(1500)
                SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
                path = SCREENSHOT_DIR / f"search_result_{index}.png"
                page.screenshot(path=str(path), full_page=False)
                screenshots.append({"url": row["url"], "title": row.get("title"), "path": str(path)})
            except Exception as exc:
                missing.append(f"Data Limitation: screenshot failed for {row.get('url')}: {exc}")
        browser.close()

    extracted = extract_from_screenshots(screenshots)
    missing.extend(extracted.get("missing", []))
    return {"extracted": extracted.get("data"), "screenshots": screenshots, "missing": missing}


def extract_from_screenshots(screenshots: list[dict[str, Any]]) -> dict[str, Any]:
    settings = get_settings()
    if not screenshots:
        return {"data": None, "missing": ["Data Limitation: no screenshots were captured."]}
    if not settings.openai_api_key:
        return {"data": None, "missing": ["Data Missing: OPENAI_API_KEY is required for screenshot extraction."]}

    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Analyze these webpage screenshots for freight and shipping data. "
                "Return JSON only using the provided schema. Do not invent exact route rates. "
                "Only fill us_west/us_east/europe if the value is visibly present in the screenshot. "
                "If only direction is visible, use inferred_trend with confidence <= 0.7."
                f"\nSchema:\n{json.dumps(EMPTY_EXTRACTION, ensure_ascii=False, indent=2)}"
            ),
        }
    ]
    for shot in screenshots:
        path = Path(shot["path"])
        data_url = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "input_text", "text": f"Source URL: {shot.get('url')}"})
        content.append({"type": "input_image", "image_url": data_url})

    try:
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json={"model": settings.openai_model, "input": [{"role": "user", "content": content}], "max_output_tokens": 900},
            timeout=httpx.Timeout(settings.openai_timeout_seconds, connect=20.0, read=settings.openai_timeout_seconds, write=20.0, pool=20.0),
        )
        response.raise_for_status()
        text = _extract_output_text(response.json())
        return {"data": _loads_json_object(text), "missing": []}
    except Exception as exc:
        return {"data": None, "missing": [f"Data Warning: OpenAI screenshot extraction failed: {exc}"]}


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
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        return json.loads(match.group(0)) if match else {}
