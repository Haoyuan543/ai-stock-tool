# 雲端自動分析部署說明

本專案目前採用：

1. GitHub Actions 執行分析
2. cron-job.org 負責準時觸發
3. Supabase 保存歷史資料
4. SMTP 寄送每日報告

這樣做的原因是 GitHub 內建 `schedule` 有時會延遲數十分鐘到數小時，不適合要求早晚固定時間收到報告的情境。

## 核心檔案

- `.github/workflows/daily-analysis.yml`
- `backend/jobs/daily_analysis_email.py`
- `CRON_JOB_ORG_SETUP.md`
- `SUPABASE_SETUP.md`
- `USER_MANUAL.md`

## 執行流程

```text
cron-job.org
  -> 呼叫 GitHub API workflow_dispatch
  -> GitHub Actions 啟動 daily-analysis.yml
  -> 安裝 Python / 套件 / Chromium
  -> 執行 backend.jobs.daily_analysis_email
  -> 抓股價、法人、SCFI、新聞、基本面、市場環境、油價
  -> 呼叫 OpenAI 產生報告
  -> 寫入 Supabase
  -> 產生 Markdown / HTML / JSON artifact
  -> 寄出 Email
```

## 為什麼不用 GitHub 內建 schedule

GitHub Actions 的 `schedule` 是 GitHub 平台排隊派發，不保證準點。

實測曾發生：

- 預期台灣 20:10
- 實際 GitHub 約 22:06 才派發

這不是 UTC+8 換算錯誤，因為：

- `10 12 * * *` = UTC 12:10 = 台灣 20:10
- 如果是台灣 22:10，cron 會是 `10 14 * * *`

因此目前正式做法改為 cron-job.org 準時呼叫 GitHub API。

## GitHub Actions 設定

目前 workflow 只保留：

```text
workflow_dispatch
```

也就是：

- 可以手動按 Run workflow
- 可以由 cron-job.org 呼叫 GitHub API 觸發
- 不再依賴 GitHub 內建 schedule

## GitHub Secrets

到：

```text
Repository -> Settings -> Secrets and variables -> Actions
```

必要：

```text
OPENAI_API_KEY
FINMIND_TOKEN
NEWS_API_KEY
```

建議：

```text
OPENAI_MODEL
SERPAPI_API_KEY
BRAVE_SEARCH_API_KEY
TAVILY_API_KEY
```

Email：

```text
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
SMTP_STARTTLS
REPORT_EMAIL_FROM
REPORT_EMAIL_TO
```

Supabase：

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
UPDATE_SUPABASE
SUPABASE_REQUIRED
VALIDATE_PREDICTIONS
```

## cron-job.org 設定

完整設定請看：

```text
CRON_JOB_ORG_SETUP.md
```

建議建立兩個 Job：

- 每天 08:10 Asia/Taipei
- 每天 20:10 Asia/Taipei

HTTP Method：

```text
POST
```

URL：

```text
https://api.github.com/repos/Haoyuan543/ai-stock-tool/actions/workflows/daily-analysis.yml/dispatches
```

Body：

```json
{
  "ref": "main",
  "inputs": {
    "symbol": "2603.TW",
    "symbols": "",
    "mode": "general",
    "model": "",
    "send_email": "true"
  }
}
```

成功時 GitHub API 會回：

```text
204 No Content
```

## 手動測試

GitHub：

```text
Actions -> Daily AI Investment Analysis -> Run workflow
```

建議輸入：

```text
symbol: 2603.TW
mode: general
send_email: true
```

本機測試：

```powershell
$env:SEND_EMAIL="false"
$env:UPDATE_SUPABASE="false"
$env:UPDATE_GOOGLE_SHEET="false"
.\.venv\Scripts\python.exe -m backend.jobs.daily_analysis_email --symbol 2603.TW --mode general --model gpt-5.5
```

## 成功後要看哪裡

GitHub Actions 最新 run：

- Event 應該是 `workflow_dispatch`
- `Print trigger diagnostics` 應顯示 `event_name=workflow_dispatch`
- `taipei_time` 應接近 cron-job.org 設定時間

Supabase：

- `analysis_runs` 應新增一筆
- `market_snapshots` 應新增股價快照
- `prediction_validations` 會在到期時新增驗證資料

Email：

- 應收到 HTML / Markdown 報告
- 報告內會標示分析時間、股價資料日期、資料來源與資料限制

## 常見問題

### GitHub 沒準時跑

如果 Event 是 `schedule`，代表你還在用 GitHub 內建排程。

如果 Event 是 `workflow_dispatch`，但時間不準，請檢查 cron-job.org 的時區是不是 Asia/Taipei。

### cron-job.org 顯示 401

GitHub Token 錯誤或過期。

### cron-job.org 顯示 403

GitHub Token 權限不足。請確認 Actions 是 Read and write。

### cron-job.org 顯示 404

可能是 repository 權限、workflow 檔名、或 URL 錯誤。

### 報告沒有寄出

檢查：

- SMTP secrets 是否完整
- Gmail 是否使用應用程式密碼
- `REPORT_EMAIL_FROM` 與 `SMTP_USER` 是否一致
- `SEND_EMAIL` 是否為 `true`

### Supabase 沒資料

檢查：

- `UPDATE_SUPABASE=true`
- `SUPABASE_URL` 是否為 `https://...supabase.co`
- `SUPABASE_SERVICE_ROLE_KEY` 是否填 Secret key，不是 Publishable key
- `database/schema.sql` 是否已在 Supabase SQL Editor 執行成功

## 目前限制

- 股價多數情況是最近收盤價，不一定是即時盤中價
- ETF 被動買盤仍是低權重搜尋推論，不能當精確持股變化
- 公司公告 / 法說仍需交叉確認
- 搜尋推論資料不會當成官方精確資料
- 如果 OpenAI 沒成功，系統不會用本機模板假裝 AI 分析完成
