from __future__ import annotations

import os
import json
import time
import math
import asyncio
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import pandas as pd

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# 1) ENVIRONMENT + BASIC CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BINGX_API_KEY = os.getenv("BINGX_API_KEY", "").strip()
BINGX_SECRET = os.getenv("BINGX_SECRET", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN environment variable")
if not ENV_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_CHAT_ID environment variable")
if not BINGX_API_KEY:
    raise RuntimeError("Missing BINGX_API_KEY environment variable")
if not BINGX_SECRET:
    raise RuntimeError("Missing BINGX_SECRET environment variable")

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
STATS_FILE = DATA_DIR / "stats.json"
OPEN_META_FILE = DATA_DIR / "open_trades.json"

# You can override symbols from Render ENV:
# SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
SYMBOLS = [
    s.strip()
    for s in os.getenv("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT").split(",")
    if s.strip()
]

TIMEFRAME = os.getenv("TIMEFRAME", "15m")
OHLCV_LIMIT = int(os.getenv("OHLCV_LIMIT", "260"))

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
POSITION_CHECK_SECONDS = int(os.getenv("POSITION_CHECK_SECONDS", "20"))

EMA_PERIOD = int(os.getenv("EMA_PERIOD", "200"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))

ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.8"))
ATR_TRAIL_MULT = float(os.getenv("ATR_TRAIL_MULT", "1.25"))

NORMAL_LEVERAGE = int(os.getenv("NORMAL_LEVERAGE", "3"))
AGGRESSIVE_LEVERAGE = int(os.getenv("AGGRESSIVE_LEVERAGE", "5"))

NORMAL_RISK_PCT = float(os.getenv("NORMAL_RISK_PCT", "1.0"))       # 1.0 = 1%
AGGRESSIVE_RISK_PCT = float(os.getenv("AGGRESSIVE_RISK_PCT", "1.5")) # 1.5 = 1.5%

MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "45"))

# Safety cap: max notional per trade as a percentage of equity * leverage.
# Example: equity 1000, leverage 3, cap 0.30 => max notional 900 USDT.
POSITION_EQUITY_CAP = float(os.getenv("POSITION_EQUITY_CAP", "0.30"))

MIN_NOTIONAL_USDT = float(os.getenv("MIN_NOTIONAL_USDT", "6"))

# Reduce noisy repeated alerts.
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "120"))

# =========================================================
# 2) LOGGING
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("BINGX_LIVE_EMA_RSI_SAR_BOT")

# =========================================================
# 3) JSON STORAGE
# =========================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if not path.exists():
            atomic_write_json(path, default)
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed to read JSON %s: %s", path, exc)
        return dict(default)


DEFAULT_STATE: Dict[str, Any] = {
    "running": True,
    "emergency": False,
    "mode": "NORMAL",
    "leverage": NORMAL_LEVERAGE,
    "risk_pct": NORMAL_RISK_PCT,
    "last_scan": {},
    "last_trade_ts": {},
    "last_alert_ts": {},
    "started_at": utc_now(),
    "updated_at": utc_now(),
}

DEFAULT_STATS: Dict[str, Any] = {
    "since": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "total_closed": 0,
    "wins": 0,
    "losses": 0,
    "win_rate": 0.0,
    "total_pnl_pct": 0.0,
    "best_trade_pct": 0.0,
    "worst_trade_pct": 0.0,
    "history": [],
    "updated_at": utc_now(),
}

DEFAULT_OPEN_META: Dict[str, Any] = {}

STATE = read_json(STATE_FILE, DEFAULT_STATE)
STATS = read_json(STATS_FILE, DEFAULT_STATS)
OPEN_META = read_json(OPEN_META_FILE, DEFAULT_OPEN_META)


def save_state() -> None:
    STATE["updated_at"] = utc_now()
    atomic_write_json(STATE_FILE, STATE)


def save_stats() -> None:
    total = int(STATS.get("total_closed", 0))
    wins = int(STATS.get("wins", 0))
    STATS["win_rate"] = round((wins / total * 100.0), 2) if total else 0.0
    STATS["total_pnl_pct"] = round(float(STATS.get("total_pnl_pct", 0.0)), 4)
    STATS["best_trade_pct"] = round(float(STATS.get("best_trade_pct", 0.0)), 4)
    STATS["worst_trade_pct"] = round(float(STATS.get("worst_trade_pct", 0.0)), 4)
    STATS["updated_at"] = utc_now()

    # Keep file small.
    history = STATS.get("history", [])
    if isinstance(history, list) and len(history) > 300:
        STATS["history"] = history[-300:]

    atomic_write_json(STATS_FILE, STATS)


def save_open_meta() -> None:
    atomic_write_json(OPEN_META_FILE, OPEN_META)


# =========================================================
# 4) HEALTH SERVER FOR RENDER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = {
            "status": "ok",
            "service": "bingx-live-trading-bot",
            "running": STATE.get("running"),
            "emergency": STATE.get("emergency"),
            "time": utc_now(),
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    def _run() -> None:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info("Health server started on port %s", PORT)
        server.serve_forever()

    thread = threading.Thread(target=_run, name="health-server", daemon=True)
    thread.start()


