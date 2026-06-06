from __future__ import annotations

import re
from typing import Any


ENGINEERING_TOKENS = [
    "Data Missing",
    "Data Limitation",
    "Data Warning",
    "holding_change",
    "AUM_change",
    "stale_event_over_14_days",
    "Market Regime",
    "ETF Flow",
    "jsonl",
    "python -m",
]


def _score(result: dict[str, Any]) -> dict[str, Any]:
    return (result.get("local_scores") or {}).get("revised_score") or {}


def _report(result: dict[str, Any]) -> str:
    return result.get("report_markdown") or result.get("ai_report") or ""


def _is_aggressive_action(action: str) -> bool:
    conservative_phrases = ["不積極", "警戒", "觀察", "等待", "不追", "不動"]
    if any(phrase in action for phrase in conservative_phrases):
        return False
    return any(phrase in action for phrase in ["追", "積極買", "大幅加碼", "積極", "大幅", "加碼"])


def audit_report(result: dict[str, Any]) -> dict[str, Any]:
    scores = _score(result)
    summary = result.get("summary") or {}
    truth = result.get("truthfulness") or {}
    freshness = result.get("data_freshness") or {}
    report = _report(result)
    warnings: list[str] = []
    failed_rules: list[str] = []

    overall = float(scores.get("overall_score") or summary.get("conviction_score") or 0)
    timing = float(scores.get("timing_score") or 0)
    risk = float(scores.get("risk_score") or 0)
    truthfulness = float(scores.get("truthfulness_score") or truth.get("truthfulness_score") or 0)
    market_state = str(summary.get("market_state") or "")
    action = str(summary.get("action") or "")

    if overall < 65 and re.search(r"\bBullish\b|強烈偏多|積極偏多", market_state, re.IGNORECASE):
        failed_rules.append("overall_score_verdict_mismatch")
        warnings.append("綜合分數低於 65，卻出現偏強多方結論。")

    if timing < 50 and _is_aggressive_action(action):
        failed_rules.append("timing_action_mismatch")
        warnings.append("時機分數低於 50，不應建議追價或積極加碼。")

    if risk < 50 and _is_aggressive_action(action):
        failed_rules.append("risk_action_mismatch")
        warnings.append("風險分數低於 50，不應給積極操作建議。")

    if truthfulness < 60 and "資料不足" not in report:
        failed_rules.append("low_truthfulness_not_disclosed")
        warnings.append("可信度低於 60，但報告未明確揭露資料不足。")

    if freshness.get("is_realtime_price") is False and "不是即時" not in report and "非今日" not in report:
        failed_rules.append("freshness_warning_missing")
        warnings.append("股價不是即時資料，但報告未清楚提醒。")

    found_tokens = [token for token in ENGINEERING_TOKENS if token in report]
    if found_tokens:
        failed_rules.append("engineering_language_leak")
        warnings.append(f"報告仍含工程字眼：{', '.join(found_tokens[:5])}")

    required_sections = ["決策摘要", "今日操作建議", "核心證據", "資料品質"]
    missing_sections = [section for section in required_sections if section not in report]
    if missing_sections:
        failed_rules.append("required_sections_missing")
        warnings.append(f"報告缺少重要區塊：{', '.join(missing_sections)}")

    if len(report.splitlines()) < 80:
        failed_rules.append("report_too_short")
        warnings.append("報告內容過短，可能缺乏足夠證據。")

    score = 100
    score -= 15 * sum(1 for rule in failed_rules if rule.endswith("mismatch"))
    score -= 20 if "engineering_language_leak" in failed_rules else 0
    score -= 15 if "low_truthfulness_not_disclosed" in failed_rules else 0
    score -= 10 if "freshness_warning_missing" in failed_rules else 0
    score -= 10 if "required_sections_missing" in failed_rules else 0
    score -= 10 if "report_too_short" in failed_rules else 0
    score = max(0, score)

    return {
        "audit_score": score,
        "needs_revision": score < 80 or bool(failed_rules),
        "audit_warnings": warnings,
        "failed_rules": failed_rules,
        "recommended_changes": _recommended_changes(failed_rules),
    }


def _recommended_changes(failed_rules: list[str]) -> list[str]:
    mapping = {
        "overall_score_verdict_mismatch": "將結論降級為中性偏多 / 中性，並補充分數原因。",
        "timing_action_mismatch": "把追價建議改成等待回檔或分批觀察。",
        "risk_action_mismatch": "降低操作強度，明確列出風險觸發條件。",
        "low_truthfulness_not_disclosed": "在摘要加入資料可信度與資料缺口說明。",
        "freshness_warning_missing": "在股價區塊和摘要提醒資料不是即時價。",
        "engineering_language_leak": "將工程字眼改寫成使用者可理解的中文。",
        "required_sections_missing": "補齊決策摘要、操作建議、核心證據與資料品質區塊。",
        "report_too_short": "補充實際證據、資料來源與分數拆解。",
    }
    return [mapping[rule] for rule in failed_rules if rule in mapping]


