# AI Stock Tool 使用手冊

這份手冊整理目前專案能做什麼、怎麼用、哪些功能需要額外設定、以及後續更新流程。

## 1. 目前已完成的能力

### 本機即時分析

可以在本機啟動 FastAPI + 前端頁面，手動輸入股票代號後立即分析。

支援：

- 股票即時或最近收盤資料
- 技術面
- 法人籌碼
- SCFI / 航線運價
- 新聞與國際事件
- 基本面與股利
- ETF 被動買盤推論
- 市場環境
- Personalized Mode 持股建議
- OpenAI 分析報告
- 報告下載

### GitHub Actions 雲端排程

目前 GitHub Actions 會在台灣時間：

```text
每天 08:10
每天 20:10
```

自動執行分析。

雲端會：

- 建立 Ubuntu runner
- 安裝 Python 3.12
- 安裝 requirements
- 安裝 Playwright Chromium
- 執行 `backend.jobs.daily_analysis_email`
- 產生 Markdown / HTML / JSON 報告
- 產生 batch summary
- 寄 Email
- 上傳 artifact

### Google Sheet 自動更新

已實作，但需要你設定 Google Secrets。

設定後，每次分析會 append 一列到 Google Sheet。

如果未設定：

```text
Google Sheet skipped
```

不會導致 GitHub Actions 失敗。

### Supabase 長期資料庫

已實作，但需要你建立 Supabase 並設定 Secrets。

設定後會寫入：

```text
analysis_runs
market_snapshots
prediction_validations
```

如果未設定：

```text
Supabase skipped
```

不會導致 GitHub Actions 失敗。

### 7 / 30 / 90 天績效驗證

已實作，依賴 Supabase 歷史資料。

每次排程跑完後會自動：

- 找出已滿 7 / 30 / 90 天的舊分析
- 抓股價歷史
- 計算實際報酬
- 計算最大回撤
- 判斷 AI 方向是否正確
- 寫入 `prediction_validations`

如果 Supabase 未設定，會安全跳過。

### Self Audit Engine

已實作。

每份報告產生後會自我審查：

- 分數與結論是否矛盾
- 風險低分是否仍建議積極操作
- 時機低分是否仍建議追價
- 資料可信度低是否有揭露
- 股價非即時是否有提醒
- 是否出現工程字眼
- 是否缺少核心區塊
- 報告是否太短

輸出：

```text
audit_score
needs_revision
failed_rules
audit_warnings
recommended_changes
```

## 2. 本機使用方式

啟動：

```powershell
cd C:\Users\dkm4j\Desktop\AI_STOCK
.\START_AI_PLATFORM.bat
```

或雙擊：

```text
啟動AI平台.bat
```

打開：

```text
http://127.0.0.1:8010
```

停止：

```text
STOP_AI_PLATFORM.bat
```

或雙擊：

```text
停止AI平台.bat
```

## 3. GitHub Actions 手動執行

到 GitHub repo：

```text
Actions -> Daily AI Investment Analysis -> Run workflow
```

單檔：

```text
symbol: 2603.TW
symbols: 留空
mode: personalized
model: gpt-5
send_email: true
```

多檔：

```text
symbol: 2603.TW
symbols: 2603.TW,2609.TW,2615.TW
mode: personalized
model: gpt-5
send_email: true
```

`symbols` 有填時，會優先使用 `symbols`，忽略單一 `symbol`。

## 4. GitHub Secrets

到：

```text
GitHub repo -> Settings -> Secrets and variables -> Actions
```

### 必要

```text
OPENAI_API_KEY
FINMIND_TOKEN
NEWS_API_KEY
```

### Email

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=你的 Gmail
SMTP_PASSWORD=Google 應用程式密碼
SMTP_STARTTLS=true
REPORT_EMAIL_FROM=你的 Gmail
REPORT_EMAIL_TO=收件信箱
```

### Google Sheet

```text
UPDATE_GOOGLE_SHEET=true
GOOGLE_SHEET_REQUIRED=false
GOOGLE_SERVICE_ACCOUNT_JSON=整份 service account JSON
GOOGLE_SHEET_ID=Google Sheet ID
GOOGLE_SHEET_WORKSHEET=analysis_log
```

未設定時會跳過，不會失敗。

### Supabase

```text
UPDATE_SUPABASE=true
SUPABASE_REQUIRED=false
SUPABASE_URL=Supabase project URL
SUPABASE_SERVICE_ROLE_KEY=Supabase service_role key
VALIDATE_PREDICTIONS=true
```

未設定時會跳過，不會失敗。

### Report Audit

```text
RUN_REPORT_AUDIT=true
```

## 5. Google Sheet 設定

詳細看：

```text
GOOGLE_SHEETS_SETUP.md
```

簡要流程：

1. 建立 Google Sheet
2. 建立 `analysis_log` 分頁
3. Google Cloud 啟用 Google Sheets API
4. 建立 Service Account
5. 下載 JSON key
6. 把 Sheet 分享給 service account 的 `client_email`
7. GitHub Secrets 放：

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SHEET_ID
GOOGLE_SHEET_WORKSHEET
```

## 6. Supabase 設定

詳細看：

```text
SUPABASE_SETUP.md
```

簡要流程：

1. 建立 Supabase project
2. 到 SQL Editor
3. 執行：

```text
database/schema.sql
```

4. GitHub Secrets 放：

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
UPDATE_SUPABASE
SUPABASE_REQUIRED
VALIDATE_PREDICTIONS
```

## 7. GitHub Actions 產出

每次 workflow 成功後，artifact 會有：

```text
scheduled-analysis-report
```

裡面包含：

```text
每檔股票 .md
每檔股票 .html
每檔股票 .json
*_batch_summary.md
```

Email：

- 單檔時寄完整報告
- 多檔時寄批次摘要，並附上各檔報告

## 8. 開發與更新流程

建議流程：

```text
你提出需求
-> 本機修改
-> 本機測試
-> git add
-> git commit
-> git push
-> GitHub Actions 手動 Run workflow 測試
-> 等正式排程
```

推送範例：

```powershell
git add .
git commit -m "Update AI stock tool"
git push
```

## 9. 目前還需要你確認的事

### 是否已推送最新本機修改

請在 PowerShell 執行：

```powershell
cd C:\Users\dkm4j\Desktop\AI_STOCK
git status
```

如果看到有修改未 commit，請執行：

```powershell
git add .
git commit -m "Update scheduled analysis platform"
git push
```

### 是否已設定 Google Sheet Secrets

如果沒設定，Google Sheet 功能會跳過。

### 是否已設定 Supabase Secrets

如果沒設定，長期資料庫與 7 / 30 / 90 天驗證會跳過。

### 是否已執行 Supabase schema

如果有使用 Supabase，需要在 SQL Editor 執行：

```text
database/schema.sql
```

## 10. 目前仍可再加強的功能

尚未完成或未正式產品化：

- Google Drive / S3 備份所有報告
- GitHub Pages 報告瀏覽入口
- Supabase dashboard 查詢頁
- 多產業股票分析 profile
- LINE / Telegram 通知
- 更完整的投資組合管理
- 自動產出週報 / 月報
- 回測統計視覺化

