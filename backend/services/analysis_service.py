from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.ai_analyzer import AIAnalyzer
from backend.data_fetchers.announcement_fetcher import fetch_announcement_data
from backend.data_fetchers.freight_fetcher import fetch_freight_data
from backend.data_fetchers.fundamental_fetcher import fetch_fundamental_data
from backend.data_fetchers.institutional_fetcher import fetch_institutional_data
from backend.data_fetchers.news_fetcher import fetch_news_data
from backend.data_fetchers.stock_fetcher import fetch_stock_data
from backend.services.announcement_intelligence import build_announcement_intelligence
from backend.services.data_quality import build_data_quality
from backend.services.etf_flow_engine import build_etf_flow
from backend.services.fill_dividend_probability import estimate_fill_dividend_probability
from backend.services.freight_intelligence import build_freight_intelligence
from backend.services.gap_hunter import build_gap_report
from backend.services.international_event_engine import build_international_events
from backend.services.market_regime_engine import build_market_regime
from backend.services.news_relevance_filter import filter_relevant_news
from backend.services.prediction_tracker import record_prediction
from backend.services.prompt_builder import build_analysis_prompt
from backend.services.red_sea_intelligence import build_red_sea_intelligence
from backend.services.truthfulness_engine import build_truthfulness


ROOT = Path(__file__).resolve().parents[2]
HISTORY_FILE = ROOT / "data" / "analysis_history.jsonl"
TZ = timezone(timedelta(hours=8))


