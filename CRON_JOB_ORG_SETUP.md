# cron-job.org 外部排程設定

這個專案已改成由 cron-job.org 準點呼叫 GitHub Actions 的 `workflow_dispatch`。

這樣做的目的：避開 GitHub 內建 `schedule` 可能延遲數十分鐘到數小時的問題。

## 1. GitHub Token

到 GitHub 建立 Fine-grained personal access token：

1. 右上角頭像
2. Settings
3. Developer settings
4. Personal access tokens
5. Fine-grained tokens
6. Generate new token

設定：

- Repository access：只選 `Haoyuan543/ai-stock-tool`
- Permissions：
  - Actions：Read and write
  - Contents：Read-only

產生後請先保存 token。之後 GitHub 不會再顯示完整 token。

## 2. cron-job.org Job 設定

建立兩個 job：

- 早上 08:10，時區選 Asia/Taipei
- 晚上 20:10，時區選 Asia/Taipei

兩個 job 的 HTTP 設定相同。

### URL

```text
https://api.github.com/repos/Haoyuan543/ai-stock-tool/actions/workflows/daily-analysis.yml/dispatches
```

### Method

```text
POST
```

### Headers

```text
Accept: application/vnd.github+json
Authorization: Bearer 你的_GitHub_Token
X-GitHub-Api-Version: 2022-11-28
User-Agent: cron-job.org
Content-Type: application/json
```

### Body

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

說明：

- `mode` 預設使用 `general`
- `model` 留空時會使用 GitHub Secrets 裡的 `OPENAI_MODEL`
- `send_email` 設為 `true` 會寄出報告
- 之後要分析多檔股票時，把 `symbols` 改成例如 `2603.TW,2330.TW`

## 3. 成功判斷

cron-job.org 呼叫成功時，GitHub API 通常回：

```text
204 No Content
```

這代表 workflow 已被 GitHub 接收。

接著到 GitHub：

```text
Actions -> Daily AI Investment Analysis
```

應該會看到事件類型是：

```text
workflow_dispatch
```

如果看到 `schedule`，代表仍是 GitHub 內建排程在跑，不是 cron-job.org 觸發。

## 4. 常見錯誤

### 401 Unauthorized

GitHub Token 錯誤、過期，或 Header 沒有填：

```text
Authorization: Bearer ...
```

### 403 Forbidden

Token 權限不足。請確認 Actions 是 Read and write。

### 404 Not Found

常見原因：

- repository 沒有選到 `Haoyuan543/ai-stock-tool`
- workflow 檔名不是 `daily-analysis.yml`
- token 沒有該 repository 權限

### 422 Unprocessable Entity

Body 格式錯誤，或 `ref` 不是 `main`。

## 5. 驗證重點

成功後請檢查最新 run 的 `Print trigger diagnostics`：

- `event_name=workflow_dispatch`
- `input_symbol=2603.TW`
- `input_mode=general`
- `taipei_time` 接近你設定的時間

如果 cron-job.org 是 20:10 觸發，GitHub Actions 通常應在幾分鐘內出現。
