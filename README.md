# Real-time AI Investment Analyst

FastAPI + OpenAI based investment research tool. Data is fetched only after the user clicks Analyze Now. The tool does not place orders and does not send notifications.

## Collaboration Rule

Codex must not push commits to GitHub unless the user explicitly asks Codex to push in that specific request. Local commits are allowed when requested or needed for review, but `git push` requires an explicit user instruction every time.

## Cloud Scheduled Reports

This project now supports cloud scheduled analysis through GitHub Actions.
For more accurate timing, use cron-job.org to trigger GitHub Actions through `workflow_dispatch`.

- Workflow: `.github/workflows/daily-analysis.yml`
- Job script: `backend/jobs/daily_analysis_email.py`
- Full setup guide: `CLOUD_DEPLOYMENT.md`
- cron-job.org setup: `CRON_JOB_ORG_SETUP.md`
- Google Sheet setup: `GOOGLE_SHEETS_SETUP.md`
- Supabase setup: `SUPABASE_SETUP.md`
- Implementation plan: `PROJECT_EXECUTION_PLAN.md`
- GitHub Project issue drafts: `GITHUB_PROJECT_TASKS.md`
- Full user manual: `USER_MANUAL.md`

Default cloud trigger:

```text
cron-job.org calls GitHub workflow_dispatch at 08:10 and 20:10 Asia/Taipei.
```

The cloud job will:

- run the same AI analysis pipeline as the local tool
- generate Markdown, HTML, and JSON reports
- generate a batch summary artifact
- append report audit results to each scheduled report
- upload the report as a GitHub Actions artifact
- email the report when SMTP secrets are configured

Local test without sending email:

```powershell
$env:SEND_EMAIL="false"
.\.venv\Scripts\python.exe -m backend.jobs.daily_analysis_email --symbol 2603.TW --mode general --model gpt-5
```

Multi-symbol local test:

```powershell
$env:SEND_EMAIL="false"
.\.venv\Scripts\python.exe -m backend.jobs.daily_analysis_email --symbols "2603.TW,2609.TW,2615.TW" --mode general --model gpt-5
```

Required GitHub secrets:

```text
OPENAI_API_KEY
FINMIND_TOKEN
NEWS_API_KEY
```

Optional long-term database secrets:

```text
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
UPDATE_SUPABASE
SUPABASE_REQUIRED
```

Email secrets:

```text
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
SMTP_STARTTLS
REPORT_EMAIL_FROM
REPORT_EMAIL_TO
```

## Run

Double click:

```text
START_AI_PLATFORM.bat
```

Then open:

```text
http://127.0.0.1:8010
```

## Environment

Edit `.env`:

```env
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-5.5
OPENAI_TIMEOUT_SECONDS=300
OPENAI_MAX_OUTPUT_TOKENS=6000
FINMIND_TOKEN=your_finmind_token
NEWS_API_KEY=your_newsapi_key
EIA_API_KEY=
SERPAPI_API_KEY=
TAVILY_API_KEY=
BRAVE_SEARCH_API_KEY=
DEFAULT_TICKER=2603
```

## Modes

Personalized Mode is the default. It reads `user_profile.yaml` and adds position-aware advice:

- lots
- average cost
- unrealized profit/loss
- core lots
- flexible lots
- suggested sell lots
- price-zone actions

General Mode remains available but does not read holdings, cost, tax rate, or risk preference.

## Model Selection

The frontend has a model selector. Leave it blank to use `OPENAI_MODEL` from `.env`, or choose a model for a single analysis request.

- `gpt-5.5`: faster/balanced default option.
- `gpt-5.5-pro`: deeper analysis, usually slower.
- `gpt-5`: compatibility option if your account has access.

If the selected model is not enabled for your OpenAI account, the backend falls back to the local rule-based report and shows the OpenAI error.

## Implemented Data Sources

- Stock price/OHLCV: Yahoo Finance first, FinMind fallback
- Technical indicators: 20MA, 60MA, RSI14, MACD, Bollinger Bands
- Institutional flows: FinMind
- Fundamentals: FinMind monthly revenue, EPS, dividend, dividend yield, PER, PBR
- Announcements: TWSE OpenAPI / MOPS current material information
- News: NewsAPI if configured; Web Search Intelligence fallback
- Freight: official SSE SCFI chart OCR, `data/scfi_routes.csv`, Web Search Intelligence fallback, and manual advanced fields
- International events: Google News RSS, public search fallback, Yahoo/Stooq oil price fallback, and EIA oil price API if configured

