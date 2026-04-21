import os
import asyncio
import time
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, List

import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# =========================================================
# 1) CONFIG
# =========================================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")
PORT = int(os.environ.get("PORT", "8080"))

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")
if not BINGX_API_KEY or not BINGX_SECRET:
    raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET")

# BingX Perpetual USDT-M
MARGIN_MODE = "isolated"
DEFAULT_LEVERAGE = 3

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "DOGE/USDT:USDT",
    "LINK/USDT:USDT",
    "AVAX/USDT:USDT",
    "LTC/USDT:USDT",
    "DOT/USDT:USDT",
    "TRX/USDT:USDT",
    "BCH/USDT:USDT",
    "ATOM/USDT:USDT",
    "NEAR/USDT:USDT",
]

TF_4H = "4h"
TF_1H = "1h"
TF_15M = "15m"

MAX_OPEN_POSITIONS = 3
COOLDOWN_MINUTES = 45

# Risk can be changed from Telegram
risk_per_trade = 0.006  # 0.6%

EMA_FILTER_PERIOD = 50
ATR_PERIOD = 14

SAR_AF = 0.02
SAR_MAX_AF = 0.2

TP1_R = 1.0
TP2_R = 2.0
TRAILING_ATR_MULTIPLIER = 1.2

SCAN_INTERVAL_SECONDS = 60
POSITION_CHECK_INTERVAL_SECONDS = 20

# Current strategy filters
RECENT_FLIP_MAX_BARS = 3
MAX_DISTANCE_FROM_EMA50 = 0.01  # 1%

# Early Entry Mode
EARLY_FLIP_MAX_BARS = 6
EARLY_MAX_DISTANCE_FROM_EMA50 = 0.02  # 2%

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("SAR_MTF_Bot")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    },
})

# =========================================================
# 2) STATE
# =========================================================
cooldowns: Dict[str, int] = {}
trade_state: Dict[str, Dict[str, Any]] = {}
last_scan_summary = "No scans yet."
last_signal_summary = "No signals yet."
bot_paused = False

daily_stats = {
    "date": datetime.now(timezone.utc).date().isoformat(),
    "closed_trades": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl": 0.0,
}

# =========================================================
# 3) HELPERS
# =========================================================
def now_ts() -> int:
    return int(time.time())


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def reset_daily_stats_if_needed() -> None:
    global daily_stats
    today = utc_today()
    if daily_stats["date"] != today:
        daily_stats = {
            "date": today,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
        }


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def format_num(v: float, digits: int = 6) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def get_active_chat_id(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Optional[str]:
    if context and context.bot_data.get("chat_id"):
        return str(context.bot_data["chat_id"])
    if ENV_CHAT_ID:
        return str(ENV_CHAT_ID)
    return None


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = get_active_chat_id(context)
    if not chat_id:
        logger.info("No active chat id yet; skipping notification")
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


def is_in_cooldown(symbol: str) -> bool:
    return now_ts() < cooldowns.get(symbol, 0)


def set_cooldown(symbol: str) -> None:
    cooldowns[symbol] = now_ts() + COOLDOWN_MINUTES * 60


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    val = sum(values[:period]) / period
    for x in values[period:]:
        val = x * k + val * (1 - k)
    return val


def atr_from_ohlcv(ohlcv: List[List[float]], period: int) -> Optional[float]:
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        prev_close = ohlcv[i - 1][4]
        curr_high = ohlcv[i][2]
        curr_low = ohlcv[i][3]
        tr = max(
            curr_high - curr_low,
            abs(curr_high - prev_close),
            abs(curr_low - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def normalize_amount(symbol: str, amount: float) -> float:
    try:
        return float(exchange.amount_to_precision(symbol, amount))
    except Exception:
        return amount


def normalize_price(symbol: str, price: float) -> float:
    try:
        return float(exchange.price_to_precision(symbol, price))
    except Exception:
        return price


def calc_psar(ohlcv: List[List[float]], af_start: float = SAR_AF, af_max: float = SAR_MAX_AF) -> Optional[List[float]]:
    if len(ohlcv) < 5:
        return None

    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]

    psar = [0.0] * len(ohlcv)

    bull = True
    if highs[1] + lows[1] < highs[0] + lows[0]:
        bull = False

    af = af_start
    ep = highs[0] if bull else lows[0]
    psar[0] = lows[0] if bull else highs[0]

    for i in range(1, len(ohlcv)):
        prev_psar = psar[i - 1]
        current_psar = prev_psar + af * (ep - prev_psar)

        if bull:
            if i >= 2:
                current_psar = min(current_psar, lows[i - 1], lows[i - 2])
            else:
                current_psar = min(current_psar, lows[i - 1])

            if lows[i] < current_psar:
                bull = False
                current_psar = ep
                ep = lows[i]
                af = af_start
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_start, af_max)
        else:
            if i >= 2:
                current_psar = max(current_psar, highs[i - 1], highs[i - 2])
            else:
                current_psar = max(current_psar, highs[i - 1])

            if highs[i] > current_psar:
                bull = True
                current_psar = ep
                ep = highs[i]
                af = af_start
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_start, af_max)

        psar[i] = current_psar

    return psar