# =========================================================
# 5) EXCHANGE SETUP
# =========================================================

EXCHANGE_LOCK = threading.RLock()
JOB_LOCK = asyncio.Lock()

exchange = ccxt.bingx(
    {
        "apiKey": BINGX_API_KEY,
        "secret": BINGX_SECRET,
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",
            "adjustForTimeDifference": True,
        },
    }
)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def normalize_symbol(symbol: str) -> str:
    """Return an available ccxt symbol where possible."""
    try:
        if symbol in exchange.markets:
            return symbol
        base = symbol.split("/")[0].upper()
        for candidate in (f"{base}/USDT:USDT", f"{base}/USDT"):
            if candidate in exchange.markets:
                return candidate
    except Exception:
        pass
    return symbol


def init_exchange() -> None:
    with EXCHANGE_LOCK:
        markets = exchange.load_markets()
        logger.info("Loaded %s BingX markets", len(markets))

        # Normalize configured symbols after market load.
        for idx, s in enumerate(list(SYMBOLS)):
            SYMBOLS[idx] = normalize_symbol(s)

        # Try one-way mode. Some accounts/exchanges reject this call if already set.
        try:
            if hasattr(exchange, "set_position_mode"):
                exchange.set_position_mode(False)
                logger.info("Position mode set to One-Way")
        except Exception as exc:
            logger.warning("Could not set global One-Way mode, will continue: %s", exc)

        # Preload per-symbol isolated + leverage.
        for s in SYMBOLS:
            configure_symbol(s, int(STATE.get("leverage", NORMAL_LEVERAGE)))


def configure_symbol(symbol: str, leverage: int) -> None:
    """Set isolated margin and leverage. Errors are logged but not fatal."""
    with EXCHANGE_LOCK:
        try:
            if hasattr(exchange, "set_margin_mode"):
                exchange.set_margin_mode("isolated", symbol)
                logger.info("Margin mode set to isolated: %s", symbol)
        except Exception as exc:
            msg = str(exc).lower()
            if "no need" not in msg and "not modified" not in msg and "same" not in msg:
                logger.warning("Could not set isolated margin for %s: %s", symbol, exc)

        try:
            if hasattr(exchange, "set_leverage"):
                exchange.set_leverage(leverage, symbol, params={"marginMode": "isolated"})
                logger.info("Leverage set to %sx: %s", leverage, symbol)
        except Exception as exc:
            logger.warning("Could not set leverage for %s: %s", symbol, exc)


# =========================================================
# 6) INDICATORS: EMA, RSI, ATR, PARABOLIC SAR
# =========================================================

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.astype(float).fillna(50.0)


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().astype(float)


