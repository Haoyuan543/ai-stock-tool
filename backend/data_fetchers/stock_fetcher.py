from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.config import get_settings

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _today_taipei() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


def _exchange_datetime(timestamp: int | float | None, gmtoffset: int | None = None) -> datetime | None:
    if not timestamp:
        return None
    exchange_timezone = timezone(timedelta(seconds=gmtoffset or 0))
    return datetime.fromtimestamp(float(timestamp), exchange_timezone)


def _enrich_technical(rows: list[dict[str, Any]], missing: list[str]) -> dict[str, Any]:
    closes = [row["close"] for row in rows if row.get("close") is not None]
    latest_technical: dict[str, Any] = {
        "rsi14": None,
        "macd": None,
        "macd_signal": None,
        "macd_histogram": None,
        "bollinger_upper": None,
        "bollinger_middle": None,
        "bollinger_lower": None,
    }
    if len(closes) >= 15:
        latest_technical["rsi14"] = _rsi(closes, 14)
    else:
        missing.append("Data Missing: RSI14 unavailable because price history has fewer than 15 closes.")

    if len(closes) >= 35:
        macd, signal, histogram = _macd(closes)
        latest_technical["macd"] = macd
        latest_technical["macd_signal"] = signal
        latest_technical["macd_histogram"] = histogram
    else:
        missing.append("Data Missing: MACD unavailable because price history has fewer than 35 closes.")

    if len(closes) >= 20:
        middle = sum(closes[-20:]) / 20
        variance = sum((value - middle) ** 2 for value in closes[-20:]) / 20
        std = variance ** 0.5
        latest_technical["bollinger_upper"] = middle + 2 * std
        latest_technical["bollinger_middle"] = middle
        latest_technical["bollinger_lower"] = middle - 2 * std
    else:
        missing.append("Data Missing: Bollinger Bands unavailable because price history has fewer than 20 closes.")
    return latest_technical


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) <= period:
        return None
    gains = []
    losses = []
    for index in range(-period, 0):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * alpha + output[-1] * (1 - alpha))
    return output


def _macd(closes: list[float]) -> tuple[float | None, float | None, float | None]:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_series = [fast - slow for fast, slow in zip(ema12[-len(ema26) :], ema26)]
    signal_series = _ema(macd_series, 9)
    if not macd_series or not signal_series:
        return None, None, None
    macd = macd_series[-1]
    signal = signal_series[-1]
    return macd, signal, macd - signal


def fetch_stock_data(symbol: str) -> dict[str, Any]:
    settings = get_settings()
    sources = [{"name": "Yahoo Finance", "url": f"https://finance.yahoo.com/quote/{symbol}"}]
    missing: list[str] = []
    status = "ok"
    params = {"range": "6mo", "interval": "1d"}

    try:
        result = _fetch_yahoo_chart(symbol, params, settings)
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        meta = result.get("meta", {})
    except Exception as exc:
        fallback = _fetch_finmind_stock(symbol, settings)
        fallback["sources"] = sources + fallback["sources"]
        if fallback["status"] == "missing":
            fallback["missing"].insert(0, f"Data Missing: Yahoo Finance chart fetch failed and fallback was unavailable: {exc}")
        else:
            fallback["data"]["primary_price_source"] = "FinMind fallback"
            fallback["data"]["source_note"] = "Yahoo Finance was temporarily unavailable or rate-limited; FinMind price data was used instead."
        return fallback

    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    opens = quote.get("open") or []
    volumes = quote.get("volume") or []
    rows = []
    for i, ts in enumerate(timestamps):
        close = _safe_float(closes[i] if i < len(closes) else None)
        if close is None:
            continue
        rows.append(
            {
                "date": datetime.utcfromtimestamp(ts).date().isoformat(),
                "open": _safe_float(opens[i] if i < len(opens) else None),
                "high": _safe_float(highs[i] if i < len(highs) else None),
                "low": _safe_float(lows[i] if i < len(lows) else None),
                "close": close,
                "volume": _safe_float(volumes[i] if i < len(volumes) else None),
            }
        )

    if not rows:
        return {
            "status": "missing",
            "data": {},
            "sources": sources,
            "missing": ["Data Missing: no stock OHLCV history returned."],
        }

    latest = rows[-1]
    close = latest["close"]
    volume = latest["volume"]
    ma20 = sum(row["close"] for row in rows[-20:]) / 20 if len(rows) >= 20 else None
    ma60 = sum(row["close"] for row in rows[-60:]) / 60 if len(rows) >= 60 else None
    support = min(row["low"] for row in rows[-20:] if row["low"] is not None) if len(rows) >= 20 else None
    resistance = max(row["high"] for row in rows[-20:] if row["high"] is not None) if len(rows) >= 20 else None
    prev_close = rows[-2]["close"] if len(rows) >= 2 else None
    change_pct = ((close - prev_close) / prev_close * 100) if close is not None and prev_close else None

    if ma20 is None:
        missing.append("Data Missing: 20MA unavailable because price history has fewer than 20 rows.")
        status = "partial"
    if ma60 is None:
        missing.append("Data Missing: 60MA unavailable because price history has fewer than 60 rows.")
        status = "partial"
    technical = _enrich_technical(rows, missing)
    if any(item.startswith("Data Missing: RSI") or item.startswith("Data Missing: MACD") or item.startswith("Data Missing: Bollinger") for item in missing):
        status = "partial"

    realtime = _fetch_intraday_quote(symbol, settings)
    if realtime and realtime.get("date") == _today_taipei():
        sources.append({"name": realtime.get("source") or "盤中行情", "url": realtime.get("url"), "as_of": realtime.get("time")})
        close = realtime.get("price") or close
        prev_close = realtime.get("previous_close") or prev_close
        volume = realtime.get("volume") or volume
        change_pct = ((close - prev_close) / prev_close * 100) if close is not None and prev_close else change_pct
        latest["date"] = realtime.get("date") or latest["date"]
    else:
        realtime = None

    return {
        "status": status,
        "data": {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "latest_date": latest["date"],
            "close": close,
            "volume": volume,
            "is_realtime_price": bool(realtime),
            "realtime_time": realtime.get("time") if realtime else None,
            "realtime_source": realtime.get("source") if realtime else None,
            "is_delayed_price": bool(realtime and realtime.get("is_delayed")),
            "price_delay_note": realtime.get("delay_note") if realtime else None,
            "change_pct": change_pct,
            "ma20": ma20,
            "ma60": ma60,
            "support_20d": support,
            "resistance_20d": resistance,
            "technical": technical,
            "currency": meta.get("currency"),
            "exchange": meta.get("exchangeName"),
            "bars": rows[-90:],
        },
        "sources": _dedupe_sources(sources),
        "missing": missing,
    }