def psar_side(ohlcv: List[List[float]]) -> Optional[str]:
    psar = calc_psar(ohlcv)
    if not psar:
        return None
    close = ohlcv[-1][4]
    return "BULL" if psar[-1] < close else "BEAR"


def psar_flip_signal(ohlcv: List[List[float]]) -> Optional[str]:
    psar = calc_psar(ohlcv)
    if not psar or len(psar) < 3:
        return None

    prev_close = ohlcv[-2][4]
    curr_close = ohlcv[-1][4]

    prev_bull = psar[-2] < prev_close
    curr_bull = psar[-1] < curr_close

    if (not prev_bull) and curr_bull:
        return "LONG"
    if prev_bull and (not curr_bull):
        return "SHORT"
    return None


def psar_flip_bars_ago(ohlcv: List[List[float]]) -> Optional[int]:
    psar = calc_psar(ohlcv)
    if not psar or len(psar) < 4:
        return None

    bull_states = []
    for i in range(len(ohlcv)):
        close_i = ohlcv[i][4]
        bull_states.append(psar[i] < close_i)

    for i in range(len(bull_states) - 1, 0, -1):
        if bull_states[i] != bull_states[i - 1]:
            return (len(bull_states) - 1) - i
    return None


def is_recent_bull_flip(ohlcv: List[List[float]], max_bars: int) -> bool:
    side = psar_side(ohlcv)
    bars_ago = psar_flip_bars_ago(ohlcv)
    return side == "BULL" and bars_ago is not None and bars_ago <= max_bars


def is_recent_bear_flip(ohlcv: List[List[float]], max_bars: int) -> bool:
    side = psar_side(ohlcv)
    bars_ago = psar_flip_bars_ago(ohlcv)
    return side == "BEAR" and bars_ago is not None and bars_ago <= max_bars


# =========================================================
# 4) EXCHANGE WRAPPERS
# =========================================================
def fetch_balance_usdt() -> float:
    try:
        bal = exchange.fetch_balance({"type": "swap"})
        if isinstance(bal.get("USDT"), dict):
            return safe_float(bal["USDT"].get("free", 0.0))
        if isinstance(bal.get("free"), dict):
            return safe_float(bal["free"].get("USDT", 0.0))
        return 0.0
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0


def fetch_positions() -> List[dict]:
    try:
        return exchange.fetch_positions(params={"type": "swap"})
    except Exception as e:
        logger.error(f"fetch_positions error: {e}")
        return []


def get_symbol_position(symbol: str) -> Optional[dict]:
    try:
        positions = exchange.fetch_positions([symbol], params={"type": "swap"})
        for p in positions:
            if safe_float(p.get("contracts", 0)) != 0:
                return p
        return None
    except Exception as e:
        logger.error(f"{symbol}: get_symbol_position error: {e}")
        return None


def count_open_positions() -> int:
    count = 0
    for pos in fetch_positions():
        if safe_float(pos.get("contracts", 0)) != 0:
            count += 1
    return count


def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[List[List[float]]]:
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        logger.error(f"{symbol} {timeframe} fetch_ohlcv error: {e}")
        return None


