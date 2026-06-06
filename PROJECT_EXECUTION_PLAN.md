# AI Stock Tool Implementation Plan

這份文件用來安排後續功能開發。原則是：

```text
本機開發與測試
-> push 到 GitHub
-> GitHub Actions runner 定時執行
-> 結果寫入 Google Sheet / 資料庫 / Email / artifact
```

GitHub runner 適合「排程執行」，不適合「直接在上面開發」。  
所以開發與除錯仍建議在本機完成，測過後再推上 GitHub。

## 目前已完成

- 本機 FastAPI 即時分析工具
- GitHub Actions 每天 08:10 / 20:10 自動跑分析
- Email 報告寄送
- Markdown / HTML / JSON 報告輸出
- OpenAI / FinMind / News / SCFI / Freight Intelligence / ETF 推論 / 市場環境 / 國際事件整合

## Milestone 1：Google Sheet 自動更新

目標：每天分析後，把摘要寫入 Google Sheet，形成可閱讀的每日紀錄表。

Status:

```text
implemented locally
```

執行位置：

```text
開發：本機
正式排程：GitHub runner
```

已新增 / 修改：

- `backend/integrations/google_sheets.py`
- `backend/jobs/daily_analysis_email.py` 整合 Google Sheet append
- `.env.example` 新增 Google Sheet 設定
- `.github/workflows/daily-analysis.yml` 新增 secrets
- `requirements.txt` 新增 `google-auth`

GitHub Secrets：

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
GOOGLE_SHEET_WORKSHEET
UPDATE_GOOGLE_SHEET
GOOGLE_SHEET_REQUIRED
```

Google Sheet 欄位：

```text
analysis_time
symbol
mode
price_date
close
market_state
action
buy_advice
sell_advice
direction_score
timing_score
valuation_score
risk_score
data_coverage
truthfulness_score
overall_score
analysis_mode
model_used
report_html_artifact
top_risks
data_limitations
audit_score
needs_revision
```

驗證方式：

- 本機用假資料測 append
- 本機用真實分析結果寫入測試 Sheet
- GitHub Actions 手動 Run workflow
- 確認 Sheet 新增一列

完成標準：

- 每次排程都會新增一列
- 不會覆蓋舊資料
- API key 不會出現在 repo
- Email 仍正常寄送

Setup guide:

```text
GOOGLE_SHEETS_SETUP.md
```

## Milestone 2：多股票分析

目標：一次排程可以分析多個股票。

Status:

```text
implemented locally
```

執行位置：

```text
開發：本機
正式排程：GitHub runner
```

已新增 / 修改：

- `backend/jobs/daily_analysis_email.py` 支援 `REPORT_SYMBOLS`
- 單一股票失敗時，不中斷其他股票
- 每檔股票各自產生 Markdown / HTML / JSON
- Google Sheet 每檔股票新增一列
- 單檔時維持原本一封完整 email
- 多檔時寄一封批次摘要，並附上各股票 Markdown / HTML

建議第一批股票：

```text
2603.TW
2609.TW
2615.TW
```

注意：

- 航運股可以共用 SCFI / Freight Intelligence
- 若未來加入台積電、金融股，需要新增 sector profile，不能硬套航運邏輯

GitHub Secrets / Variables：

```text
REPORT_SYMBOLS=2603.TW,2609.TW,2615.TW
```

手動 Run workflow 時，也可以直接填 `symbols`：

```text
2603.TW,2609.TW,2615.TW
```

完成標準：

- 一次 workflow 成功分析多檔
- 任一檔失敗會記錄錯誤，但其他股票繼續
- Email / artifact / Sheet 都能對應每個 symbol

## Milestone 3：長期保存歷史資料到資料庫

目標：把完整分析結果長期保存，讓未來能做回測、準確率、長期統計。

Status:

```text
implemented locally
```

建議服務：

```text
Supabase PostgreSQL
```

執行位置：

```text
開發：本機
正式寫入：GitHub runner
資料保存：Supabase
```

已新增 / 修改：

- `backend/integrations/supabase_client.py`
- `database/schema.sql`
- `backend/services/history_writer.py`
- `backend/jobs/daily_analysis_email.py` 整合 Supabase 寫入
- `.github/workflows/daily-analysis.yml` 新增 secrets

GitHub Secrets：

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
UPDATE_SUPABASE
SUPABASE_REQUIRED
```

