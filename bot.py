import os
import asyncio
import logging
import threading
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

PORT = int(os.environ.get("PORT", 8080))

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
]

TREND_TIMEFRAME = "1h"
ENTRY_TIMEFRAME = "15m"

# Strategy
EMA_FAST = 20
EMA_MED = 50
EMA_SLOW = 200
ATR_PERIOD = 14
VOL_MA_PERIOD = 20

MIN_TREND_STRENGTH = 0.0025   # 0.25%
MIN_VOLUME_FACTOR = 1.2
MIN_RR_FOR_ENTRY = 1.5

RISK_PER_TRADE = 0.0075       # 0.75%
LEVERAGE = 3
MAX_OPEN_POSITIONS = 1
COOLDOWN_MINUTES = 60

TP1_R = 1.0
TP2_R = 2.0
TRAILING_ATR_MULTIPLIER = 1.2

# Scan / control
SCAN_INTERVAL_SECONDS = 60
POSITION_CHECK_INTERVAL_SECONDS = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("TrendPullbackV2")

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")
if not BINGX_API_KEY or not BINGX_SECRET:
    raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# =========================================================
# 2) RUNTIME STATE
# =========================================================
cooldowns: Dict[str, int] = {}
trade_state: Dict[str, Dict[str, Any]] = {}
last_scan_summary = "No scans yet."
last_signal_summary = "No signals yet."
bot_paused = False


