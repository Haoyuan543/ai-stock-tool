# Supabase 設定手冊

Supabase 是這個工具的雲端資料庫，用來保存 GitHub Actions 每次分析產生的資料。GitHub runner 是臨時機器，執行完就會消失，所以長期資料不能只放在 runner 裡的檔案。

## 主要用途

- 保存每次分析紀錄。
- 保存市場快照。
- 保存 7 / 30 / 90 天預測驗證結果。
- 保存航線運價資料 `freight_routes`，讓雲端分析不用只靠 Repo 內建 CSV。

## 必要資料表

請到 Supabase SQL Editor 執行：

```text
database/schema.sql
```

這會建立或更新：

- `analysis_runs`
- `market_snapshots`
- `prediction_validations`
- `freight_cache`
- `freight_routes`

## GitHub Secrets

到 GitHub Repo：

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

至少設定：

```text
SUPABASE_URL=你的 Supabase Project URL
SUPABASE_SERVICE_ROLE_KEY=你的 service_role secret key
UPDATE_SUPABASE=true
SUPABASE_REQUIRED=false
READ_SUPABASE_FREIGHT=true
SCFI_AUTO_UPDATE_CLOUD=true
SCFI_AUTO_UPDATE_LOCAL_CSV=false
```

## 航線資料雲端化

`data/scfi_routes.csv` 只是 Repo 內建備援檔，不是雲端即時資料庫。

雲端執行時的正確順序：

1. 先讀 Supabase `freight_routes` 最新資料。
2. 若 Supabase 沒資料，才讀 Repo 內建 `data/scfi_routes.csv`。
3. 若公開資料或新聞抽取取得完整 SCFI、美西、美東、歐洲線數字，寫回 Supabase。
4. 報告會標示來源是「Supabase 雲端航線資料庫」或「Repo 內建 CSV 航線備援」。

## 關於即時性

SCFI 與主要航線運價通常是週資料，不是盤中逐筆跳動報價。這個工具的「即時」意思是：

- 使用者按下分析或排程觸發時，重新抓當下可取得的最新公開資料。
- 若抓到新的完整航線資料，更新到 Supabase。
- 報告標示資料日期與本次抓取時間。

因此它可以做到雲端自動更新與雲端保存，但不能把週資料變成盤中報價。

## 如何確認成功

GitHub Actions log 應出現：

```text
Supabase history written.
```

如果有新的完整航線資料，報告或 log 會顯示航線資料已寫入 Supabase `freight_routes`。

Supabase Table Editor 可檢查：

- `analysis_runs` 是否新增一筆分析紀錄。
- `market_snapshots` 是否新增市場快照。
- `freight_routes` 是否有最新航線資料。

## 常見問題

- 如果 `analysis_runs` 有資料但 `freight_routes` 沒更新，通常代表本次公開資料沒有取得完整 SCFI、美西、美東、歐洲線數字。
- 如果報告顯示「Repo 內建 CSV 航線備援」，代表 Supabase 沒有可用資料或 GitHub Secrets 沒有設定完整。
- 如果 Supabase 寫入失敗，請檢查 `SUPABASE_URL` 是否為 Project URL，`SUPABASE_SERVICE_ROLE_KEY` 是否是 Secret key，不是 publishable key。
