import os
import math
from datetime import datetime, timezone
from typing import List, Dict, Any

import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

APP_NAME = "IDX Signal API (Personal)"
DEFAULT_DAYS = 240  # ~1 tahun trading (kurang lebih)
CACHE_TTL_SECONDS = 60 * 10  # 10 menit

# --- Simple in-memory cache (personal use) ---
_cache: Dict[str, Dict[str, Any]] = {}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    if _now_ts() - item["ts"] > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return item["value"]


def cache_set(key: str, value: Any):
    _cache[key] = {"ts": _now_ts(), "value": value}


def _to_jk_ticker(t: str) -> str:
    t = t.strip().upper()
    if t.startswith("^"):  # index like ^JKSE
        return t
    if t.endswith(".JK"):
        return t
    return f"{t}.JK"


def _normalize_yf_columns(df: pd.DataFrame, yf_ticker: str) -> pd.DataFrame:
    """Normalize yfinance output so single-ticker OHLCV columns are flat."""
    if isinstance(df.columns, pd.MultiIndex):
        flat = []
        for col in df.columns:
            parts = [str(x) for x in col if x and str(x) != ""]
            flat.append("_".join(parts))
        df.columns = flat

        ticker_suffix = f"_{yf_ticker}"
        renamed = {}
        for c in df.columns:
            if c.endswith(ticker_suffix):
                renamed[c] = c[: -len(ticker_suffix)]
        if renamed:
            df = df.rename(columns=renamed)

    return df