def set_leverage_and_margin(symbol: str, leverage: int) -> None:
    try:
        exchange.set_position_mode(False, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set position mode warning: {e}")

    try:
        exchange.set_margin_mode(MARGIN_MODE, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set margin mode warning: {e}")

    try:
        exchange.set_leverage(leverage, symbol, {"side": "BOTH"})
    except Exception as e:
        logger.warning(f"{symbol}: set leverage warning: {e}")


def get_order_params(reduce_only: bool = False) -> dict:
    params = {
        "positionSide": "BOTH",
    }
    if reduce_only:
        params["reduceOnly"] = True
    return params


# =========================================================
# 5) STRATEGY
# =========================================================
def get_market_snapshot(symbol: str) -> Optional[dict]:
    bars_4h = fetch_ohlcv_safe(symbol, TF_4H, 120)
    bars_1h = fetch_ohlcv_safe(symbol, TF_1H, 120)
    bars_15m = fetch_ohlcv_safe(symbol, TF_15M, 120)

    if not bars_4h or not bars_1h or not bars_15m:
        return None

    closes_15m = [b[4] for b in bars_15m]
    ema50_15m = ema(closes_15m, EMA_FILTER_PERIOD)
    atr_15m = atr_from_ohlcv(bars_15m, ATR_PERIOD)

    if ema50_15m is None or atr_15m is None:
        return None

    side_4h = psar_side(bars_4h)
    side_1h = psar_side(bars_1h)
    entry_flip = psar_flip_signal(bars_15m)

    recent_bull_4h = is_recent_bull_flip(bars_4h, RECENT_FLIP_MAX_BARS)
    recent_bull_1h = is_recent_bull_flip(bars_1h, RECENT_FLIP_MAX_BARS)
    recent_bear_4h = is_recent_bear_flip(bars_4h, RECENT_FLIP_MAX_BARS)
    recent_bear_1h = is_recent_bear_flip(bars_1h, RECENT_FLIP_MAX_BARS)

    early_bull_4h = is_recent_bull_flip(bars_4h, EARLY_FLIP_MAX_BARS)
    early_bull_1h = is_recent_bull_flip(bars_1h, EARLY_FLIP_MAX_BARS)
    early_bear_4h = is_recent_bear_flip(bars_4h, EARLY_FLIP_MAX_BARS)
    early_bear_1h = is_recent_bear_flip(bars_1h, EARLY_FLIP_MAX_BARS)

    close_15m = bars_15m[-1][4]
    above_ema = close_15m > ema50_15m
    below_ema = close_15m < ema50_15m
    distance_from_ema50 = abs(close_15m - ema50_15m) / ema50_15m if ema50_15m > 0 else 999

    # =========================
    # NORMAL ENTRY
    # =========================
    if recent_bull_4h and recent_bull_1h and entry_flip == "LONG" and above_ema and distance_from_ema50 <= MAX_DISTANCE_FROM_EMA50:
        return {
            "symbol": symbol,
            "signal": "LONG",
            "reason": "Recent 4H+1H bullish SAR start, 15m bullish SAR flip, near EMA50",
            "bars_15m": bars_15m,
            "close_15m": close_15m,
            "atr_15m": atr_15m,
        }

    if recent_bear_4h and recent_bear_1h and entry_flip == "SHORT" and below_ema and distance_from_ema50 <= MAX_DISTANCE_FROM_EMA50:
        return {
            "symbol": symbol,
            "signal": "SHORT",
            "reason": "Recent 4H+1H bearish SAR start, 15m bearish SAR flip, near EMA50",
            "bars_15m": bars_15m,
            "close_15m": close_15m,
            "atr_15m": atr_15m,
        }

    # =========================
    # EARLY ENTRY MODE
    # =========================
    if early_bull_4h and early_bull_1h and above_ema and distance_from_ema50 <= EARLY_MAX_DISTANCE_FROM_EMA50:
        return {
            "symbol": symbol,
            "signal": "EARLY_LONG",
            "reason": "Early bull trend detected on 4H+1H, waiting cleaner confirmation",
            "bars_15m": bars_15m,
            "close_15m": close_15m,
            "atr_15m": atr_15m,
        }

    if early_bear_4h and early_bear_1h and below_ema and distance_from_ema50 <= EARLY_MAX_DISTANCE_FROM_EMA50:
        return {
            "symbol": symbol,
            "signal": "EARLY_SHORT",
            "reason": "Early bear trend detected on 4H+1H, waiting cleaner confirmation",
            "bars_15m": bars_15m,
            "close_15m": close_15m,
            "atr_15m": atr_15m,
        }

    # =========================
    # WAIT STATES
    # =========================
    if side_4h == "BULL" and side_1h == "BULL":
        if not (recent_bull_4h and recent_bull_1h):
            return {
                "symbol": symbol,
                "signal": "WAIT",
                "reason": "Bull trend exists but not early enough on 4H/1H",
            }
        if distance_from_ema50 > MAX_DISTANCE_FROM_EMA50:
            return {
                "symbol": symbol,
                "signal": "WAIT",
                "reason": "Bull trend active but price is too far from EMA50",
            }
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "Early bull trend active, waiting 15m bullish SAR flip",
        }

    if side_4h == "BEAR" and side_1h == "BEAR":
        if not (recent_bear_4h and recent_bear_1h):
            return {
                "symbol": symbol,
                "signal": "WAIT",
                "reason": "Bear trend exists but not early enough on 4H/1H",
            }
        if distance_from_ema50 > MAX_DISTANCE_FROM_EMA50:
            return {
                "symbol": symbol,
                "signal": "WAIT",
                "reason": "Bear trend active but price is too far from EMA50",
            }
        return {
            "symbol": symbol,
            "signal": "WAIT",
            "reason": "Early bear trend active, waiting 15m bearish SAR flip",
        }

    return {
        "symbol": symbol,
        "signal": "NO_TRADE",
        "reason": "4H and 1H trend not aligned",
    }


def calculate_trade_plan(
    symbol: str,
    snapshot: dict,
    side: str,
    balance: float,
    risk_multiplier: float = 1.0,
) -> Optional[dict]:
    entry = snapshot["close_15m"]
    atr_15m = snapshot["atr_15m"]
    bars_15m = snapshot["bars_15m"]

    if side == "LONG":
        swing_stop = min([b[3] for b in bars_15m[-3:]])
        atr_stop = entry - atr_15m * 1.2
        stop_loss = min(swing_stop, atr_stop)
        risk_per_unit = entry - stop_loss
    else:
        swing_stop = max([b[2] for b in bars_15m[-3:]])
        atr_stop = entry + atr_15m * 1.2
        stop_loss = max(swing_stop, atr_stop)
        risk_per_unit = stop_loss - entry

    if risk_per_unit <= 0:
        return None

    effective_risk = risk_per_trade * risk_multiplier
    risk_amount = balance * effective_risk
    raw_amount = risk_amount / risk_per_unit
    amount = normalize_amount(symbol, raw_amount)

    if amount <= 0:
        return None

    one_r = risk_per_unit
    if side == "LONG":
        tp1 = entry + one_r * TP1_R
        tp2 = entry + one_r * TP2_R
    else:
        tp1 = entry - one_r * TP1_R
        tp2 = entry - one_r * TP2_R

    return {
        "entry": normalize_price(symbol, entry),
        "stop_loss": normalize_price(symbol, stop_loss),
        "tp1": normalize_price(symbol, tp1),
        "tp2": normalize_price(symbol, tp2),
        "amount": amount,
        "risk_amount": risk_amount,
        "risk_per_unit": risk_per_unit,
        "effective_risk": effective_risk,
    }


# =========================================================
# 6) ORDERS / POSITION MANAGEMENT
# =========================================================
def open_position(symbol: str, side: str, plan: dict) -> bool:
    order_side = "buy" if side == "LONG" else "sell"
    try:
        set_leverage_and_margin(symbol, DEFAULT_LEVERAGE)

        order = exchange.create_market_order(
            symbol,
            order_side,
            plan["amount"],
            params=get_order_params(reduce_only=False),
        )
        logger.info(f"{symbol}: opened {side} -> {order.get('id')}")

        trade_state[symbol] = {
            "symbol": symbol,
            "side": side,
            "entry": plan["entry"],
            "stop_loss": plan["stop_loss"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "tp1_taken": False,
            "tp2_taken": False,
            "trailing_active": False,
            "trailing_stop": None,
            "opened_at": now_ts(),
        }
        return True
    except Exception as e:
        logger.error(f"{symbol}: open position error: {e}")
        return False


def close_position(symbol: str, position: dict, portion: float = 1.0) -> bool:
    try:
        contracts = safe_float(position.get("contracts", 0))
        if contracts <= 0:
            return False

        amount = normalize_amount(symbol, contracts * portion)
        if amount <= 0:
            return False

        side = str(position.get("side", "")).lower()
        close_side = "sell" if side == "long" else "buy"

        exchange.create_market_order(
            symbol,
            close_side,
            amount,
            params=get_order_params(reduce_only=True),
        )
        return True
    except Exception as e:
        logger.error(f"{symbol}: close position error: {e}")
        return False


def record_closed_trade(entry_price: float, exit_price: float, side: str) -> None:
    reset_daily_stats_if_needed()

    pnl_pct = 0.0
    if entry_price > 0:
        if side == "LONG":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100

    daily_stats["closed_trades"] += 1
    daily_stats["realized_pnl"] += pnl_pct
    if pnl_pct >= 0:
        daily_stats["wins"] += 1
    else:
        daily_stats["losses"] += 1


async def notify_close(context: ContextTypes.DEFAULT_TYPE, symbol: str, side: str, reason: str, entry: float, exit_price: float) -> None:
    pnl_pct = 0.0
    if entry > 0:
        if side == "LONG":
            pnl_pct = ((exit_price - entry) / entry) * 100
        else:
            pnl_pct = ((entry - exit_price) / entry) * 100

    await notify(
        context,
        (
            f"📌 Position Closed\n"
            f"Symbol: {symbol}\n"
            f"Side: {side}\n"
            f"Reason: {reason}\n"
            f"Entry: {format_num(entry, 6)}\n"
            f"Exit: {format_num(exit_price, 6)}\n"
            f"PnL%: {format_num(pnl_pct, 2)}%"
        )
    )


async def manage_open_positions(context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_positions()
    if not positions:
        return

    for pos in positions:
        contracts = safe_float(pos.get("contracts", 0))
        if contracts == 0:
            continue

        symbol = pos["symbol"]
        entry_price = safe_float(pos.get("entryPrice", 0))
        mark_price = safe_float(pos.get("markPrice", 0))
        side_raw = str(pos.get("side", "")).lower()

        if entry_price <= 0 or mark_price <= 0:
            continue

        side = "LONG" if side_raw == "long" else "SHORT"

        state = trade_state.get(symbol)
        if not state:
            trade_state[symbol] = {
                "symbol": symbol,
                "side": side,
                "entry": entry_price,
                "stop_loss": entry_price * (0.99 if side == "LONG" else 1.01),
                "tp1": entry_price * (1.01 if side == "LONG" else 0.99),
                "tp2": entry_price * (1.02 if side == "LONG" else 0.98),
                "tp1_taken": False,
                "tp2_taken": False,
                "trailing_active": False,
                "trailing_stop": None,
                "opened_at": now_ts(),
            }
            state = trade_state[symbol]

        bars_15m = fetch_ohlcv_safe(symbol, TF_15M, 120)
        if not bars_15m:
            continue
        atr_15m = atr_from_ohlcv(bars_15m, ATR_PERIOD)
        if atr_15m is None:
            continue

        if side == "LONG":
            if mark_price <= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(entry_price, mark_price, side)
                    await notify_close(context, symbol, side, "Stop Loss", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price >= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        f"✅ TP1 LONG\n{symbol}\nClosed 50%\nSL moved to breakeven"
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price >= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        f"🚀 TP2 LONG\n{symbol}\nTrailing stop activated"
                    )
                    continue

            if state["trailing_active"]:
                new_trailing = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                if state["trailing_stop"] is None:
                    state["trailing_stop"] = new_trailing
                else:
                    state["trailing_stop"] = max(state["trailing_stop"], new_trailing)

                if mark_price <= state["trailing_stop"]:
                    refreshed = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed, 1.0):
                        record_closed_trade(entry_price, mark_price, side)
                        await notify_close(context, symbol, side, "Trailing Stop", entry_price, mark_price)
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue

        else:
            if mark_price >= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(entry_price, mark_price, side)
                    await notify_close(context, symbol, side, "Stop Loss", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price <= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        f"✅ TP1 SHORT\n{symbol}\nClosed 50%\nSL moved to breakeven"
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price <= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        f"🚀 TP2 SHORT\n{symbol}\nTrailing stop activated"
                    )
                    continue

            if state["trailing_active"]:
                new_trailing = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                if state["trailing_stop"] is None:
                    state["trailing_stop"] = new_trailing
                else:
                    state["trailing_stop"] = min(state["trailing_stop"], new_trailing)

                if mark_price >= state["trailing_stop"]:
                    refreshed = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed, 1.0):
                        record_closed_trade(entry_price, mark_price, side)
                        await notify_close(context, symbol, side, "Trailing Stop", entry_price, mark_price)
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue


# =========================================================
# 7) SCAN JOB
# =========================================================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    global last_scan_summary, last_signal_summary, bot_paused

    reset_daily_stats_if_needed()

    if bot_paused:
        last_scan_summary = "Bot is paused."
        return

    await manage_open_positions(context)

    if count_open_positions() >= MAX_OPEN_POSITIONS:
        last_scan_summary = "Max open positions reached."
        logger.info(last_scan_summary)
        return

    scan_lines = []

    for symbol in SYMBOLS:
        try:
            if is_in_cooldown(symbol):
                scan_lines.append(f"{symbol}: COOLDOWN")
                continue

            if get_symbol_position(symbol):
                scan_lines.append(f"{symbol}: ALREADY_OPEN")
                continue

            snapshot = get_market_snapshot(symbol)
            if not snapshot:
                scan_lines.append(f"{symbol}: NO_DATA")
                continue

            signal = snapshot["signal"]
            reason = snapshot["reason"]

            scan_lines.append(f"{symbol}: {signal} | {reason}")
            last_signal_summary = f"{symbol}: {signal} | {reason}"

            entry_side = None
            risk_multiplier = 1.0

            if signal == "LONG":
                entry_side = "LONG"
                risk_multiplier = 1.0
            elif signal == "SHORT":
                entry_side = "SHORT"
                risk_multiplier = 1.0
            elif signal == "EARLY_LONG":
                entry_side = "LONG"
                risk_multiplier = 0.5
            elif signal == "EARLY_SHORT":
                entry_side = "SHORT"
                risk_multiplier = 0.5
            else:
                continue

            balance = fetch_balance_usdt()
            if balance <= 5:
                scan_lines.append(f"{symbol}: LOW_BALANCE")
                continue

            plan = calculate_trade_plan(symbol, snapshot, entry_side, balance, risk_multiplier=risk_multiplier)
            if not plan:
                scan_lines.append(f"{symbol}: PLAN_REJECTED")
                continue

            if open_position(symbol, entry_side, plan):
                await notify(
                    context,
                    (
                        f"🚀 Trade Opened\n"
                        f"Symbol: {symbol}\n"
                        f"Side: {signal}\n"
                        f"Execution Side: {entry_side}\n"
                        f"Reason: {reason}\n"
                        f"Entry: {format_num(plan['entry'], 6)}\n"
                        f"SL: {format_num(plan['stop_loss'], 6)}\n"
                        f"TP1: {format_num(plan['tp1'], 6)}\n"
                        f"TP2: {format_num(plan['tp2'], 6)}\n"
                        f"Amount: {plan['amount']}\n"
                        f"Risk: {format_num(plan['effective_risk'] * 100, 2)}%"
                    )
                )
                scan_lines.append(f"{symbol}: OPENED {signal}")
                if count_open_positions() >= MAX_OPEN_POSITIONS:
                    break
            else:
                scan_lines.append(f"{symbol}: OPEN_FAILED")

        except Exception as e:
            logger.error(f"{symbol}: trading_job error: {e}")
            scan_lines.append(f"{symbol}: ERROR")

    last_scan_summary = "\n".join(scan_lines[-12:]) if scan_lines else "No scan results."
    logger.info(f"Scan summary:\n{last_scan_summary}")


# =========================================================
# 8) TELEGRAM DASHBOARD
# =========================================================
def dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 الرصيد", callback_data="dash_balance"),
            InlineKeyboardButton("📡 الرادار", callback_data="dash_radar"),
        ],
        [
            InlineKeyboardButton("📂 الصفقات", callback_data="dash_positions"),
            InlineKeyboardButton("📈 الإحصائيات", callback_data="dash_stats"),
        ],
        [
            InlineKeyboardButton("🔎 فحص يدوي", callback_data="dash_scan"),
            InlineKeyboardButton("⚙️ المخاطرة", callback_data="dash_risk_menu"),
        ],
        [
            InlineKeyboardButton("⏸ إيقاف", callback_data="dash_pause"),
            InlineKeyboardButton("▶️ تشغيل", callback_data="dash_resume"),
        ],
        [
            InlineKeyboardButton("🛑 إغلاق الكل", callback_data="dash_close_all"),
            InlineKeyboardButton("✅ إغلاق الرابحة", callback_data="dash_close_winners"),
        ],
        [
            InlineKeyboardButton("❌ إغلاق الخاسرة", callback_data="dash_close_losers"),
        ],
    ])


