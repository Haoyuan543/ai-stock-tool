from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import get_settings
from backend.models import BacktestRequest, MarketDataset, ResearchRequest
from backend.services.alpha_engine import AlphaDiscoveryEngine
from backend.services.analysis_service import AnalysisService
from backend.services.backtest_engine import BacktestEngine
from backend.services.daily_report import DailyReportService
from backend.services.fetchers import MarketDataFetcher


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

app = FastAPI(title="AI Alpha Research Platform", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

fetcher = MarketDataFetcher()
alpha_engine = AlphaDiscoveryEngine()
backtest_engine = BacktestEngine()
daily_reports = DailyReportService()
analysis_service = AnalysisService()


class AnalyzeRequest(BaseModel):
    symbol: str = "2603.TW"
    mode: str = "general"
    model: str = ""
    freight_overrides: dict = {}
    manual_context: str = ""


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "openai_configured": bool(settings.openai_api_key),
        "finmind_configured": bool(settings.finmind_token),
        "news_configured": bool(settings.news_api_key),
        "default_model": settings.openai_model,
    }


@app.get("/models")
async def models() -> dict:
    settings = get_settings()
    options = [
        {"value": "", "label": f"使用 .env 預設模型（{settings.openai_model}）", "speed": "default"},
        {"value": "gpt-5.5", "label": "gpt-5.5：完整分析", "speed": "balanced"},
        {"value": "gpt-5.5-pro", "label": "gpt-5.5-pro：深度分析", "speed": "deep"},
        {"value": "gpt-5", "label": "gpt-5：快速分析", "speed": "fast"},
    ]
    return {"default_model": settings.openai_model, "options": options}


@app.post("/analyze")
async def analyze(request: AnalyzeRequest) -> dict:
    return analysis_service.analyze_now(
        request.symbol,
        request.mode,
        request.freight_overrides,
        request.manual_context,
        request.model,
    )


@app.post("/data-preview")
async def data_preview(request: AnalyzeRequest) -> dict:
    return analysis_service.data_preview(
        request.symbol,
        request.freight_overrides,
        request.manual_context,
    )


@app.get("/analysis-history")
async def analysis_history() -> dict:
    return {"records": analysis_service.history(limit=20)}


@app.get("/api/data/{ticker}", response_model=MarketDataset)
async def collect_data(ticker: str) -> MarketDataset:
    return await fetcher.collect(ticker)


@app.post("/api/research")
async def research(request: ResearchRequest) -> dict:
    dataset = request.dataset
    if dataset is None and request.use_live_data:
        dataset = await fetcher.collect(request.ticker)
    if dataset is None:
        dataset = MarketDataset(ticker=request.ticker, limitations=["Data Missing: no dataset supplied and live fetch disabled."])
    report = await alpha_engine.run(dataset, request.manual_context)
    return report.model_dump(mode="json")


@app.post("/api/daily-report")
async def daily_report(request: ResearchRequest) -> dict:
    dataset = request.dataset or await fetcher.collect(request.ticker)
    report = await alpha_engine.run(dataset, request.manual_context)
    prediction = daily_reports.save(dataset, report)
    return {"report": report.model_dump(mode="json"), "prediction_record": prediction.model_dump(mode="json")}


@app.get("/api/daily-report")
async def list_daily_reports() -> dict:
    return {"reports": daily_reports.latest(), "predictions": daily_reports.predictions()}


@app.post("/api/backtest")
async def backtest(request: BacktestRequest) -> dict:
    report = backtest_engine.run(request)
    return report.model_dump(mode="json")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