# =========================================================
# 3) UTILS
# =========================================================
def now_ts() -> int:
    import time
    return int(time.time())


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def get_active_chat_id(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Optional[str]:
    if context and context.bot_data.get("chat_id"):
        return str(context.bot_data["chat_id"])
    if ENV_CHAT_ID:
        return str(ENV_CHAT_ID)
    return None


def is_in_cooldown(symbol: str) -> bool:
    return now_ts() < cooldowns.get(symbol, 0)


def set_cooldown(symbol: str):
    cooldowns[symbol] = now_ts() + COOLDOWN_MINUTES * 60


def format_num(v: float, digits: int = 6) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    val = sum(values[:period]) / period
    for x in values[period:]:
        val = x * k + val * (1 - k)
    return val


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


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


def bullish_engulfing(prev_candle: List[float], curr_candle: List[float]) -> bool:
    prev_open, prev_close = prev_candle[1], prev_candle[4]
    curr_open, curr_close = curr_candle[1], curr_candle[4]
    return (
        prev_close < prev_open and
        curr_close > curr_open and
        curr_close > prev_open and
        curr_open <= prev_close
    )


def bearish_engulfing(prev_candle: List[float], curr_candle: List[float]) -> bool:
    prev_open, prev_close = prev_candle[1], prev_candle[4]
    curr_open, curr_close = curr_candle[1], curr_candle[4]
    return (
        prev_close > prev_open and
        curr_close < curr_open and
        curr_close < prev_open and
        curr_open >= prev_close
    )


def explosive_candles(candles: List[List[float]]) -> bool:
    for c in candles:
        o = safe_float(c[1], 0)
        cl = safe_float(c[4], 0)
        if o <= 0:
            continue
        body_pct = abs(cl - o) / o
        if body_pct > 0.01:
            return True
    return False


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


# =========================================================
# 4) EXCHANGE WRAPPERS
# =========================================================
def fetch_balance_usdt() -> float:
    try:
        bal = exchange.fetch_balance()
        return safe_float(bal["USDT"]["free"])
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0


def fetch_positions() -> List[dict]:
    try:
        return exchange.fetch_positions()
    except Exception as e:
        logger.error(f"fetch_positions error: {e}")
        return []


def get_symbol_position(symbol: str) -> Optional[dict]:
    try:
        positions = exchange.fetch_positions([symbol])
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


# =========================================================
# 5) STRATEGY
# =========================================================
def get_market_snapshot(symbol: str) -> Optional[dict]:
    trend_bars = fetch_ohlcv_safe(symbol, TREND_TIMEFRAME, 260)
    entry_bars = fetch_ohlcv_safe(symbol, ENTRY_TIMEFRAME, 120)

    if not trend_bars or not entry_bars:
        return None
    if len(trend_bars) < 220 or len(entry_bars) < 60:
        return None

    trend_closes = [b[4] for b in trend_bars]
    entry_closes = [b[4] for b in entry_bars]
    entry_volumes = [b[5] for b in entry_bars]

    ema20_1h = ema(trend_closes, EMA_FAST)
    ema50_1h = ema(trend_closes, EMA_MED)
    ema200_1h = ema(trend_closes, EMA_SLOW)

    ema20_15m = ema(entry_closes, EMA_FAST)
    ema50_15m = ema(entry_closes, EMA_MED)

    atr_15m = atr_from_ohlcv(entry_bars, ATR_PERIOD)
    vol_ma_15m = sma(entry_volumes, VOL_MA_PERIOD)

    if None in (ema20_1h, ema50_1h, ema200_1h, ema20_15m, ema50_15m, atr_15m, vol_ma_15m):
        return None

    current = entry_bars[-1]
    prev = entry_bars[-2]
    last3 = entry_bars[-3:]

    close_1h = trend_bars[-1][4]
    close_15m = current[4]
    high_15m = current[2]
    low_15m = current[3]
    volume_15m = current[5]

    prev_swing_high = max([b[2] for b in entry_bars[-6:-1]])
    prev_swing_low = min([b[3] for b in entry_bars[-6:-1]])

    uptrend = (
        close_1h > ema200_1h and
        ema50_1h > ema200_1h and
        ema20_1h > ema50_1h and
        ((ema50_1h - ema200_1h) / ema200_1h) >= MIN_TREND_STRENGTH
    )

    downtrend = (
        close_1h < ema200_1h and
        ema50_1h < ema200_1h and
        ema20_1h < ema50_1h and
        ((ema200_1h - ema50_1h) / ema200_1h) >= MIN_TREND_STRENGTH
    )

    long_pullback = (low_15m <= ema20_15m or low_15m <= ema50_15m)
    short_pullback = (high_15m >= ema20_15m or high_15m >= ema50_15m)

    pullback_too_deep_long = close_15m < ema50_15m and low_15m < prev_swing_low
    pullback_too_deep_short = close_15m > ema50_15m and high_15m > prev_swing_high

    long_confirmation = (
        (bullish_engulfing(prev, current) or close_15m > prev[2]) and
        volume_15m > vol_ma_15m * MIN_VOLUME_FACTOR
    )

    short_confirmation = (
        (bearish_engulfing(prev, current) or close_15m < prev[3]) and
        volume_15m > vol_ma_15m * MIN_VOLUME_FACTOR
    )

    block_trade = any([
        explosive_candles(last3),
        (atr_15m / close_15m) > 0.02,
        volume_15m <= 0,
    ])

    return {
        "symbol": symbol,
        "close_1h": close_1h,
        "close_15m": close_15m,
        "ema20_1h": ema20_1h,
        "ema50_1h": ema50_1h,
        "ema200_1h": ema200_1h,
        "ema20_15m": ema20_15m,
        "ema50_15m": ema50_15m,
        "atr_15m": atr_15m,
        "vol_ma_15m": vol_ma_15m,
        "volume_15m": volume_15m,
        "entry_bars": entry_bars,
        "uptrend": uptrend,
        "downtrend": downtrend,
        "long_pullback": long_pullback,
        "short_pullback": short_pullback,
        "pullback_too_deep_long": pullback_too_deep_long,
        "pullback_too_deep_short": pullback_too_deep_short,
        "long_confirmation": long_confirmation,
        "short_confirmation": short_confirmation,
        "block_trade": block_trade,
    }


def generate_signal(snapshot: dict) -> Dict[str, str]:
    if snapshot["block_trade"]:
        return {"signal": "NO_TRADE", "reason": "Blocked by volatility filter"}

    if snapshot["uptrend"]:
        if (
            snapshot["long_pullback"] and
            not snapshot["pullback_too_deep_long"] and
            snapshot["long_confirmation"]
        ):
            return {"signal": "LONG", "reason": "Trend pullback long"}
        return {"signal": "WAIT", "reason": "Uptrend but no clean long setup"}

    if snapshot["downtrend"]:
        if (
            snapshot["short_pullback"] and
            not snapshot["pullback_too_deep_short"] and
            snapshot["short_confirmation"]
        ):
            return {"signal": "SHORT", "reason": "Trend pullback short"}
        return {"signal": "WAIT", "reason": "Downtrend but no clean short setup"}

    return {"signal": "NO_TRADE", "reason": "No valid trend"}


def calculate_trade_plan(symbol: str, snapshot: dict, side: str, balance: float) -> Optional[dict]:
    entry = snapshot["close_15m"]
    atr_15m = snapshot["atr_15m"]
    entry_bars = snapshot["entry_bars"]

    if side == "LONG":
        swing_stop = min([b[3] for b in entry_bars[-3:]])
        atr_stop = entry - atr_15m * 1.2
        stop_loss = min(swing_stop, atr_stop)
        risk_per_unit = entry - stop_loss
    else:
        swing_stop = max([b[2] for b in entry_bars[-3:]])
        atr_stop = entry + atr_15m * 1.2
        stop_loss = max(swing_stop, atr_stop)
        risk_per_unit = stop_loss - entry

    if risk_per_unit <= 0:
        return None

    risk_amount = balance * RISK_PER_TRADE
    raw_amount = risk_amount / risk_per_unit
    amount = normalize_amount(symbol, raw_amount)

    if amount <= 0:
        return None

    one_r = risk_per_unit
    if side == "LONG":
        tp1 = entry + one_r * TP1_R
        tp2 = entry + one_r * TP2_R
        rr = (tp1 - entry) / (entry - stop_loss)
    else:
        tp1 = entry - one_r * TP1_R
        tp2 = entry - one_r * TP2_R
        rr = (entry - tp1) / (stop_loss - entry)

    if rr < MIN_RR_FOR_ENTRY:
        return None

    return {
        "entry": normalize_price(symbol, entry),
        "stop_loss": normalize_price(symbol, stop_loss),
        "tp1": normalize_price(symbol, tp1),
        "tp2": normalize_price(symbol, tp2),
        "amount": amount,
        "risk_amount": risk_amount,
        "risk_per_unit": risk_per_unit,
    }


# =========================================================
# 6) ORDERS / POSITION MGMT
# =========================================================
def set_leverage_safe(symbol: str):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set leverage warning: {e}")


def open_position(symbol: str, side: str, plan: dict) -> bool:
    order_side = "buy" if side == "LONG" else "sell"
    try:
        set_leverage_safe(symbol)
        order = exchange.create_market_order(symbol, order_side, plan["amount"])
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
            params={"reduceOnly": True}
        )
        return True
    except Exception as e:
        logger.error(f"{symbol}: close position error: {e}")
        return False


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = get_active_chat_id(context)
    if not chat_id:
        logger.info("No active chat id yet; skipping notification")
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


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
        side = str(pos.get("side", "")).lower()

        if entry_price <= 0 or mark_price <= 0:
            continue

        state = trade_state.get(symbol)
        if not state:
            trade_state[symbol] = {
                "symbol": symbol,
                "side": "LONG" if side == "long" else "SHORT",
                "entry": entry_price,
                "stop_loss": entry_price * (0.99 if side == "long" else 1.01),
                "tp1": entry_price * (1.01 if side == "long" else 0.99),
                "tp2": entry_price * (1.02 if side == "long" else 0.98),
                "tp1_taken": False,
                "tp2_taken": False,
                "trailing_active": False,
                "trailing_stop": None,
                "opened_at": now_ts(),
            }
            state = trade_state[symbol]

        snapshot = get_market_snapshot(symbol)
        if not snapshot:
            continue
        atr_15m = snapshot["atr_15m"]

        if side == "long":
            if mark_price <= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    await notify(
                        context,
                        (
                            f"🛑 LONG Closed\n"
                            f"{symbol}\n"
                            f"Reason: Stop Loss\n"
                            f"Entry: {format_num(entry_price, 6)}\n"
                            f"Exit: {format_num(mark_price, 6)}"
                        )
                    )
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if not state["tp1_taken"] and mark_price >= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        (
                            f"✅ TP1 LONG\n"
                            f"{symbol}\n"
                            f"Closed 50%\n"
                            f"SL moved to breakeven"
                        )
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos

            if not state["tp2_taken"] and mark_price >= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        (
                            f"🚀 TP2 LONG\n"
                            f"{symbol}\n"
                            f"Closed more size\n"
                            f"Trailing stop activated"
                        )
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
                        await notify(
                            context,
                            (
                                f"📉 LONG Trailing Exit\n"
                                f"{symbol}\n"
                                f"Exit: {format_num(mark_price, 6)}"
                            )
                        )
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue

        elif side == "short":
            if mark_price >= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    await notify(
                        context,
                        (
                            f"🛑 SHORT Closed\n"
                            f"{symbol}\n"
                            f"Reason: Stop Loss\n"
                            f"Entry: {format_num(entry_price, 6)}\n"
                            f"Exit: {format_num(mark_price, 6)}"
                        )
                    )
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if not state["tp1_taken"] and mark_price <= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        (
                            f"✅ TP1 SHORT\n"
                            f"{symbol}\n"
                            f"Closed 50%\n"
                            f"SL moved to breakeven"
                        )
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos

            if not state["tp2_taken"] and mark_price <= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        (
                            f"🚀 TP2 SHORT\n"
                            f"{symbol}\n"
                            f"Closed more size\n"
                            f"Trailing stop activated"
                        )
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
                        await notify(
                            context,
                            (
                                f"📈 SHORT Trailing Exit\n"
                                f"{symbol}\n"
                                f"Exit: {format_num(mark_price, 6)}"
                            )
                        )
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue


# =========================================================
# 7) SCAN JOB
# =========================================================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    global last_scan_summary, last_signal_summary, bot_paused

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
                scan_lines.append(f"{symbol}: cooldown")
                continue

            if get_symbol_position(symbol):
                scan_lines.append(f"{symbol}: already open")
                continue

            snapshot = get_market_snapshot(symbol)
            if not snapshot:
                scan_lines.append(f"{symbol}: no data")
                continue

            signal_info = generate_signal(snapshot)
            signal = signal_info["signal"]
            reason = signal_info["reason"]

            scan_lines.append(f"{symbol}: {signal} | {reason}")
            last_signal_summary = f"{symbol}: {signal} | {reason}"

            if signal not in ("LONG", "SHORT"):
                continue

            balance = fetch_balance_usdt()
            if balance <= 5:
                scan_lines.append(f"{symbol}: low balance")
                continue

            plan = calculate_trade_plan(symbol, snapshot, signal, balance)
            if not plan:
                scan_lines.append(f"{symbol}: rejected by plan (RR/amount/stop)")
                continue

            if open_position(symbol, signal, plan):
                await notify(
                    context,
                    (
                        f"🚀 Trade Opened\n"
                        f"Symbol: {symbol}\n"
                        f"Side: {signal}\n"
                        f"Reason: {reason}\n"
                        f"Entry: {format_num(plan['entry'], 6)}\n"
                        f"SL: {format_num(plan['stop_loss'], 6)}\n"
                        f"TP1: {format_num(plan['tp1'], 6)}\n"
                        f"TP2: {format_num(plan['tp2'], 6)}\n"
                        f"Amount: {plan['amount']}"
                    )
                )
                scan_lines.append(f"{symbol}: OPENED {signal}")
                break

        except Exception as e:
            logger.error(f"{symbol}: trading_job error: {e}")
            scan_lines.append(f"{symbol}: ERROR")

    last_scan_summary = "\n".join(scan_lines[-10:]) if scan_lines else "No scan results."
    logger.info(f"Scan summary:\n{last_scan_summary}")