def risk_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0.3%", callback_data="risk_0.003"),
            InlineKeyboardButton("0.5%", callback_data="risk_0.005"),
        ],
        [
            InlineKeyboardButton("0.6%", callback_data="risk_0.006"),
            InlineKeyboardButton("0.75%", callback_data="risk_0.0075"),
        ],
        [
            InlineKeyboardButton("1.0%", callback_data="risk_0.01"),
            InlineKeyboardButton("⬅️ رجوع", callback_data="dash_home"),
        ],
    ])


async def show_positions(message_target, positions: List[dict]):
    lines = []
    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue
        lines.append(
            f"{p['symbol']} | {p.get('side')} | "
            f"Entry: {format_num(safe_float(p.get('entryPrice', 0)), 6)} | "
            f"Mark: {format_num(safe_float(p.get('markPrice', 0)), 6)} | "
            f"UPnL: {format_num(safe_float(p.get('unrealizedPnl', 0)), 4)}"
        )
    if not lines:
        await message_target.reply_text("لا توجد صفقات مفتوحة.")
    else:
        await message_target.reply_text("📂 الصفقات المفتوحة:\n" + "\n".join(lines[:20]))


async def close_by_pnl(update_or_message, mode: str):
    positions = fetch_positions()
    count = 0

    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue

        upnl = safe_float(p.get("unrealizedPnl", 0))
        symbol = p["symbol"]

        should_close = (
            (mode == "all") or
            (mode == "winners" and upnl > 0) or
            (mode == "losers" and upnl < 0)
        )

        if not should_close:
            continue

        if close_position(symbol, p, 1.0):
            trade_state.pop(symbol, None)
            set_cooldown(symbol)
            count += 1

    if mode == "all":
        msg = f"🛑 تم طلب إغلاق {count} صفقة."
    elif mode == "winners":
        msg = f"✅ تم طلب إغلاق {count} صفقة رابحة."
    else:
        msg = f"❌ تم طلب إغلاق {count} صفقة خاسرة."

    await update_or_message.reply_text(msg)