## Data Source Routing and Time Labels

Every report now separates source quality more explicitly:

- Official/API data: exchange, FinMind, EIA, or other structured API data.
- Public chart/CSV data: Yahoo Finance, Stooq, or public CSV endpoints.
- RSS/Search inferred data: Google News RSS and public search snippets. These can support context, but they are not exact numeric data.
- Screenshot/OCR data: only used when a public page has no parseable table or API response.
- Cached or Supabase data: used as a fallback and always labeled with the source date and fetch time.

Report source labels include:

- data source
- fetch method
- data-as-of time
- fetch time
- whether the value is exact data or inferred context

This is intentional: the tool should show both the conclusion and the evidence timing, so you can tell whether it is using fresh data, stale data, or a fallback.

## Web Search Intelligence Layer

When a primary API cannot provide a field, the backend now uses:

- Brave Search API, SerpAPI, or Tavily when a key is configured
- Google News RSS when no search API key is configured
- Playwright DOM text/table extraction from search result pages
- Playwright network JSON response capture
- OpenAI extraction to convert search snippets into structured JSON
- Optional Playwright screenshot analysis for search result pages
- JSON cache in `data/search_cache.json` to avoid repeated searches

Search-derived data is labeled as inferred context. Exact numeric fields remain `Data Missing` unless the number is directly available from API, DOM/table text, network JSON, or screenshot evidence.

Playwright/Chromium does not fully replace API tokens. It can read public pages, rendered DOM, tables, public network JSON, and screenshots, but it does not bypass login, paywalls, CAPTCHA, rate limits, or missing public data. API keys remain the most stable option for repeatable daily analysis and future backtests.

## More Stable Freight Fetching

Freight data is now fetched with longer configurable request timeouts and a last-successful cache.

- `REQUEST_TIMEOUT_SECONDS`: default `20`; controls public-page and HTTP fetch timeout.
- `OPENAI_TIMEOUT_SECONDS`: default `180`; controls OpenAI extraction timeout.
- `FREIGHT_CACHE_MAX_DAYS`: default `21`; cached freight data older than this is not used.

The system still tries real sources first: official page/OCR, CSV import, public page extraction, RSS/search, and AI extraction. The freight cache is used only after those attempts leave missing fields. When cache is used, the report explicitly shows cache date, cache source, and which fields were filled. Cached data is not treated as real-time freight data.

For Supabase history, rerun `database/schema.sql` after pulling this version. It adds a `freight_cache` table so GitHub Actions can reuse the latest successful route data when a public source is temporarily unavailable.

## Supabase Freight Routes

GitHub Actions can also read the latest SCFI route data from Supabase, so cloud runs do not depend only on the CSV committed in the repository.