def _fetch_finmind_stock(symbol: str, settings: Any) -> dict[str, Any]:
    stock_id = symbol.split(".")[0]
    sources = [{"name": "FinMind TaiwanStockPrice", "url": "https://api.finmindtrade.com/docs"}]
    params = {"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": "2025-01-01"}
    if settings.finmind_token:
        params["token"] = settings.finmind_token
    last_error: Exception | None = None
    rows: list[dict[str, Any]] = []
    for _ in range(3):
        try:
            response = httpx.get(
                "https://api.finmindtrade.com/api/v4/data",
                params=params,
                headers=HEADERS,
                timeout=httpx.Timeout(settings.request_timeout, connect=15.0, read=settings.request_timeout, write=15.0, pool=15.0),
            )
            response.raise_for_status()
            rows = response.json().get("data") or []
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if not rows and last_error:
        return {"status": "missing", "data": {}, "sources": sources, "missing": [f"Data Missing: FinMind stock fallback failed: {last_error}"]}
    bars = []
    for row in rows[-130:]:
        close = _safe_float(row.get("close"))
        if close is None:
            continue
        bars.append(
            {
                "date": row.get("date"),
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("max")),
                "low": _safe_float(row.get("min")),
                "close": close,
                "volume": _safe_float(row.get("Trading_Volume")),
            }
        )
    if not bars:
        return {"status": "missing", "data": {}, "sources": sources, "missing": ["Data Missing: FinMind stock fallback returned no price rows."]}
    latest = bars[-1]
    close = latest["close"]
    prev_close = bars[-2]["close"] if len(bars) > 1 else None
    ma20 = sum(row["close"] for row in bars[-20:]) / 20 if len(bars) >= 20 else None
    ma60 = sum(row["close"] for row in bars[-60:]) / 60 if len(bars) >= 60 else None
    lows = [row["low"] for row in bars[-20:] if row["low"] is not None]
    highs = [row["high"] for row in bars[-20:] if row["high"] is not None]
    missing = []
    if ma20 is None:
        missing.append("Data Missing: 20MA unavailable because FinMind history has fewer than 20 rows.")
    if ma60 is None:
        missing.append("Data Missing: 60MA unavailable because FinMind history has fewer than 60 rows.")
    technical = _enrich_technical(bars, missing)
    realtime = _fetch_intraday_quote(symbol, settings)
    if realtime and realtime.get("date") == _today_taipei():
        sources.append({"name": realtime.get("source") or "盤中行情", "url": realtime.get("url"), "as_of": realtime.get("time")})
        close = realtime.get("price") or close
        prev_close = realtime.get("previous_close") or prev_close
        latest["date"] = realtime.get("date") or latest["date"]
    else:
        realtime = None

    return {
        "status": "partial" if missing else "ok",
        "data": {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "latest_date": latest["date"],
            "close": close,
            "volume": realtime.get("volume") if realtime and realtime.get("volume") is not None else latest["volume"],
            "is_realtime_price": bool(realtime),
            "realtime_time": realtime.get("time") if realtime else None,
            "realtime_source": realtime.get("source") if realtime else None,
            "is_delayed_price": bool(realtime and realtime.get("is_delayed")),
            "price_delay_note": realtime.get("delay_note") if realtime else None,
            "change_pct": ((close - prev_close) / prev_close * 100) if prev_close else None,
            "ma20": ma20,
            "ma60": ma60,
            "support_20d": min(lows) if lows else None,
            "resistance_20d": max(highs) if highs else None,
            "technical": technical,
            "currency": "TWD",
            "exchange": "TWSE/TPEx via FinMind",
            "bars": bars[-90:],
        },
        "sources": _dedupe_sources(sources),
        "missing": missing,
    }


