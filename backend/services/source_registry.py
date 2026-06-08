from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


TZ = timezone(timedelta(hours=8))


def now_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def source_stamp(
    name: str,
    url: str = "",
    *,
    data_as_of: Any = None,
    method: str = "api",
    fetched_at: str | None = None,
    confidence: float | None = None,
    is_exact: bool | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "url": url,
        "as_of": data_as_of,
        "data_as_of": data_as_of,
        "fetched_at": fetched_at or now_taipei(),
        "method": method,
        "confidence": confidence,
        "is_exact": is_exact,
        "note": note,
    }


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()
    for source in sources:
        key = (
            source.get("name"),
            source.get("url"),
            source.get("as_of") or source.get("data_as_of"),
            source.get("method"),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(source)
    return output


def source_summary_lines(sources: list[dict[str, Any]], limit: int = 18) -> list[str]:
    lines: list[str] = []
    for source in _prioritize_sources(dedupe_sources(sources))[:limit]:
        name = source.get("name") or source.get("source") or "未命名資料來源"
        method = source.get("method") or "未標示"
        as_of = source.get("data_as_of") or source.get("as_of") or source.get("published_at") or source.get("date") or "資料時間不足"
        fetched_at = source.get("fetched_at") or "抓取時間不足"
        confidence = source.get("confidence")
        confidence_text = f"｜信心：{confidence}" if confidence is not None else ""
        exact = "精確資料" if source.get("is_exact") else "非精確 / 需交叉確認"
        note = f"｜備註：{source.get('note')}" if source.get("note") else ""
        url = f"｜連結：{source.get('url')}" if source.get("url") else ""
        lines.append(
            f"- {name}｜方式：{method}｜資料時間：{as_of}｜抓取時間：{fetched_at}｜{exact}{confidence_text}{note}{url}"
        )
    return lines


def _prioritize_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def score(source: dict[str, Any]) -> tuple[int, int, int, float]:
        has_time = bool(source.get("fetched_at")) + bool(source.get("data_as_of") or source.get("as_of"))
        has_method = 1 if source.get("method") else 0
        exact = 1 if source.get("is_exact") else 0
        confidence = float(source.get("confidence") or 0)
        return (has_time, has_method, exact, confidence)

    return sorted(sources, key=score, reverse=True)