- Run `database/schema.sql` in Supabase SQL Editor after pulling this version. It adds the `freight_routes` table.
- Keep `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `UPDATE_SUPABASE=true` in GitHub Actions secrets.
- `READ_SUPABASE_FREIGHT=true` is enabled by default. When set, the freight fetcher reads the newest row from `freight_routes` before falling back to `data/scfi_routes.csv`.
- When public search extraction finds a newer complete set of SCFI, US West, US East, and Europe route numbers, the tool writes that row to both local CSV and Supabase.
- The report labels whether route values came from official pages, Supabase, CSV, or search-inferred context.

## Data Quality Classification

Every `/analyze` response includes `data_quality`:

- `exact_data`: API or directly confirmed numeric data
- `scraped_data`: Playwright DOM/table/network/screenshot or official image OCR
- `search_inferred_data`: search or page text inference, useful but lower confidence
- `stale_or_suspicious_data`: old or suspicious records, such as repeated zero institutional data
- `missing_data`: fields that remain unavailable and are not guessed

The frontend shows this classification in the side panel, and the report includes a `資料品質分層` section before deeper AI analysis.

## Freight Intelligence

Freight analysis is no longer based only on exact route-rate numbers. For shipping stocks such as `2603.TW`, the priority is:

1. freight direction
2. freight strength
3. consecutive up/down weeks
4. multi-source consistency
5. confidence score
6. exact route rates

The backend builds `market_data.freight.intelligence` with:

```json
{
  "overall_trend": "up/down/flat/unknown",
  "strength": "weak/moderate/strong",
  "weeks_up_or_down": null,
  "confidence": 0.0,
  "source_count": 0,
  "status": "inferred_from_multiple_sources"
}
```

If exact US West / US East / Europe rates are unavailable but SCFI, Freightos/Drewry/search/news evidence consistently points in the same direction, the report uses `Freight Intelligence` instead of treating freight as fully missing. Exact prices still win when available.

## Decision Quality Engines

The `/analyze` pipeline includes additional decision-support engines:

- ETF 買盤（ETF Flow）：tracks 00878, 00919, 0056, 00940, and 00929 by official pages and search fallback. Daily holdings are often unavailable, so results include `as_of`, `stale`, and confidence.
- 紅海情報（Red Sea Intelligence）：checks Red Sea, Suez, Houthi, Maersk, Hapag-Lloyd, and CMA CGM signals to estimate shipping impact and Suez return risk.
- 公告情報（Announcement Intelligence）：combines MOPS/TWSE, Evergreen IR, search fallback, and manual context. Fetch failure is not treated as "no announcement".
- 新聞相關性過濾（News Relevance Filter）：scores news articles and excludes low-relevance items from the main AI context.
- 市場環境（Market Regime）：estimates risk-on/risk-off from Taiwan market, shipping sector, and market-news context.
- 填息機率（Fill Dividend Probability）：estimates 30-day, 90-day, and 1-year fill-dividend probability when dividend and supporting data are available.

The revised score is returned as:

```json
{
  "direction_score": 0,
  "timing_score": 0,
  "valuation_score": 0,
  "risk_score": 0,
  "data_coverage": 0,
  "overall_score": 0
}
```

Decision rules:

- If `overall_score < 65`, the report must not output `Bullish`; positive direction is shown as `Neutral-Bullish / 中性偏多`.
- If `timing_score < 50`, the report states `方向偏多，但短線不適合追`.
- If `risk_score < 50`, the tool must not recommend aggressive action.
- If market regime confidence is below `0.5`, bullish conclusions are downgraded and `Strong Bullish` is not allowed.
- ETF flow with missing `holding_change`, missing `aum_change`, and `stale=true` is capped at `inferred_bullish` with confidence no higher than `0.45`.
- Announcement fetch failure is not treated as no announcement, and stale events older than 14 days are not treated as today's material event.

The report includes:

- ETF 買盤（ETF Flow）
- 紅海情報（Red Sea Intelligence）
- 公告情報（Announcement Intelligence）
- 市場環境（Market Regime）
- 填息機率（Fill Dividend Probability）
- 修正版信心分數（Revised Conviction Score）

## Prediction Tracking

Every completed `/analyze` call writes a compact prediction record to:

```text
data/predictions.jsonl
```

Manual validation command:

```bash
python -m backend.services.prediction_tracker --validate
```

Validation outputs 7-day, 30-day, and 90-day return checks when enough future price data is available. It does not schedule automatic jobs in this version.

To enable webpage screenshot analysis:

```text
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

Without Playwright, the tool still uses API, RSS, official SSE chart OCR, and text search fallback.

## Partial / Manual Data

SCFI route-level data is not guessed. Use:

```text
data/scfi_routes.csv
```

Format:

```csv
date,scfi,us_west,us_east,europe,mediterranean,asia_regional,weekly_change,monthly_change
2026-06-04,1200,1800,2800,1900,2100,900,1.2,-3.4
```

The frontend also has an Advanced Freight Supplement section for:

- SCFI latest value
- SCFI weekly change
- SCFI streak weeks
- US West weekly change
- US East weekly change
- Europe weekly change
- Red Sea status

## Data Freshness

The API returns:

```json
"data_freshness": {
  "analysis_time": "...",
  "price_data_date": "...",
  "is_realtime_price": false,
  "is_closing_price": true,
  "warning": "..."
}
```

If the price date is not today, the report warns that the data is not suitable for real-time decisions.

## Analysis Timer and History

The dashboard shows a live elapsed-time counter while analysis is running. The API also returns `elapsed_seconds`.

Every completed `/analyze` call is recorded in:

```text
data/analysis_history.jsonl
```

The dashboard shows recent records in the Analysis History section. Current records include timestamp, symbol, mode, model, market state, action, adjusted score, data coverage, and the report. They are ready for future 7/30/90-day validation, but automatic validation is not scheduled in this version.

## Conviction Score

The report now uses dual scoring:

- Raw Score
- Data Coverage
- Coverage Adjusted Score