def fetch_ohlcv(ticker: str, days: int) -> pd.DataFrame:
    """
    Fetch daily OHLCV via yfinance.
    """
    yf_ticker = _to_jk_ticker(ticker)
    key = f"ohlcv:{yf_ticker}:{days}"
    cached = cache_get(key)
    if cached is not None:
        return cached.copy()

    # period approximation based on days
    if days <= 60:
        period = "3mo"
    elif days <= 120:
        period = "6mo"
    elif days <= 260:
        period = "1y"
    else:
        period = "2y"

    df = yf.download(
        yf_ticker,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"Data kosong untuk ticker {yf_ticker}")

    df = _normalize_yf_columns(df, yf_ticker)
    df = df.reset_index()
    df = df.rename(
        columns={
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adj_close",
            "Volume": "volume",
        }
    )

    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

    if len(df) > days:
        df = df.iloc[-days:].copy()

    cache_set(key, df.copy())
    return df


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / (down.replace(0, np.nan))
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat(
        [
            (high - low),
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


def pivot_high(df: pd.DataFrame, lookback: int = 20) -> float:
    # resistance sederhana: highest high lookback terakhir (tidak termasuk hari terakhir)
    if len(df) < lookback + 2:
        return float(df["high"].max())
    window = df.iloc[-(lookback + 1) : -1]
    return float(window["high"].max())


def pivot_low(df: pd.DataFrame, lookback: int = 20) -> float:
    # support sederhana: lowest low lookback terakhir (tidak termasuk hari terakhir)
    if len(df) < lookback + 2:
        return float(df["low"].min())
    window = df.iloc[-(lookback + 1) : -1]
    return float(window["low"].min())


def compute_signal(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Setup:
    - Trend OK: close > MA50, MA20 > MA50
    - Breakout: close > resistance (pivot high 20) + volume > volMA20
    - Pullback: close dekat MA20 (<= 2% jarak) + candle bullish + trend OK
    """
    d = df.copy()
    d["ma20"] = sma(d["close"], 20)
    d["ma50"] = sma(d["close"], 50)
    d["volma20"] = sma(d["volume"], 20)
    d["rsi14"] = rsi(d["close"], 14)
    d["atr14"] = atr(d, 14)

    last = d.iloc[-1]

    close = float(last["close"])
    open_ = float(last["open"])
    vol = float(last["volume"]) if not math.isnan(float(last["volume"])) else 0.0

    ma20 = float(last["ma20"]) if not math.isnan(float(last["ma20"])) else close
    ma50 = float(last["ma50"]) if not math.isnan(float(last["ma50"])) else close
    volma20 = float(last["volma20"]) if not math.isnan(float(last["volma20"])) else vol
    atr14v = float(last["atr14"]) if not math.isnan(float(last["atr14"])) else 0.0

    resistance = pivot_high(d, 20)
    support = pivot_low(d, 20)

    trend_ok = (close > ma50) and (ma20 > ma50)

    breakout = trend_ok and (close > resistance) and (vol > volma20)

    dist_ma20 = abs(close - ma20) / ma20 if ma20 != 0 else 0
    pullback = trend_ok and (dist_ma20 <= 0.02) and (close >= open_)

    entry = None
    sl = None
    tp1 = None
    tp2 = None
    setup = "NONE"
    reason = []

    if breakout:
        setup = "BREAKOUT"
        entry = close
        sl = min(support, close - (2.0 * atr14v)) if atr14v > 0 else support
        tp1 = close + (close - sl) * 1.0  # 1R
        tp2 = close + (close - sl) * 2.0  # 2R
        reason = [
            "Trend OK (Close>MA50 & MA20>MA50)",
            f"Close tembus resistance ~{resistance:.2f}",
            "Volume > rata-rata 20 hari",
        ]
    elif pullback:
        setup = "PULLBACK_MA20"
        entry = close
        sl = min(support, ma20 * 0.98)
        tp1 = resistance
        tp2 = resistance + (resistance - sl)
        reason = [
            "Trend OK (Close>MA50 & MA20>MA50)",
            "Harga dekat MA20 (pullback sehat)",
            "Candle bullish (close >= open)",
        ]
    else:
        reason = ["Belum ada setup rapi (breakout / pullback)"]

    rr = None
    if entry and sl and entry > sl:
        rr = {
            "risk_per_share": entry - sl,
            "r_multiple_tp1": ((tp1 - entry) / (entry - sl)) if tp1 else None,
            "r_multiple_tp2": ((tp2 - entry) / (entry - sl)) if tp2 else None,
        }

    return {
        "setup": setup,
        "trend_ok": trend_ok,
        "close": close,
        "ma20": ma20,
        "ma50": ma50,
        "resistance": resistance,
        "support": support,
        "volume": vol,
        "volma20": volma20,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "reason": reason,
        "asof": str(pd.to_datetime(last["date"]).date()),
    }


def market_regime() -> Dict[str, Any]:
    """
    Regime sederhana pakai IHSG (^JKSE):
    - RISK_ON: close > MA50 dan MA20 > MA50
    - RISK_OFF: close < MA50
    - NO_TRADE_DAY: risk-off + volatilitas tinggi (ATR% > 2%) atau drop harian <= -2%
    """
    try:
        df = fetch_ohlcv("^JKSE", days=260)
        d = df.copy()
        d["ma20"] = sma(d["close"], 20)
        d["ma50"] = sma(d["close"], 50)
        d["atr14"] = atr(d, 14)

        last = d.iloc[-1]
        prev = d.iloc[-2] if len(d) >= 2 else last

        close = float(last["close"])
        ma20 = float(last["ma20"]) if not math.isnan(float(last["ma20"])) else close
        ma50 = float(last["ma50"]) if not math.isnan(float(last["ma50"])) else close
        atr14v = float(last["atr14"]) if not math.isnan(float(last["atr14"])) else 0.0

        trend_on = (close > ma50) and (ma20 > ma50)
        trend_off = (close < ma50)

        day_change = (close - float(prev["close"])) / float(prev["close"]) if float(prev["close"]) != 0 else 0
        atr_pct = (atr14v / close) if close != 0 else 0

        status = "NEUTRAL"
        note = []

        if trend_on:
            status = "RISK_ON"
            note = ["IHSG uptrend (Close>MA50 & MA20>MA50)"]
        elif trend_off:
            status = "RISK_OFF"
            note = ["IHSG di bawah MA50 (risk-off)"]

        if trend_off and (atr_pct >= 0.02 or day_change <= -0.02):
            status = "NO_TRADE_DAY"
            note = ["IHSG risk-off + volatilitas/penurunan tajam → prefer no trade"]

        return {
            "status": status,
            "close": close,
            "ma20": ma20,
            "ma50": ma50,
            "day_change_pct": round(day_change * 100, 2),
            "atr14_pct": round(atr_pct * 100, 2),
            "asof": str(pd.to_datetime(last["date"]).date()),
            "note": note,
            "ticker": "^JKSE",
        }
    except Exception as e:
        return {
            "status": "UNKNOWN",
            "close": None,
            "ma20": None,
            "ma50": None,
            "day_change_pct": None,
            "atr14_pct": None,
            "asof": None,
            "note": [f"Data IHSG belum tersedia: {str(e)}"],
            "ticker": "^JKSE",
        }


def load_universe(name: str) -> List[str]:
    name = name.upper()
    if name != "LQ45":
        raise HTTPException(status_code=400, detail="Universe hanya support: LQ45 (sementara)")
    path = os.path.join(os.path.dirname(__file__), "tickers_lq45.json")
    import json
    with open(path, "r", encoding="utf-8") as f:
        tickers = json.load(f)
    return tickers


app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # personal use; kalau sudah publik nanti kita kunci
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "ok": True,
        "name": APP_NAME,
        "message": "API aktif. Endpoint di bawah bisa langsung dipanggil untuk data.",
        "endpoints": {
            "health": "/api/health",
            "market_regime": "/api/market-regime",
            "ohlcv": "/api/ohlcv?ticker=BBRI&days=260",
            "signal": "/api/signal?ticker=BBRI&days=260",
            "screener": "/api/screener?universe=LQ45&days=260",
        },
    }


@app.get("/api")
def api_index():
    return root()


@app.get("/api/health")
def health():
    return {"ok": True, "name": APP_NAME, "ts": _now_ts()}


@app.get("/api/market-regime")
def api_market_regime():
    return market_regime()


@app.get("/api/ohlcv")
def api_ohlcv(
    ticker: str = Query(..., description="Contoh: BBRI atau BBRI.JK atau ^JKSE"),
    days: int = Query(DEFAULT_DAYS, ge=30, le=520),
):
    df = fetch_ohlcv(ticker, days)

    out = []
    for _, r in df.iterrows():
        dt = pd.to_datetime(r["date"])
        ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        out.append(
            {
                "time": ts,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume", 0) or 0),
            }
        )
    return {"ticker": _to_jk_ticker(ticker), "bars": out}


@app.get("/api/signal")
def api_signal(
    ticker: str = Query(..., description="Contoh: BBRI / BBCA / ADRO"),
    days: int = Query(DEFAULT_DAYS, ge=60, le=520),
):
    df = fetch_ohlcv(ticker, days)
    sig = compute_signal(df)
    return {"ticker": _to_jk_ticker(ticker), "signal": sig}


@app.get("/api/screener")
def api_screener(
    universe: str = Query("LQ45"),
    days: int = Query(DEFAULT_DAYS, ge=90, le=520),
):
    tickers = load_universe(universe)
    regime = market_regime()

    results = []
    for t in tickers:
        try:
            df = fetch_ohlcv(t, days)
            sig = compute_signal(df)

            score = 0
            if sig["trend_ok"]:
                score += 2
            if sig["setup"] == "BREAKOUT":
                score += 3
            if sig["setup"] == "PULLBACK_MA20":
                score += 2
            if sig["volume"] and sig["volma20"] and sig["volume"] > sig["volma20"]:
                score += 1

            results.append(
                {
                    "ticker": _to_jk_ticker(t),
                    "score": score,
                    "setup": sig["setup"],
                    "close": sig["close"],
                    "resistance": sig["resistance"],
                    "support": sig["support"],
                    "asof": sig["asof"],
                    "reason": sig["reason"],
                }
            )
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "universe": universe.upper(),
        "market_regime": regime,
        "count": len(results),
        "top": results[:25],
    }