class AnalysisService:
    def __init__(self) -> None:
        self.ai = AIAnalyzer()

    def analyze_now(
        self,
        symbol: str,
        mode: str = "general",
        freight_overrides: dict[str, Any] | None = None,
        manual_context: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        started = datetime.now(TZ)
        mode = "general" if mode == "general" else "personalized"

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                "stock": executor.submit(fetch_stock_data, symbol),
                "institutional": executor.submit(fetch_institutional_data, symbol),
                "freight": executor.submit(fetch_freight_data, symbol, freight_overrides or {}),
                "news": executor.submit(fetch_news_data, symbol),
                "fundamentals": executor.submit(fetch_fundamental_data, symbol),
                "announcements": executor.submit(fetch_announcement_data, symbol),
            }
            stock = futures["stock"].result()
            institutional = futures["institutional"].result()
            freight = futures["freight"].result()
            news = futures["news"].result()
            fundamentals = futures["fundamentals"].result()
            announcements = futures["announcements"].result()

        profile = self._load_profile() if mode == "personalized" else None
        market_data = {
            "stock": stock.get("data", {}),
            "institutional": institutional.get("data", {}),
            "freight": freight.get("data", {}),
            "news": news.get("data", {}),
            "fundamentals": fundamentals.get("data", {}),
            "announcements": announcements.get("data", {}),
            "manual_context": manual_context,
        }
        market_data["news_relevance"] = filter_relevant_news(market_data["news"])
        market_data["freight"]["intelligence"] = build_freight_intelligence(market_data["freight"], market_data["news"])
        market_data["freight_intelligence"] = market_data["freight"]["intelligence"]
        market_data["etf_flow"] = build_etf_flow(symbol, market_data["news_relevance"])
        market_data["red_sea"] = build_red_sea_intelligence(market_data["news"])
        market_data["red_sea_intelligence"] = market_data["red_sea"]
        market_data["announcement_intelligence"] = build_announcement_intelligence(symbol, market_data["announcements"], manual_context)
        market_data["market_regime"] = build_market_regime(market_data["stock"])
        market_data["international_events"] = build_international_events()
        market_data["fill_dividend_probability"] = estimate_fill_dividend_probability(
            market_data["fundamentals"],
            market_data["stock"],
            market_data["freight"],
            market_data["institutional"],
            market_data["etf_flow"],
            market_data["market_regime"],
        )

        data_status = {
            "stock": stock.get("status", "missing"),
            "institutional": institutional.get("status", "missing"),
            "freight": freight.get("status", "missing"),
            "news": news.get("status", "missing"),
            "fundamental": fundamentals.get("status", "missing"),
            "announcements": announcements.get("status", "missing"),
        }
        ann_status = market_data["announcement_intelligence"].get("latest_event")
        if data_status["announcements"] == "missing" and ann_status in {"stale_event_over_14_days", "recent_event_within_7_days", "today_material_event", "fetch_failed", "unknown", "none"}:
            data_status["announcements"] = ann_status
        sources = self._merge_sources(stock, institutional, freight, news, fundamentals, announcements)
        sources = self._merge_engine_sources(sources, market_data)
        missing = self._merge_missing(stock, institutional, freight, news, fundamentals, announcements)
        missing.extend(self._engine_missing(market_data))
        missing = self._soften_resolved_missing(missing, market_data)
        freshness = self._data_freshness(market_data["stock"])
        if freshness.get("warning"):
            missing.append(f"Data Warning: {freshness['warning']}")
        if mode == "personalized" and profile is None:
            missing.append("Data Missing: user_profile.yaml could not be loaded; Personalized Mode cannot use profile constraints.")

        payload = {
            "symbol": symbol,
            "mode": mode,
            "timestamp": started.isoformat(),
            "selected_model": model or "",
            "market_data": market_data,
            "data_status": data_status,
            "data_freshness": freshness,
            "sources": sources,
            "missing": self._dedupe_strings(missing),
        }
        scores = self._score(payload)
        payload["local_scores"] = scores
        data_quality = build_data_quality(payload, scores)
        truthfulness = build_truthfulness(payload, data_quality)
        scores = self._apply_truthfulness(scores, payload, truthfulness)
        payload["local_scores"] = scores
        data_quality = build_data_quality(payload, scores)
        truthfulness = build_truthfulness(payload, data_quality)
        gap_report = build_gap_report(payload, data_quality)

        position_advice = self._position_advice(symbol, market_data, profile, scores) if mode == "personalized" else self._general_position_advice()
        action_plan = self._action_plan(mode, market_data, scores, position_advice)
        summary = self._summary(symbol, market_data, scores, freshness, mode, position_advice, action_plan, "pending", truthfulness)

        prompt_payload = payload | {
            "local_scores": scores,
            "data_quality": data_quality,
            "truthfulness": truthfulness,
            "gap_report": gap_report,
            "summary": summary,
            "position_advice": position_advice,
            "action_plan": action_plan,
        }
        fallback = self._compose_report(symbol, mode, prompt_payload, position_advice, action_plan, summary)
        prompt = build_analysis_prompt(prompt_payload, profile)
        ai_result = self.ai.analyze(prompt, fallback, model=model)

        ended = datetime.now(TZ)
        elapsed_seconds = round((ended - started).total_seconds(), 2)
        summary = self._summary(
            symbol,
            market_data,
            scores,
            freshness,
            mode,
            position_advice,
            action_plan,
            ai_result["analysis_mode"],
            truthfulness,
            ai_result.get("openai_error", ""),
        )
        if ai_result["analysis_mode"] == "openai":
            final_report = (ai_result.get("markdown") or "").strip()
            if not final_report:
                final_report = self._compose_report(symbol, mode, prompt_payload, position_advice, action_plan, summary)
            final_report = self._prepend_engine_status(final_report, ai_result)
            final_report += self._compose_evidence_appendix(prompt_payload)
        else:
            final_report = self._compose_unavailable_report(symbol, mode, ai_result, payload, freshness, data_status, sources)
            summary["market_state"] = "無法分析"
            summary["action"] = "暫不提供操作建議"
            summary["one_line"] = "OpenAI 未成功完成分析，系統不輸出本機模板投資結論，避免誤導。"
        clean_warnings = [self._user_message(item) for item in payload["missing"]]

        result = {
            "symbol": symbol,
            "mode": mode,
            "timestamp": started.isoformat(),
            "completed_at": ended.isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "selected_model": model or "",
            "model_used": ai_result.get("model_used") or model or "",
            "data_status": data_status,
            "data_freshness": freshness,
            "market_data": market_data,
            "analysis_mode": ai_result["analysis_mode"],
            "openai_error": ai_result.get("openai_error", ""),
            "engine_status": self._engine_status(ai_result),
            "summary": summary,
            "data_quality": data_quality,
            "truthfulness": truthfulness,
            "gap_report": gap_report,
            "action_plan": action_plan,
            "missing_reasons": self._missing_reasons(data_status, clean_warnings),
            "position_advice": position_advice,
            "ai_report": final_report,
            "report_markdown": final_report,
            "sources": sources,
            "warnings": clean_warnings,
            "data_missing": [item for item in clean_warnings if item.startswith("資料不足")],
            "data_limitations": [item for item in clean_warnings if item.startswith("資料限制") or item.startswith("資料提醒")],
            "local_scores": scores,
        }
        result["prediction_record"] = record_prediction(result)
        self._save_history(result)
        return result

    def _engine_status(self, ai_result: dict[str, Any]) -> dict[str, Any]:
        mode = ai_result.get("analysis_mode")
        model = ai_result.get("model_used") or "gpt-5"
        raw_error = str(ai_result.get("openai_error") or "").strip()
        if mode == "openai":
            return {
                "status": "openai",
                "label": "OpenAI 分析成功",
                "model_used": model,
                "reason": "",
                "impact": "報告已使用 OpenAI 分析，並由系統整理成固定格式。",
            }

        reason = self._friendly_openai_error(raw_error)
        return {
            "status": "fallback",
            "label": "OpenAI 未成功，本次使用本機規則備援",
            "model_used": model,
            "reason": reason,
            "impact": "報告仍可參考即時資料與規則判斷，但語意推理、交叉解讀與投資論述品質會下降。",
        }

    def _friendly_openai_error(self, raw_error: str) -> str:
        text = raw_error.lower()
        if not raw_error:
            return "未取得明確錯誤訊息，請查看 GitHub Actions 執行紀錄。"
        if "timed out" in text or "timeout" in text or "read operation timed out" in text:
            return "OpenAI 回應逾時，可能是模型回應時間過長、網路不穩或輸入內容太大。"
        if "insufficient_quota" in text or "quota" in text or "billing" in text:
            return "OpenAI 帳號額度不足，或付款 / Billing 尚未成功啟用。"
        if "invalid_api_key" in text or "unauthorized" in text or "401" in text:
            return "OpenAI API Key 無效、填錯，或 GitHub Secret 沒有正確讀取。"
        if "model" in text and ("not found" in text or "does not exist" in text or "unsupported" in text):
            return "OpenAI 模型名稱不正確，或你的帳號目前沒有該模型權限。"
        if "rate limit" in text or "429" in text:
            return "OpenAI 呼叫次數或速率受到限制，稍後重試或提高額度後再跑。"
        return "OpenAI 呼叫失敗，詳細原因已記錄在 GitHub Actions 與資料庫欄位中。"

    def _prepend_engine_status(self, report: str, ai_result: dict[str, Any]) -> str:
        status = self._engine_status(ai_result)
        if status["status"] == "openai":
            block = f"""# 分析引擎狀態
- AI 狀態：OpenAI 分析成功
- 使用模型：{status["model_used"]}
- 報告說明：{status["impact"]}

"""
        else:
            block = f"""# 分析引擎狀態
- AI 狀態：OpenAI 未成功，本次使用本機規則備援
- 使用模型：{status["model_used"]}
- 失敗原因：{status["reason"]}
- 對報告的影響：{status["impact"]}

"""
        return block + (report or "")

    def _compose_unavailable_report(
        self,
        symbol: str,
        mode: str,
        ai_result: dict[str, Any],
        payload: dict[str, Any],
        freshness: dict[str, Any],
        data_status: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> str:
        status = self._engine_status(ai_result)
        missing = payload.get("missing") or []
        source_lines = []
        for source in sources[:12]:
            name = source.get("name") or source.get("source") or "資料來源"
            url = source.get("url") or ""
            source_lines.append(f"- {name}{f'：{url}' if url else ''}")
        if not source_lines:
            source_lines = ["- 本次未取得可列示的資料來源。"]

        missing_lines = [f"- {self._user_message(item)}" for item in missing[:10]]
        if not missing_lines:
            missing_lines = ["- 尚無額外資料缺口。"]

        status_lines = [f"- {key}：{value}" for key, value in data_status.items()]

        return f"""# 無法完成 AI 投資分析

## 分析引擎狀態
- AI 狀態：OpenAI 未成功，本次不產生投資結論
- 使用模型：{status["model_used"]}
- 失敗原因：{status["reason"]}
- 對報告的影響：系統已停止輸出本機模板的多空判斷、買賣建議與分數，避免讓使用者誤以為這是完整 AI 分析。

## 本次不提供的內容
- 今日結論
- 買進或賣出建議
- 多空裁決
- 信心分數
- 個人化部位操作建議

## 已完成的資料抓取狀態
- 股票代號：{symbol}
- 分析模式：{mode}
- 分析時間：{freshness.get("analysis_time") or payload.get("timestamp")}
- 股價資料日期：{freshness.get("price_data_date") or "資料不足"}
- 是否即時股價：{"是" if freshness.get("is_realtime_price") else "否"}
{chr(10).join(status_lines)}

## 可以怎麼處理
1. 到 GitHub Actions log 查看 `OpenAI analysis fallback` 後面的原因。
2. 確認 GitHub Secret `OPENAI_API_KEY`、`OPENAI_MODEL` 是否正確。
3. 若原因是逾時，可以改用較快模型或提高 `OPENAI_TIMEOUT_SECONDS`。
4. 若原因是額度或 Billing，請到 OpenAI Platform 檢查 Usage / Billing。
5. 修正後重新手動執行 GitHub Actions。

## 本次資料缺口
{chr(10).join(missing_lines)}

## 資料來源
{chr(10).join(source_lines)}

## 免責聲明
本次 OpenAI 未完成分析，因此系統不提供投資建議。以上僅列出資料抓取狀態與排錯方向。
"""

    def _compose_evidence_appendix(self, payload: dict[str, Any]) -> str:
        market = payload.get("market_data") or {}
        stock = market.get("stock") or {}
        freight = market.get("freight") or {}
        freight_intel = market.get("freight_intelligence") or freight.get("intelligence") or {}
        institutional = market.get("institutional") or {}
        fundamentals = market.get("fundamentals") or {}
        etf = market.get("etf_flow") or {}
        red_sea = market.get("red_sea_intelligence") or market.get("red_sea") or {}
        regime = market.get("market_regime") or {}
        intl = market.get("international_events") or {}
        freshness = payload.get("data_freshness") or {}
        scores = (payload.get("local_scores") or {}).get("revised_score") or {}
        truth = payload.get("truthfulness") or {}

        def val(value: Any, suffix: str = "") -> str:
            if value is None or value == "":
                return "資料不足"
            if isinstance(value, float):
                text = f"{value:.2f}".rstrip("0").rstrip(".")
            else:
                text = str(value)
            return f"{text}{suffix}"

        def trend(value: Any) -> str:
            mapping = {
                "up": "上升",
                "down": "下降",
                "flat": "持平",
                "unknown": "資料不足",
                "bullish": "偏多",
                "bearish": "偏空",
                "neutral": "中性",
                "risk_on": "風險偏好",
                "risk_off": "風險趨避",
                "inferred_bullish": "推論偏多",
                "inferred_bearish": "推論偏空",
                "escalating": "升溫",
                "stable": "穩定",
                "improving": "改善",
                "normalizing": "正常化",
            }
            return mapping.get(str(value), val(value))

        latest_inst = institutional.get("latest") or {}
        flow = institutional.get("flow_sums") or {}
        technical = stock.get("technical") or {}
        oil = (intl.get("oil_prices") or {})
        wti = oil.get("wti") or {}
        brent = oil.get("brent") or {}
        sources = payload.get("sources") or []
        source_lines = []
        for source in sources[:14]:
            name = source.get("name") or source.get("source") or "資料來源"
            url = source.get("url") or ""
            source_lines.append(f"- {name}{f'：{url}' if url else ''}")
        if not source_lines:
            source_lines = ["- 本次沒有可列示的資料來源。"]

        warnings = [self._user_message(item) for item in payload.get("missing", [])[:12]]
        warning_lines = [f"- {item}" for item in warnings] or ["- 尚無主要資料缺口。"]

        return f"""

---

## 事實依據摘要

### 資料新鮮度
- 分析時間：{val(freshness.get("analysis_time") or payload.get("timestamp"))}
- 股價資料日期：{val(freshness.get("price_data_date"))}
- 是否即時股價：{"是" if freshness.get("is_realtime_price") else "否"}
- 是否收盤資料：{"是" if freshness.get("is_closing_price") else "否"}
- 新鮮度提醒：{val(freshness.get("warning"))}

### 股價與技術面
- 收盤 / 最新價：{val(stock.get("close"))}
- 成交量：{val(stock.get("volume"))}
- 20 日均線：{val(stock.get("ma20"))}
- 60 日均線：{val(stock.get("ma60"))}
- RSI：{val(technical.get("rsi14"))}
- MACD：{val(technical.get("macd"))}
- 20 日支撐：{val(stock.get("support_20d"))}
- 20 日壓力：{val(stock.get("resistance_20d"))}

### 運價與航運景氣
- SCFI 最新值：{val(freight.get("scfi_latest"))}
- SCFI 週變化：{val(freight.get("weekly_change"), "%")}
- SCFI 連續週數：{val(freight.get("scfi_streak_weeks"))}
- 美西線運價：{val(freight.get("us_west"))}
- 美西線週變化：{val(freight.get("us_west_weekly_change"), "%")}
- 美東線運價：{val(freight.get("us_east"))}
- 歐洲線運價：{val(freight.get("europe"))}
- 運價方向推論：{trend(freight_intel.get("overall_trend"))}
- 運價強度：{trend(freight_intel.get("strength"))}
- 運價信心：{val(freight_intel.get("confidence"))}
- 來源數量：{val(freight_intel.get("source_count"))}

### 法人籌碼
- 最新日期：{val(latest_inst.get("date"))}
- 外資：{val(latest_inst.get("foreign"))}
- 投信：{val(latest_inst.get("trust"))}
- 自營商：{val(latest_inst.get("dealer"))}
- 三大法人合計：{val(latest_inst.get("total"))}
- 外資 1 / 3 / 5 / 10 日：{val((flow.get("foreign") or {}).get("1d"))} / {val((flow.get("foreign") or {}).get("3d"))} / {val((flow.get("foreign") or {}).get("5d"))} / {val((flow.get("foreign") or {}).get("10d"))}
- 投信 1 / 3 / 5 / 10 日：{val((flow.get("trust") or {}).get("1d"))} / {val((flow.get("trust") or {}).get("3d"))} / {val((flow.get("trust") or {}).get("5d"))} / {val((flow.get("trust") or {}).get("10d"))}

### 基本面
- EPS：{val(fundamentals.get("eps"))}
- 本益比：{val(fundamentals.get("per"))}
- 股價淨值比：{val(fundamentals.get("pbr"))}
- 股利殖利率：{val(fundamentals.get("dividend_yield"), "%")}
- 月營收年增率：{val(fundamentals.get("monthly_revenue_yoy"), "%")}

### 事件與風險
- ETF 被動買盤：{trend(etf.get("etf_flow"))}，信心 {val(etf.get("confidence"))}
- 紅海 / 蘇伊士狀態：{trend(red_sea.get("status"))}，航運影響 {trend(red_sea.get("shipping_impact"))}，信心 {val(red_sea.get("confidence"))}
- 市場環境：{trend(regime.get("market_regime"))}，台股 {trend(regime.get("taiwan_market"))}，航運類股 {trend(regime.get("shipping_sector"))}，信心 {val(regime.get("confidence"))}
- WTI 油價：{val(wti.get("close"))}，近 5 日變化 {val(wti.get("change_5d_pct"), "%")}
- Brent 油價：{val(brent.get("close"))}，近 5 日變化 {val(brent.get("change_5d_pct"), "%")}

### 系統交叉檢查分數
- 方向分數：{val(scores.get("direction_score"))}
- 時機分數：{val(scores.get("timing_score"))}
- 估值分數：{val(scores.get("valuation_score"))}
- 風險分數：{val(scores.get("risk_score"))}
- 資料覆蓋率：{val(scores.get("data_coverage"), "%")}
- 資料可信度：{val(scores.get("truthfulness_score") or truth.get("truthfulness_score"))}/100
- 綜合分數：{val(scores.get("overall_score"))}

### 資料限制
{chr(10).join(warning_lines)}

### 資料來源
{chr(10).join(source_lines)}
"""

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        if not HISTORY_FILE.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-limit:][::-1]

    def _user_message(self, text: str) -> str:
        value = str(text or "")
        replacements = {
            "Data Missing: ": "資料不足：",
            "Data Limitation: ": "資料限制：",
            "Data Warning: ": "資料提醒：",
            "Announcement Intelligence": "公告情報",
            "ETF Flow": "ETF 被動買盤",
            "holding_change / AUM_change": "持股變化與基金規模變化",
            "holding_change": "持股變化",
            "AUM_change": "基金規模變化",
            "stale_event_over_14_days": "事件超過 14 日，僅能當背景",
            "recent_event_within_7_days": "近 7 日有事件",
            "today_material_event": "今日有重大事件",
            "fetch_failed": "抓取失敗",
            "unknown": "未知",
            "Yahoo Finance": "Yahoo Finance",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        value = value.replace("Data Limitation: ", "資料限制：")
        value = value.replace("Data Missing: ", "資料不足：")
        value = value.replace("資料限制：ETF 被動買盤: 資料限制：", "資料限制：ETF 被動買盤：")
        value = value.replace("資料限制：資料限制：", "資料限制：")
        value = value.replace("： ", "：")
        return value

    def _save_history(self, result: dict[str, Any]) -> None:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": result["timestamp"],
            "completed_at": result["completed_at"],
            "elapsed_seconds": result["elapsed_seconds"],
            "symbol": result["symbol"],
            "mode": result["mode"],
            "model_used": result.get("model_used"),
            "analysis_mode": result["analysis_mode"],
            "price_data_date": result["data_freshness"].get("price_data_date"),
            "market_state": result["summary"].get("market_state"),
            "action": result["summary"].get("action"),
            "buy_advice": result["summary"].get("buy_advice"),
            "sell_advice": result["summary"].get("sell_advice"),
            "coverage_adjusted_score": result["local_scores"].get("coverage_adjusted_score"),
            "data_coverage": result["local_scores"].get("data_coverage"),
            "truthfulness_score": result.get("truthfulness", {}).get("truthfulness_score"),
            "revised_score": result["local_scores"].get("revised_score", {}),
            "prediction_id": result.get("prediction_record", {}).get("prediction_id"),
            "missing_count": len(result.get("data_missing", [])),
            "limitation_count": len(result.get("data_limitations", [])),
        }
        with HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_profile(self) -> dict[str, Any] | None:
        try:
            with (ROOT / "user_profile.yaml").open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        except Exception:
            return None

    def _merge_sources(self, *parts: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()
        for part in parts:
            for source in part.get("sources", []):
                key = (source.get("name"), source.get("url"))
                if key not in seen:
                    seen.add(key)
                    out.append(source)
        return out

    def _merge_engine_sources(self, sources: list[dict[str, Any]], market_data: dict[str, Any]) -> list[dict[str, Any]]:
        extra: list[dict[str, Any]] = []
        for key in ("etf_flow", "red_sea", "announcement_intelligence", "market_regime", "international_events"):
            extra.extend(market_data.get(key, {}).get("sources", []) or [])
        return self._dedupe_sources(sources + extra)

    def _dedupe_sources(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, Any]] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            key = (row.get("name"), row.get("url"))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    def _merge_missing(self, *parts: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for part in parts:
            out.extend(part.get("missing", []))
        return out

    def _engine_missing(self, market_data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        if market_data.get("etf_flow", {}).get("missing_reason"):
            etf = market_data["etf_flow"]
            prefix = "Data Limitation" if etf.get("etf_flow") != "unknown" else "Data Missing"
            out.append(f"{prefix}: ETF Flow：{etf['missing_reason']}")
        if market_data.get("announcement_intelligence", {}).get("missing_reason"):
            ann = market_data["announcement_intelligence"]
            if ann.get("latest_event") != "stale_event_over_14_days":
                prefix = "Data Limitation" if ann.get("latest_event") in {"fetch_failed", "unknown"} else "Data Missing"
                out.append(f"{prefix}: Announcement Intelligence：{ann['missing_reason']}")
        if market_data.get("red_sea", {}).get("status") == "unknown":
            out.append("Data Missing: 紅海/蘇伊士航運狀態不足，不能判斷繞航支撐是否持續。")
        regime = market_data.get("market_regime", {})
        if regime.get("market_regime") == "unknown" and not regime.get("market_snapshot"):
            out.append("Data Missing: 市場環境資料不足，risk-on/risk-off 信心偏低。")
        elif regime.get("missing_reason"):
            out.append(regime["missing_reason"])
        news_missing = market_data.get("news_relevance", {}).get("missing_reason")
        if news_missing:
            out.append(news_missing)
        return out

    def _soften_resolved_missing(self, missing: list[str], market_data: dict[str, Any]) -> list[str]:
        freight_intel = market_data.get("freight", {}).get("intelligence") or {}
        announcement_intel = market_data.get("announcement_intelligence", {}) or {}
        softened: list[str] = []
        for item in missing:
            if "US West / US East / Europe route freight rates unavailable" in item and freight_intel.get("confidence", 0) >= 0.55:
                softened.append(
                    "Data Limitation: 美西/美東/歐洲線 exact rate 缺漏；已由 Freight Intelligence 以多來源推論運價方向，不能當精確運價。"
                )
                continue
            if "data/scfi_routes.csv not found or empty" in item and freight_intel.get("confidence", 0) >= 0.55:
                softened.append(
                    "Data Limitation: data/scfi_routes.csv 未提供；目前使用搜尋/頁面/新聞推論運價趨勢。"
                )
                continue
            if (
                item.startswith("Data Missing: MOPS/TWSE")
                or "no matching MOPS/TWSE company announcements" in item
                or "company announcements are not connected yet" in item
                or "investor conference materials are not connected yet" in item
            ):
                status = announcement_intel.get("latest_event", "unknown")
                softened.append(
                    f"Data Limitation: 官方公告/法說資料未穩定串接或抓取受限；Announcement Intelligence 目前分類為 {status}，不可解讀為無公告。"
                )
                continue
            softened.append(item)
        return self._dedupe_strings(softened)

    def _data_freshness(self, stock: dict[str, Any]) -> dict[str, Any]:
        analysis_time = datetime.now(TZ)
        price_date = stock.get("latest_date")
        today = analysis_time.date().isoformat()
        warning = ""
        is_realtime = bool(stock.get("is_realtime_price"))
        if is_realtime:
            warning = ""
        elif price_date and price_date != today:
            warning = "價格資料非今日，僅供參考，不適合即時決策。若為前一交易日資料，請以收盤資料解讀。"
        if not price_date:
            warning = "Data Missing: 無法確認股價資料日期。"
        return {
            "analysis_time": analysis_time.isoformat(timespec="seconds"),
            "price_data_date": price_date or "Data Missing",
            "is_realtime_price": is_realtime,
            "is_closing_price": not is_realtime,
            "realtime_time": stock.get("realtime_time"),
            "realtime_source": stock.get("realtime_source"),
            "warning": warning,
        }

    def _score(self, payload: dict[str, Any]) -> dict[str, Any]:
        market = payload["market_data"]
        stock = market["stock"]
        inst = market["institutional"]
        freight = market["freight"]
        news = market["news"]
        fundamentals = market["fundamentals"]
        technical = stock.get("technical") or {}

        close, ma20, ma60 = stock.get("close"), stock.get("ma20"), stock.get("ma60")
        technical_score = 50
        if close is not None and ma20 is not None:
            technical_score += 10 if close > ma20 else -8
        if close is not None and ma60 is not None:
            technical_score += 8 if close > ma60 else -6
        if technical.get("rsi14") and technical["rsi14"] > 75:
            technical_score -= 8
        if technical.get("macd_histogram") is not None:
            technical_score += 5 if technical["macd_histogram"] > 0 else -5

        institutional_score = 50
        total_streak = inst.get("consecutive_trend", {})
        if total_streak.get("direction") == "buy":
            institutional_score += min(20, total_streak.get("days", 0) * 4)
        elif total_streak.get("direction") == "sell":
            institutional_score -= min(20, total_streak.get("days", 0) * 4)
        if inst.get("suspicious_zero_data"):
            institutional_score -= 4

        freight_intel = freight.get("intelligence") or {}
        freight_score = self._freight_score(freight)
        fundamental_score = 50 + (10 if (fundamentals.get("monthly_revenue_yoy") or 0) > 0 else 0) + (5 if fundamentals.get("eps") is not None else 0)
        sentiment_score = 42 if not news.get("articles") else 55
        risk_score = 50 - (10 if total_streak.get("direction") == "sell" and total_streak.get("days", 0) >= 3 else 0)

        coverage_items = {
            "stock": bool(stock.get("close")),
            "institutional": bool(inst.get("latest")),
            "freight": bool(freight_score is not None and (freight.get("scfi_latest") is not None or freight_intel.get("confidence", 0) >= 0.6)),
            "news": bool((market.get("news_relevance") or {}).get("articles")),
            "fundamental": fundamentals.get("eps") is not None,
            "announcements": market.get("announcement_intelligence", {}).get("latest_event") not in {"fetch_failed", "unknown"},
            "etf": bool(
                market.get("etf_flow", {}).get("holding_change") is not None
                or market.get("etf_flow", {}).get("aum_change") is not None
                or market.get("etf_flow", {}).get("coverage_credit", 0) >= 0.35
            ),
            "red_sea": bool(market.get("red_sea", {}).get("confidence", 0) >= 0.45),
            "market_regime": bool(market.get("market_regime", {}).get("confidence", 0) >= 0.3 or market.get("market_regime", {}).get("coverage_credit", 0) >= 0.5),
        }
        coverage_values = {
            key: 1.0 if ok else 0.0
            for key, ok in coverage_items.items()
        }
        etf = market.get("etf_flow", {})
        if coverage_items["etf"] and etf.get("holding_change") is None and etf.get("aum_change") is None:
            coverage_values["etf"] = min(0.5, float(etf.get("coverage_credit") or 0.5))
        regime = market.get("market_regime", {})
        if coverage_items["market_regime"] and regime.get("confidence", 0) < 0.5:
            coverage_values["market_regime"] = min(0.5, float(regime.get("coverage_credit") or 0.5))
        data_coverage = round(sum(coverage_values.values()) / len(coverage_values) * 100)
        component_values = [technical_score, institutional_score, fundamental_score, sentiment_score, risk_score]
        if freight_score is not None:
            component_values.append(freight_score)
        raw_score = self._clamp(sum(component_values) / len(component_values))
        revised = self._revised_score(payload, {
            "technical_score": technical_score,
            "institutional_score": institutional_score,
            "fundamental_score": fundamental_score,
            "sentiment_score": sentiment_score,
            "risk_score": risk_score,
            "freight_score": freight_score,
            "data_coverage": data_coverage,
        })
        risk_level = "Low Risk" if revised["risk_score"] >= 65 else "High Risk" if revised["risk_score"] <= 45 else "Medium Risk"
        return {
            "raw_score": raw_score,
            "data_coverage": data_coverage,
            "coverage_adjusted_score": revised["overall_score"],
            "conviction_score": revised["overall_score"],
            "revised_score": revised,
            "market_state": "pending",
            "risk_level": risk_level,
            "freight_score": freight_score,
            "institutional_score": self._clamp(institutional_score),
            "fundamental_score": self._clamp(fundamental_score),
            "technical_score": self._clamp(technical_score),
            "sentiment_score": self._clamp(sentiment_score),
            "risk_score": self._clamp(risk_score),
            "coverage_items": coverage_items,
        }

    def _revised_score(self, payload: dict[str, Any], base: dict[str, Any]) -> dict[str, int]:
        market = payload["market_data"]
        stock = market["stock"]
        technical = stock.get("technical") or {}
        etf = market.get("etf_flow", {})
        red_sea = market.get("red_sea", {})
        regime = market.get("market_regime", {})
        announcement = market.get("announcement_intelligence", {})
        freight_score = base.get("freight_score")

        etf_component = 50
        if etf.get("etf_flow") == "bullish":
            etf_component = 65
        elif etf.get("etf_flow") == "inferred_bullish":
            etf_component = 52
        elif etf.get("etf_flow") in {"bearish", "inferred_bearish"}:
            etf_component = 42

        direction = self._clamp(
            0.30 * (freight_score if freight_score is not None else 45)
            + 0.22 * base["institutional_score"]
            + 0.18 * base["fundamental_score"]
            + 0.16 * base["technical_score"]
            + 0.09 * base["sentiment_score"]
            + 0.05 * etf_component
        )
        timing = 55
        close, ma20 = stock.get("close"), stock.get("ma20")
        if close is not None and ma20 is not None:
            timing += 8 if close >= ma20 else -8
        if technical.get("rsi14") and technical["rsi14"] > 75:
            timing -= 18
        if close is not None and close >= 245:
            timing -= 10

        valuation = 50
        if market["fundamentals"].get("dividend_yield"):
            valuation += min(18, float(market["fundamentals"]["dividend_yield"]) * 2)
        if market["fundamentals"].get("per") and float(market["fundamentals"]["per"]) < 12:
            valuation += 8

        risk = base["risk_score"]
        if red_sea.get("status") == "normalizing":
            risk -= 12
        elif red_sea.get("shipping_impact") == "high":
            risk += 4
        if regime.get("market_regime") == "risk_off":
            risk -= 10
        if announcement.get("materiality") == "high" and announcement.get("latest_event") in {"today_material_event", "recent_event_within_7_days"}:
            risk -= 8
        if etf.get("stale") and etf.get("holding_change") is None and etf.get("aum_change") is None:
            risk -= 3

        data_coverage = base["data_coverage"]
        overall = self._clamp(0.34 * direction + 0.18 * self._clamp(timing) + 0.18 * self._clamp(valuation) + 0.15 * self._clamp(risk) + 0.15 * data_coverage)
        return {
            "direction_score": direction,
            "timing_score": self._clamp(timing),
            "valuation_score": self._clamp(valuation),
            "risk_score": self._clamp(risk),
            "data_coverage": data_coverage,
            "overall_score": overall,
        }

    def _apply_truthfulness(self, scores: dict[str, Any], payload: dict[str, Any], truthfulness: dict[str, Any]) -> dict[str, Any]:
        scores = dict(scores)
        revised = dict(scores["revised_score"])
        truth_score = int(truthfulness.get("truthfulness_score") or 0)
        revised["truthfulness_score"] = truth_score
        if truth_score < 75:
            revised["overall_score"] = min(revised["overall_score"], 64)
        if truth_score < 60:
            revised["overall_score"] = min(revised["overall_score"], 54)
        scores["revised_score"] = revised
        scores["coverage_adjusted_score"] = revised["overall_score"]
        scores["conviction_score"] = revised["overall_score"]
        scores["market_state"] = self._market_state(revised, payload, truthfulness)
        scores["risk_level"] = "Low Risk" if revised["risk_score"] >= 65 else "High Risk" if revised["risk_score"] <= 45 else "Medium Risk"
        return scores

    def _market_state(self, revised: dict[str, int], payload: dict[str, Any], truthfulness: dict[str, Any]) -> str:
        overall = revised["overall_score"]
        timing = revised["timing_score"]
        risk = revised["risk_score"]
        regime = payload["market_data"].get("market_regime", {})
        truth = truthfulness.get("truthfulness_score", 0)
        p0_missing = truthfulness.get("p0_missing_count", 0)
        if truth < 50 or p0_missing >= 3:
            return "Insufficient Data / 資料不足"
        if truth < 60:
            return "Neutral-Bullish / 中性偏多" if revised["direction_score"] >= 55 and p0_missing == 0 else "Neutral / 中性"
        if overall < 40:
            return "Bearish / 偏空"
        if overall < 65:
            return "Neutral-Bullish / 中性偏多" if revised["direction_score"] >= 55 else "Neutral / 中性"
        if timing < 50 or risk < 50 or regime.get("confidence", 0) < 0.5 or truth < 75:
            return "Neutral-Bullish / 中性偏多"
        return "Bullish / 偏多"

    def _position_advice(self, symbol: str, market_data: dict[str, Any], profile: dict[str, Any] | None, scores: dict[str, Any]) -> dict[str, Any]:
        stock = market_data["stock"]
        close, ma20 = stock.get("close"), stock.get("ma20")
        position = next((p for p in (profile or {}).get("positions", []) if p.get("symbol") == symbol), None)
        if not position or close is None:
            return {"available": False, "reason": "user_profile.yaml has no matching position or price is missing."}
        lots = position.get("lots", 0)
        avg = position.get("average_cost", 0)
        pnl = (close - avg) * lots * 1000
        risk_score = scores.get("revised_score", {}).get("risk_score", 50)
        sell = 0
        recommendation = "不動"
        if risk_score < 50:
            recommendation = "警戒但不主動賣核心；先確認 SCFI、法人與市場風險。"
        elif close >= 270:
            sell, recommendation = 3, "重新評估，可考慮再減 3~5 張機動部位。"
        elif close >= 255:
            sell, recommendation = 3, "可考慮賣 3~5 張機動部位。"
        elif close >= 245:
            sell, recommendation = 2, "可考慮賣 2~3 張機動部位。"
        elif close < (ma20 or 0) and (market_data["freight"].get("weekly_change") or 0) < 0:
            recommendation = "跌破 20MA 且運價轉弱，提高警戒。"
        return {
            "available": True,
            "lots": lots,
            "average_cost": avg,
            "unrealized_pnl": pnl,
            "core_lots": position.get("core_lots"),
            "flexible_lots": position.get("flexible_lots"),
            "recommendation": recommendation,
            "suggested_sell_lots": sell,
            "sell_today": sell > 0,
            "if_245_250": "可考慮賣 2~3 張機動部位，不動核心部位。",
            "if_255_260": "可考慮再賣 3~5 張，觀察運價與法人是否同步支持。",
            "if_220_230": "若基本面與 SCFI 未轉弱，可觀察買回機動部位。",
            "if_below_20ma": "若跌破 20MA 且 SCFI/法人同步轉弱，提高警戒並降低機動部位。",
        }

    def _general_position_advice(self) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "General Mode 不讀取持股，只提供通用買賣區間與風險條件。",
        }

    def _action_plan(self, mode: str, market_data: dict[str, Any], scores: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
        stock = market_data["stock"]
        close, ma20 = stock.get("close"), stock.get("ma20")
        freight_intel = market_data["freight"].get("intelligence") or {}
        risk = scores.get("revised_score", {}).get("risk_score", 50)
        timing = scores.get("revised_score", {}).get("timing_score", 50)
        direction = scores.get("revised_score", {}).get("direction_score", 50)
        buy_zone = "220~230：若基本面與運價未轉弱，可觀察買回；200~220：需重新檢查循環風險。"
        sell_zone = "245~250：可考慮機動減碼；255~260：再評估減碼；270 以上：重新評估風險報酬。"
        lots = 0
        if risk < 50:
            action = "警戒但不積極操作"
        elif timing < 50 and direction >= 55:
            action = "方向偏多，但短線不適合追"
        elif mode == "personalized" and position.get("available"):
            action = position["recommendation"]
            lots = position.get("suggested_sell_lots", 0)
        elif close is not None and close >= 245:
            action = "偏高區，不追價；若已有持股可分批檢視賣點。"
        elif close is not None and ma20 is not None and close < ma20:
            action = "等待重新站回 20MA 或資料轉強。"
        else:
            action = "不動，等待價格到賣點或回檔買回區。"
        data_caution = ""
        if scores.get("freight_score") is None or freight_intel.get("confidence", 0) < 0.55:
            data_caution = "航運核心資料信心不足，對長榮判斷信心下降。"
        return {
            "recommendation": action,
            "suggested_lots": lots,
            "buy_advice": buy_zone,
            "sell_advice": sell_zone,
            "reason": "綜合股價位置、20MA、法人籌碼、運價情報、ETF/紅海/市場環境與資料真實度。",
            "trigger": "245~250、255~260、270 以上、跌破 20MA、SCFI/法人同步轉弱。",
            "invalidated_by": "SCFI 轉弱、外資與投信同步賣超、紅海風險正常化、跌破 20MA 且放量。",
            "next_sell_point": "245~250；若站穩再看 255~260。",
            "next_buyback_point": "220~230；若基本面未轉弱才考慮。",
            "view_change_conditions": "SCFI 連跌、外資/投信/ETF 同步轉弱、跌破 20MA、紅海恢復正常導致運價支撐消失。",
            "data_caution": data_caution,
        }

    def _summary(
        self,
        symbol: str,
        market_data: dict[str, Any],
        scores: dict[str, Any],
        freshness: dict[str, Any],
        mode: str,
        position: dict[str, Any],
        action_plan: dict[str, Any],
        analysis_mode: str,
        truthfulness: dict[str, Any],
        error: str = "",
    ) -> dict[str, Any]:
        revised = scores.get("revised_score", {})
        timing_note = "方向偏多，但短線不適合追。" if revised.get("timing_score", 100) < 50 and revised.get("direction_score", 0) >= 55 else ""
        gaps = [key for key, ok in scores["coverage_items"].items() if not ok]
        note = "OpenAI analysis completed successfully." if analysis_mode == "openai" else "Fallback Mode：OpenAI 未成功完成，本報告由本機規則與即時資料產生。"
        if error:
            note += f" Error: {error}"
        return {
            "market_state": scores["market_state"],
            "conviction_score": scores["coverage_adjusted_score"],
            "raw_score": scores["raw_score"],
            "data_coverage": scores["data_coverage"],
            "truthfulness_score": truthfulness.get("truthfulness_score"),
            "risk_level": scores["risk_level"],
            "action": action_plan["recommendation"],
            "buy_advice": action_plan["buy_advice"],
            "sell_advice": action_plan["sell_advice"],
            "suggested_lots": action_plan["suggested_lots"],
            "one_line": f"{symbol} 目前為 {scores['market_state']}，資料覆蓋率 {scores['data_coverage']}%，真實度 {truthfulness.get('truthfulness_score')}/100，綜合分數 {scores['coverage_adjusted_score']}/100。{timing_note}",
            "primary_risk": freshness.get("warning") or action_plan.get("data_caution") or "SCFI、ETF、紅海或市場環境資料仍可能不完整。",
            "key_data_gaps": gaps,
            "analysis_mode_note": note,
        }

    def _missing_reasons(self, data_status: dict[str, str], missing: list[str]) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        if data_status.get("freight") in {"partial", "missing", "inferred_from_search"}:
            reasons.append({
                "category": "SCFI / Freight",
                "status": data_status.get("freight", "missing"),
                "reason": "Exact route-level freight prices are incomplete. Freight Intelligence may infer direction, but it is not exact data.",
                "how_to_fix": "Use official/paid freight source, stable DOM table, or verified CSV import.",
            })
        if data_status.get("news") == "missing":
            reasons.append({"category": "News", "status": "missing", "reason": "NewsAPI/RSS returned no usable news.", "how_to_fix": "Check NEWS_API_KEY or RSS/network access."})
        if data_status.get("announcements") == "missing":
            reasons.append({"category": "Company announcements", "status": "missing", "reason": "TWSE/MOPS returned no usable current announcements.", "how_to_fix": "Retry official sources; fetch failure is unknown, not no-event."})
        return reasons

    def _freight_score(self, freight: dict[str, Any]) -> int | None:
        intel = freight.get("intelligence") or {}
        trend = intel.get("overall_trend")
        confidence = float(intel.get("confidence") or 0.0)
        strength = intel.get("strength")
        if trend == "unknown" or confidence < 0.4:
            return None
        base = 50
        if trend == "up":
            base += 12
        elif trend == "down":
            base -= 12
        if strength == "strong":
            base += 8 if trend == "up" else -8 if trend == "down" else 0
        elif strength == "moderate":
            base += 4 if trend == "up" else -4 if trend == "down" else 0
        if intel.get("weeks_up_or_down") and int(intel["weeks_up_or_down"]) >= 3:
            base += 5 if trend == "up" else -5 if trend == "down" else 0
        if freight.get("scfi_latest") is not None:
            base += 3
        if not intel.get("exact_route_count"):
            base -= 3
        return self._clamp(base * (0.75 + 0.25 * confidence))

    def _compose_report(
        self,
        symbol: str,
        mode: str,
        payload: dict[str, Any],
        position: dict[str, Any],
        action_plan: dict[str, Any],
        summary: dict[str, Any],
        top_only: bool = False,
    ) -> str:
        market = payload["market_data"]
        stock = market["stock"]
        freight = market["freight"]
        freight_intel = freight.get("intelligence") or {}
        inst = market["institutional"]
        scores = payload["local_scores"]
        revised = scores["revised_score"]
        freshness = payload["data_freshness"]
        quality = payload["data_quality"]
        truth = payload["truthfulness"]
        gap = payload["gap_report"]
        missing_only = [item for item in payload["missing"] if item.startswith("Data Missing")]
        limitations = [item for item in payload["missing"] if item.startswith("Data Limitation") or item.startswith("Data Warning")]
        missing_text = "\n".join(f"- {item}" for item in missing_only) or "- 無重大資料缺漏。"
        limitation_text = "\n".join(f"- {item}" for item in limitations) or "- 無重大資料限制。"
        sources = "\n".join(f"- {s.get('name')}: {s.get('url')}" for s in payload["sources"]) or "- Data Missing"
        pos = position if position.get("available") else {}
        if mode == "personalized":
            position_section = f"""## 對我目前部位的建議
- 持股張數：{pos.get("lots", "Data Missing")}
- 均價：{pos.get("average_cost", "Data Missing")}
- 目前損益：{self._fmt(pos.get("unrealized_pnl")) if pos else "Data Missing"}
- 核心部位：{pos.get("core_lots", "Data Missing")}
- 機動部位：{pos.get("flexible_lots", "Data Missing")}
- 今日是否建議賣：{"是" if pos.get("sell_today") else "否"}
- 建議賣出張數：{pos.get("suggested_sell_lots", 0)}
- 若漲到 245~250：{pos.get("if_245_250", "可視為第一個分批檢視區。")}
- 若漲到 255~260：{pos.get("if_255_260", "可視為第二個分批檢視區。")}
- 若跌回 220~230：{pos.get("if_220_230", "若基本面未轉弱，可觀察買回。")}
- 若跌破 20MA：{pos.get("if_below_20ma", "提高警戒，等待重新站回或資料轉強。")}"""
        else:
            position_section = f"""## 通用買賣區間與風險條件
- General Mode 不讀取 user_profile.yaml 或使用者持股。
- 今日動作：{summary["action"]}
- 下一個賣點：{action_plan.get("next_sell_point")}
- 下一個買回點：{action_plan.get("next_buyback_point")}
- 追價限制：若 Timing Score < 50，方向偏多也不適合追價。
- 風險限制：若 Risk Score < 50，不給積極加碼或大幅買進建議。
- 改變看法的條件：{action_plan.get("view_change_conditions")}"""

        decision = f"""# 即時 AI 投資分析報告

## 重點結論
- **今日結論：{scores["market_state"]}**
- **今日動作：{summary["action"]}**
- **下一個賣點：{action_plan.get("next_sell_point")}**
- **下一個買回點：{action_plan.get("next_buyback_point")}**
- **最大風險：{summary["primary_risk"]}**

## A. 決策摘要（Decision Brief）
1. **今日結論：** {scores["market_state"]}
2. **方向：** Direction Score {revised["direction_score"]}/100。
3. **風險：** {scores["risk_level"]}，Risk Score {revised["risk_score"]}/100。
4. **今日動作：** {summary["action"]}
5. **核心部位：** {pos.get("core_lots", "General Mode 不讀取持股")}
6. **機動部位：** {pos.get("flexible_lots", "General Mode 不讀取持股")}
7. **下一個賣點：** {action_plan.get("next_sell_point")}
8. **下一個買回點：** {action_plan.get("next_buyback_point")}
9. **改變看法的條件：** {action_plan.get("view_change_conditions")}
10. **最大資料缺口：** {", ".join(summary.get("key_data_gaps") or []) or "無重大缺口"}
"""
        if top_only:
            return decision

        return decision + f"""
## B. 詳細報告（Detailed Report）

## 今日操作建議
- **建議：{action_plan["recommendation"]}**
- **建議張數：{action_plan["suggested_lots"]}**
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}

{position_section}

## 本次分析最大資料缺口
1. {self._gap_line(gap, 0)}
2. {self._gap_line(gap, 1)}
3. {self._gap_line(gap, 2)}

這些缺口如何影響結論：缺口越多，Truthfulness Score 與 Data Coverage 越低，系統會降級 Bullish/Bearish 結論。

## Freight Intelligence（運價情報）
- 整體方向：{freight_intel.get("overall_trend", "unknown")}
- 強度：{freight_intel.get("strength", "weak")}
- 信心分數：{freight_intel.get("confidence", 0)}
- 來源數量：{freight_intel.get("source_count", 0)} independent / {freight_intel.get("raw_signal_count", 0)} raw signals
- 狀態：{freight_intel.get("status", "missing")}
- 美西：{(freight_intel.get("us_west") or {}).get("trend", "unknown")} / confidence {(freight_intel.get("us_west") or {}).get("confidence", 0)}
- 美東：{(freight_intel.get("us_east") or {}).get("trend", "unknown")} / confidence {(freight_intel.get("us_east") or {}).get("confidence", 0)}
- 歐洲：{(freight_intel.get("europe") or {}).get("trend", "unknown")} / confidence {(freight_intel.get("europe") or {}).get("confidence", 0)}
- 對長榮的意義：{freight_intel.get("summary") or "Data Missing"}

{self._decision_modules_markdown(market)}

## Revised Conviction Score（修正版信心分數）
- **Direction Score：{revised["direction_score"]}**
- **Timing Score：{revised["timing_score"]}**
- **Valuation Score：{revised["valuation_score"]}**
- **Risk Score：{revised["risk_score"]}**
- **Coverage Score：{revised["data_coverage"]}**
- **Truthfulness Score：{revised.get("truthfulness_score")}**
- **Overall Score：{revised["overall_score"]}**

## Truthfulness Score（真實度分數）
- **Truthfulness Score：{truth.get("truthfulness_score")}/100**
- Exact Data Share：{truth.get("exact_data_share")}
- Scraped Data Share：{truth.get("scraped_data_share")}
- Search-Inferred Share：{truth.get("search_inferred_share")}
- Stale Data Share：{truth.get("stale_data_share")}
- Missing Data Share：{truth.get("missing_data_share")}
- Conflict Data Share：{truth.get("conflict_data_share")}
- 警告：{"；".join(truth.get("warnings") or []) or "無"}

## Market Data Snapshot（市場資料快照）
- 分析時間：{freshness.get("analysis_time")}
- 股價資料日期：{freshness.get("price_data_date")}
- 即時資料：{freshness.get("is_realtime_price")}
- 收盤資料：{freshness.get("is_closing_price")}
- 收盤價：{self._fmt(stock.get("close"))}
- 成交量：{self._fmt(stock.get("volume"))}
- 20MA：{self._fmt(stock.get("ma20"))}
- 60MA：{self._fmt(stock.get("ma60"))}
- RSI14：{self._fmt((stock.get("technical") or {}).get("rsi14"))}
- MACD：{self._fmt((stock.get("technical") or {}).get("macd"))}
- 法人 1/3/5/10 日：{inst.get("flow_sums", {})}

## Alpha Discovery（Alpha 發現）
- 市場目前最在意：SCFI/分航線運價、紅海繞航、法人籌碼與填息。
- 市場可能忽略：ETF 被動買盤若缺 exact holding，不可過度視為支撐。
- 尚未充分定價：若 Freight Intelligence 多來源一致但股價未反映，才構成 alpha。
- 過度反映：若只有搜尋推論而股價已大漲，需防假 Bullish。

## Divergence Analysis（背離分析）
- 股價 vs SCFI：{freight_intel.get("overall_trend", "unknown")}
- 股價 vs 法人籌碼：{inst.get("consecutive_trend", "Data Missing")}
- 股價 vs 基本面：月營收 YoY {self._fmt(market["fundamentals"].get("monthly_revenue_yoy"))}
- 股價 vs 新聞情緒：{len((market.get("news_relevance") or {}).get("articles") or [])} 則高相關新聞
- 股價 vs ETF 被動買盤：{market.get("etf_flow", {}).get("etf_flow", "unknown")}

## Bull vs Bear（多空辯論）
- Bull Case：運價方向若持續向上、法人賣壓減輕、股價守住 20MA，方向偏多。
- Bear Case：分航線 exact data、ETF actual holdings、公告與市場環境若缺漏，不能高信心追價。
- CIO Final Judgment：{scores["market_state"]}

## Prediction Tracker（預測追蹤）
- 本次分析會寫入 data/predictions.jsonl。
- 未來可用 `python -m backend.services.prediction_tracker --validate` 驗證 7/30/90 天結果。
- 目前結論：{summary["one_line"]}

## Data Quality（資料品質）
{self._quality_markdown(quality)}

## Data Sources（資料來源）
{sources}

## Data Missing（資料缺漏）
{missing_text}

## Data Limitations（資料限制）
{limitation_text}

## Disclaimer（免責聲明）
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。資料缺漏或搜尋推論比例偏高時，請勿做強結論。
"""

    def _decision_modules_markdown(self, market: dict[str, Any]) -> str:
        etf = market.get("etf_flow", {})
        red = market.get("red_sea", {})
        ann = market.get("announcement_intelligence", {})
        regime = market.get("market_regime", {})
        fill = market.get("fill_dividend_probability", {})
        return f"""## ETF Flow（ETF 被動買盤）
- ETF Flow：{etf.get("etf_flow", "unknown")}
- 主要 ETF：{", ".join(etf.get("top_etfs") or []) or "未取得明確 ETF 名單"}
- Holding Change：{self._fmt(etf.get("holding_change"))}
- AUM Change：{self._fmt(etf.get("aum_change"))}
- Confidence：{etf.get("confidence", 0)}
- as_of / stale：{etf.get("as_of") or "日期未知"} / {etf.get("stale")}
- 說明：{etf.get("missing_reason") or "無"}

## Red Sea Intelligence（紅海情報）
- 狀態：{red.get("status", "unknown")}
- Shipping Impact：{red.get("shipping_impact", "unknown")}
- Suez Return Risk：{red.get("suez_return_risk", "unknown")}
- Confidence：{red.get("confidence", 0)}
- 摘要：{red.get("summary") or "資料不足，僅能降低信心，不硬判。"}

## Announcement Intelligence（公告情報）
- 最新事件狀態：{ann.get("latest_event", "unknown")}
- 重大性：{ann.get("materiality", "unknown")}
- 今日重大事件：{len(ann.get("today_material_event") or [])}
- 7 日內事件：{len(ann.get("recent_event_within_7_days") or [])}
- 14 日以上舊事件：{len(ann.get("stale_event_over_14_days") or [])}
- Fetch Failed：{ann.get("fetch_failed", False)}
- Confidence：{ann.get("confidence", 0)}
- 說明：{self._announcement_label(ann)}

## Market Regime（市場環境）
- Market Regime：{regime.get("market_regime", "unknown")}
- Taiwan Market：{regime.get("taiwan_market", "neutral")}
- Shipping Sector：{regime.get("shipping_sector", "neutral")}
- Confidence：{regime.get("confidence", 0)}

## Fill Dividend Probability（填息機率）
- 30 天：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 天：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- Expected Days：{fill.get("expected_days") if fill.get("expected_days") is not None else "資料不足不硬估"}
- Confidence：{fill.get("confidence", 0)}
- historical_fill_score：{self._fmt(fill.get("historical_fill_score"))}
- freight_score：{self._fmt(fill.get("freight_score"))}
- institutional_score：{self._fmt(fill.get("institutional_score"))}
- etf_score：{self._fmt(fill.get("etf_score"))}
- technical_score：{self._fmt(fill.get("technical_score"))}
- market_regime_score：{self._fmt(fill.get("market_regime_score"))}
- Key Factors：{"；".join(fill.get("key_factors") or []) or "資料不足，填息機率不硬估。"}
- Risks：{"；".join(fill.get("risks") or []) or "資料不足時信心下降"}"""

    def _quality_markdown(self, quality: dict[str, Any]) -> str:
        sections = [
            ("精確資料（Exact Data）", quality.get("exact_data", [])),
            ("爬取資料（Scraped Data）", quality.get("scraped_data", [])),
            ("搜尋推論（Search-Inferred Data）", quality.get("search_inferred_data", [])),
            ("過舊/可疑資料（Stale / Suspicious Data）", quality.get("stale_or_suspicious_data", [])),
            ("缺漏資料（Missing Data）", quality.get("missing_data", [])),
            ("衝突資料（Conflict Data）", quality.get("conflict_data", [])),
        ]
        lines: list[str] = []
        for title, items in sections:
            lines.append(f"- {title}:")
            if items:
                lines.extend(f"  - {item}" for item in items[:8])
            else:
                lines.append("  - 無")
        return "\n".join(lines)

    def _announcement_label(self, ann: dict[str, Any]) -> str:
        if ann.get("fetch_failed"):
            return "公告抓取失敗，不能解讀為無公告。"
        if ann.get("today_material_event"):
            return "有今日重大事件，需要人工確認公告內容。"
        if ann.get("recent_event_within_7_days"):
            return "有 7 日內事件，可作為近期背景。"
        if ann.get("stale_event_over_14_days"):
            return "只有 14 日以上舊事件，不可當今日重大事件。"
        return "未確認到可用公告資料。"

    def _gap_line(self, gap_report: dict[str, Any], index: int) -> str:
        gaps = sorted(gap_report.get("gaps") or [], key=lambda item: {"P0": 0, "P1": 1, "P2": 2}.get(item.get("priority"), 9))
        if index >= len(gaps):
            return "無"
        gap = gaps[index]
        return f"{gap.get('priority')} {gap.get('field')}：{gap.get('next_action')}"

    def _pct_or_missing(self, value: Any) -> str:
        if value is None:
            return "資料不足不硬估"
        return f"{float(value) * 100:.0f}%"

    def _fmt(self, value: Any) -> str:
        if value is None:
            return "未取得 exact data"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    def _clamp(self, value: float | int) -> int:
        return int(max(0, min(100, round(float(value)))))

    def _dedupe_strings(self, items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out


def _clean_engine_missing(self: AnalysisService, market_data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    etf = market_data.get("etf_flow", {})
    if etf.get("missing_reason"):
        prefix = "Data Limitation" if etf.get("etf_flow") != "unknown" else "Data Missing"
        out.append(f"{prefix}: ETF Flow: {etf['missing_reason']}")
    ann = market_data.get("announcement_intelligence", {})
    if ann.get("missing_reason"):
        if ann.get("latest_event") != "stale_event_over_14_days":
            prefix = "Data Limitation" if ann.get("latest_event") in {"fetch_failed", "unknown"} else "Data Missing"
            out.append(f"{prefix}: Announcement Intelligence: {ann['missing_reason']}")
    red = market_data.get("red_sea", {})
    if red.get("status") == "unknown":
        out.append("Data Missing: 紅海/蘇伊士航運狀態不足，不能判斷繞航支撐是否持續。")
    regime = market_data.get("market_regime", {})
    if regime.get("market_regime") == "unknown" and not regime.get("market_snapshot"):
        out.append("Data Missing: 市場環境資料不足，risk-on/risk-off 信心偏低。")
    elif regime.get("missing_reason"):
        out.append(regime["missing_reason"])
    news_missing = market_data.get("news_relevance", {}).get("missing_reason")
    if news_missing:
        out.append(news_missing)
    return out


def _clean_soften_resolved_missing(self: AnalysisService, missing: list[str], market_data: dict[str, Any]) -> list[str]:
    freight_intel = market_data.get("freight", {}).get("intelligence") or {}
    announcement_intel = market_data.get("announcement_intelligence", {}) or {}
    softened: list[str] = []
    for item in missing:
        if "US West / US East / Europe route freight rates unavailable" in item and freight_intel.get("confidence", 0) >= 0.55:
            softened.append("Data Limitation: 美西/美東/歐洲線 exact rate 未完整，但 Freight Intelligence 已由多來源推論方向；不可當成官方精確運價。")
            continue
        if "data/scfi_routes.csv not found or empty" in item and freight_intel.get("confidence", 0) >= 0.55:
            softened.append("Data Limitation: data/scfi_routes.csv 尚未匯入，但已使用搜尋/公開頁/新聞 fallback 補足運價方向。")
            continue
        if item.startswith("Data Missing: MOPS/TWSE") or "no matching MOPS/TWSE company announcements" in item or "company announcements are not connected yet" in item or "investor conference materials are not connected yet" in item:
            status = announcement_intel.get("latest_event", "unknown")
            softened.append(f"Data Limitation: 官方公告/法說資料仍需交叉確認；Announcement Intelligence 目前分類為 {status}，不可解讀為無公告。")
            continue
        softened.append(item)
    return self._dedupe_strings(softened)


def _clean_data_freshness(self: AnalysisService, stock: dict[str, Any]) -> dict[str, Any]:
    analysis_time = datetime.now(TZ)
    price_date = stock.get("latest_date")
    today = analysis_time.date().isoformat()
    warning = ""
    if price_date and price_date != today:
        warning = "價格資料非今日，僅供參考，不適合即時決策。若為前一交易日資料，請以收盤資料解讀。"
    if not price_date:
        warning = "Data Missing: 無法確認股價資料日期。"
    return {
        "analysis_time": analysis_time.isoformat(timespec="seconds"),
        "price_data_date": price_date or "Data Missing",
        "is_realtime_price": False,
        "is_closing_price": True,
        "warning": warning,
    }


def _clean_market_state(self: AnalysisService, revised: dict[str, int], payload: dict[str, Any], truthfulness: dict[str, Any]) -> str:
    overall = revised["overall_score"]
    timing = revised["timing_score"]
    risk = revised["risk_score"]
    regime = payload["market_data"].get("market_regime", {})
    truth = truthfulness.get("truthfulness_score", 0)
    p0_missing = truthfulness.get("p0_missing_count", 0)
    if truth < 50 or p0_missing >= 3:
        return "Insufficient Data / 資料不足"
    if truth < 60:
        return "Neutral-Bullish / 中性偏多" if revised["direction_score"] >= 55 and p0_missing == 0 else "Neutral / 中性"
    if overall < 40:
        return "Bearish / 偏空"
    if overall < 65:
        return "Neutral-Bullish / 中性偏多" if revised["direction_score"] >= 55 else "Neutral / 中性"
    if timing < 50 or risk < 50 or regime.get("confidence", 0) < 0.5 or truth < 75:
        return "Neutral-Bullish / 中性偏多"
    return "Bullish / 偏多"


def _clean_position_advice(self: AnalysisService, symbol: str, market_data: dict[str, Any], profile: dict[str, Any] | None, scores: dict[str, Any]) -> dict[str, Any]:
    stock = market_data["stock"]
    close, ma20 = stock.get("close"), stock.get("ma20")
    position = next((p for p in (profile or {}).get("positions", []) if p.get("symbol") == symbol), None)
    if not position or close is None:
        return {"available": False, "reason": "user_profile.yaml 沒有對應持股，或股價資料缺漏。"}
    lots = position.get("lots", 0)
    avg = position.get("average_cost", 0)
    pnl = (close - avg) * lots * 1000
    risk_score = scores.get("revised_score", {}).get("risk_score", 50)
    sell = 0
    recommendation = "不動"
    if risk_score < 50:
        recommendation = "警戒但不急著賣，先確認 SCFI、法人與市場風險是否同步轉弱"
    elif close >= 270:
        sell, recommendation = 3, "重新評估，可考慮賣 3~5 張機動部位"
    elif close >= 255:
        sell, recommendation = 3, "可考慮再賣 3~5 張機動部位"
    elif close >= 245:
        sell, recommendation = 2, "可考慮賣 2~3 張機動部位"
    elif close < (ma20 or 0) and (market_data["freight"].get("weekly_change") or 0) < 0:
        recommendation = "跌破 20MA 且運價轉弱，提高警戒"
    return {
        "available": True,
        "lots": lots,
        "average_cost": avg,
        "unrealized_pnl": pnl,
        "core_lots": position.get("core_lots"),
        "flexible_lots": position.get("flexible_lots"),
        "recommendation": recommendation,
        "suggested_sell_lots": sell,
        "sell_today": sell > 0,
        "if_245_250": "可考慮賣 2~3 張機動部位，不動核心部位。",
        "if_255_260": "可考慮再賣 3~5 張，並觀察法人是否續買。",
        "if_220_230": "若基本面與運價未轉弱，可觀察買回機動部位。",
        "if_below_20ma": "若跌破 20MA 且 SCFI/法人同步轉弱，降低機動部位並提高警戒。",
    }


def _clean_general_position_advice(self: AnalysisService) -> dict[str, Any]:
    return {"available": False, "reason": "General Mode 不讀取 user_profile.yaml，只輸出市場層面的買賣區間。"}


def _clean_action_plan(self: AnalysisService, mode: str, market_data: dict[str, Any], scores: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    stock = market_data["stock"]
    close, ma20 = stock.get("close"), stock.get("ma20")
    freight_intel = market_data["freight"].get("intelligence") or {}
    risk = scores.get("revised_score", {}).get("risk_score", 50)
    timing = scores.get("revised_score", {}).get("timing_score", 50)
    direction = scores.get("revised_score", {}).get("direction_score", 50)
    buy_zone = "220~230：若基本面與運價未轉弱可觀察買回；200~220：重新評估風險後再分批。"
    sell_zone = "245~250：第一個機動賣點；255~260：第二個機動賣點；270 以上：重新評估是否進一步減碼。"
    lots = 0
    if risk < 50:
        action = "警戒但不積極操作"
    elif timing < 50 and direction >= 55:
        action = "方向偏多，但短線不適合追"
    elif mode == "personalized" and position.get("available"):
        action = position["recommendation"]
        lots = position.get("suggested_sell_lots", 0)
    elif close is not None and close >= 245:
        action = "接近賣點，General Mode 建議只做區間觀察"
    elif close is not None and ma20 is not None and close < ma20:
        action = "等待站回 20MA 或資料轉強"
    else:
        action = "不動，等待下一個明確買賣區"
    data_caution = ""
    if scores.get("freight_score") is None or freight_intel.get("confidence", 0) < 0.55:
        data_caution = "航運核心資料信心不足，不應做強結論。"
    return {
        "recommendation": action,
        "suggested_lots": lots,
        "buy_advice": buy_zone,
        "sell_advice": sell_zone,
        "reason": "綜合股價位置、20MA、法人籌碼、運價方向、ETF/紅海/市場風險與資料可信度。",
        "trigger": "價格進入 245~250、255~260、270 以上，或跌破 20MA 且 SCFI/法人轉弱。",
        "invalidated_by": "SCFI 轉弱、外資與投信同步賣超、ETF 支撐消失、紅海正常化、或價格跌破 20MA 後無法收回。",
        "next_sell_point": "245~250；若站穩再看 255~260。",
        "next_buyback_point": "220~230；前提是基本面與運價未轉弱。",
        "view_change_conditions": "SCFI 連跌、法人同步賣超、ETF 減碼、紅海風險解除、跌破 20MA 後量增轉弱。",
        "data_caution": data_caution,
    }


def _clean_summary(
    self: AnalysisService,
    symbol: str,
    market_data: dict[str, Any],
    scores: dict[str, Any],
    freshness: dict[str, Any],
    mode: str,
    position: dict[str, Any],
    action_plan: dict[str, Any],
    analysis_mode: str,
    truthfulness: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    revised = scores.get("revised_score", {})
    timing_note = "方向偏多，但短線不適合追。" if revised.get("timing_score", 100) < 50 and revised.get("direction_score", 0) >= 55 else ""
    gaps = [key for key, ok in scores["coverage_items"].items() if not ok]
    note = "OpenAI analysis completed successfully." if analysis_mode == "openai" else "Fallback Mode：OpenAI 沒有成功完成分析，本報告由即時資料與本機規則產生。"
    if error:
        note += f" Error: {error}"
    return {
        "market_state": scores["market_state"],
        "conviction_score": scores["coverage_adjusted_score"],
        "raw_score": scores["raw_score"],
        "data_coverage": scores["data_coverage"],
        "truthfulness_score": truthfulness.get("truthfulness_score"),
        "risk_level": scores["risk_level"],
        "action": action_plan["recommendation"],
        "buy_advice": action_plan["buy_advice"],
        "sell_advice": action_plan["sell_advice"],
        "suggested_lots": action_plan["suggested_lots"],
        "one_line": f"{symbol}：{scores['market_state']}，Data Coverage {scores['data_coverage']}%，Truthfulness {truthfulness.get('truthfulness_score')}/100，Overall {scores['coverage_adjusted_score']}/100。{timing_note}",
        "primary_risk": freshness.get("warning") or action_plan.get("data_caution") or "SCFI、ETF、紅海與市場環境資料仍需交叉確認。",
        "key_data_gaps": gaps,
        "analysis_mode_note": note,
    }


def _clean_compose_report(
    self: AnalysisService,
    symbol: str,
    mode: str,
    payload: dict[str, Any],
    position: dict[str, Any],
    action_plan: dict[str, Any],
    summary: dict[str, Any],
    top_only: bool = False,
) -> str:
    market = payload["market_data"]
    stock = market["stock"]
    freight = market["freight"]
    freight_intel = freight.get("intelligence") or {}
    inst = market["institutional"]
    scores = payload["local_scores"]
    revised = scores["revised_score"]
    freshness = payload["data_freshness"]
    quality = payload["data_quality"]
    truth = payload["truthfulness"]
    gap = payload["gap_report"]
    missing_only = [item for item in payload["missing"] if item.startswith("Data Missing")]
    limitations = [item for item in payload["missing"] if item.startswith("Data Limitation") or item.startswith("Data Warning")]
    missing_text = "\n".join(f"- {item}" for item in missing_only) or "- 無核心 Data Missing。"
    limitation_text = "\n".join(f"- {item}" for item in limitations) or "- 無重大資料限制。"
    sources = "\n".join(f"- {s.get('name')}: {s.get('url')}" for s in payload["sources"]) or "- Data Missing"
    pos = position if position.get("available") else {}

    position_section = _position_section(self, mode, pos, position)
    decision = f"""# 即時 AI 投資分析報告

## A. Decision Brief
1. 今日結論：**{scores["market_state"]}**
2. 今日動作：**{summary["action"]}**
3. 方向分數：Direction Score {revised["direction_score"]}/100
4. 風險分數：Risk Score {revised["risk_score"]}/100，{scores["risk_level"]}
5. 短線提醒：{"方向偏多，但短線不適合追。" if revised["timing_score"] < 50 and revised["direction_score"] >= 55 else "短線位置尚可，但仍需看資料品質。"}
6. 下一個賣點：{action_plan.get("next_sell_point")}
7. 下一個買回點：{action_plan.get("next_buyback_point")}
8. 改變看法的條件：{action_plan.get("view_change_conditions")}
9. 最大資料缺口：{", ".join(summary.get("key_data_gaps") or []) or "無核心缺口"}
10. 資料可信度：Truthfulness {truth.get("truthfulness_score")}/100，Coverage {scores["data_coverage"]}%
"""
    if top_only:
        return decision

    return decision + f"""
## B. Detailed Report

## 今日操作建議
- 建議：{action_plan["recommendation"]}
- 建議張數：{action_plan["suggested_lots"]}
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}

{position_section}

## 本次分析最大資料缺口
1. {self._gap_line(gap, 0)}
2. {self._gap_line(gap, 1)}
3. {self._gap_line(gap, 2)}

這些缺口如何影響結論：資料缺口越多，Bullish/Bearish 結論越需要降級；本工具會把 missing、stale、search-inferred 與 exact data 分開處理。

## Freight Intelligence（運價智慧層）
- 整體方向：{freight_intel.get("overall_trend", "unknown")}
- 強度：{freight_intel.get("strength", "weak")}
- 信心分數：{freight_intel.get("confidence", 0)}
- 來源數量：{freight_intel.get("source_count", 0)} independent / {freight_intel.get("raw_signal_count", 0)} raw signals
- 狀態：{freight_intel.get("status", "missing")}
- SCFI 最新值：{self._fmt(freight.get("scfi_latest"))}
- SCFI 週變化：{self._fmt(freight.get("weekly_change"))}%
- SCFI 連續上/下跌週數：{self._fmt(freight.get("scfi_streak_weeks"))}
- 美西：{self._fmt(freight.get("us_west"))}，週變化 {self._fmt(freight.get("us_west_weekly_change"))}%
- 美東：{self._fmt(freight.get("us_east"))}，週變化 {self._fmt(freight.get("us_east_weekly_change"))}%
- 歐洲：{self._fmt(freight.get("europe"))}，週變化 {self._fmt(freight.get("europe_weekly_change"))}%
- 對長榮的意義：{freight_intel.get("summary") or "Data Missing"}

{self._decision_modules_markdown(market)}

## Revised Conviction Score（重構信心分數）
- Direction Score：{revised["direction_score"]}
- Timing Score：{revised["timing_score"]}
- Valuation Score：{revised["valuation_score"]}
- Risk Score：{revised["risk_score"]}
- Coverage Score：{revised["data_coverage"]}
- Truthfulness Score：{revised.get("truthfulness_score")}
- Overall Score：{revised["overall_score"]}

## Market Data Snapshot（市場資料快照）
- 分析時間：{freshness.get("analysis_time")}
- 股價資料日期：{freshness.get("price_data_date")}
- 是否即時資料：{freshness.get("is_realtime_price")}
- 是否收盤資料：{freshness.get("is_closing_price")}
- 收盤價：{self._fmt(stock.get("close"))}
- 成交量：{self._fmt(stock.get("volume"))}
- 20MA：{self._fmt(stock.get("ma20"))}
- 60MA：{self._fmt(stock.get("ma60"))}
- RSI14：{self._fmt((stock.get("technical") or {}).get("rsi14"))}
- MACD：{self._fmt((stock.get("technical") or {}).get("macd"))}
- 法人 1/3/5/10 日：{inst.get("flow_sums", {})}

## Alpha Discovery（超額訊號）
- 市場最在意：SCFI、歐美線運價、法人籌碼、紅海繞航與填息預期。
- 市場可能忽略：ETF 被動買盤與法說/公告資料如果未完整，不能直接給高信心結論。
- 領先指標：運價方向、法人籌碼、紅海風險。
- 落後指標：月營收、財報 EPS、股利殖利率。

## Divergence Analysis（背離分析）
- 股價 vs SCFI：{freight_intel.get("overall_trend", "unknown")}
- 股價 vs 法人籌碼：{inst.get("consecutive_trend", "Data Missing")}
- 股價 vs 基本面：月營收 YoY {self._fmt(market["fundamentals"].get("monthly_revenue_yoy"))}
- 股價 vs 新聞情緒：{len((market.get("news_relevance") or {}).get("articles") or [])} 則高相關新聞
- 股價 vs ETF 被動買盤：{market.get("etf_flow", {}).get("etf_flow", "unknown")}

## Bull vs Bear / CIO Agent
- Bull Case：運價方向、技術面與基本面若同步改善，方向分數加分。
- Bear Case：若短線過熱、法人轉賣、紅海正常化或 ETF 支撐消失，需降級結論。
- CIO Final Judgment：{scores["market_state"]}

## Prediction Tracker（回溯驗證）
- 本次分析已記錄至 data/predictions.jsonl。
- 可執行 `python -m backend.services.prediction_tracker --validate` 驗證 7/30/90 天後結果。
- 本次一句話結論：{summary["one_line"]}

## Data Quality（資料品質）
{self._quality_markdown(quality)}

## Data Sources（資料來源）
{sources}

## Data Missing（資料缺漏）
{missing_text}

## Data Limitations（資料限制）
{limitation_text}

## Disclaimer（免責聲明）
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。缺漏或推論資料不可當成確定事實。
"""


def _position_section(self: AnalysisService, mode: str, pos: dict[str, Any], original_position: dict[str, Any]) -> str:
    if mode == "personalized":
        return f"""## 對我目前部位的建議
- 持股張數：{pos.get("lots", "Data Missing")}
- 均價：{pos.get("average_cost", "Data Missing")}
- 目前損益：{self._fmt(pos.get("unrealized_pnl")) if pos else "Data Missing"}
- 核心部位：{pos.get("core_lots", "Data Missing")}
- 機動部位：{pos.get("flexible_lots", "Data Missing")}
- 今日是否建議賣：{"是" if pos.get("sell_today") else "否"}
- 建議賣出張數：{pos.get("suggested_sell_lots", 0)}
- 若漲到 245~250：{pos.get("if_245_250", "可考慮分批賣機動部位。")}
- 若漲到 255~260：{pos.get("if_255_260", "可考慮再分批減碼。")}
- 若跌回 220~230：{pos.get("if_220_230", "觀察是否買回機動部位。")}
- 若跌破 20MA：{pos.get("if_below_20ma", "提高警戒並重新評估。")}"""
    return f"""## General Mode 買賣區間
- General Mode 不讀取 user_profile.yaml。
- 今日動作：{original_position.get("reason", "只輸出市場分析，不代入個人持股。")}
"""


def _clean_decision_modules_markdown(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    fill = market.get("fill_dividend_probability", {})
    return f"""## ETF Flow（ETF 被動買盤）
- ETF Flow：{etf.get("etf_flow", "unknown")}
- 主要 ETF：{", ".join(etf.get("top_etfs") or []) or "未取得明確 ETF 名單"}
- Holding Change：{self._fmt(etf.get("holding_change"))}
- AUM Change：{self._fmt(etf.get("aum_change"))}
- Confidence：{etf.get("confidence", 0)}
- as_of / stale：{etf.get("as_of") or "Data Missing"} / {etf.get("stale")}
- 說明：{etf.get("missing_reason") or "無重大限制"}

## Red Sea Intelligence（紅海/蘇伊士）
- 狀態：{red.get("status", "unknown")}
- Shipping Impact：{red.get("shipping_impact", "unknown")}
- Suez Return Risk：{red.get("suez_return_risk", "unknown")}
- Confidence：{red.get("confidence", 0)}
- 摘要：{red.get("summary") or "Data Missing"}

## Announcement Intelligence（公告/法說）
- 最新事件狀態：{ann.get("latest_event", "unknown")}
- 重大性：{ann.get("materiality", "unknown")}
- 今日重大事件：{len(ann.get("today_material_event") or [])}
- 7 天內事件：{len(ann.get("recent_event_within_7_days") or [])}
- 14 天以上舊事件：{len(ann.get("stale_event_over_14_days") or [])}
- Fetch Failed：{ann.get("fetch_failed", False)}
- Confidence：{ann.get("confidence", 0)}
- 說明：{self._announcement_label(ann)}

## Market Regime（市場環境）
- Market Regime：{regime.get("market_regime", "unknown")}
- Taiwan Market：{regime.get("taiwan_market", "neutral")}
- Shipping Sector：{regime.get("shipping_sector", "neutral")}
- Confidence：{regime.get("confidence", 0)}

## Fill Dividend Probability（填息機率）
- 30 天：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 天：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- Expected Days：{fill.get("expected_days") if fill.get("expected_days") is not None else "Data Missing"}
- Confidence：{fill.get("confidence", 0)}
- historical_fill_score：{self._fmt(fill.get("historical_fill_score"))}
- freight_score：{self._fmt(fill.get("freight_score"))}
- institutional_score：{self._fmt(fill.get("institutional_score"))}
- etf_score：{self._fmt(fill.get("etf_score"))}
- technical_score：{self._fmt(fill.get("technical_score"))}
- market_regime_score：{self._fmt(fill.get("market_regime_score"))}
- Key Factors：{"；".join(fill.get("key_factors") or []) or "Data Missing"}
- Risks：{"；".join(fill.get("risks") or []) or "Data Missing"}"""


def _clean_quality_markdown(self: AnalysisService, quality: dict[str, Any]) -> str:
    sections = [
        ("Exact Data（精確資料）", quality.get("exact_data", [])),
        ("Scraped Data（爬取資料）", quality.get("scraped_data", [])),
        ("Search-Inferred Data（搜尋推論）", quality.get("search_inferred_data", [])),
        ("Stale / Suspicious Data（過期或可疑資料）", quality.get("stale_or_suspicious_data", [])),
        ("Missing Data（缺漏資料）", quality.get("missing_data", [])),
        ("Conflict Data（衝突資料）", quality.get("conflict_data", [])),
    ]
    lines: list[str] = []
    for title, items in sections:
        lines.append(f"- {title}:")
        lines.extend(f"  - {item}" for item in (items[:8] if items else ["無"]))
    return "\n".join(lines)


def _clean_announcement_label(self: AnalysisService, ann: dict[str, Any]) -> str:
    if ann.get("fetch_failed"):
        return "抓取失敗；不可解讀為沒有公告。"
    if ann.get("today_material_event"):
        return "今天有可能影響交易判斷的重大事件，需讀原文確認。"
    if ann.get("recent_event_within_7_days"):
        return "7 天內有事件，可納入低到中權重判斷。"
    if ann.get("stale_event_over_14_days"):
        return "只有 14 天以上舊事件，不可當成今日重大事件。"
    return "未取得明確近期公告。"


def _clean_gap_line(self: AnalysisService, gap_report: dict[str, Any], index: int) -> str:
    gaps = sorted(gap_report.get("gaps") or [], key=lambda item: {"P0": 0, "P1": 1, "P2": 2}.get(item.get("priority"), 9))
    if index >= len(gaps):
        return "無"
    gap = gaps[index]
    return f"{gap.get('priority')} {gap.get('field')}：{gap.get('next_action')}"


def _clean_pct_or_missing(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "Data Missing"
    return f"{float(value) * 100:.0f}%"


def _clean_fmt(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "Data Missing"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


AnalysisService._engine_missing = _clean_engine_missing
AnalysisService._soften_resolved_missing = _clean_soften_resolved_missing
AnalysisService._data_freshness = _clean_data_freshness
AnalysisService._market_state = _clean_market_state
AnalysisService._position_advice = _clean_position_advice
AnalysisService._general_position_advice = _clean_general_position_advice
AnalysisService._action_plan = _clean_action_plan
AnalysisService._summary = _clean_summary
AnalysisService._compose_report = _clean_compose_report
AnalysisService._decision_modules_markdown = _clean_decision_modules_markdown
AnalysisService._quality_markdown = _clean_quality_markdown
AnalysisService._announcement_label = _clean_announcement_label
AnalysisService._gap_line = _clean_gap_line
AnalysisService._pct_or_missing = _clean_pct_or_missing
AnalysisService._fmt = _clean_fmt


def _zh_trend(value: Any) -> str:
    return {"up": "上升", "down": "下降", "flat": "持平", "unknown": "未知", None: "未知"}.get(value, str(value))


def _zh_strength(value: Any) -> str:
    return {"strong": "強", "moderate": "中等", "weak": "弱", "unknown": "未知", None: "未知"}.get(value, str(value))


def _zh_bool(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _zh_source_status(value: Any) -> str:
    mapping = {
        "partial_exact": "部分精確資料加趨勢推論",
        "inferred_from_multiple_independent_sources": "多來源一致推論",
        "inferred": "搜尋推論",
        "low_confidence_inferred": "低信心推論",
        "missing": "資料不足",
        "ok": "正常",
        "partial": "部分資料",
        "unknown": "未知",
    }
    return mapping.get(value, str(value or "未知"))


def _zh_signal(value: Any) -> str:
    mapping = {
        "buy": "買超",
        "sell": "賣超",
        "flat": "持平",
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "inferred_bullish": "搜尋推論偏多",
        "inferred_bearish": "搜尋推論偏空",
        "unknown": "未知",
        "escalating": "風險升高",
        "stable": "風險仍在",
        "improving": "改善中",
        "normalizing": "正常化",
        "escalating": "升溫",
        "high": "高",
        "medium": "中",
        "low": "低",
        "stale_event_over_14_days": "只有 14 天以上舊事件",
        "recent_event_within_7_days": "7 天內有事件",
        "today_material_event": "今日有重大事件",
        "fetch_failed": "抓取失敗",
    }
    return mapping.get(value, str(value or "未知"))


def _fmt_shares(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "資料不足"
    sign = "+" if num >= 0 else "-"
    num = abs(num)
    if num >= 10000:
        return f"{sign}{num / 10000:.1f} 萬股"
    return f"{sign}{num:.0f} 股"


def _fmt_flow_sums(flow_sums: Any) -> str:
    if not isinstance(flow_sums, dict):
        return "資料不足"
    labels = {"foreign": "外資", "trust": "投信", "dealer": "自營商", "total": "三大法人合計"}
    lines: list[str] = []
    for key in ("foreign", "trust", "dealer", "total"):
        row = flow_sums.get(key) or {}
        if not isinstance(row, dict):
            continue
        lines.append(
            f"- {labels[key]}：1 日 {_fmt_shares(row.get('1d'))}，3 日 {_fmt_shares(row.get('3d'))}，5 日 {_fmt_shares(row.get('5d'))}，10 日 {_fmt_shares(row.get('10d'))}"
        )
    return "\n".join(lines) or "資料不足"


def _fmt_latest_inst(latest: Any) -> str:
    if not isinstance(latest, dict):
        return "資料不足"
    return (
        f"{latest.get('date', '日期不明')}：外資 {_fmt_shares(latest.get('foreign'))}，"
        f"投信 {_fmt_shares(latest.get('trust'))}，自營商 {_fmt_shares(latest.get('dealer'))}，"
        f"合計 {_fmt_shares(latest.get('total'))}"
    )


def _fmt_streak(streak: Any) -> str:
    if not isinstance(streak, dict):
        return "資料不足"
    return f"{_zh_signal(streak.get('direction'))} {streak.get('days', 0)} 天"


def _pretty_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "資料不足"
    sign = "+" if amount >= 0 else "-"
    amount = abs(amount)
    if amount >= 10000:
        return f"{sign}{amount / 10000:.1f} 萬元"
    return f"{sign}{amount:.0f} 元"


def _friendly_missing(item: str) -> str:
    text = str(item)
    text = text.replace("stale_event_over_14_days", "只有 14 天以上舊事件")
    text = text.replace("shipping impact high", "對航運影響高")
    text = text.replace("Market Regime", "市場環境")
    text = text.replace("holding/AUM", "持股與基金規模")
    text = text.replace("holding_change and AUM_change", "持股變化與基金規模變化")
    text = text.replace("holding_change / AUM_change", "持股變化與基金規模變化")
    text = text.replace("investment trust data is suspiciously zero for many days; verify with TWSE or broker data.", "投信資料連續多日為 0，可能是資料分類或來源限制，需與交易所或券商資料交叉確認。")
    text = text.replace("investor conference materials were not confirmed by search fallback.", "法說會或投資人簡報尚未由搜尋來源確認。")
    text = text.replace("ETF signal is search-inferred only. holding_change and AUM_change are unavailable, so confidence is capped and score boost must be minimal.", "ETF 訊號僅來自搜尋推論，尚未取得實際持股變化與基金規模變化，因此信心上限較低，不能大幅加分。")
    text = text.replace("ETF 只屬搜尋推論，holding/AUM 缺漏，不能大幅加分。", "ETF 只屬搜尋推論，尚未取得實際持股與基金規模變化，不能大幅加分。")
    text = text.replace("Market Regime 信心不足。", "市場環境信心不足。")
    replacements = {
        "Data Missing: ": "資料不足：",
        "Data Limitation: ": "資料限制：",
        "Data Warning: ": "資料提醒：",
        "ETF Flow": "ETF 被動買盤",
        "Announcement Intelligence": "公告與法說資料",
        "market regime": "市場環境",
        "holding_change": "持股變化",
        "AUM_change": "基金規模變化",
        "stale": "資料較舊",
        "search-inferred": "搜尋推論",
        "exact rate": "精確運價",
        "confidence": "信心",
        "fetch failed": "抓取失敗",
        "signal is": "訊號為",
        "only": "僅供參考",
        "are unavailable": "尚未取得",
        "unavailable": "尚未取得",
        "score boost must be minimal": "不能大幅加分",
        "capped": "上限受限",
        " and ": "與",
        " so ": "，因此",
        " is ": "為",
        "fallback": "備援搜尋",
        "strong": "強",
        "moderate": "中等",
        "weak": "弱",
        "shipping impact": "對航運影響",
        "high": "高",
        "medium": "中",
        "low": "低",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("ETF 訊號為 搜尋推論 僅供參考. 持股變化與基金規模變化 尚未取得,，因此信心為上限受限與不能大幅加分.", "ETF 訊號僅來自搜尋推論；尚未取得實際持股變化與基金規模變化，因此信心上限較低，不能大幅加分。")
    text = text.replace("持股與基金規模 缺漏", "尚未取得實際持股與基金規模變化")
    text = text.replace("市場環境 信心不足", "市場環境信心不足")
    text = text.replace("對航運影響 高", "對航運影響高")
    text = text.replace(" ,", "，").replace(",，", "，").replace(".", "。")
    return text


def _pretty_one_line(summary: dict[str, Any]) -> str:
    text = str(summary.get("one_line") or "")
    text = text.replace("Data Coverage", "資料覆蓋率")
    text = text.replace("Truthfulness", "資料可信度")
    text = text.replace("Overall", "綜合分數")
    return text


def _pretty_list(items: list[str], empty: str = "目前沒有重大缺口。", limit: int = 6) -> str:
    rows = [f"- {_friendly_missing(item)}" for item in items[:limit]]
    return "\n".join(rows) if rows else f"- {empty}"


def _pretty_position_section(self: AnalysisService, mode: str, pos: dict[str, Any], original_position: dict[str, Any]) -> str:
    if mode != "personalized":
        return """## 3. General Mode 說明
- 本次為一般模式，只分析市場與股票條件。
- 未讀取持股、均價、稅率或個人風險設定。
- 買賣區間僅供觀察，不代表個人化操作建議。"""
    return f"""## 3. 對我目前部位的建議
- 持股張數：{pos.get("lots", "資料不足")} 張
- 均價：{pos.get("average_cost", "資料不足")}
- 目前損益：約 {_pretty_money(pos.get("unrealized_pnl"))}
- 核心部位：{pos.get("core_lots", "資料不足")} 張
- 機動部位：{pos.get("flexible_lots", "資料不足")} 張
- 今日是否建議賣：{"是" if pos.get("sell_today") else "否"}
- 建議賣出張數：{pos.get("suggested_sell_lots", 0)} 張
- 245~250：{pos.get("if_245_250", "可考慮分批處理機動部位。")}
- 255~260：{pos.get("if_255_260", "可考慮再分批減碼。")}
- 220~230：{pos.get("if_220_230", "觀察是否買回機動部位。")}
- 跌破 20MA：{pos.get("if_below_20ma", "提高警戒並重新評估。")}"""


def _pretty_decision_modules_markdown(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    fill = market.get("fill_dividend_probability", {})
    return f"""## 8. 新聞與事件風險
### ETF 被動買盤
- 判斷：{_zh_signal(etf.get("etf_flow"))}
- 主要觀察 ETF：{", ".join(etf.get("top_etfs") or []) or "資料不足"}
- 信心：{etf.get("confidence", 0)}
- 解讀：{_friendly_missing(etf.get("missing_reason") or "目前沒有重大限制。")}

### 紅海與蘇伊士航運
- 狀態：{_zh_signal(red.get("status"))}
- 對航運影響：{_zh_signal(red.get("shipping_impact"))}
- 蘇伊士回流風險：{_zh_signal(red.get("suez_return_risk"))}
- 信心：{red.get("confidence", 0)}
- 摘要：{_friendly_missing(red.get("summary") or "資料不足")}

### 公司公告與法說
- 最新事件狀態：{_zh_signal(ann.get("latest_event"))}
- 重大性：{_zh_signal(ann.get("materiality"))}
- 解讀：{self._announcement_label(ann)}

### 大盤環境
- 市場環境：{_zh_signal(regime.get("market_regime"))}
- 台股：{_zh_signal(regime.get("taiwan_market"))}
- 航運類股：{_zh_signal(regime.get("shipping_sector"))}
- 信心：{regime.get("confidence", 0)}

## 7. 基本面與股利
- 30 天填息機率：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 天填息機率：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年填息機率：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- 目前限制：{_friendly_missing("；".join(fill.get("risks") or []) or "資料不足，不能硬估。")}"""


def _pretty_quality_summary(self: AnalysisService, payload: dict[str, Any]) -> str:
    truth = payload["truthfulness"]
    missing = [item for item in payload["missing"] if item.startswith("Data Missing")]
    limitations = [item for item in payload["missing"] if item.startswith("Data Limitation") or item.startswith("Data Warning")]
    return f"""## 10. 資料可信度與限制
- 資料可信度：{truth.get("truthfulness_score")}/100
- 資料覆蓋率：{payload["local_scores"].get("data_coverage")}%
- 核心缺漏：
{_pretty_list(missing, "目前沒有核心資料缺漏。", 4)}
- 重要限制：
{_pretty_list(limitations, "目前沒有重大資料限制。", 4)}

解讀原則：資料越多來自搜尋推論或舊資料，結論就越需要保守；本報告不會把缺漏資料當成利多或利空。"""


def _pretty_compose_report(
    self: AnalysisService,
    symbol: str,
    mode: str,
    payload: dict[str, Any],
    position: dict[str, Any],
    action_plan: dict[str, Any],
    summary: dict[str, Any],
    top_only: bool = False,
) -> str:
    market = payload["market_data"]
    stock = market["stock"]
    freight = market["freight"]
    freight_intel = freight.get("intelligence") or {}
    inst = market["institutional"]
    scores = payload["local_scores"]
    revised = scores["revised_score"]
    freshness = payload["data_freshness"]
    pos = position if position.get("available") else {}

    price = self._fmt(stock.get("close"))
    ma20 = self._fmt(stock.get("ma20"))
    ma60 = self._fmt(stock.get("ma60"))
    rsi = self._fmt((stock.get("technical") or {}).get("rsi14"))
    technical_note = "短線偏熱，追價要保守。" if (stock.get("technical") or {}).get("rsi14", 0) and (stock.get("technical") or {}).get("rsi14", 0) > 75 else "技術面未顯示明顯過熱。"
    one_minute = f"""# 即時 AI 投資分析報告

## 1. 一分鐘結論
- 今日結論：**{scores["market_state"]}**
- 今日動作：**{summary["action"]}**
- 現在價格：{price}，20 日均線 {ma20}，60 日均線 {ma60}
- 可賣位置：{action_plan.get("next_sell_point")}
- 可買回位置：{action_plan.get("next_buyback_point")}
- 最大風險：{summary.get("primary_risk")}
- 資料可信度：{summary.get("truthfulness_score")}/100，資料覆蓋率 {summary.get("data_coverage")}%
- 短線提醒：{technical_note}
"""
    if top_only:
        return one_minute

    position_section = _pretty_position_section(self, mode, pos, position)
    return one_minute + f"""
## 2. 操作建議
- 現在是否適合買：不建議追價；若要買，等回到買回區並確認運價與籌碼沒有轉弱。
- 現在是否適合賣：{action_plan["recommendation"]}
- 建議張數：{action_plan["suggested_lots"]} 張
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}

{position_section}

## 4. 支撐與壓力
- 目前價格：{price}
- 20 日均線：{ma20}
- 60 日均線：{ma60}
- RSI：{rsi}
- 觀察支撐：{action_plan.get("next_buyback_point")}
- 觀察壓力：{action_plan.get("next_sell_point")}

## 5. 運價與航運景氣
- 運價方向：{_zh_trend(freight_intel.get("overall_trend"))}
- 強度：{_zh_strength(freight_intel.get("strength"))}
- 信心：{freight_intel.get("confidence", 0)}
- 資料狀態：{_zh_source_status(freight_intel.get("status"))}
- SCFI 最新值：{self._fmt(freight.get("scfi_latest"))}
- SCFI 週變化：{self._fmt(freight.get("weekly_change"))}%
- SCFI 連續上/下跌週數：{self._fmt(freight.get("scfi_streak_weeks"))}
- 美西線：{self._fmt(freight.get("us_west"))}，週變化 {self._fmt(freight.get("us_west_weekly_change"))}%
- 美東線：{self._fmt(freight.get("us_east"))}，週變化 {self._fmt(freight.get("us_east_weekly_change"))}%
- 歐洲線：{self._fmt(freight.get("europe"))}，週變化 {self._fmt(freight.get("europe_weekly_change"))}%
- 對長榮的意義：{_friendly_missing(freight_intel.get("summary") or "資料不足")}

## 6. 法人與籌碼
- 最新法人資料：{_fmt_latest_inst(inst.get("latest"))}
- 連續買賣超：{_fmt_streak(inst.get("consecutive_trend"))}
- 近 1/3/5/10 日合計：
{_fmt_flow_sums(inst.get("flow_sums"))}
- 注意：若投信資料長期為 0，應視為資料可能不完整，而不是投信完全沒有動作。

{self._decision_modules_markdown(market)}

## 9. 多空辯論
### 多方理由
- 運價若維持上升，對航運獲利與市場預期有支撐。
- 價格仍在中期均線之上，趨勢尚未完全破壞。
- 若基本面與股利條件維持，低檔仍有評價支撐。

### 空方理由
- 短線若過熱，追價風險高。
- 法人若持續賣超，會壓抑股價續攻力道。
- ETF、公告、大盤環境若資料不足，不能給過高信心。

### 最終判斷
- {_pretty_one_line(summary)}

{_pretty_quality_summary(self, payload)}

## 11. 免責聲明
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。若資料缺漏或來源信心不足，請以保守方式解讀。"""


def _pretty_fmt(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _pretty_pct_or_missing(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    return f"{float(value) * 100:.0f}%"


def _pretty_announcement_label(self: AnalysisService, ann: dict[str, Any]) -> str:
    if ann.get("fetch_failed"):
        return "抓取失敗，不能解讀為沒有公告。"
    if ann.get("today_material_event"):
        return "今天有可能影響交易判斷的重大事件，需要讀原文確認。"
    if ann.get("recent_event_within_7_days"):
        return "7 天內有事件，可納入低到中權重判斷。"
    if ann.get("stale_event_over_14_days"):
        return "目前只有較舊事件，不能當成今日重大事件。"
    return "未取得明確近期公告。"


AnalysisService._compose_report = _pretty_compose_report
AnalysisService._decision_modules_markdown = _pretty_decision_modules_markdown
AnalysisService._fmt = _pretty_fmt
AnalysisService._pct_or_missing = _pretty_pct_or_missing
AnalysisService._announcement_label = _pretty_announcement_label


def _clean2_zh_signal(value: Any) -> str:
    mapping = {
        "buy": "買超",
        "sell": "賣超",
        "flat": "持平",
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "risk_on": "風險偏好",
        "risk_off": "避險",
        "inferred_bullish": "搜尋推論偏多",
        "inferred_bearish": "搜尋推論偏空",
        "unknown": "未知",
        "escalating": "風險升高",
        "stable": "風險仍在",
        "improving": "改善中",
        "normalizing": "正常化",
        "elevated": "偏高",
        "watch": "觀察中",
        "rising": "上升",
        "falling": "下降",
        "high": "高",
        "medium": "中",
        "low": "低",
        "cost_pressure": "成本壓力",
        "cost_relief": "成本下降",
        "freight_supportive": "支撐運價",
        "positive": "正面",
        "negative": "負面",
        "mixed": "多空混合",
        "stale_event_over_14_days": "只有 14 天以上舊事件",
        "recent_event_within_7_days": "7 天內有事件",
        "today_material_event": "今日有重大事件",
        "fetch_failed": "抓取失敗",
    }
    return mapping.get(value, str(value or "未知"))


_base_revised_score_for_international = AnalysisService._revised_score


def _clean2_revised_score(self: AnalysisService, payload: dict[str, Any], base: dict[str, Any]) -> dict[str, int]:
    original = _base_revised_score_for_international(self, payload, base)
    intl = payload["market_data"].get("international_events", {})
    oil = (intl.get("oil") or {}).get("impact")
    risk = intl.get("overall_risk")
    adjusted = dict(original)
    if risk == "high":
        adjusted["risk_score"] = self._clamp(adjusted["risk_score"] - 8)
    elif risk == "medium":
        adjusted["risk_score"] = self._clamp(adjusted["risk_score"] - 4)
    elif risk == "low":
        adjusted["risk_score"] = self._clamp(adjusted["risk_score"] + 3)
    if oil == "cost_pressure":
        adjusted["valuation_score"] = self._clamp(adjusted["valuation_score"] - 4)
        adjusted["risk_score"] = self._clamp(adjusted["risk_score"] - 3)
    elif oil == "cost_relief":
        adjusted["valuation_score"] = self._clamp(adjusted["valuation_score"] + 3)
    adjusted["overall_score"] = self._clamp(
        0.34 * adjusted["direction_score"]
        + 0.18 * adjusted["timing_score"]
        + 0.18 * adjusted["valuation_score"]
        + 0.15 * adjusted["risk_score"]
        + 0.15 * adjusted["data_coverage"]
    )
    return adjusted


def _clean2_engine_missing(self: AnalysisService, market_data: dict[str, Any]) -> list[str]:
    out = _clean_engine_missing(self, market_data)
    intl = market_data.get("international_events", {})
    if intl.get("missing_reason"):
        out.append(intl["missing_reason"])
    return out


def _clean2_pretty_decision_modules_markdown(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    intl = market.get("international_events", {})
    oil = intl.get("oil") or {}
    oil_prices = intl.get("oil_prices") or {}
    wti = oil_prices.get("wti") or {}
    brent = oil_prices.get("brent") or {}
    policy = intl.get("us_policy") or {}
    war = intl.get("war_geopolitics") or {}
    fill = market.get("fill_dividend_probability", {})
    return f"""## 7. 基本面與股利
- 30 天填息機率：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 天填息機率：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年填息機率：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- 目前限制：{_friendly_missing("；".join(fill.get("risks") or []) or "資料不足，不能硬估。")}

## 8. 國際事件與事件風險
### 美國政策與全球貿易
- 狀態：{_clean2_zh_signal(policy.get("status"))}
- 影響：{_clean2_zh_signal(policy.get("impact"))}
- 解讀：{policy.get("summary") or "目前沒有明確訊號。"}

### 戰爭與地緣風險
- 狀態：{_clean2_zh_signal(war.get("status"))}
- 影響：{_clean2_zh_signal(war.get("impact"))}
- 解讀：{war.get("summary") or "目前沒有明確訊號。"}

### 油價
- WTI：{self._fmt(wti.get("close"))}，5 日變化 {self._fmt(wti.get("change_5d_pct"))}%
- Brent：{self._fmt(brent.get("close"))}，5 日變化 {self._fmt(brent.get("change_5d_pct"))}%
- 成本影響：{_clean2_zh_signal(oil.get("impact"))}
- 解讀：{oil.get("summary") or "油價資料不足。"}

### 國際事件總結
- 整體國際風險：{_clean2_zh_signal(intl.get("overall_risk"))}
- 信心：{intl.get("confidence", 0)}
- 摘要：{intl.get("summary") or "資料不足。"}

### ETF 被動買盤
- 判斷：{_clean2_zh_signal(etf.get("etf_flow"))}
- 主要觀察 ETF：{", ".join(etf.get("top_etfs") or []) or "資料不足"}
- 信心：{etf.get("confidence", 0)}
- 解讀：{_friendly_missing(etf.get("missing_reason") or "目前沒有重大限制。")}

### 紅海與蘇伊士航運
- 狀態：{_clean2_zh_signal(red.get("status"))}
- 對航運影響：{_clean2_zh_signal(red.get("shipping_impact"))}
- 蘇伊士回流風險：{_clean2_zh_signal(red.get("suez_return_risk"))}
- 信心：{red.get("confidence", 0)}
- 摘要：{_friendly_missing(red.get("summary") or "資料不足")}

### 公司公告與法說
- 最新事件狀態：{_clean2_zh_signal(ann.get("latest_event"))}
- 重大性：{_clean2_zh_signal(ann.get("materiality"))}
- 解讀：{self._announcement_label(ann)}

### 大盤環境
- 市場環境：{_clean2_zh_signal(regime.get("market_regime"))}
- 台股：{_clean2_zh_signal(regime.get("taiwan_market"))}
- 航運類股：{_clean2_zh_signal(regime.get("shipping_sector"))}
- 信心：{regime.get("confidence", 0)}"""


AnalysisService._revised_score = _clean2_revised_score
AnalysisService._engine_missing = _clean2_engine_missing
AnalysisService._decision_modules_markdown = _clean2_pretty_decision_modules_markdown


# Final clean report renderer. This intentionally sits at the end of the file so
# older experimental renderers cannot override the user-facing report.
def _final_label(value: Any) -> str:
    mapping = {
        "Strong Bullish": "強多",
        "Bullish": "偏多",
        "Neutral-Bullish": "中性偏多",
        "Neutral": "中性",
        "Bearish": "偏空",
        "Strong Bearish": "強空",
        "buy": "買超",
        "sell": "賣超",
        "flat": "持平",
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "risk_on": "風險偏好",
        "risk_off": "風險趨避",
        "inferred_bullish": "推論偏多",
        "inferred_bearish": "推論偏空",
        "unknown": "未知",
        "up": "上升",
        "down": "下降",
        "moderate": "中等",
        "strong": "強",
        "weak": "弱",
        "high": "高",
        "medium": "中",
        "low": "低",
        "watch": "觀察",
        "improving": "改善",
        "normalizing": "正常化",
        "elevated": "偏高",
        "rising": "上升",
        "falling": "下降",
        "stable": "穩定",
        "cost_pressure": "成本壓力",
        "cost_relief": "成本緩解",
        "freight_supportive": "支撐運價",
        "positive": "正面",
        "negative": "負面",
        "mixed": "混合",
        "today_material_event": "今日重大事件",
        "recent_event_within_7_days": "7 日內事件",
        "stale_event_over_14_days": "超過 14 日舊事件",
        "fetch_failed": "抓取失敗",
        "none": "未發現重大事件",
    }
    return mapping.get(value, str(value or "未知"))


def _final_fmt(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _final_pct(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value) * 100:.0f}%"
    except Exception:
        return str(value)


def _final_inst_line(inst: dict[str, Any]) -> str:
    latest = inst.get("latest") or {}
    if not latest:
        return "法人最新資料不足"
    return (
        f"{latest.get('date', '日期未知')}：外資 {latest.get('foreign', '資料不足')}、"
        f"投信 {latest.get('trust', '資料不足')}、自營商 {latest.get('dealer', '資料不足')}、"
        f"合計 {latest.get('total', '資料不足')}"
    )


def _final_flow_lines(inst: dict[str, Any]) -> str:
    sums = inst.get("flow_sums") or {}
    if not sums:
        return "- 近 1/3/5/10 日：資料不足"
    rows = []
    for key, label in [("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商"), ("total", "三大法人合計")]:
        row = sums.get(key) or {}
        rows.append(f"- {label}：1日 {row.get('1d', '資料不足')}、3日 {row.get('3d', '資料不足')}、5日 {row.get('5d', '資料不足')}、10日 {row.get('10d', '資料不足')}")
    return "\n".join(rows)


def _final_position_section(self: AnalysisService, mode: str, position: dict[str, Any], action_plan: dict[str, Any]) -> str:
    if mode != "personalized" or not position.get("available"):
        return """## 對目前部位的建議
- General Mode 不讀取使用者持股。
- 以下買賣區間只代表市場層面的觀察，不是個人部位建議。"""
    return f"""## 對我目前部位的建議
- 持股張數：{position.get("lots", "資料不足")} 張
- 均價：{position.get("average_cost", "資料不足")}
- 目前損益：{self._fmt(position.get("unrealized_pnl"))}
- 核心部位：{position.get("core_lots", "資料不足")} 張
- 機動部位：{position.get("flexible_lots", "資料不足")} 張
- 今日是否建議賣：{action_plan.get("recommendation")}
- 建議賣出張數：{action_plan.get("suggested_lots")} 張
- 若漲到 245~250：可考慮賣 2~3 張機動部位
- 若漲到 255~260：可考慮再賣 3~5 張
- 若跌回 220~230：若基本面未轉弱，可觀察買回
- 若跌破 20MA：若同時 SCFI 轉弱與法人轉賣，風險升高"""


def _final_module_markdown(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    intl = market.get("international_events", {})
    oil = intl.get("oil") or {}
    oil_prices = intl.get("oil_prices") or {}
    wti = oil_prices.get("wti") or {}
    brent = oil_prices.get("brent") or {}
    policy = intl.get("us_policy") or {}
    war = intl.get("war_geopolitics") or {}
    fill = market.get("fill_dividend_probability", {})

    return f"""## 國際事件與事件風險
### 美國政策與貿易
- 狀態：{_final_label(policy.get("status"))}
- 影響：{_final_label(policy.get("impact"))}
- 解讀：{policy.get("summary") or "目前沒有明確訊號。"}

### 戰爭與地緣政治
- 狀態：{_final_label(war.get("status"))}
- 影響：{_final_label(war.get("impact"))}
- 解讀：{war.get("summary") or "目前沒有明確訊號。"}

### 油價
- WTI：{self._fmt(wti.get("close"))}，近 5 日變化 {self._fmt(wti.get("change_5d_pct"))}%
- Brent：{self._fmt(brent.get("close"))}，近 5 日變化 {self._fmt(brent.get("change_5d_pct"))}%
- 對航運成本影響：{_final_label(oil.get("impact"))}
- 解讀：{oil.get("summary") or "油價資料不足。"}

### 國際事件總結
- 整體風險：{_final_label(intl.get("overall_risk"))}
- 信心分數：{intl.get("confidence", 0)}
- 摘要：{intl.get("summary") or "資料不足。"}

## ETF Flow
- 判斷：{_final_label(etf.get("etf_flow"))}
- 主要 ETF：{", ".join(etf.get("top_etfs") or []) or "資料不足"}
- 信心分數：{etf.get("confidence", 0)}
- 限制：{self._user_message(etf.get("missing_reason") or "無明確限制")}

## Red Sea Intelligence
- 狀態：{_final_label(red.get("status"))}
- 對航運影響：{_final_label(red.get("shipping_impact"))}
- 蘇伊士回歸風險：{_final_label(red.get("suez_return_risk"))}
- 信心分數：{red.get("confidence", 0)}
- 摘要：{red.get("summary") or "資料不足"}

## Announcement Intelligence
- 事件狀態：{_final_label(ann.get("latest_event"))}
- 重大性：{_final_label(ann.get("materiality"))}
- 解讀：抓取失敗不可解讀為沒有公告；超過 14 日的事件不可列為今日重大事件。

## Market Regime
- 市場環境：{_final_label(regime.get("market_regime"))}
- 台股：{_final_label(regime.get("taiwan_market"))}
- 航運族群：{_final_label(regime.get("shipping_sector"))}
- 美股：{_final_label(regime.get("us_market"))}
- 信心分數：{regime.get("confidence", 0)}

## Fill Dividend Probability
- 30 日填息機率：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 日填息機率：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年填息機率：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- 主要依據：歷史填息、運價方向、法人籌碼、ETF 支撐、技術面、市場環境
- 風險：{", ".join(fill.get("risks") or []) or "資料不足"}"""


def _final_compose_report(
    self: AnalysisService,
    symbol: str,
    mode: str,
    payload: dict[str, Any],
    position: dict[str, Any],
    action_plan: dict[str, Any],
    summary: dict[str, Any],
    top_only: bool = False,
) -> str:
    market = payload["market_data"]
    stock = market["stock"]
    freight = market["freight"]
    freight_intel = freight.get("intelligence") or {}
    inst = market["institutional"]
    fundamentals = market["fundamentals"]
    scores = payload["local_scores"]
    revised = scores["revised_score"]
    freshness = payload["data_freshness"]
    truth = payload["truthfulness"]

    price = self._fmt(stock.get("close"))
    ma20 = self._fmt(stock.get("ma20"))
    ma60 = self._fmt(stock.get("ma60"))
    rsi = self._fmt((stock.get("technical") or {}).get("rsi14"))
    timing_note = "方向偏多，但短線不適合追" if revised.get("timing_score", 0) < 50 else "位置尚可，但仍需等訊號確認"

    brief = f"""# 即時 AI 投資分析報告

## A. Decision Brief
1. 今日結論：**{summary["market_state"]}**
2. 一句話：{summary["one_line"]}
3. 方向：Direction Score {revised.get("direction_score")}/100
4. 風險：Risk Score {revised.get("risk_score")}/100，{summary.get("risk_level")}
5. 今日動作：**{summary["action"]}**
6. 買進建議：{summary.get("buy_advice")}
7. 賣出建議：{summary.get("sell_advice")}
8. 下一個賣點：{action_plan.get("next_sell_point")}
9. 下一個買回點：{action_plan.get("next_buyback_point")}
10. 改變看法的條件：{action_plan.get("view_change_conditions")}"""

    if top_only:
        return brief

    position_section = _final_position_section(self, mode, position, action_plan)
    gaps = summary.get("key_data_gaps") or []
    return brief + f"""

## B. Detailed Report

## 今日操作建議
- 建議：{action_plan["recommendation"]}
- 建議張數：{action_plan["suggested_lots"]} 張
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}
- 短線提醒：{timing_note}

{position_section}

## 本次分析最大資料缺口
{chr(10).join(f"- {item}" for item in gaps) if gaps else "- 無核心缺口"}
- 影響：資料缺漏會降低 Data Coverage 與 Truthfulness，系統不會把缺漏資料當成中性，也不會硬給強結論。

## Market Data Snapshot
- 股價資料日期：{freshness.get("price_data_date") or "資料不足"}
- 分析時間：{freshness.get("analysis_time") or "資料不足"}
- 是否即時資料：{"是" if freshness.get("is_realtime_price") else "否"}
- 是否收盤資料：{"是" if freshness.get("is_closing_price") else "否"}
- 收盤價：{price}
- 成交量：{self._fmt(stock.get("volume"))}
- 20MA：{ma20}
- 60MA：{ma60}
- RSI：{rsi}
- MACD：{self._fmt((stock.get("technical") or {}).get("macd"))}
- EPS：{self._fmt(fundamentals.get("eps"))}
- 股利殖利率：{self._fmt(fundamentals.get("dividend_yield"))}

## Freight Intelligence
- 整體方向：{_final_label(freight_intel.get("overall_trend"))}
- 強度：{_final_label(freight_intel.get("strength"))}
- 信心分數：{freight_intel.get("confidence", 0)}
- 來源數量：{freight_intel.get("source_count", 0)}
- SCFI 最新值：{self._fmt(freight.get("scfi_latest"))}
- SCFI 週變化：{self._fmt(freight.get("weekly_change"))}%
- 連續上漲/下跌週數：{self._fmt(freight.get("scfi_streak_weeks"))}
- 美西線：{self._fmt(freight.get("us_west"))}，週變化 {self._fmt(freight.get("us_west_weekly_change"))}%
- 美東線：{self._fmt(freight.get("us_east"))}，週變化 {self._fmt(freight.get("us_east_weekly_change"))}%
- 歐洲線：{self._fmt(freight.get("europe"))}，週變化 {self._fmt(freight.get("europe_weekly_change"))}%
- 對長榮的意義：{_e_freight_meaning(freight, freight_intel)}

## 法人籌碼
- 最新資料：{_final_inst_line(inst)}
- 連續趨勢：{_final_label((inst.get("consecutive_trend") or {}).get("direction"))}，{(inst.get("consecutive_trend") or {}).get("days", "資料不足")} 日
{_final_flow_lines(inst)}
- 提醒：若投信多日為 0，可能是資料源分類不完整，需與 TWSE 或券商資料交叉確認。

{self._decision_modules_markdown(market)}

## Revised Conviction Score
- Direction Score：{revised.get("direction_score")}
- Timing Score：{revised.get("timing_score")}
- Valuation Score：{revised.get("valuation_score")}
- Risk Score：{revised.get("risk_score")}
- Data Coverage：{revised.get("data_coverage")}
- Truthfulness Score：{revised.get("truthfulness_score")}
- Overall Score：{revised.get("overall_score")}
- 解讀：{timing_note}。

## Bull vs Bear
### Bull Case
- 運價方向若持續上升，對長榮 EPS、填息與股價支撐較有利。
- 股價若站穩 20MA 且法人賣壓收斂，短線結構改善。
- 若紅海與繞航風險延續，貨櫃航運供給有效運力仍可能偏緊。

### Bear Case
- RSI 過熱或價格接近壓力區時，短線追價風險升高。
- 外資連賣、投信未明確承接、ETF 資料不足時，不宜把被動買盤當強支撐。
- 美國政策、戰爭與油價若轉為成本壓力，會降低估值與風險分數。

### CIO Final Judgment
- 目前結論：{summary["one_line"]}

## Data Quality
- Truthfulness Score：{truth.get("truthfulness_score")}/100
- Data Coverage：{scores.get("data_coverage")}%
- Exact Data：{len((payload.get("data_quality") or {}).get("exact_data") or [])}
- Scraped Data：{len((payload.get("data_quality") or {}).get("scraped_data") or [])}
- Search Inferred Data：{len((payload.get("data_quality") or {}).get("search_inferred_data") or [])}
- Missing Data：{len((payload.get("data_quality") or {}).get("missing_data") or [])}

## Disclaimer
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。"""


AnalysisService._compose_report = _final_compose_report
AnalysisService._decision_modules_markdown = _final_module_markdown
AnalysisService._fmt = _final_fmt
AnalysisService._pct_or_missing = _final_pct


# Clean Traditional Chinese renderer, kept last so it wins over older renderers.
def _tw_label(value: Any) -> str:
    mapping = {
        "Strong Bullish": "強多",
        "Bullish": "偏多",
        "Neutral-Bullish": "中性偏多",
        "Neutral": "中性",
        "Bearish": "偏空",
        "Strong Bearish": "強空",
        "Insufficient Data": "資料不足",
        "buy": "買超",
        "sell": "賣超",
        "flat": "持平",
        "up": "上升",
        "down": "下降",
        "unknown": "未知",
        "strong": "強",
        "moderate": "中等",
        "weak": "弱",
        "high": "高",
        "medium": "中",
        "low": "低",
        "risk_on": "風險偏好",
        "risk_off": "風險趨避",
        "neutral": "中性",
        "bullish": "偏多",
        "bearish": "偏空",
        "inferred_bullish": "推論偏多",
        "inferred_bearish": "推論偏空",
        "watch": "觀察",
        "known_price_only": "僅有最新價",
        "rising": "上升",
        "falling": "下降",
        "stable": "穩定",
        "cost_pressure": "成本壓力",
        "cost_relief": "成本緩解",
        "freight_supportive": "支撐運價",
        "positive": "正面",
        "negative": "負面",
        "mixed": "混合",
        "elevated": "偏高",
        "improving": "改善",
        "normalizing": "正常化",
        "today_material_event": "今日重大事件",
        "recent_event_within_7_days": "7 日內事件",
        "stale_event_over_14_days": "超過 14 日舊事件",
        "fetch_failed": "抓取失敗",
        "none": "未發現重大事件",
    }
    return mapping.get(value, str(value or "未知"))


def _tw_state(value: Any) -> str:
    text = str(value or "資料不足")
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return _tw_label(text)


def _tw_risk(value: Any) -> str:
    text = str(value or "")
    if "High" in text:
        return "高風險"
    if "Medium" in text:
        return "中風險"
    if "Low" in text:
        return "低風險"
    return _tw_label(value)


def _tw_num(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "資料不足"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.000001:
        return f"{number:,.0f}"
    return f"{number:,.{decimals}f}"


def _tw_shares_to_lots(value: Any) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value) / 1000:,.1f} 張"
    except (TypeError, ValueError):
        return str(value)


def _tw_fmt(self: AnalysisService, value: Any) -> str:
    return _tw_num(value)


def _tw_pct(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _tw_summary(
    self: AnalysisService,
    symbol: str,
    market_data: dict[str, Any],
    scores: dict[str, Any],
    freshness: dict[str, Any],
    mode: str,
    position_advice: dict[str, Any],
    action_plan: dict[str, Any],
    analysis_mode: str,
    truthfulness: dict[str, Any],
    openai_error: str = "",
) -> dict[str, Any]:
    timing_note = "方向偏多，但短線不適合追。" if scores["revised_score"].get("timing_score", 0) < 50 else ""
    market_state = _tw_state(scores["market_state"])
    risk_level = _tw_risk(scores.get("risk_level"))
    gaps = [_tw_gap_label(key) for key, ok in scores["coverage_items"].items() if not ok]
    return {
        "market_state": market_state,
        "conviction_score": scores["coverage_adjusted_score"],
        "raw_score": scores["raw_score"],
        "data_coverage": scores["data_coverage"],
        "truthfulness_score": truthfulness.get("truthfulness_score"),
        "risk_level": risk_level,
        "action": action_plan["recommendation"],
        "buy_advice": action_plan["buy_advice"],
        "sell_advice": action_plan["sell_advice"],
        "suggested_lots": action_plan["suggested_lots"],
        "one_line": f"{symbol} 目前為{market_state}，資料覆蓋率 {scores['data_coverage']}%，可信度 {truthfulness.get('truthfulness_score')}/100，綜合分數 {scores['coverage_adjusted_score']}/100。{timing_note}",
        "primary_risk": freshness.get("warning") or action_plan.get("data_caution") or "SCFI、ETF、紅海與市場環境資料仍需交叉確認。",
        "key_data_gaps": gaps,
        "analysis_mode_note": "OpenAI 分析完成。" if analysis_mode != "fallback" else f"OpenAI 未完成，本次使用本機規則。{openai_error}",
    }


def _tw_gap_label(key: str) -> str:
    mapping = {
        "stock": "股價資料",
        "institutional": "法人籌碼",
        "freight": "SCFI / 航線運價",
        "news": "新聞事件",
        "fundamental": "基本面資料",
        "announcements": "公司公告與法說",
        "etf": "ETF 精確持股 / 基金規模變化",
        "red_sea": "紅海與繞航風險",
        "market_regime": "市場環境完整資料",
    }
    return mapping.get(key, key)


def _tw_inst_line(inst: dict[str, Any]) -> str:
    latest = inst.get("latest") or {}
    if not latest:
        return "法人最新資料不足"
    return (
        f"{latest.get('date', '日期未知')}："
        f"外資 {_tw_shares_to_lots(latest.get('foreign'))}、"
        f"投信 {_tw_shares_to_lots(latest.get('trust'))}、"
        f"自營商 {_tw_shares_to_lots(latest.get('dealer'))}、"
        f"合計 {_tw_shares_to_lots(latest.get('total'))}"
    )


def _tw_flow_lines(inst: dict[str, Any]) -> str:
    sums = inst.get("flow_sums") or {}
    if not sums:
        return "- 近 1/3/5/10 日：資料不足"
    rows = []
    for key, label in [("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商"), ("total", "三大法人合計")]:
        row = sums.get(key) or {}
        rows.append(
            f"- {label}：1 日 {_tw_shares_to_lots(row.get('1d'))}、"
            f"3 日 {_tw_shares_to_lots(row.get('3d'))}、"
            f"5 日 {_tw_shares_to_lots(row.get('5d'))}、"
            f"10 日 {_tw_shares_to_lots(row.get('10d'))}"
        )
    return "\n".join(rows)


def _tw_modules(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    intl = market.get("international_events", {})
    oil = intl.get("oil") or {}
    oil_prices = intl.get("oil_prices") or {}
    wti = oil_prices.get("wti") or {}
    brent = oil_prices.get("brent") or {}
    policy = intl.get("us_policy") or {}
    war = intl.get("war_geopolitics") or {}
    fill = market.get("fill_dividend_probability", {})
    return f"""## 國際事件與事件風險
### 美國政策與貿易
- 狀態：{_tw_label(policy.get("status"))}
- 影響：{_tw_label(policy.get("impact"))}
- 解讀：{policy.get("summary") or "目前沒有明確訊號。"}

### 戰爭與地緣政治
- 狀態：{_tw_label(war.get("status"))}
- 影響：{_tw_label(war.get("impact"))}
- 解讀：{war.get("summary") or "目前沒有明確訊號。"}

### 油價
- 西德州原油（WTI）：{self._fmt(wti.get("close"))}，近 5 日變化 {self._fmt(wti.get("change_5d_pct"))}%
- 布蘭特原油（Brent）：{self._fmt(brent.get("close"))}，近 5 日變化 {self._fmt(brent.get("change_5d_pct"))}%
- 對航運成本影響：{_tw_label(oil.get("impact"))}
- 解讀：{oil.get("summary") or "油價資料不足。"}

### 國際事件總結
- 整體風險：{_tw_label(intl.get("overall_risk"))}
- 信心分數：{intl.get("confidence", 0)}
- 摘要：{intl.get("summary") or "資料不足。"}

## ETF 被動買盤
- 判斷：{_tw_label(etf.get("etf_flow"))}
- 主要 ETF：{", ".join(etf.get("top_etfs") or []) or "資料不足"}
- 信心分數：{etf.get("confidence", 0)}
- 限制：{etf.get("missing_reason") or "無明確限制"}

## 紅海與繞航風險
- 狀態：{_tw_label(red.get("status"))}
- 對航運影響：{_tw_label(red.get("shipping_impact"))}
- 蘇伊士回歸風險：{_tw_label(red.get("suez_return_risk"))}
- 信心分數：{red.get("confidence", 0)}
- 摘要：{red.get("summary") or "資料不足"}

## 公司公告與法說
- 事件狀態：{_tw_label(ann.get("latest_event"))}
- 重大性：{_tw_label(ann.get("materiality"))}
- 解讀：抓取失敗不可解讀為沒有公告；超過 14 日的事件不可列為今日重大事件。

## 市場環境
- 市場環境：{_tw_label(regime.get("market_regime"))}
- 台股：{_tw_label(regime.get("taiwan_market"))}
- 航運族群：{_tw_label(regime.get("shipping_sector"))}
- 美股：{_tw_label(regime.get("us_market"))}
- 信心分數：{regime.get("confidence", 0)}

## 填息機率
- 30 日填息機率：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 日填息機率：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年填息機率：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- 主要依據：歷史填息、運價方向、法人籌碼、ETF 支撐、技術面、市場環境
- 風險：{", ".join(fill.get("risks") or []) or "資料不足"}"""


def _tw_position_section(self: AnalysisService, mode: str, position: dict[str, Any], action_plan: dict[str, Any]) -> str:
    if mode != "personalized" or not position.get("available"):
        return """## 對目前部位的建議
- 一般模式不讀取使用者持股。
- 以下買賣區間只代表市場層面的觀察，不是個人部位建議。"""
    return f"""## 對我目前部位的建議
- 持股張數：{position.get("lots", "資料不足")} 張
- 均價：{position.get("average_cost", "資料不足")}
- 目前損益：{self._fmt(position.get("unrealized_pnl"))}
- 核心部位：{position.get("core_lots", "資料不足")} 張
- 機動部位：{position.get("flexible_lots", "資料不足")} 張
- 今日是否建議賣：{action_plan.get("recommendation")}
- 建議賣出張數：{action_plan.get("suggested_lots")} 張
- 若漲到 245~250：可考慮賣 2~3 張機動部位
- 若漲到 255~260：可考慮再賣 3~5 張
- 若跌回 220~230：若基本面未轉弱，可觀察買回
- 若跌破 20MA：若同時 SCFI 轉弱與法人轉賣，風險升高"""


def _tw_compose_report(
    self: AnalysisService,
    symbol: str,
    mode: str,
    payload: dict[str, Any],
    position: dict[str, Any],
    action_plan: dict[str, Any],
    summary: dict[str, Any],
    top_only: bool = False,
) -> str:
    market = payload["market_data"]
    stock = market["stock"]
    freight = market["freight"]
    freight_intel = freight.get("intelligence") or {}
    inst = market["institutional"]
    fundamentals = market["fundamentals"]
    scores = payload["local_scores"]
    revised = scores["revised_score"]
    freshness = payload["data_freshness"]
    truth = payload["truthfulness"]
    timing_note = "方向偏多，但短線不適合追。" if revised.get("timing_score", 0) < 50 else "位置尚可，但仍需等訊號確認。"
    market_state = _tw_state(summary["market_state"])
    risk_level = _tw_risk(summary.get("risk_level"))
    one_line = f"{symbol} 目前為{market_state}，資料覆蓋率 {summary.get('data_coverage')}%，可信度 {summary.get('truthfulness_score')}/100，綜合分數 {summary.get('conviction_score')}/100。{timing_note}"
    gaps = summary.get("key_data_gaps") or []

    brief = f"""# 即時 AI 投資分析報告

## 決策摘要
1. 今日結論：**{market_state}**
2. 一句話：{one_line}
3. 方向分數：{revised.get("direction_score")}/100
4. 風險分數：{revised.get("risk_score")}/100，{risk_level}
5. 今日動作：**{summary["action"]}**
6. 買進建議：{summary.get("buy_advice")}
7. 賣出建議：{summary.get("sell_advice")}
8. 下一個賣點：{action_plan.get("next_sell_point")}
9. 下一個買回點：{action_plan.get("next_buyback_point")}
10. 改變看法的條件：{action_plan.get("view_change_conditions")}"""
    if top_only:
        return brief

    return brief + f"""

## 詳細報告

## 今日操作建議
- 建議：{action_plan["recommendation"]}
- 建議張數：{action_plan["suggested_lots"]} 張
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}
- 短線提醒：{timing_note}

{_tw_position_section(self, mode, position, action_plan)}

## 本次分析最大資料缺口
{chr(10).join(f"- {item}" for item in gaps) if gaps else "- 無核心缺口"}
- 影響：資料缺漏會降低資料覆蓋率與可信度，系統不會把缺漏資料當成中性，也不會硬給強結論。

## 市場資料快照
- 股價資料日期：{freshness.get("price_data_date") or "資料不足"}
- 分析時間：{freshness.get("analysis_time") or "資料不足"}
- 是否即時資料：{"是" if freshness.get("is_realtime_price") else "否"}
- 是否收盤資料：{"是" if freshness.get("is_closing_price") else "否"}
- 收盤價：{self._fmt(stock.get("close"))}
- 成交量：{self._fmt(stock.get("volume"))}
- 20 日均線：{self._fmt(stock.get("ma20"))}
- 60 日均線：{self._fmt(stock.get("ma60"))}
- 相對強弱指標（RSI）：{self._fmt((stock.get("technical") or {}).get("rsi14"))}
- 指數平滑異同移動平均線（MACD）：{self._fmt((stock.get("technical") or {}).get("macd"))}
- 每股盈餘（EPS）：{self._fmt(fundamentals.get("eps"))}
- 股利殖利率：{self._fmt(fundamentals.get("dividend_yield"))}

## 運價智慧分析
- 整體方向：{_tw_label(freight_intel.get("overall_trend"))}
- 強度：{_tw_label(freight_intel.get("strength"))}
- 信心分數：{freight_intel.get("confidence", 0)}
- 來源數量：{freight_intel.get("source_count", 0)}
- 上海出口集裝箱運價指數（SCFI）最新值：{self._fmt(freight.get("scfi_latest"))}
- SCFI 週變化：{self._fmt(freight.get("weekly_change"))}%
- 連續上漲/下跌週數：{self._fmt(freight.get("scfi_streak_weeks"))}
- 美西線：{self._fmt(freight.get("us_west"))}，週變化 {self._fmt(freight.get("us_west_weekly_change"))}%
- 美東線：{self._fmt(freight.get("us_east"))}，週變化 {self._fmt(freight.get("us_east_weekly_change"))}%
- 歐洲線：{self._fmt(freight.get("europe"))}，週變化 {self._fmt(freight.get("europe_weekly_change"))}%
- 對長榮的意義：{_e_freight_meaning(freight, freight_intel)}

## 法人籌碼
- 最新資料：{_tw_inst_line(inst)}
- 連續趨勢：{_tw_label((inst.get("consecutive_trend") or {}).get("direction"))}，{(inst.get("consecutive_trend") or {}).get("days", "資料不足")} 日
{_tw_flow_lines(inst)}
- 單位說明：FinMind 原始資料為「股」，報告已換算成「張」。
- 提醒：若投信多日為 0，可能是資料源分類不完整，需與 TWSE 或券商資料交叉確認。

{self._decision_modules_markdown(market)}

## 修正後信心分數（Conviction Score）
- 方向分數：{revised.get("direction_score")}
- 時機分數：{revised.get("timing_score")}
- 估值分數：{revised.get("valuation_score")}
- 風險分數：{revised.get("risk_score")}
- 資料覆蓋率：{revised.get("data_coverage")}
- 資料可信度：{revised.get("truthfulness_score")}
- 綜合分數：{revised.get("overall_score")}
- 解讀：{timing_note}

## 多空辯論
### 多方論點
- 運價方向若持續上升，對長榮 EPS、填息與股價支撐較有利。
- 股價若站穩 20 日均線且法人賣壓收斂，短線結構改善。
- 若紅海與繞航風險延續，貨櫃航運供給有效運力仍可能偏緊。

### 空方論點
- RSI 過熱或價格接近壓力區時，短線追價風險升高。
- 外資連賣、投信未明確承接、ETF 資料不足時，不宜把被動買盤當強支撐。
- 美國政策、戰爭與油價若轉為成本壓力，會降低估值與風險分數。

### 最終判斷
- 目前結論：{one_line}

## 資料品質
- 資料可信度：{truth.get("truthfulness_score")}/100
- 資料覆蓋率：{scores.get("data_coverage")}%
- 精確資料筆數：{len((payload.get("data_quality") or {}).get("exact_data") or [])}
- 爬取資料筆數：{len((payload.get("data_quality") or {}).get("scraped_data") or [])}
- 搜尋推論資料筆數：{len((payload.get("data_quality") or {}).get("search_inferred_data") or [])}
- 缺漏資料筆數：{len((payload.get("data_quality") or {}).get("missing_data") or [])}

## 免責聲明
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。"""


AnalysisService._summary = _tw_summary
AnalysisService._compose_report = _tw_compose_report
AnalysisService._decision_modules_markdown = _tw_modules
AnalysisService._fmt = _tw_fmt
AnalysisService._pct_or_missing = _tw_pct


# Evidence-rich Traditional Chinese report renderer. Keep this block last.
def _e_label(value: Any) -> str:
    mapping = {
        "Strong Bullish": "強多",
        "Bullish": "偏多",
        "Neutral-Bullish": "中性偏多",
        "Neutral": "中性",
        "Bearish": "偏空",
        "Strong Bearish": "強空",
        "Insufficient Data": "資料不足",
        "buy": "買超",
        "sell": "賣超",
        "flat": "持平",
        "up": "上升",
        "down": "下降",
        "unknown": "未知",
        "strong": "強",
        "moderate": "中等",
        "weak": "弱",
        "high": "高",
        "medium": "中",
        "low": "低",
        "risk_on": "風險偏好",
        "risk_off": "風險趨避",
        "neutral": "中性",
        "bullish": "偏多",
        "bearish": "偏空",
        "inferred_bullish": "推論偏多",
        "inferred_bearish": "推論偏空",
        "watch": "觀察",
        "known_price_only": "僅有最新價",
        "rising": "上升",
        "falling": "下降",
        "stable": "穩定",
        "cost_pressure": "成本壓力",
        "cost_relief": "成本緩解",
        "freight_supportive": "支撐運價",
        "positive": "正面",
        "negative": "負面",
        "mixed": "多空混合",
        "elevated": "偏高",
        "improving": "改善",
        "normalizing": "正常化",
        "today_material_event": "今日重大事件",
        "recent_event_within_7_days": "7 日內事件",
        "stale_event_over_14_days": "超過 14 日舊事件",
        "fetch_failed": "抓取失敗",
        "none": "未發現重大事件",
    }
    return mapping.get(value, str(value or "未知"))


def _e_state(value: Any) -> str:
    text = str(value or "資料不足")
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return _e_label(text)


def _e_risk(value: Any) -> str:
    text = str(value or "")
    if "High" in text:
        return "高風險"
    if "Medium" in text:
        return "中風險"
    if "Low" in text:
        return "低風險"
    return _e_label(value)


def _e_num(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "資料不足"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.000001:
        return f"{number:,.0f}"
    return f"{number:,.{decimals}f}"


def _e_pct(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value):,.{decimals}f}%"
    except (TypeError, ValueError):
        return str(value)


def _e_lots(value: Any) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value) / 1000:,.1f} 張"
    except (TypeError, ValueError):
        return str(value)


def _e_fmt(self: AnalysisService, value: Any) -> str:
    return _e_num(value)


def _e_pct_prob(self: AnalysisService, value: Any) -> str:
    if value is None:
        return "資料不足"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _e_gap_label(key: str) -> str:
    mapping = {
        "stock": "股價資料",
        "institutional": "法人籌碼",
        "freight": "SCFI / 航線運價",
        "news": "新聞事件",
        "fundamental": "基本面資料",
        "announcements": "公司公告與法說",
        "etf": "ETF 精確持股 / 基金規模變化",
        "red_sea": "紅海與繞航風險",
        "market_regime": "市場環境完整資料",
    }
    return mapping.get(key, key)


def _e_sources(sources: list[dict[str, Any]], limit: int = 18) -> str:
    rows = []
    seen = set()
    for source in sources:
        name = source.get("name") or "資料來源"
        url = source.get("url") or ""
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        suffix = f"（截至 {source.get('as_of')}）" if source.get("as_of") else ""
        rows.append(f"- {name}{suffix}：{url or '未提供網址'}")
        if len(rows) >= limit:
            break
    return "\n".join(rows) if rows else "- 資料來源不足"


def _e_inst_line(inst: dict[str, Any]) -> str:
    latest = inst.get("latest") or {}
    if not latest:
        return "法人最新資料不足"
    return (
        f"{latest.get('date', '日期未知')}："
        f"外資 {_e_lots(latest.get('foreign'))}、"
        f"投信 {_e_lots(latest.get('trust'))}、"
        f"自營商 {_e_lots(latest.get('dealer'))}、"
        f"合計 {_e_lots(latest.get('total'))}"
    )


def _e_flow_lines(inst: dict[str, Any]) -> str:
    sums = inst.get("flow_sums") or {}
    if not sums:
        return "- 近 1/3/5/10 日：資料不足"
    rows = []
    for key, label in [("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商"), ("total", "三大法人合計")]:
        row = sums.get(key) or {}
        rows.append(
            f"- {label}：1 日 {_e_lots(row.get('1d'))}、"
            f"3 日 {_e_lots(row.get('3d'))}、"
            f"5 日 {_e_lots(row.get('5d'))}、"
            f"10 日 {_e_lots(row.get('10d'))}"
        )
    return "\n".join(rows)


def _e_market_snapshot(regime: dict[str, Any]) -> str:
    snapshot = regime.get("market_snapshot") or {}
    if not snapshot:
        return "- 指數快照：資料不足"
    labels = {
        "taiwan_index": "台股加權指數",
        "sp500": "美股 S&P 500",
        "vix": "VIX 恐慌指數",
        "dxy": "美元指數",
        "usd_twd": "美元兌台幣",
    }
    rows = []
    for key, row in snapshot.items():
        rows.append(
            f"- {labels.get(key, key)}：{_e_num(row.get('close'))}，"
            f"日期 {row.get('date') or '未知'}，來源 {row.get('source') or '未知'}，"
            f"近 5 日變化 {_e_pct(row.get('change_5d_pct'))}"
        )
    return "\n".join(rows)


def _e_freight_meaning(freight: dict[str, Any], freight_intel: dict[str, Any]) -> str:
    trend = _e_label(freight_intel.get("overall_trend"))
    strength = _e_label(freight_intel.get("strength"))
    confidence = freight_intel.get("confidence", 0)
    source_count = freight_intel.get("source_count", 0)
    weeks = freight_intel.get("weeks_up_or_down")
    exact_count = freight_intel.get("exact_route_count", 0)
    route_bits = []
    for key, label in [("us_west", "美西"), ("us_east", "美東"), ("europe", "歐洲")]:
        value = freight.get(key)
        change = freight.get(f"{key}_weekly_change")
        if value is not None or change is not None:
            route_bits.append(f"{label}線運價 {_e_num(value)}、週變化 {_e_pct(change)}")
    route_text = "；".join(route_bits) if route_bits else "細分航線精確數字仍不足"
    week_text = f"，連續週數 {weeks}" if weeks is not None else ""
    return (
        f"運價方向為{trend}，強度{strength}，信心 {confidence}，來源數 {source_count}{week_text}。"
        f"{route_text}。若方向持續上升，較支持長榮短期獲利與填息敘事；"
        "若後續轉為連跌，應下修方向分數與風險評估。"
        f"目前細分航線精確資料筆數：{exact_count}。"
    )


def _e_news_lines(market: dict[str, Any]) -> str:
    articles = (market.get("news_relevance") or {}).get("articles") or []
    if not articles:
        raw = (market.get("news") or {}).get("articles") or []
        return f"- 高相關新聞：資料不足；原始新聞筆數 {_e_num(len(raw), 0)}"
    rows = []
    for item in articles[:5]:
        rows.append(
            f"- {item.get('title') or '無標題'}"
            f"（相關度 {_e_num(item.get('relevance_score'))}，情緒 {_e_label(item.get('sentiment'))}）"
        )
    return "\n".join(rows)


def _e_quality_examples(data_quality: dict[str, Any], truth: dict[str, Any]) -> str:
    def rows(title: str, items: list[str], limit: int = 4) -> str:
        shown = items[:limit]
        body = "\n".join(f"- {item}" for item in shown) if shown else "- 無"
        return f"### {title}\n{body}"

    warnings = truth.get("warnings") or []
    warning_text = "\n".join(f"- {item}" for item in warnings) if warnings else "- 無重大可信度警告"
    return "\n".join(
        [
            "### 可信度拆解",
            f"- 精確資料占比：{_e_num((truth.get('exact_data_share') or 0) * 100, 0)}%",
            f"- 爬取 / OCR 資料占比：{_e_num((truth.get('scraped_data_share') or 0) * 100, 0)}%",
            f"- 搜尋推論占比：{_e_num((truth.get('search_inferred_share') or 0) * 100, 0)}%",
            f"- 過期或可疑資料占比：{_e_num((truth.get('stale_data_share') or 0) * 100, 0)}%",
            f"- 缺漏資料占比：{_e_num((truth.get('missing_data_share') or 0) * 100, 0)}%",
            "",
            rows("代表性精確資料", data_quality.get("exact_data") or []),
            "",
            rows("代表性搜尋推論資料", data_quality.get("search_inferred_data") or []),
            "",
            rows("過期或可疑資料", data_quality.get("stale_or_suspicious_data") or []),
            "",
            "### 可信度警告",
            warning_text,
        ]
    )


def _e_summary(
    self: AnalysisService,
    symbol: str,
    market_data: dict[str, Any],
    scores: dict[str, Any],
    freshness: dict[str, Any],
    mode: str,
    position_advice: dict[str, Any],
    action_plan: dict[str, Any],
    analysis_mode: str,
    truthfulness: dict[str, Any],
    openai_error: str = "",
) -> dict[str, Any]:
    market_state = _e_state(scores["market_state"])
    risk_level = _e_risk(scores.get("risk_level"))
    timing_note = "方向偏多，但短線不適合追。" if scores["revised_score"].get("timing_score", 0) < 50 else "短線位置尚可，但仍需看資料品質。"
    gaps = [_e_gap_label(key) for key, ok in scores["coverage_items"].items() if not ok]
    return {
        "market_state": market_state,
        "conviction_score": scores["coverage_adjusted_score"],
        "raw_score": scores["raw_score"],
        "data_coverage": scores["data_coverage"],
        "truthfulness_score": truthfulness.get("truthfulness_score"),
        "risk_level": risk_level,
        "action": action_plan["recommendation"],
        "buy_advice": action_plan["buy_advice"],
        "sell_advice": action_plan["sell_advice"],
        "suggested_lots": action_plan["suggested_lots"],
        "one_line": f"{symbol} 目前為{market_state}，資料覆蓋率 {scores['data_coverage']}%，可信度 {truthfulness.get('truthfulness_score')}/100，綜合分數 {scores['coverage_adjusted_score']}/100。{timing_note}",
        "primary_risk": freshness.get("warning") or action_plan.get("data_caution") or "SCFI、ETF、紅海與市場環境資料仍需交叉確認。",
        "key_data_gaps": gaps,
        "analysis_mode_note": "OpenAI 分析完成。" if analysis_mode != "fallback" else f"OpenAI 未完成，本次使用本機規則。{openai_error}",
    }


def _e_action_plan(self: AnalysisService, mode: str, market_data: dict[str, Any], scores: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    stock = market_data["stock"]
    freight = market_data["freight"]
    inst = market_data["institutional"]
    close = stock.get("close")
    ma20 = stock.get("ma20")
    revised = scores.get("revised_score", {})
    risk = revised.get("risk_score", 50)
    timing = revised.get("timing_score", 50)
    direction = revised.get("direction_score", 50)
    freight_trend = (freight.get("intelligence") or {}).get("overall_trend")
    inst_trend = (inst.get("consecutive_trend") or {}).get("direction")

    lots = 0
    if risk < 50:
        recommendation = "警戒但不積極操作"
    elif timing < 50 and direction >= 55:
        recommendation = "方向偏多，但短線不追價"
    elif mode == "personalized" and position.get("available"):
        recommendation = position.get("recommendation") or "不動"
        lots = position.get("suggested_sell_lots", 0)
    elif close is not None and close >= 255:
        recommendation = "接近第二賣點，分批評估"
        lots = 3
    elif close is not None and close >= 245:
        recommendation = "接近第一賣點，可小幅分批"
        lots = 2
    else:
        recommendation = "不追價，等價格或資料訊號更明確"

    if close is not None and ma20 is not None and close < ma20 and freight_trend == "down":
        recommendation = "跌破 20 日均線且運價轉弱，提高警戒"

    reason = (
        f"方向分數 {direction}/100、時機分數 {timing}/100、風險分數 {risk}/100；"
        f"運價方向為{_e_label(freight_trend)}，法人合計趨勢為{_e_label(inst_trend)}。"
    )
    return {
        "recommendation": recommendation,
        "suggested_lots": lots,
        "buy_advice": "220~230：若運價與基本面未轉弱，可觀察買回；200~220：需重新評估風險後再分批。",
        "sell_advice": "245~250：第一個機動賣點；255~260：第二個機動賣點；270 以上：重新評估是否進一步減碼。",
        "reason": reason,
        "trigger": "價格進入 245~250、255~260、270 以上，或跌破 20 日均線且 SCFI / 法人同步轉弱。",
        "invalidated_by": "SCFI 連跌、外資與投信同步賣超、ETF 支撐消失、紅海風險解除、或價格跌破 20 日均線後量增轉弱。",
        "next_sell_point": "245~250；若站穩再看 255~260。",
        "next_buyback_point": "220~230；前提是運價與基本面未轉弱。",
        "view_change_conditions": "SCFI 連跌、法人同步賣超、ETF 減碼、紅海風險解除、跌破 20 日均線後量增轉弱。",
        "data_caution": "" if scores.get("data_coverage", 0) >= 80 else "資料覆蓋率不足，操作建議需保守。",
    }


def _e_position_advice(self: AnalysisService, symbol: str, market_data: dict[str, Any], profile: dict[str, Any] | None, scores: dict[str, Any]) -> dict[str, Any]:
    if not profile:
        return {"available": False, "reason": "找不到 user_profile.yaml。"}
    stock = market_data["stock"]
    close = stock.get("close")
    ma20 = stock.get("ma20")
    positions = profile.get("positions") or []
    position = next((row for row in positions if row.get("symbol") == symbol), None)
    if not position or close is None:
        return {"available": False, "reason": "user_profile.yaml 沒有對應持股，或股價資料不足。"}

    lots = position.get("lots", 0)
    avg = position.get("average_cost", 0)
    shares = position.get("shares") or lots * 1000
    pnl = (close - avg) * shares if avg else None
    risk = scores.get("revised_score", {}).get("risk_score", 50)
    sell = 0
    recommendation = "不動"
    if risk < 50:
        recommendation = "警戒但不急著賣核心，先確認 SCFI、法人與市場風險是否同步轉弱"
    elif close >= 270:
        sell, recommendation = 3, "重新評估，可考慮再賣 3~5 張機動部位"
    elif close >= 255:
        sell, recommendation = 3, "可考慮再賣 3~5 張機動部位"
    elif close >= 245:
        sell, recommendation = 2, "可考慮賣 2~3 張機動部位"
    elif ma20 is not None and close < ma20:
        recommendation = "跌破 20 日均線，提高警戒但不急著賣核心"
    return {
        "available": True,
        "lots": lots,
        "average_cost": avg,
        "unrealized_pnl": pnl,
        "core_lots": position.get("core_lots"),
        "flexible_lots": position.get("flexible_lots"),
        "recommendation": recommendation,
        "suggested_sell_lots": sell,
        "sell_today": sell > 0,
    }


def _e_general_position_advice(self: AnalysisService) -> dict[str, Any]:
    return {"available": False, "reason": "一般模式不讀取 user_profile.yaml，只輸出市場層面的買賣區間。"}


def _e_position_section(self: AnalysisService, mode: str, position: dict[str, Any], action_plan: dict[str, Any]) -> str:
    if mode != "personalized" or not position.get("available"):
        return """## 對目前部位的建議
- 一般模式不讀取使用者持股。
- 以下買賣區間只代表市場層面的觀察，不是個人部位建議。"""
    return f"""## 對我目前部位的建議
- 持股張數：{position.get("lots", "資料不足")} 張
- 均價：{position.get("average_cost", "資料不足")}
- 目前未實現損益：{self._fmt(position.get("unrealized_pnl"))}
- 核心部位：{position.get("core_lots", "資料不足")} 張
- 機動部位：{position.get("flexible_lots", "資料不足")} 張
- 今日是否建議賣：{action_plan.get("recommendation")}
- 建議賣出張數：{action_plan.get("suggested_lots")} 張
- 245~250：可考慮賣 2~3 張機動部位
- 255~260：可考慮再賣 3~5 張
- 220~230：若基本面與運價未轉弱，可觀察買回
- 跌破 20 日均線：若同時 SCFI 轉弱與法人轉賣，風險升高"""


def _e_modules(self: AnalysisService, market: dict[str, Any]) -> str:
    etf = market.get("etf_flow", {})
    red = market.get("red_sea", {})
    ann = market.get("announcement_intelligence", {})
    regime = market.get("market_regime", {})
    intl = market.get("international_events", {})
    oil = intl.get("oil") or {}
    oil_prices = intl.get("oil_prices") or {}
    wti = oil_prices.get("wti") or {}
    brent = oil_prices.get("brent") or {}
    policy = intl.get("us_policy") or {}
    war = intl.get("war_geopolitics") or {}
    fill = market.get("fill_dividend_probability", {})
    return f"""## 國際事件與事件風險
### 美國政策與貿易
- 證據：{policy.get("summary") or "目前沒有明確訊號。"}
- 判讀：狀態為{_e_label(policy.get("status"))}，影響為{_e_label(policy.get("impact"))}。

### 戰爭與地緣政治
- 證據：{war.get("summary") or "目前沒有明確訊號。"}
- 判讀：狀態為{_e_label(war.get("status"))}，對航運影響為{_e_label(war.get("impact"))}。

### 油價
- 證據：西德州原油（WTI）{self._fmt(wti.get("close"))}，日期 {wti.get("date") or "未知"}，來源 {wti.get("source") or "未知"}。
- 證據：布蘭特原油（Brent）{self._fmt(brent.get("close"))}，日期 {brent.get("date") or "未知"}，來源 {brent.get("source") or "未知"}。
- 判讀：{oil.get("summary") or "油價資料不足。"} 對航運成本影響為{_e_label(oil.get("impact"))}。

## ETF 被動買盤
- 證據：觀察 ETF 為 {", ".join(etf.get("top_etfs") or []) or "資料不足"}。
- 判讀：{_e_label(etf.get("etf_flow"))}，信心分數 {etf.get("confidence", 0)}。
- 限制：{etf.get("missing_reason") or "無明確限制"}

## 紅海與繞航風險
- 證據：{red.get("summary") or "資料不足"}
- 判讀：狀態為{_e_label(red.get("status"))}，對航運影響為{_e_label(red.get("shipping_impact"))}，信心分數 {red.get("confidence", 0)}。

## 公司公告與法說
- 證據：事件狀態為{_e_label(ann.get("latest_event"))}，重大性為{_e_label(ann.get("materiality"))}。
- 判讀：抓取失敗不可解讀為沒有公告；超過 14 日的事件不可列為今日重大事件。

## 市場環境
- 證據：市場環境為{_e_label(regime.get("market_regime"))}，台股{_e_label(regime.get("taiwan_market"))}，航運族群{_e_label(regime.get("shipping_sector"))}，美股{_e_label(regime.get("us_market"))}。
- 信心分數：{regime.get("confidence", 0)}
{_e_market_snapshot(regime)}
- 限制：{self._user_message(regime.get("missing_reason") or "無重大限制")}

## 填息機率
- 30 日填息機率：{self._pct_or_missing(fill.get("fill_probability_30d"))}
- 90 日填息機率：{self._pct_or_missing(fill.get("fill_probability_90d"))}
- 1 年填息機率：{self._pct_or_missing(fill.get("fill_probability_1y"))}
- 主要證據：歷史填息、運價方向、法人籌碼、ETF 支撐、技術面、市場環境。
- 風險：{", ".join(fill.get("risks") or []) or "資料不足"}"""


def _e_compose_report(
    self: AnalysisService,
    symbol: str,
    mode: str,
    payload: dict[str, Any],
    position: dict[str, Any],
    action_plan: dict[str, Any],
    summary: dict[str, Any],
    top_only: bool = False,
) -> str:
    market = payload["market_data"]
    stock = market["stock"]
    freight = market["freight"]
    freight_intel = freight.get("intelligence") or {}
    inst = market["institutional"]
    fundamentals = market["fundamentals"]
    scores = payload["local_scores"]
    revised = scores["revised_score"]
    freshness = payload["data_freshness"]
    truth = payload["truthfulness"]
    data_quality = payload.get("data_quality") or {}
    timing_note = "方向偏多，但短線不適合追。" if revised.get("timing_score", 0) < 50 else "短線位置尚可，但仍需看資料品質。"
    market_state = _e_state(summary["market_state"])
    risk_level = _e_risk(summary.get("risk_level"))
    gaps = summary.get("key_data_gaps") or []
    one_line = f"{symbol} 目前為{market_state}，資料覆蓋率 {summary.get('data_coverage')}%，可信度 {summary.get('truthfulness_score')}/100，綜合分數 {summary.get('conviction_score')}/100。{timing_note}"

    brief = f"""# 即時 AI 投資分析報告

## 決策摘要
1. 今日結論：**{market_state}**
2. 一句話：{one_line}
3. 今日動作：**{summary["action"]}**
4. 買進建議：{summary.get("buy_advice")}
5. 賣出建議：{summary.get("sell_advice")}
6. 下一個賣點：{action_plan.get("next_sell_point")}
7. 下一個買回點：{action_plan.get("next_buyback_point")}
8. 主要風險：{summary.get("primary_risk")}
9. 改變看法的條件：{action_plan.get("view_change_conditions")}
10. 資料可信度：{summary.get("truthfulness_score")}/100"""
    if top_only:
        return brief

    return brief + f"""

## 今日操作建議
- 建議：{action_plan["recommendation"]}
- 建議張數：{action_plan["suggested_lots"]} 張
- 理由：{action_plan["reason"]}
- 觸發條件：{action_plan["trigger"]}
- 失效條件：{action_plan["invalidated_by"]}
- 短線提醒：{timing_note}

{_e_position_section(self, mode, position, action_plan)}

## 核心證據總表
- 股價與技術：收盤價 {self._fmt(stock.get("close"))}，20 日均線 {self._fmt(stock.get("ma20"))}，60 日均線 {self._fmt(stock.get("ma60"))}，相對強弱指標（RSI）{self._fmt((stock.get("technical") or {}).get("rsi14"))}。
- 運價：SCFI {self._fmt(freight.get("scfi_latest"))}，週變化 {self._fmt(freight.get("weekly_change"))}%，連續週數 {self._fmt(freight.get("scfi_streak_weeks"))}，運價方向{_e_label(freight_intel.get("overall_trend"))}。
- 法人：{_e_inst_line(inst)}。
- 基本面：每股盈餘（EPS）{self._fmt(fundamentals.get("eps"))}，股利殖利率 {self._fmt(fundamentals.get("dividend_yield"))}，月營收年增率 {self._fmt(fundamentals.get("monthly_revenue_yoy"))}%。
- ETF：{_e_label((market.get("etf_flow") or {}).get("etf_flow"))}，信心 {(market.get("etf_flow") or {}).get("confidence", 0)}，但精確持股與基金規模變化仍有限制。
- 市場環境：{_e_label((market.get("market_regime") or {}).get("market_regime"))}，信心 {(market.get("market_regime") or {}).get("confidence", 0)}。

## 股價與技術面證據
- 股價資料日期：{freshness.get("price_data_date") or "資料不足"}
- 分析時間：{freshness.get("analysis_time") or "資料不足"}
- 是否即時資料：{"是" if freshness.get("is_realtime_price") else "否"}
- 是否收盤資料：{"是" if freshness.get("is_closing_price") else "否"}
- 收盤價：{self._fmt(stock.get("close"))}
- 成交量：{self._fmt(stock.get("volume"))}
- 20 日均線：{self._fmt(stock.get("ma20"))}
- 60 日均線：{self._fmt(stock.get("ma60"))}
- 支撐區：{self._fmt(stock.get("support_20d"))}
- 壓力區：{self._fmt(stock.get("resistance_20d"))}
- 相對強弱指標（RSI）：{self._fmt((stock.get("technical") or {}).get("rsi14"))}
- 指數平滑異同移動平均線（MACD）：{self._fmt((stock.get("technical") or {}).get("macd"))}
- 證據判讀：股價相對均線決定方向分數；RSI 過高會降低時機分數，避免追高。

## 運價與航運景氣證據
- SCFI 最新值：{self._fmt(freight.get("scfi_latest"))}
- SCFI 週變化：{self._fmt(freight.get("weekly_change"))}%
- SCFI 連續上漲/下跌週數：{self._fmt(freight.get("scfi_streak_weeks"))}
- 美西線：{self._fmt(freight.get("us_west"))}，週變化 {self._fmt(freight.get("us_west_weekly_change"))}%
- 美東線：{self._fmt(freight.get("us_east"))}，週變化 {self._fmt(freight.get("us_east_weekly_change"))}%
- 歐洲線：{self._fmt(freight.get("europe"))}，週變化 {self._fmt(freight.get("europe_weekly_change"))}%
- 運價智慧判讀：方向{_e_label(freight_intel.get("overall_trend"))}，強度{_e_label(freight_intel.get("strength"))}，信心 {freight_intel.get("confidence", 0)}，來源數 {freight_intel.get("source_count", 0)}。
- 對長榮的意義：{_e_freight_meaning(freight, freight_intel)}

## 法人籌碼證據
- 最新資料：{_e_inst_line(inst)}
- 連續趨勢：{_e_label((inst.get("consecutive_trend") or {}).get("direction"))}，{(inst.get("consecutive_trend") or {}).get("days", "資料不足")} 日
{_e_flow_lines(inst)}
- 單位說明：FinMind 原始資料為「股」，報告已換算成「張」。
- 證據判讀：外資、投信、自營商若同步賣超，會提高風險；若外資賣但投信或 ETF 承接，偏向換手而非單純看空。

## 基本面與股利證據
- 每股盈餘（EPS）：{self._fmt(fundamentals.get("eps"))}
- 本益比（PER）：{self._fmt(fundamentals.get("per"))}
- 股價淨值比（PBR）：{self._fmt(fundamentals.get("pbr"))}
- 股利殖利率：{self._fmt(fundamentals.get("dividend_yield"))}
- 月營收年增率：{self._fmt(fundamentals.get("monthly_revenue_yoy"))}%
- 證據判讀：EPS、股利殖利率與月營收用來支撐估值分數；但航運股仍需和運價同步看。

{self._decision_modules_markdown(market)}

## 新聞與公告證據
{_e_news_lines(market)}
- 公告判讀：{_e_label((market.get("announcement_intelligence") or {}).get("latest_event"))}；重大性 {_e_label((market.get("announcement_intelligence") or {}).get("materiality"))}。

## 多空辯論
### 多方證據
- 運價方向若維持上升，對長榮 EPS、填息與股價支撐較有利。
- 股價若站穩 20 日均線且法人賣壓收斂，短線結構改善。
- 若紅海與繞航風險延續，貨櫃航運有效供給仍可能偏緊。

### 空方證據
- RSI 過熱或價格接近壓力區時，短線追價風險升高。
- 若外資與投信同步賣超，且 ETF 精確持股資料不足，不宜把被動買盤當強支撐。
- 美國政策、戰爭與油價若轉為成本壓力，會降低估值與風險分數。

### 最終判斷
- {one_line}

## 修正後信心分數
- 方向分數：{revised.get("direction_score")}
- 時機分數：{revised.get("timing_score")}
- 估值分數：{revised.get("valuation_score")}
- 風險分數：{revised.get("risk_score")}
- 資料覆蓋率：{revised.get("data_coverage")}
- 資料可信度：{revised.get("truthfulness_score")}
- 綜合分數：{revised.get("overall_score")}
- 解讀：{timing_note}

## 資料品質與限制
- 資料可信度：{truth.get("truthfulness_score")}/100
- 資料覆蓋率：{scores.get("data_coverage")}%
- 精確資料筆數：{len(data_quality.get("exact_data") or [])}
- 爬取資料筆數：{len(data_quality.get("scraped_data") or [])}
- 搜尋推論資料筆數：{len(data_quality.get("search_inferred_data") or [])}
- 缺漏資料筆數：{len(data_quality.get("missing_data") or [])}
- 最大資料缺口：{", ".join(gaps) if gaps else "無核心缺口"}
- 重要限制：搜尋推論不等於精確資料；ETF 與市場環境若只有推論，不能大幅加分。

{_e_quality_examples(data_quality, truth)}

## 資料來源
{_e_sources(payload.get("sources") or [])}

## 免責聲明
這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。"""


AnalysisService._summary = _e_summary
AnalysisService._action_plan = _e_action_plan
AnalysisService._position_advice = _e_position_advice
AnalysisService._general_position_advice = _e_general_position_advice
AnalysisService._compose_report = _e_compose_report
AnalysisService._decision_modules_markdown = _e_modules
AnalysisService._fmt = _e_fmt
AnalysisService._pct_or_missing = _e_pct_prob