# =========================================================
# 8) TELEGRAM DASHBOARD
# =========================================================
def get_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="dash_status"),
            InlineKeyboardButton("💰 Balance", callback_data="dash_balance"),
        ],
        [
            InlineKeyboardButton("📂 Positions", callback_data="dash_positions"),
            InlineKeyboardButton("📡 Radar", callback_data="dash_radar"),
        ],
        [
            InlineKeyboardButton("🔎 Force Scan", callback_data="dash_scan"),
            InlineKeyboardButton("⏸ Pause", callback_data="dash_pause"),
        ],
        [
            InlineKeyboardButton("▶️ Resume", callback_data="dash_resume"),
            InlineKeyboardButton("🚨 Close All", callback_data="dash_close_all"),
        ],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        context.bot_data["chat_id"] = str(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Trend Pullback Bot V2 is live.\n"
        "Use the dashboard below.",
        reply_markup=get_dashboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - dashboard\n"
        "/help - commands list\n"
        "/status - bot status\n"
        "/balance - USDT balance\n"
        "/positions - open positions\n"
        "/radar - last scan summary\n"
        "/scan - force one scan\n"
        "/pause - pause bot\n"
        "/resume - resume bot\n"
        "/closeall - close all open positions"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_balance_usdt()
    open_count = count_open_positions()
    paused_text = "YES" if bot_paused else "NO"

    await update.message.reply_text(
        f"📊 Status\n"
        f"Paused: {paused_text}\n"
        f"Balance: {bal:.2f} USDT\n"
        f"Open positions: {open_count}/{MAX_OPEN_POSITIONS}\n"
        f"Cooldown: {COOLDOWN_MINUTES} min\n"
        f"Last signal: {last_signal_summary}\n\n"
        f"Last scan:\n{last_scan_summary}"
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_balance_usdt()
    await update.message.reply_text(f"💰 Available balance: {bal:.2f} USDT")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_positions()
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
        await update.message.reply_text("No open positions.")
    else:
        await update.message.reply_text("📂 Open positions:\n" + "\n".join(lines[:10]))


async def cmd_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📡 Last scan:\n{last_scan_summary}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ Bot paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ Bot resumed.")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Running forced scan...")
    await trading_job(context)
    await update.message.reply_text(f"Done.\n\n{last_scan_summary}")


async def cmd_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_positions()
    count = 0
    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue
        if close_position(p["symbol"], p, 1.0):
            trade_state.pop(p["symbol"], None)
            set_cooldown(p["symbol"])
            count += 1

    await update.message.reply_text(f"🚨 Close-all requested for {count} position(s).")


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "dash_status":
        bal = fetch_balance_usdt()
        open_count = count_open_positions()
        paused_text = "YES" if bot_paused else "NO"
        await query.message.reply_text(
            f"📊 Status\n"
            f"Paused: {paused_text}\n"
            f"Balance: {bal:.2f} USDT\n"
            f"Open positions: {open_count}/{MAX_OPEN_POSITIONS}\n"
            f"Last signal: {last_signal_summary}\n\n"
            f"Last scan:\n{last_scan_summary}"
        )

    elif data == "dash_balance":
        bal = fetch_balance_usdt()
        await query.message.reply_text(f"💰 Available balance: {bal:.2f} USDT")

    elif data == "dash_positions":
        positions = fetch_positions()
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
        await query.message.reply_text(
            "📂 Open positions:\n" + "\n".join(lines[:10]) if lines else "No open positions."
        )

    elif data == "dash_radar":
        await query.message.reply_text(f"📡 Last scan:\n{last_scan_summary}")

    elif data == "dash_scan":
        await query.message.reply_text("🔎 Running forced scan...")
        await trading_job(context)
        await query.message.reply_text(f"Done.\n\n{last_scan_summary}")

    elif data == "dash_pause":
        bot_paused = True
        await query.message.reply_text("⏸ Bot paused.")

    elif data == "dash_resume":
        bot_paused = False
        await query.message.reply_text("▶️ Bot resumed.")

    elif data == "dash_close_all":
        positions = fetch_positions()
        count = 0
        for p in positions:
            contracts = safe_float(p.get("contracts", 0))
            if contracts == 0:
                continue
            if close_position(p["symbol"], p, 1.0):
                trade_state.pop(p["symbol"], None)
                set_cooldown(p["symbol"])
                count += 1
        await query.message.reply_text(f"🚨 Close-all requested for {count} position(s).")


# =========================================================
# 9) HEALTH SERVER
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"TREND_PULLBACK_V2_LIVE")


# =========================================================
# 10) MAIN
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
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("radar", cmd_radar))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("closeall", cmd_close_all))
    app.add_handler(CallbackQueryHandler(dashboard_handler))

    app.job_queue.run_repeating(trading_job, interval=SCAN_INTERVAL_SECONDS, first=10)
    app.job_queue.run_repeating(manage_open_positions, interval=POSITION_CHECK_INTERVAL_SECONDS, first=15)

    logger.info("Starting Trend Pullback Bot V2...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