# =========================================================
# 9) COMMANDS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        context.bot_data["chat_id"] = str(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 SAR Multi-Timeframe Bot جاهز.\nاستخدم لوحة التحكم:",
        reply_markup=dashboard_kb()
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused = "نعم" if bot_paused else "لا"
    bal = fetch_balance_usdt()
    await update.message.reply_text(
        f"📊 الحالة\n"
        f"متوقف: {paused}\n"
        f"الرصيد: {bal:.2f} USDT\n"
        f"الصفقات المفتوحة: {count_open_positions()}/{MAX_OPEN_POSITIONS}\n"
        f"المخاطرة الحالية: {risk_per_trade * 100:.2f}%\n"
        f"آخر إشارة: {last_signal_summary}\n\n"
        f"آخر فحص:\n{last_scan_summary}",
        reply_markup=dashboard_kb()
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_balance_usdt()
    await update.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_positions(update.message, fetch_positions())


async def cmd_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📡 آخر فحص:\n{last_scan_summary}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ تم إيقاف البوت.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ تم تشغيل البوت.")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 جاري الفحص اليدوي...")
    await trading_job(context)
    await update.message.reply_text(f"تم.\n\n{last_scan_summary}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_daily_stats_if_needed()
    win_rate = 0.0
    if daily_stats["closed_trades"] > 0:
        win_rate = (daily_stats["wins"] / daily_stats["closed_trades"]) * 100

    await update.message.reply_text(
        f"📈 إحصائيات اليوم\n"
        f"التاريخ: {daily_stats['date']}\n"
        f"الصفقات المغلقة: {daily_stats['closed_trades']}\n"
        f"الرابحة: {daily_stats['wins']}\n"
        f"الخاسرة: {daily_stats['losses']}\n"
        f"نسبة النجاح: {win_rate:.2f}%\n"
        f"إجمالي PnL%: {daily_stats['realized_pnl']:.2f}%"
    )


async def cmd_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await close_by_pnl(update.message, "all")


# =========================================================
# 10) CALLBACKS
# =========================================================
async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused, risk_per_trade

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "dash_home":
        await query.message.reply_text("🎛 لوحة التحكم:", reply_markup=dashboard_kb())

    elif data == "dash_balance":
        bal = fetch_balance_usdt()
        await query.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT")

    elif data == "dash_radar":
        await query.message.reply_text(f"📡 آخر فحص:\n{last_scan_summary}")

    elif data == "dash_positions":
        await show_positions(query.message, fetch_positions())

    elif data == "dash_stats":
        reset_daily_stats_if_needed()
        win_rate = 0.0
        if daily_stats["closed_trades"] > 0:
            win_rate = (daily_stats["wins"] / daily_stats["closed_trades"]) * 100
        await query.message.reply_text(
            f"📈 إحصائيات اليوم\n"
            f"التاريخ: {daily_stats['date']}\n"
            f"الصفقات المغلقة: {daily_stats['closed_trades']}\n"
            f"الرابحة: {daily_stats['wins']}\n"
            f"الخاسرة: {daily_stats['losses']}\n"
            f"نسبة النجاح: {win_rate:.2f}%\n"
            f"إجمالي PnL%: {daily_stats['realized_pnl']:.2f}%"
        )

    elif data == "dash_scan":
        await query.message.reply_text("🔎 جاري الفحص اليدوي...")
        await trading_job(context)
        await query.message.reply_text(f"تم.\n\n{last_scan_summary}")

    elif data == "dash_pause":
        bot_paused = True
        await query.message.reply_text("⏸ تم إيقاف البوت.")

    elif data == "dash_resume":
        bot_paused = False
        await query.message.reply_text("▶️ تم تشغيل البوت.")

    elif data == "dash_risk_menu":
        await query.message.reply_text(
            f"⚙️ اختر المخاطرة الحالية\nالحالية: {risk_per_trade * 100:.2f}%",
            reply_markup=risk_kb()
        )

    elif data.startswith("risk_"):
        try:
            risk_per_trade = float(data.split("_", 1)[1])
            await query.message.reply_text(
                f"✅ تم تغيير المخاطرة إلى {risk_per_trade * 100:.2f}%"
            )
        except Exception:
            await query.message.reply_text("❌ فشل تغيير المخاطرة.")

    elif data == "dash_close_all":
        await close_by_pnl(query.message, "all")

    elif data == "dash_close_winners":
        await close_by_pnl(query.message, "winners")

    elif data == "dash_close_losers":
        await close_by_pnl(query.message, "losers")


# =========================================================
# 11) HEALTH
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SAR_MULTI_TIMEFRAME_BOT_LIVE")


# =========================================================
# 12) MAIN
# =========================================================
def main():
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(),
        daemon=True,
    ).start()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(TOKEN).build()

    if app.job_queue is None:
        raise RuntimeError('JobQueue unavailable. Install "python-telegram-bot[job-queue]".')

    if ENV_CHAT_ID:
        app.bot_data["chat_id"] = str(ENV_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("radar", cmd_radar))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("closeall", cmd_close_all))
    app.add_handler(CallbackQueryHandler(dashboard_handler))

    app.job_queue.run_repeating(trading_job, interval=SCAN_INTERVAL_SECONDS, first=10)
    app.job_queue.run_repeating(manage_open_positions, interval=POSITION_CHECK_INTERVAL_SECONDS, first=15)

    logger.info("Starting SAR Multi-Timeframe Bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
