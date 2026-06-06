from __future__ import annotations

import json
from typing import Any


def build_analysis_prompt(payload: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
    """Build a clean Traditional Chinese investment-report prompt."""

    mode = payload.get("mode", "general")
    market_data = payload.get("market_data", {}) or {}
    freight = market_data.get("freight", {}) or {}
    freight_intel = freight.get("intelligence", {}) or {}
    route_status = _freight_route_status(freight)
    profile_text = (
        json.dumps(profile, ensure_ascii=False, indent=2)
        if profile
        else "General Mode：不讀取使用者持股、均價、稅率或個人部位。"
    )

    context = {
        "symbol": payload.get("symbol"),
        "mode": mode,
        "data_freshness": payload.get("data_freshness", {}),
        "summary": payload.get("summary", {}),
        "action_plan": payload.get("action_plan", {}),
        "position_advice": payload.get("position_advice", {}),
        "truthfulness": payload.get("truthfulness", {}),
        "market_data": market_data,
        "local_scores": payload.get("local_scores", {}),
        "missing": payload.get("missing", []),
        "sources": payload.get("sources", []),
        "freight_route_status": route_status,
    }

    return f"""
你是繁體中文的 AI 投資研究員，請根據提供的 JSON context 產生專業、可讀、不可幻想資料的投資分析報告。

重要規則：
- 報告主要使用繁體中文；必要英文術語請放括號，例如「信心分數（Conviction Score）」。
- 不要輸出程式語言或工程字眼，例如 Data Missing、jsonl、python -m、exact_data、search_inferred。
- 若資料不足，請用投資人看得懂的中文描述，例如「資料不足：ETF 實際持股變化尚未取得」。
- 不可把搜尋推論當成官方精確資料；請明確標示「搜尋推論」或「仍需交叉確認」。
- 不可把 API 抓取失敗解讀成事件不存在。
- 不可因為某些非核心資料不足就否定已取得的核心資料。
- 若 OpenAI 無法從 context 找到證據，請寫「資料不足」，不要自行補數字。

航運資料硬規則：
- 目前航線資料狀態：{route_status["label"]}。
- SCFI 最新值：{_value(freight.get("scfi_latest"))}
- SCFI 週變化：{_value(freight.get("weekly_change"))}%
- SCFI 連續週數：{_value(freight.get("scfi_streak_weeks"))}
- 美西線：{_value(freight.get("us_west"))}，週變化 {_value(freight.get("us_west_weekly_change"))}%
- 美東線：{_value(freight.get("us_east"))}，週變化 {_value(freight.get("us_east_weekly_change"))}%
- 歐洲線：{_value(freight.get("europe"))}，週變化 {_value(freight.get("europe_weekly_change"))}%
- Freight Intelligence：方向 {freight_intel.get("overall_trend", "unknown")}，強度 {freight_intel.get("strength", "unknown")}，信心 {freight_intel.get("confidence", 0)}，精確航線筆數 {freight_intel.get("exact_route_count", 0)}。
- 如果美西、美東、歐洲三條航線都有數字與週變化，不得寫「航線資料不足」、「部分航線不完整」、「不能判斷航線方向」。
- 若只有地中海、亞洲區域或其他非主要航線缺漏，請寫「非核心補充航線仍未完整」，不要否定美西、美東、歐洲三條主要航線。

報告格式：

# 即時 AI 投資分析報告

## 1. 一分鐘結論
請用 6 到 8 行，包含：
- 今日結論
- 今日動作
- 買進觀察價
- 賣出觀察價
- 最大風險
- 需要再確認的資料

## 2. 操作建議
General Mode 也要提供買賣區間，但不可假裝知道使用者持股。
Personalized Mode 必須代入 user_profile，輸出核心部位、機動部位、今日是否賣、建議張數。

## 3. 市場資料快照
列出股價日期、收盤價、成交量、20MA、60MA、RSI、MACD、支撐、壓力。

## 4. 運價與航運景氣
必須附上實際證據：
- SCFI 最新值、週變化、連續週數
- 美西線、美東線、歐洲線數字與週變化
- 紅海 / 蘇伊士 / 繞航狀態
- 對長榮 EPS、填息、股價續漲的意義

## 5. 法人籌碼
列出外資、投信、自營商、三大法人近 1/3/5/10 日買賣超。

## 6. 基本面與填息
列出月營收、EPS、股利、殖利率、填息機率依據。

## 7. 國際事件與市場環境
分析美國政策、戰爭因素、油價、VIX、美元、台幣、台股與美股。

## 8. 多空辯論
列出多方證據、空方證據、最後裁決。

## 9. 修正版信心分數
列出方向分數、時機分數、估值分數、風險分數、資料完整度、真實性分數、總分。

## 10. 資料限制
只列真正會影響判斷的限制。若三條主要航線已取得，不要把航線列為缺漏。

## 11. 免責聲明
提醒這不是投資建議，不會自動下單。

User profile:
{profile_text}

Context JSON:
{json.dumps(context, ensure_ascii=False, indent=2)}
"""


def _value(value: Any) -> str:
    if value is None or value == "":
        return "資料不足"
    return str(value)


def _freight_route_status(freight: dict[str, Any]) -> dict[str, Any]:
    main_routes = {
        "美西線": (freight.get("us_west"), freight.get("us_west_weekly_change")),
        "美東線": (freight.get("us_east"), freight.get("us_east_weekly_change")),
        "歐洲線": (freight.get("europe"), freight.get("europe_weekly_change")),
    }
    complete = [name for name, (rate, change) in main_routes.items() if rate is not None and change is not None]
    if len(complete) == 3:
        label = "三條主要航線皆有精確數字與週變化"
    elif complete:
        label = f"部分主要航線有精確資料：{', '.join(complete)}"
    else:
        label = "主要航線精確數字不足，僅能使用多來源趨勢推論"
    return {"label": label, "complete_main_route_count": len(complete), "complete_main_routes": complete}
