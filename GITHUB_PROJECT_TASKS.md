# GitHub Project Tasks

你可以把以下內容建立成 GitHub Issues，然後加入 GitHub Project 看板。

## Issue 1

Title:

```text
[P0] Google Sheet daily report log
```

Body:

```text
Goal:
每天 GitHub Actions 跑完分析後，自動把摘要寫入 Google Sheet。

Tasks:
- 建立 Google Cloud service account
- 啟用 Google Sheets API
- 建立 GOOGLE_SERVICE_ACCOUNT_JSON secret
- 建立 GOOGLE_SHEET_ID secret
- 新增 backend/integrations/google_sheets.py
- daily_analysis_email.py 整合 append row
- 本機測試寫入 Sheet
- GitHub Actions 手動測試

Done:
- 每天 08:10 / 20:10 都會新增一列
- Email 和 artifact 不受影響
```

## Issue 2

Title:

```text
[P1] Multi-symbol scheduled analysis
```

Body:

```text
Goal:
支援 REPORT_SYMBOLS=2603.TW,2609.TW,2615.TW，一次排程分析多檔股票。

Tasks:
- daily_analysis_email.py 支援 REPORT_SYMBOLS
- 每檔股票獨立產生 md/html/json
- 單一股票失敗不中斷整批
- Email 支援合併摘要
- Google Sheet 每檔新增一列

Done:
- GitHub Actions 一次可分析多檔
- 失敗股票有錯誤紀錄
```

## Issue 3

Title:

```text
[P1] Supabase historical database
```

Body:

```text
Goal:
長期保存每次分析結果、market snapshot、prediction，供未來回測使用。

Tasks:
- 建立 Supabase project
- 建立 database/schema.sql
- 新增 SUPABASE_URL secret
- 新增 SUPABASE_SERVICE_ROLE_KEY secret
- 新增 backend/integrations/supabase_client.py
- 寫入 analysis_runs / market_snapshots / prediction_records

Done:
- 每次分析完整 JSON 會進資料庫
- 可用 symbol 和日期查詢
```

## Issue 4

Title:

```text
[P2] 7/30/90-day prediction validation
```

Body:

```text
Goal:
每天自動驗證過去 AI 判斷，計算 7 / 30 / 90 天報酬與正確率。

Tasks:
- 新增 prediction_validation_service.py
- 找出到期 prediction
- 抓未來價格
- 計算 actual_return
- 計算 max_drawdown
- 判斷 correct
- 寫回 Supabase / Google Sheet

Done:
- 可看到每個 horizon 的勝率與平均報酬
- Status: implemented locally
```

## Issue 5

Title:

```text
[P2] Self audit and report quality engine
```

Body:

```text
Goal:
每份報告寄出前自動檢查資料品質、分數一致性與文字專業度。

Tasks:
- 新增 report_auditor.py
- 檢查強結論與資料品質是否矛盾
- 檢查 timing/risk/action 是否矛盾
- 檢查工程字眼
- 輸出 audit_score / needs_revision / warnings
- Email 加入 audit 摘要
- Sheet / DB 保存 audit 結果

Done:
- 報告不再出現不專業或矛盾結論
- audit score 低於門檻會標示需修正
- Status: implemented locally
```