def calc_psar(high: pd.Series, low: pd.Series, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    """
    Simple Parabolic SAR implementation.
    Returns SAR value for each candle.
    """
    h = high.astype(float).tolist()
    l = low.astype(float).tolist()
    n = len(h)
    if n < 3:
        return pd.Series([float("nan")] * n, index=high.index)

    psar = [float("nan")] * n
    bull = True
    af = step
    ep = h[0]
    sar = l[0]

    # Choose initial trend from first two candles.
    if h[1] + l[1] < h[0] + l[0]:
        bull = False
        ep = l[0]
        sar = h[0]

    for i in range(1, n):
        prev_sar = sar

        if bull:
            sar = prev_sar + af * (ep - prev_sar)
            if i >= 2:
                sar = min(sar, l[i - 1], l[i - 2])
            else:
                sar = min(sar, l[i - 1])

            if l[i] < sar:
                bull = False
                sar = ep
                ep = l[i]
                af = step
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(af + step, max_step)
        else:
            sar = prev_sar + af * (ep - prev_sar)
            if i >= 2:
                sar = max(sar, h[i - 1], h[i - 2])
            else:
                sar = max(sar, h[i - 1])

            if h[i] > sar:
                bull = True
                sar = ep
                ep = h[i]
                af = step
            else:
                if l[i] < ep:
                    ep = l[i]
                    af = min(af + step, max_step)

        psar[i] = sar

    return pd.Series(psar, index=high.index).astype(float)


def fetch_ohlcv_df(symbol: str, timeframe: str = TIMEFRAME, limit: int = OHLCV_LIMIT) -> Optional[pd.DataFrame]:
    with EXCHANGE_LOCK:
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:
            logger.warning("fetch_ohlcv failed for %s: %s", symbol, exc)
            return None

    if not rows or len(rows) < EMA_PERIOD + 5:
        return None

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["ema200"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    df["rsi"] = calc_rsi(df["close"], RSI_PERIOD)
    df["atr"] = calc_atr(df, ATR_PERIOD)
    df["psar"] = calc_psar(df["high"], df["low"])

    return df.dropna().reset_index(drop=True)


def get_last_closed_candles(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    # Use closed candles: -2 is safer than the still-forming -1 candle.
    if len(df) >= 3:
        return df.iloc[-2], df.iloc[-3]
    return df.iloc[-1], df.iloc[-2]


def build_signal(symbol: str, df: pd.DataFrame) -> Dict[str, Any]:
    last, prev = get_last_closed_candles(df)

    close = safe_float(last["close"])
    open_ = safe_float(last["open"])
    high = safe_float(last["high"])
    low = safe_float(last["low"])
    ema = safe_float(last["ema200"])
    rsi = safe_float(last["rsi"], 50.0)
    prev_rsi = safe_float(prev["rsi"], 50.0)
    atr = safe_float(last["atr"])
    psar = safe_float(last["psar"])

    above_ema = close > ema
    below_ema = close < ema
    psar_bull = psar < close
    psar_bear = psar > close

    green_candle = close > open_
    red_candle = close < open_

    # RSI Recovery for LONG:
    # Price above EMA200, RSI was weak and recovers upward, SAR supports direction.
    long_recovery = prev_rsi <= 44 and rsi >= 46 and rsi > prev_rsi
    long_rejection = 45 <= rsi <= 62 and low <= ema * 1.01 and close > ema and rsi >= prev_rsi
    long_ok = above_ema and psar_bull and green_candle and (long_recovery or long_rejection)

    # RSI Rejection for SHORT:
    # Price below EMA200, RSI was strong and rejects downward, SAR supports direction.
    short_rejection = prev_rsi >= 56 and rsi <= 54 and rsi < prev_rsi
    short_continuation = 38 <= rsi <= 55 and high >= ema * 0.99 and close < ema and rsi <= prev_rsi
    short_ok = below_ema and psar_bear and red_candle and (short_rejection or short_continuation)

    if long_ok:
        action = "LONG"
        reason = "EMA200 bullish + RSI recovery/rejection + PSAR bullish"
    elif short_ok:
        action = "SHORT"
        reason = "EMA200 bearish + RSI rejection + PSAR bearish"
    else:
        action = "NO_TRADE"
        parts = []
        if above_ema:
            parts.append("above EMA200")
        elif below_ema:
            parts.append("below EMA200")
        else:
            parts.append("near EMA200")

        parts.append("PSAR bull" if psar_bull else "PSAR bear")
        parts.append(f"RSI {rsi:.1f}")
        reason = " | ".join(parts)

    return {
        "symbol": symbol,
        "action": action,
        "reason": reason,
        "close": round(close, 8),
        "ema200": round(ema, 8),
        "rsi": round(rsi, 2),
        "prev_rsi": round(prev_rsi, 2),
        "atr": round(atr, 8),
        "psar": round(psar, 8),
        "candle_time": str(last.get("datetime", "")),
        "checked_at": utc_now(),
    }


# =========================================================
# 7) POSITIONS, BALANCE, ORDERS
# =========================================================

def get_usdt_balance() -> Dict[str, float]:
    with EXCHANGE_LOCK:
        balance = exchange.fetch_balance(params={"type": "swap"})

    usdt = balance.get("USDT", {}) if isinstance(balance, dict) else {}
    total = safe_float(usdt.get("total"), 0.0)
    free = safe_float(usdt.get("free"), 0.0)
    used = safe_float(usdt.get("used"), 0.0)

    # Some exchanges keep futures values in info.
    if total <= 0 and isinstance(balance, dict):
        total = safe_float(balance.get("total", {}).get("USDT"), total)
        free = safe_float(balance.get("free", {}).get("USDT"), free)
        used = safe_float(balance.get("used", {}).get("USDT"), used)

    return {"total": total, "free": free, "used": used}


def fetch_ticker_price(symbol: str) -> float:
    with EXCHANGE_LOCK:
        ticker = exchange.fetch_ticker(symbol)
    for key in ("last", "mark", "close", "bid", "ask"):
        price = safe_float(ticker.get(key), 0.0)
        if price > 0:
            return price
    raise RuntimeError(f"Could not fetch valid price for {symbol}")


def fetch_positions_safe(symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with EXCHANGE_LOCK:
        try:
            positions = exchange.fetch_positions(symbols or SYMBOLS)
        except TypeError:
            positions = exchange.fetch_positions()
        except Exception as exc:
            logger.warning("fetch_positions failed: %s", exc)
            return []

    parsed: List[Dict[str, Any]] = []
    for p in positions or []:
        pp = parse_position(p)
        if pp and pp["contracts"] > 0:
            parsed.append(pp)
    return parsed


def parse_position(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    info = pos.get("info", {}) or {}

    symbol = pos.get("symbol") or info.get("symbol") or info.get("currency")
    if not symbol:
        return None

    # Normalize BingX raw symbol if needed.
    symbol = normalize_symbol(str(symbol).replace("-", "/"))

    contracts = safe_float(pos.get("contracts"), 0.0)
    if contracts <= 0:
        # Fallbacks for different raw API shapes.
        for k in ("positionAmt", "positionAmtAbs", "availableAmt", "holdingAmount", "size", "qty", "quantity"):
            contracts = abs(safe_float(info.get(k), 0.0))
            if contracts > 0:
                break

    if contracts <= 0:
        return None

    side_raw = str(pos.get("side") or info.get("side") or info.get("positionSide") or "").lower()
    amount_signed = safe_float(info.get("positionAmt"), 0.0)

    if "short" in side_raw or side_raw == "sell" or amount_signed < 0:
        side = "SHORT"
    elif "long" in side_raw or side_raw == "buy" or amount_signed > 0:
        side = "LONG"
    else:
        # Last fallback: infer from notional if available.
        side = "LONG"

    entry = safe_float(pos.get("entryPrice"), 0.0)
    if entry <= 0:
        for k in ("avgPrice", "averagePrice", "entryPrice", "avgOpenPrice"):
            entry = safe_float(info.get(k), 0.0)
            if entry > 0:
                break

    mark = safe_float(pos.get("markPrice"), 0.0)
    if mark <= 0:
        for k in ("markPrice", "latestPrice", "lastPrice"):
            mark = safe_float(info.get(k), 0.0)
            if mark > 0:
                break

    leverage = safe_float(pos.get("leverage"), safe_float(info.get("leverage"), STATE.get("leverage", NORMAL_LEVERAGE)))
    unrealized = safe_float(pos.get("unrealizedPnl"), safe_float(info.get("unrealizedPnl"), 0.0))

    meta = OPEN_META.get(symbol, {})
    sl = safe_float(meta.get("sl"), 0.0)

    if mark <= 0:
        try:
            mark = fetch_ticker_price(symbol)
        except Exception:
            mark = entry

    pnl_pct = calc_pnl_pct(side, entry, mark, leverage)

    return {
        "symbol": symbol,
        "side": side,
        "contracts": contracts,
        "entry": entry,
        "mark": mark,
        "leverage": leverage,
        "unrealized": unrealized,
        "pnl_pct": pnl_pct,
        "sl": sl,
        "raw": pos,
    }


def calc_pnl_pct(side: str, entry: float, exit_price: float, leverage: float) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    direction = 1.0 if side.upper() == "LONG" else -1.0
    raw_pct = ((exit_price - entry) / entry) * direction * 100.0
    return round(raw_pct * max(leverage, 1.0), 4)


def calc_order_amount(symbol: str, entry: float, sl: float, equity: float, leverage: int, risk_pct: float) -> float:
    if entry <= 0 or sl <= 0 or equity <= 0:
        return 0.0

    stop_pct = abs(entry - sl) / entry
    if stop_pct <= 0:
        return 0.0

    risk_usdt = equity * (risk_pct / 100.0)
    notional_by_risk = risk_usdt / stop_pct
    notional_cap = equity * leverage * POSITION_EQUITY_CAP
    notional = min(notional_by_risk, notional_cap)

    if notional < MIN_NOTIONAL_USDT:
        logger.warning("Notional below minimum: %.4f < %.4f", notional, MIN_NOTIONAL_USDT)
        return 0.0

    amount = notional / entry

    with EXCHANGE_LOCK:
        try:
            precise = exchange.amount_to_precision(symbol, amount)
            amount = safe_float(precise, 0.0)
        except Exception:
            pass

    return amount


def count_open_positions() -> int:
    return len(fetch_positions_safe(SYMBOLS))


def has_open_position(symbol: str) -> bool:
    for p in fetch_positions_safe([symbol]):
        if p["symbol"] == symbol:
            return True
    return False


def is_in_cooldown(symbol: str) -> bool:
    last_ts = safe_float(STATE.get("last_trade_ts", {}).get(symbol), 0.0)
    if last_ts <= 0:
        return False
    return (time.time() - last_ts) < COOLDOWN_MINUTES * 60


def create_market_order(symbol: str, side: str, amount: float, reduce_only: bool = False) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if reduce_only:
        params["reduceOnly"] = True

    with EXCHANGE_LOCK:
        try:
            return exchange.create_order(symbol, "market", side, amount, None, params=params)
        except Exception as first_exc:
            if not reduce_only:
                raise

            # Retry with positionSide BOTH for one-way accounts if required by BingX.
            try:
                params["positionSide"] = "BOTH"
                return exchange.create_order(symbol, "market", side, amount, None, params=params)
            except Exception:
                raise first_exc


def open_position(symbol: str, signal: Dict[str, Any]) -> Tuple[bool, str]:
    if STATE.get("emergency"):
        return False, "Emergency mode is active."
    if not STATE.get("running", True):
        return False, "Bot is paused."
    if has_open_position(symbol):
        return False, f"{symbol}: position already open."
    if count_open_positions() >= MAX_OPEN_POSITIONS:
        return False, "Max open positions reached."
    if is_in_cooldown(symbol):
        return False, f"{symbol}: cooldown active."

    action = signal.get("action")
    if action not in ("LONG", "SHORT"):
        return False, f"{symbol}: no trade signal."

    leverage = int(STATE.get("leverage", NORMAL_LEVERAGE))
    risk_pct = float(STATE.get("risk_pct", NORMAL_RISK_PCT))

    configure_symbol(symbol, leverage)

    entry_price = fetch_ticker_price(symbol)
    atr = safe_float(signal.get("atr"), 0.0)
    if atr <= 0:
        return False, f"{symbol}: ATR unavailable."

    if action == "LONG":
        sl = entry_price - (ATR_SL_MULT * atr)
        order_side = "buy"
    else:
        sl = entry_price + (ATR_SL_MULT * atr)
        order_side = "sell"

    balance = get_usdt_balance()
    equity = balance["total"] if balance["total"] > 0 else balance["free"]
    amount = calc_order_amount(symbol, entry_price, sl, equity, leverage, risk_pct)

    if amount <= 0:
        return False, f"{symbol}: amount too small."

    try:
        order = create_market_order(symbol, order_side, amount, reduce_only=False)
    except Exception as exc:
        logger.exception("Open order failed for %s: %s", symbol, exc)
        return False, f"{symbol}: open order failed: {exc}"

    avg = safe_float(order.get("average"), 0.0)
    filled_price = avg if avg > 0 else entry_price

    OPEN_META[symbol] = {
        "symbol": symbol,
        "side": action,
        "entry": filled_price,
        "amount": amount,
        "leverage": leverage,
        "risk_pct": risk_pct,
        "sl": sl,
        "atr_entry": atr,
        "opened_at": utc_now(),
        "mode": STATE.get("mode", "NORMAL"),
        "signal": signal,
    }
    save_open_meta()

    STATE.setdefault("last_trade_ts", {})[symbol] = time.time()
    save_state()

    msg = (
        f"✅ صفقة جديدة {action}\n"
        f"{symbol}\n"
        f"Entry: {filled_price:.8f}\n"
        f"Amount: {amount}\n"
        f"SL: {sl:.8f}\n"
        f"Leverage: {leverage}x\n"
        f"Risk: {risk_pct:.2f}%\n"
        f"Reason: {signal.get('reason')}"
    )
    return True, msg


def record_closed_trade(
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    leverage: float,
    reason: str,
    amount: float = 0.0,
    unrealized: float = 0.0,
) -> Dict[str, Any]:
    pnl_pct = calc_pnl_pct(side, entry, exit_price, leverage)

    STATS["total_closed"] = int(STATS.get("total_closed", 0)) + 1
    if pnl_pct > 0:
        STATS["wins"] = int(STATS.get("wins", 0)) + 1
    else:
        STATS["losses"] = int(STATS.get("losses", 0)) + 1

    STATS["total_pnl_pct"] = float(STATS.get("total_pnl_pct", 0.0)) + pnl_pct

    if int(STATS.get("total_closed", 0)) == 1:
        STATS["best_trade_pct"] = pnl_pct
        STATS["worst_trade_pct"] = pnl_pct
    else:
        STATS["best_trade_pct"] = max(float(STATS.get("best_trade_pct", 0.0)), pnl_pct)
        STATS["worst_trade_pct"] = min(float(STATS.get("worst_trade_pct", 0.0)), pnl_pct)

    trade = {
        "symbol": symbol,
        "side": side,
        "entry": round(entry, 8),
        "exit": round(exit_price, 8),
        "amount": amount,
        "leverage": leverage,
        "pnl_pct": pnl_pct,
        "unrealized_pnl_usdt": round(unrealized, 4),
        "reason": reason,
        "closed_at": utc_now(),
    }
    STATS.setdefault("history", []).append(trade)
    save_stats()
    return trade


def close_position(symbol: str, reason: str = "manual") -> Tuple[bool, str]:
    symbol = normalize_symbol(symbol)
    positions = fetch_positions_safe([symbol])
    pos = next((p for p in positions if p["symbol"] == symbol), None)
    if not pos:
        return False, f"لا توجد صفقة مفتوحة على {symbol}"

    amount = safe_float(pos["contracts"], 0.0)
    if amount <= 0:
        return False, f"{symbol}: كمية الصفقة غير صالحة."

    close_side = "sell" if pos["side"] == "LONG" else "buy"
    exit_price_before = pos["mark"]

    try:
        order = create_market_order(symbol, close_side, amount, reduce_only=True)
    except Exception as exc:
        logger.exception("Close order failed for %s: %s", symbol, exc)
        return False, f"{symbol}: فشل إغلاق الصفقة: {exc}"

    exit_price = safe_float(order.get("average"), 0.0)
    if exit_price <= 0:
        try:
            exit_price = fetch_ticker_price(symbol)
        except Exception:
            exit_price = exit_price_before

    trade = record_closed_trade(
        symbol=symbol,
        side=pos["side"],
        entry=pos["entry"],
        exit_price=exit_price,
        leverage=pos["leverage"],
        reason=reason,
        amount=amount,
        unrealized=pos.get("unrealized", 0.0),
    )

    if symbol in OPEN_META:
        OPEN_META.pop(symbol, None)
        save_open_meta()

    text = (
        f"🔒 تم إغلاق الصفقة\n"
        f"{symbol}\n"
        f"Side: {pos['side']}\n"
        f"Entry: {pos['entry']:.8f}\n"
        f"Exit: {exit_price:.8f}\n"
        f"PnL: {trade['pnl_pct']:.2f}%\n"
        f"Reason: {reason}"
    )
    return True, text


def close_positions_by_filter(kind: str) -> str:
    positions = fetch_positions_safe(SYMBOLS)
    if not positions:
        return "لا توجد صفقات مفتوحة."

    results: List[str] = []
    for p in positions:
        should_close = False
        if kind == "all":
            should_close = True
        elif kind == "winning" and p["pnl_pct"] > 0:
            should_close = True
        elif kind == "losing" and p["pnl_pct"] < 0:
            should_close = True

        if not should_close:
            continue

        ok, msg = close_position(p["symbol"], reason=f"telegram_{kind}")
        results.append(msg)

    if not results:
        if kind == "winning":
            return "لا توجد صفقات رابحة حالياً."
        if kind == "losing":
            return "لا توجد صفقات خاسرة حالياً."
        return "لا توجد صفقات مطابقة."

    return "\n\n".join(results)


# =========================================================
# 8) POSITION MANAGEMENT: ATR SL + PSAR TRAIL
# =========================================================

def ensure_meta_for_position(pos: Dict[str, Any]) -> None:
    symbol = pos["symbol"]
    if symbol in OPEN_META:
        return

    df = fetch_ohlcv_df(symbol)
    atr = 0.0
    if df is not None and len(df):
        last, _ = get_last_closed_candles(df)
        atr = safe_float(last.get("atr"), 0.0)

    entry = pos["entry"]
    if atr <= 0:
        atr = entry * 0.01

    if pos["side"] == "LONG":
        sl = entry - ATR_SL_MULT * atr
    else:
        sl = entry + ATR_SL_MULT * atr

    OPEN_META[symbol] = {
        "symbol": symbol,
        "side": pos["side"],
        "entry": entry,
        "amount": pos["contracts"],
        "leverage": pos["leverage"],
        "risk_pct": STATE.get("risk_pct", NORMAL_RISK_PCT),
        "sl": sl,
        "atr_entry": atr,
        "opened_at": utc_now(),
        "mode": STATE.get("mode", "NORMAL"),
        "reconstructed": True,
    }
    save_open_meta()


def update_trailing_stop(pos: Dict[str, Any]) -> Tuple[bool, str]:
    symbol = pos["symbol"]
    ensure_meta_for_position(pos)

    meta = OPEN_META.get(symbol, {})
    old_sl = safe_float(meta.get("sl"), pos.get("sl", 0.0))

    df = fetch_ohlcv_df(symbol)
    if df is None or len(df) < 5:
        return False, f"{symbol}: no data for trailing."

    last, _ = get_last_closed_candles(df)
    close = safe_float(last["close"])
    atr = safe_float(last["atr"])
    psar = safe_float(last["psar"])
    mark = pos["mark"]

    side = pos["side"]
    new_sl = old_sl

    if side == "LONG":
        atr_sl = mark - ATR_TRAIL_MULT * atr
        candidates = [old_sl]
        if psar < mark:
            candidates.append(psar)
        if atr_sl < mark:
            candidates.append(atr_sl)
        new_sl = max(candidates)

        # Exit if market touches SL or PSAR flips bearish.
        if mark <= new_sl:
            return True, f"ATR/PSAR SL hit at {mark:.8f} <= {new_sl:.8f}"
        if psar > close:
            return True, "PSAR flipped bearish"

    else:
        atr_sl = mark + ATR_TRAIL_MULT * atr
        candidates = [old_sl if old_sl > 0 else pos["entry"] + ATR_SL_MULT * atr]
        if psar > mark:
            candidates.append(psar)
        if atr_sl > mark:
            candidates.append(atr_sl)
        new_sl = min(candidates)

        # Exit if market touches SL or PSAR flips bullish.
        if mark >= new_sl:
            return True, f"ATR/PSAR SL hit at {mark:.8f} >= {new_sl:.8f}"
        if psar < close:
            return True, "PSAR flipped bullish"

    if new_sl > 0 and abs(new_sl - old_sl) > 0:
        OPEN_META[symbol]["sl"] = float(new_sl)
        OPEN_META[symbol]["last_trail_update"] = utc_now()
        save_open_meta()

    return False, f"{symbol}: trailing ok. SL {old_sl:.8f} -> {new_sl:.8f}"


def cleanup_missing_positions(current_positions: List[Dict[str, Any]]) -> List[str]:
    current_symbols = {p["symbol"] for p in current_positions}
    removed: List[str] = []
    for symbol in list(OPEN_META.keys()):
        if symbol not in current_symbols:
            OPEN_META.pop(symbol, None)
            removed.append(symbol)
    if removed:
        save_open_meta()
    return removed


# =========================================================
# 9) TELEGRAM HELPERS + DASHBOARD
# =========================================================

DASHBOARD_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["الرصيد 💰", "الصفقات 📁", "الإحصائيات 📊"],
        ["الرادار 📡", "تشغيل ▶️", "إيقاف ⏸️"],
        ["Normal 3x 🧠", "Aggressive 5x 🔥"],
        ["إغلاق الكل 🛑", "إغلاق الرابحة ✅", "إغلاق الخاسرة ❌"],
        ["طوارئ 🚨"],
    ],
    resize_keyboard=True,
)


def authorized(update: Update) -> bool:
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) == str(ENV_CHAT_ID)


async def reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text, reply_markup=DASHBOARD_KEYBOARD)


async def send_to_owner(app_or_context: Any, text: str) -> None:
    try:
        bot = app_or_context.bot if hasattr(app_or_context, "bot") else app_or_context.application.bot
        await bot.send_message(chat_id=ENV_CHAT_ID, text=text)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def format_balance() -> str:
    bal = get_usdt_balance()
    return (
        "💰 الرصيد\n"
        f"Total: {bal['total']:.2f} USDT\n"
        f"Free: {bal['free']:.2f} USDT\n"
        f"Used: {bal['used']:.2f} USDT\n"
        f"Mode: {STATE.get('mode')} ({STATE.get('leverage')}x)\n"
        f"Risk: {STATE.get('risk_pct')}%\n"
        f"Bot: {'ON' if STATE.get('running') else 'OFF'}"
    )


def format_positions() -> str:
    positions = fetch_positions_safe(SYMBOLS)
    if not positions:
        return "📁 لا توجد صفقات مفتوحة حالياً."

    lines = ["📁 الصفقات المفتوحة:"]
    for p in positions:
        meta = OPEN_META.get(p["symbol"], {})
        sl = safe_float(meta.get("sl"), p.get("sl", 0.0))
        lines.append(
            "\n"
            f"{p['symbol']}\n"
            f"Side: {p['side']}\n"
            f"Entry: {p['entry']:.8f}\n"
            f"Now: {p['mark']:.8f}\n"
            f"SL: {sl:.8f}\n"
            f"Lev: {p['leverage']}x\n"
            f"PnL: {p['unrealized']:.2f} USDT ({p['pnl_pct']:.2f}%)"
        )
    return "\n".join(lines)


def format_stats() -> str:
    total = int(STATS.get("total_closed", 0))
    wins = int(STATS.get("wins", 0))
    losses = int(STATS.get("losses", 0))
    return (
        "📊 الإحصائيات الكاملة\n"
        f"منذ: {STATS.get('since')}\n"
        f"إجمالي الصفقات المغلقة: {total}\n"
        f"الرابحة: {wins}\n"
        f"الخاسرة: {losses}\n"
        f"نسبة النجاح: {float(STATS.get('win_rate', 0.0)):.2f}%\n"
        f"إجمالي PnL%: {float(STATS.get('total_pnl_pct', 0.0)):.2f}%\n"
        f"أفضل صفقة: {float(STATS.get('best_trade_pct', 0.0)):.2f}%\n"
        f"أسوأ صفقة: {float(STATS.get('worst_trade_pct', 0.0)):.2f}%\n"
        f"آخر تحديث: {STATS.get('updated_at')}"
    )


def format_radar() -> str:
    last_scan = STATE.get("last_scan", {})
    if not last_scan:
        return "📡 لا توجد قراءات بعد. انتظر أول دورة فحص."

    lines = ["📡 آخر فحص:"]
    for symbol in SYMBOLS:
        s = last_scan.get(symbol)
        if not s:
            lines.append(f"{symbol}: لم يتم فحصه بعد")
            continue
        lines.append(
            f"{symbol}: {s.get('action')} | {s.get('reason')} | "
            f"RSI {s.get('rsi')} | Price {s.get('close')}"
        )
    return "\n".join(lines)


# =========================================================
# 10) TELEGRAM COMMANDS
# =========================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(
        update,
        "✅ بوت BingX Futures شغال.\n"
        "استخدم الأزرار للتحكم.\n\n"
        "أوامر الإغلاق المحدد:\n"
        "/close BTC\n"
        "/close ETH\n"
        "/close SOL"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(update, format_radar())


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(update, format_balance())


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(update, format_positions())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await reply(update, format_stats())


def coin_to_symbol(coin: str) -> Optional[str]:
    coin = coin.strip().upper().replace("/", "")
    if not coin:
        return None

    for symbol in SYMBOLS:
        base = symbol.split("/")[0].upper()
        if coin == base or coin == symbol.upper():
            return symbol

    # Fallback for user commands like /close BTC even if SYMBOLS changed.
    candidate = f"{coin}/USDT:USDT"
    return normalize_symbol(candidate)


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return

    if not context.args:
        await reply(update, "اكتب العملة بعد الأمر، مثال: /close BTC")
        return

    symbol = coin_to_symbol(context.args[0])
    if not symbol:
        await reply(update, "لم أتعرف على العملة.")
        return

    ok, msg = close_position(symbol, reason="telegram_command")
    await reply(update, msg)


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    if not update.message:
        return

    text = (update.message.text or "").strip()

    try:
        if text == "الرصيد 💰":
            await reply(update, format_balance())

        elif text == "الصفقات 📁":
            await reply(update, format_positions())

        elif text == "الإحصائيات 📊":
            await reply(update, format_stats())

        elif text == "الرادار 📡":
            await reply(update, format_radar())

        elif text == "تشغيل ▶️":
            STATE["running"] = True
            STATE["emergency"] = False
            save_state()
            await reply(update, "▶️ تم تشغيل البوت.")

        elif text == "إيقاف ⏸️":
            STATE["running"] = False
            save_state()
            await reply(update, "⏸️ تم إيقاف فتح صفقات جديدة. إدارة الصفقات الحالية ما زالت تعمل.")

        elif text == "Normal 3x 🧠":
            STATE["mode"] = "NORMAL"
            STATE["leverage"] = NORMAL_LEVERAGE
            STATE["risk_pct"] = NORMAL_RISK_PCT
            save_state()
            for s in SYMBOLS:
                configure_symbol(s, NORMAL_LEVERAGE)
            await reply(update, f"🧠 تم تفعيل Normal {NORMAL_LEVERAGE}x | Risk {NORMAL_RISK_PCT}%")

        elif text == "Aggressive 5x 🔥":
            STATE["mode"] = "AGGRESSIVE"
            STATE["leverage"] = AGGRESSIVE_LEVERAGE
            STATE["risk_pct"] = AGGRESSIVE_RISK_PCT
            save_state()
            for s in SYMBOLS:
                configure_symbol(s, AGGRESSIVE_LEVERAGE)
            await reply(update, f"🔥 تم تفعيل Aggressive {AGGRESSIVE_LEVERAGE}x | Risk {AGGRESSIVE_RISK_PCT}%")

        elif text == "إغلاق الكل 🛑":
            await reply(update, close_positions_by_filter("all"))

        elif text == "إغلاق الرابحة ✅":
            await reply(update, close_positions_by_filter("winning"))

        elif text == "إغلاق الخاسرة ❌":
            await reply(update, close_positions_by_filter("losing"))

        elif text == "طوارئ 🚨":
            STATE["running"] = False
            STATE["emergency"] = True
            save_state()
            msg = close_positions_by_filter("all")
            await reply(update, "🚨 تم تفعيل الطوارئ وإيقاف البوت.\n\n" + msg)

        else:
            await reply(update, "استخدم الأزرار أو الأوامر المتاحة.")

    except Exception as exc:
        logger.exception("Dashboard error: %s", exc)
        await reply(update, f"حدث خطأ: {exc}")


# =========================================================
# 11) JOBS: SCAN + MANAGE OPEN POSITIONS
# =========================================================

async def trading_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if JOB_LOCK.locked():
        return

    async with JOB_LOCK:
        if STATE.get("emergency"):
            return
        if not STATE.get("running", True):
            return

        scan_results: Dict[str, Any] = {}

        for symbol in SYMBOLS:
            try:
                df = fetch_ohlcv_df(symbol)
                if df is None:
                    scan_results[symbol] = {
                        "symbol": symbol,
                        "action": "NO_DATA",
                        "reason": "not enough OHLCV data",
                        "checked_at": utc_now(),
                    }
                    continue

                signal = build_signal(symbol, df)
                scan_results[symbol] = signal

                if signal["action"] in ("LONG", "SHORT"):
                    ok, msg = open_position(symbol, signal)
                    if ok:
                        await send_to_owner(context, msg)
                    else:
                        logger.info("Signal not opened: %s", msg)

            except Exception as exc:
                logger.exception("trading_job symbol error %s: %s", symbol, exc)
                scan_results[symbol] = {
                    "symbol": symbol,
                    "action": "ERROR",
                    "reason": str(exc),
                    "checked_at": utc_now(),
                }

        STATE["last_scan"] = scan_results
        save_state()


async def manage_positions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE.get("emergency"):
        return

    try:
        positions = fetch_positions_safe(SYMBOLS)
        cleanup_missing_positions(positions)

        for pos in positions:
            try:
                should_close, reason = update_trailing_stop(pos)
                logger.info(reason)

                if should_close:
                    ok, msg = close_position(pos["symbol"], reason=reason)
                    await send_to_owner(context, msg)

            except Exception as exc:
                logger.exception("Position manage error %s: %s", pos.get("symbol"), exc)

    except Exception as exc:
        logger.exception("manage_positions_job failed: %s", exc)


async def post_init(app: Application) -> None:
    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. Install with: pip install 'python-telegram-bot[job-queue]'"
        )

    app.job_queue.run_repeating(
        trading_job,
        interval=SCAN_INTERVAL_SECONDS,
        first=10,
        name="trading_job",
    )
    app.job_queue.run_repeating(
        manage_positions_job,
        interval=POSITION_CHECK_SECONDS,
        first=15,
        name="manage_positions_job",
    )

    await app.bot.send_message(
        chat_id=ENV_CHAT_ID,
        text=(
            "✅ Bot started on Render\n"
            f"Symbols: {', '.join(SYMBOLS)}\n"
            f"Mode: {STATE.get('mode')} ({STATE.get('leverage')}x)\n"
            f"Data dir: {DATA_DIR}\n"
            f"Scan: {SCAN_INTERVAL_SECONDS}s | Manage: {POSITION_CHECK_SECONDS}s"
        ),
        reply_markup=DASHBOARD_KEYBOARD,
    )


# =========================================================
# 12) MAIN
# =========================================================

def main() -> None:
    start_health_server()
    init_exchange()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("wallet", wallet_cmd))
    application.add_handler(CommandHandler("positions", positions_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("close", close_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, dashboard_handler))

    logger.info("Starting Telegram polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