def _clean_is_aggressive_action(action: str) -> bool:
    conservative_phrases = ["不動", "暫不", "警戒", "等待", "觀察", "不追", "保守", "無法分析"]
    if any(phrase in action for phrase in conservative_phrases):
        return False
    return any(phrase in action for phrase in ["買進", "加碼", "追價", "大幅減碼", "積極操作"])


def audit_report(result: dict[str, Any]) -> dict[str, Any]:
    scores = _score(result)
    summary = result.get("summary") or {}
    truth = result.get("truthfulness") or {}
    freshness = result.get("data_freshness") or {}
    report = _report(result)
    warnings: list[str] = []
    failed_rules: list[str] = []

    overall = float(scores.get("overall_score") or summary.get("conviction_score") or 0)
    timing = float(scores.get("timing_score") or 0)
    risk = float(scores.get("risk_score") or 0)
    truthfulness = float(scores.get("truthfulness_score") or truth.get("truthfulness_score") or 0)
    market_state = str(summary.get("market_state") or "")
    action = str(summary.get("action") or "")

    if result.get("analysis_mode") == "fallback" and "無法完成 AI 投資分析" not in report:
        failed_rules.append("fallback_report_should_not_give_verdict")
        warnings.append("OpenAI 未完成時，報告不應輸出投資結論或買賣建議。")

    overly_bullish = any(token in market_state for token in ["Strong Bullish", "強烈偏多"])
    overly_bullish = overly_bullish or (("Bullish" in market_state or "偏多" in market_state) and "Neutral-Bullish" not in market_state and "中性偏多" not in market_state)
    if overall < 65 and overly_bullish:
        failed_rules.append("overall_score_verdict_mismatch")
        warnings.append("綜合分數低於 65，不應輸出單純偏多或強烈偏多結論；可使用中性偏多。")

    if timing < 50 and _clean_is_aggressive_action(action):
        failed_rules.append("timing_action_mismatch")
        warnings.append("時機分數低於 50，不應給積極操作建議。")

    if risk < 50 and _clean_is_aggressive_action(action):
        failed_rules.append("risk_action_mismatch")
        warnings.append("風險分數低於 50，不應給積極操作建議。")

    if truthfulness < 60 and "資料可信度" not in report:
        failed_rules.append("low_truthfulness_not_disclosed")
        warnings.append("資料可信度低於 60，報告應明確揭露資料限制。")

    if freshness.get("is_realtime_price") is False and "股價資料日期" not in report:
        failed_rules.append("freshness_warning_missing")
        warnings.append("股價不是即時資料時，報告應列出股價資料日期。")

    found_tokens = [token for token in ENGINEERING_TOKENS if token in report]
    if found_tokens:
        failed_rules.append("engineering_language_leak")
        warnings.append(f"報告仍含工程字眼：{', '.join(found_tokens[:5])}")

    required_sections = ["分析引擎狀態"]
    if result.get("analysis_mode") == "openai":
        required_sections.extend(["事實依據摘要", "資料來源"])
    missing_sections = [section for section in required_sections if section not in report]
    if missing_sections:
        failed_rules.append("required_sections_missing")
        warnings.append(f"報告缺少必要區塊：{', '.join(missing_sections)}")

    if result.get("analysis_mode") == "openai" and len(report.splitlines()) < 60:
        failed_rules.append("report_too_short")
        warnings.append("OpenAI 成功時，報告內容偏短，可能缺少證據摘要或資料來源。")

    score = 100
    score -= 15 * sum(1 for rule in failed_rules if rule.endswith("mismatch"))
    score -= 20 if "fallback_report_should_not_give_verdict" in failed_rules else 0
    score -= 20 if "engineering_language_leak" in failed_rules else 0
    score -= 15 if "low_truthfulness_not_disclosed" in failed_rules else 0
    score -= 10 if "freshness_warning_missing" in failed_rules else 0
    score -= 10 if "required_sections_missing" in failed_rules else 0
    score -= 10 if "report_too_short" in failed_rules else 0
    score = max(0, score)

    return {
        "audit_score": score,
        "needs_revision": score < 80 or bool(failed_rules),
        "audit_warnings": warnings,
        "failed_rules": failed_rules,
        "recommended_changes": _clean_recommended_changes(failed_rules),
    }


def _clean_recommended_changes(failed_rules: list[str]) -> list[str]:
    mapping = {
        "fallback_report_should_not_give_verdict": "OpenAI 失敗時只顯示無法分析與排錯資訊。",
        "overall_score_verdict_mismatch": "將市場結論降級為中性或資料不足。",
        "timing_action_mismatch": "把操作建議改成等待、觀察或不追價。",
        "risk_action_mismatch": "降低操作強度，明確標示風險條件。",
        "low_truthfulness_not_disclosed": "在摘要與證據區補上資料可信度與資料限制。",
        "freshness_warning_missing": "補上股價資料日期與是否即時資料。",
        "engineering_language_leak": "把工程欄位名稱改成投資人看得懂的中文。",
        "required_sections_missing": "補上分析引擎狀態、事實依據摘要與資料來源。",
        "report_too_short": "補充股價、運價、法人、基本面、事件與資料來源證據。",
    }
    return [mapping[rule] for rule in failed_rules if rule in mapping]