def _fetch_intraday_quote(symbol: str, settings: Any) -> dict[str, Any] | None:
    realtime = _fetch_twse_realtime(symbol)
    if realtime:
        return realtime
    return _fetch_yahoo_intraday_quote(symbol, settings)


def _fetch_twse_realtime(symbol: str) -> dict[str, Any] | None:
    stock_id = symbol.split(".")[0]
    if not stock_id.isdigit():
        return None
    channels = [f"tse_{stock_id}.tw", f"otc_{stock_id}.tw"]
    channel_queries = ["|".join(channels), *channels]
    for channel in channel_queries:
        try:
            response = httpx.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": channel, "json": "1", "delay": "0", "_": str(int(datetime.now().timestamp() * 1000))},
                headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp", "User-Agent": "Mozilla/5.0"},
                timeout=12.0,
            )
            response.raise_for_status()
            rows = response.json().get("msgArray") or []
        except Exception:
            continue
        if not rows:
            continue
        row = rows[0]
        price = _safe_float(row.get("z")) or _safe_float(row.get("pz"))
        if price is None or price <= 0:
            continue
        volume = _safe_float(row.get("v"))
        if volume is not None and 0 < volume < 1_000_000:
            volume *= 1000
        date_raw = str(row.get("d") or "")
        time_raw = str(row.get("t") or "")
        date_text = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}" if len(date_raw) == 8 else None
        if ":" in time_raw:
            time_text = time_raw
        else:
            digits = "".join(ch for ch in time_raw if ch.isdigit())
            time_text = f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}" if len(digits) >= 6 else None
        return {
            "price": price,
            "volume": volume,
            "date": date_text,
            "time": f"{date_text} {time_text}".strip() if date_text or time_text else None,
            "channel": channel,
            "source": "TWSE MIS 即時行情",
            "url": "https://mis.twse.com.tw/stock/index.jsp",
            "is_delayed": False,
            "delay_note": "TWSE MIS 即時行情。",
        }
    return None


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for source in sources:
        key = (source.get("name"), source.get("url"), source.get("as_of"))
        if key in seen:
            continue
        seen.add(key)
        output.append(source)
    return output


def _fetch_yahoo_intraday_quote(symbol: str, settings: Any) -> dict[str, Any] | None:
    for host in YAHOO_HOSTS:
        try:
            response = httpx.get(
                f"https://{host}/v8/finance/chart/{symbol}",
                params={"range": "1d", "interval": "1m"},
                headers=HEADERS,
                timeout=httpx.Timeout(settings.request_timeout, connect=15.0, read=settings.request_timeout, write=15.0, pool=15.0),
            )
            response.raise_for_status()
            result = response.json()["chart"]["result"][0]
        except Exception:
            continue
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        latest_index = None
        for index in range(len(timestamps) - 1, -1, -1):
            if index < len(closes) and _safe_float(closes[index]) is not None:
                latest_index = index
                break
        if latest_index is None:
            continue
        ts = timestamps[latest_index]
        gmtoffset = _safe_float(meta.get("gmtoffset"))
        if (gmtoffset is None or gmtoffset == 0) and symbol.upper().endswith((".TW", ".TWO")):
            gmtoffset = 8 * 60 * 60
        exchange_dt = _exchange_datetime(ts, gmtoffset)
        price = _safe_float(closes[latest_index])
        volume = _safe_float(meta.get("regularMarketVolume"))
        if volume is None and latest_index < len(volumes):
            volume = _safe_float(volumes[latest_index])
        previous_close = _safe_float(meta.get("chartPreviousClose")) or _safe_float(meta.get("previousClose"))
        if not price or not exchange_dt:
            continue
        return {
            "price": price,
            "volume": volume,
            "previous_close": previous_close,
            "date": exchange_dt.date().isoformat(),
            "time": exchange_dt.isoformat(timespec="seconds"),
            "source": "Yahoo Finance 盤中延遲行情",
            "url": f"https://finance.yahoo.com/quote/{symbol}",
            "is_delayed": True,
            "delay_note": "TWSE MIS 未取得時使用 Yahoo Finance 1 分鐘盤中/延遲行情，不等同券商即時報價。",
        }
    return None


def _fetch_yahoo_chart(symbol: str, params: dict[str, str], settings: Any) -> dict[str, Any]:
    errors: list[str] = []
    encoded_symbol = symbol
    for host in YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{encoded_symbol}"
        for _ in range(2):
            try:
                response = httpx.get(
                    url,
                    params=params,
                    headers=HEADERS,
                    timeout=httpx.Timeout(settings.request_timeout, connect=15.0, read=settings.request_timeout, write=15.0, pool=15.0),
                )
                response.raise_for_status()
                result = response.json()["chart"]["result"][0]
                if result:
                    return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{host}: {exc}")
    raise RuntimeError("Yahoo Finance chart fetch failed after retries: " + " | ".join(errors[-3:]))