資料表：

```text
analysis_runs
market_snapshots
prediction_records
prediction_validations
report_audits
```

最低欄位：

```text
id
analysis_time
symbol
mode
price
price_date
market_state
recommendation
scores_json
market_data_json
summary_json
report_markdown
data_quality_json
truthfulness_json
created_at
```

完成標準：

- 每次分析完整 JSON 寫入資料庫
- 可依 symbol / 日期查詢
- 不依賴 GitHub runner 的臨時檔案

## Milestone 4：7 / 30 / 90 天績效驗證

目標：自動檢查過去 AI 判斷是否有效。

Status:

```text
implemented locally
```

前置條件：

```text
Milestone 3 資料庫完成
```

已新增 / 修改：

- `backend/services/prediction_validation_service.py`
- GitHub Actions 每天分析後執行 validation
- Supabase 寫入 `prediction_validations`

驗證內容：

```text
7 天後報酬
30 天後報酬
90 天後報酬
最大回撤
是否符合方向判斷
是否達到賣點 / 買回點
AI 是否過度樂觀或過度悲觀
```

輸出欄位：

```text
prediction_id
symbol
horizon
base_price
future_price
actual_return
max_drawdown
correct
validated_at
```

完成標準：

- 每天自動找出到期預測
- 能更新 7 / 30 / 90 天結果
- 能在 Sheet 或 DB 看勝率與平均報酬

## Milestone 5：Self Audit Engine

目標：每次報告產生後，系統自我審查一次，避免假強結論、資料不足卻過度樂觀、報告格式不專業。

Status:

```text
implemented locally
```

執行位置：

```text
開發：本機
正式排程：GitHub runner
```

已新增 / 修改：

- `backend/services/report_auditor.py`
- `backend/jobs/daily_analysis_email.py` 整合 audit
- `database/schema.sql` 保存 audit_json

審查項目：

```text
是否有資料不足卻輸出強烈多空
是否 Overall Score < 65 卻寫 Bullish
是否 Timing Score < 50 卻建議追價
是否 Risk Score < 50 卻給積極操作
ETF 推論是否過度加分
公告抓取失敗是否被誤解為無公告
股價日期是否非今日卻被當即時價
報告是否含工程字眼
報告是否缺少實際證據
Action Plan 是否太空泛
Personalized Mode 是否有帶入持股
```

輸出：

```text
audit_score
needs_revision
audit_warnings
failed_rules
recommended_changes
```

完成標準：

- 每份報告都有 audit score
- 嚴重錯誤會標示 `needs_revision=true`
- Email 會包含 audit 摘要
- Google Sheet / DB 會保存 audit 結果

## 建議開發順序

```text
1. Google Sheet 自動更新
2. 多股票分析
3. Supabase 長期資料庫
4. 7 / 30 / 90 天績效驗證
5. Self Audit Engine
```

理由：

- Google Sheet 最快看到成果，也方便人工檢查
- 多股票可以提高每日報告價值
- 資料庫是長期回測的基礎
- 績效驗證必須依賴歷史資料
- 自我審查最後接上，可以把前面所有資料品質一起納入

## GitHub Project 建議欄位

如果要在 GitHub Project 做看板，建議欄位：

```text
Backlog
Ready
In Progress
Local Testing
Ready to Push
GitHub Actions Testing
Done
```

建議建立 5 個 issue：

```text
[P0] Google Sheet daily report log
[P1] Multi-symbol scheduled analysis
[P1] Supabase historical database
[P2] 7/30/90-day prediction validation
[P2] Self audit and report quality engine
```

## 每次功能開發流程

```text
1. 本機建立或修改功能
2. 本機 compileall
3. 本機跑對應 job
4. 檢查輸出檔 / Sheet / DB
5. git add
6. git commit
7. git push
8. GitHub Actions 手動 Run workflow
9. 確認 Email / artifact / Sheet / DB
10. 再等待正式排程自動跑
```
