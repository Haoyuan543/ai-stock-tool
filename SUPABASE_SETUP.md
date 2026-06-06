# Supabase 長期資料庫設定

此功能會把每次 GitHub Actions 分析結果保存到 Supabase PostgreSQL，供未來做回測、勝率統計、7 / 30 / 90 天驗證。

## 1. 建立 Supabase Project

1. 到 [Supabase](https://supabase.com/)
2. 建立新 project
3. 進入：

```text
Project Settings -> API
```

取得：

```text
Project URL
service_role key
```

注意：`service_role key` 權限很高，只能放 GitHub Secrets，不要 commit 到 repo。

## 2. 建立資料表

到 Supabase：

```text
SQL Editor -> New query
```

貼上：

```text
database/schema.sql
```

執行後會建立：

```text
analysis_runs
market_snapshots
prediction_validations
```

## 3. 設定 GitHub Secrets

到：

```text
GitHub repo -> Settings -> Secrets and variables -> Actions
```

新增：

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
UPDATE_SUPABASE
SUPABASE_REQUIRED
```

建議：

```text
UPDATE_SUPABASE=true
SUPABASE_REQUIRED=false
```

`SUPABASE_REQUIRED=false` 的意思是：資料庫暫時寫入失敗時，Email 和 Google Sheet 仍會正常執行。

## 4. 寫入內容

`analysis_runs` 會保存：

```text
分析時間
股票代號
模式
股價
資料日期
市場狀態
操作建議
Direction / Timing / Valuation / Risk / Coverage / Truthfulness / Overall Score
完整 market_data_json
完整 report_markdown
資料品質
可信度
warnings
```

`market_snapshots` 會保存：

```text
股價
成交量
20MA
60MA
SCFI
運價趨勢
法人合計
EPS
股利殖利率
原始 market_data JSON
```

## 5. 本機測試

`.env` 加上：

```env
UPDATE_SUPABASE=true
SUPABASE_REQUIRED=false
SUPABASE_URL=你的 Supabase project URL
SUPABASE_SERVICE_ROLE_KEY=你的 service_role key
```

執行：

```powershell
$env:SEND_EMAIL="false"
.\.venv\Scripts\python.exe -m backend.jobs.daily_analysis_email --symbol 2603.TW --mode personalized --model gpt-5
```

成功時 log 會出現：

```text
Supabase history written.
```

## 6. 注意事項

- `service_role key` 不可公開。
- 如果 table 尚未建立，會回傳 404 或 relation not found。
- 如果 Row Level Security 擋住寫入，請確認你使用的是 service_role key。
- GitHub runner 是臨時機器，長期歷史要靠 Supabase 保存，不要靠 runner 裡的 `data/*.jsonl`。

