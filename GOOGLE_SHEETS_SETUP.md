# Google Sheet 自動更新設定

此功能會在 GitHub Actions 每次分析完成後，把摘要寫入 Google Sheet。

## 1. 建立 Google Sheet

建立一份新的 Google Sheet，例如：

```text
AI Stock Daily Log
```

建立分頁：

```text
analysis_log
```

第一列可以先留空，程式會從下一列開始 append。建議你手動貼上欄位名稱：

```text
analysis_time
completed_at
symbol
mode
price_data_date
is_realtime_price
close
volume
market_state
action
buy_advice
sell_advice
next_sell_point
next_buyback_point
direction_score
timing_score
valuation_score
risk_score
data_coverage
truthfulness_score
overall_score
analysis_mode
model_used
elapsed_seconds
audit_score
needs_revision
top_data_limitations
html_report_file
```

## 2. 建立 Google Cloud Service Account

1. 到 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立或選擇一個 project
3. 啟用：

```text
Google Sheets API
```

4. 到：

```text
IAM & Admin -> Service Accounts
```

5. 建立 service account，例如：

```text
ai-stock-sheets-writer
```

6. 建立 JSON key，下載 JSON 檔。

## 3. 分享 Sheet 給 Service Account

打開下載的 JSON，找到：

```json
"client_email": "xxxx@xxxx.iam.gserviceaccount.com"
```

到 Google Sheet 右上角按「共用」，把這個 email 加進去，權限選：

```text
Editor / 編輯者
```

## 4. 設定 GitHub Secrets

到：

```text
GitHub repo -> Settings -> Secrets and variables -> Actions
```

新增：

```text
GOOGLE_SERVICE_ACCOUNT_JSON
```

Secret 內容貼上整份 JSON key。

再新增：

```text
GOOGLE_SHEET_ID
```

Sheet ID 在網址中：

```text
https://docs.google.com/spreadsheets/d/<這一段就是 Sheet ID>/edit
```

再新增：

```text
GOOGLE_SHEET_WORKSHEET
```

值：

```text
analysis_log
```

可選：

```text
UPDATE_GOOGLE_SHEET=true
GOOGLE_SHEET_REQUIRED=false
```

建議 `GOOGLE_SHEET_REQUIRED=false`，這樣 Google Sheet 寫入失敗時，Email 報告仍會寄出。

## 5. 本機測試

如果你要在本機測試，`.env` 可填：

```env
UPDATE_GOOGLE_SHEET=true
GOOGLE_SERVICE_ACCOUNT_JSON={整份 JSON}
GOOGLE_SHEET_ID=你的 sheet id
GOOGLE_SHEET_WORKSHEET=analysis_log
```

然後執行：

```powershell
$env:SEND_EMAIL="false"
.\.venv\Scripts\python.exe -m backend.jobs.daily_analysis_email --symbol 2603.TW --mode personalized --model gpt-5
```

成功時 log 會出現：

```text
Google Sheet appended: analysis_log
```

## 6. 注意事項

- 不要把 service account JSON commit 到 GitHub。
- JSON key 只放在 GitHub Secrets。
- 如果 Sheet 沒有分享給 service account，會出現 403 權限錯誤。
- 如果分頁名稱不對，會出現 range 或 worksheet 相關錯誤。