If Freight, News, ETF, or announcement data is missing, Data Coverage falls and the adjusted score is reduced. Missing freight data sets `Freight Score = null`; the report must not make a strong bullish conclusion from incomplete shipping data.

## API

```http
POST /analyze
```

Request:

```json
{
  "symbol": "2603.TW",
  "mode": "general",
  "freight_overrides": {
    "scfi_latest": "1200",
    "scfi_weekly_change": "1.2",
    "scfi_streak_weeks": "3",
    "us_west_weekly_change": "-2.0",
    "us_east_weekly_change": "0.5",
    "europe_weekly_change": "1.0",
    "red_sea_status": "緊張"
  }
}
```

Response includes:

```json
{
  "symbol": "2603.TW",
  "mode": "general",
  "data_status": {},
  "data_freshness": {},
  "market_data": {},
  "summary": {},
  "action_plan": {},
  "position_advice": {},
  "local_scores": {},
  "ai_report": "...",
  "warnings": []
}
```

## API Keys

- OpenAI: required for AI report. Billing/credits must be active.
- FinMind: recommended for Taiwan stock, institutional, and fundamental data.
- NewsAPI: optional but recommended. Without it, the tool uses Google News RSS fallback.
- EIA API: optional but recommended for WTI / Brent oil price data. Without it, the tool uses Yahoo Finance and Stooq fallback. You can request a free key from EIA Open Data.
- SerpAPI / Tavily / Brave Search: optional. Without them, the tool uses Google News RSS fallback.
- TWSE OpenAPI / MOPS: current announcement fetcher does not require a key, but public endpoints may be limited.

No registration is required for Google News RSS. It is useful for international events such as US policy, war risk, oil price headlines, and shipping disruptions, but the report labels it as RSS/search context rather than official numeric data.

## Why Data Missing Matters

When freight, news, ETF, or announcement data is missing, the tool intentionally lowers data coverage and avoids strong conclusions. This prevents the AI from treating incomplete evidence as confirmed facts.

## 2026-06-05 Decision Quality Hardening

This version adds stricter truthfulness and downgrade controls for 2603.TW analysis.

### Truthfulness Engine

Every `/analyze` response now includes:

```json
{
  "truthfulness": {
    "truthfulness_score": 0,
    "exact_data_share": 0.0,
    "scraped_data_share": 0.0,
    "search_inferred_share": 0.0,
    "stale_data_share": 0.0,
    "missing_data_share": 0.0,
    "conflict_data_share": 0.0,
    "p0_missing_count": 0,
    "warnings": []
  }
}
```

Rules:

- `Truthfulness Score < 50` or too many P0 gaps downgrades the verdict to `Insufficient Data / 資料不足`.
- `Truthfulness Score 50~60` with no P0 gap may only output `Neutral` or `Neutral-Bullish / 中性偏多`; it must not output `Bullish`.
- High search-inferred share triggers a warning and prevents strong conclusions.
- P0 missing data, such as SCFI, route freight, Red Sea status, price freshness, or institutional data, is penalized more heavily.

### Gap Hunter Report

Every `/analyze` response now includes:

```json
{
  "gap_report": {
    "status": "gaps_found",
    "resolution_order": ["api", "playwright", "rss", "search", "ai_extraction"],
    "gaps": []
  }
}
```

This is currently a report-mode resolver. It identifies missing, stale, suspicious, and conflict fields and explains the next best resolution path. It does not pretend to have resolved a gap unless data actually exists.

### Verdict Downgrade Rules

- `overall_score < 65`: do not output `Bullish`; use `Neutral-Bullish / 中性偏多` when direction is positive.
- `timing_score < 50`: report must say `方向偏多，但短線不適合追`.
- `risk_score < 50`: do not give aggressive buy/add recommendations.
- `market_regime.confidence < 0.5`: do not output `Strong Bullish`.
- ETF flow with missing `holding_change`, missing `aum_change`, and `stale=true` is capped as search-inferred and cannot provide a large score boost.
- Announcement fetch failure is unknown, not proof of no announcement.

### Prediction Tracker v2

Prediction records now include:

- Direction Score
- Timing Score
- Valuation Score
- Risk Score
- Coverage Score
- Truthfulness Score
- P0 missing count
- Truthfulness warnings

Manual validation remains:

```bash
python -m backend.services.prediction_tracker --validate
```

## Disclaimer

This tool is decision support only, not investment advice.
