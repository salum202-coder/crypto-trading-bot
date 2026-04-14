import os
import asyncio
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
]

TREND_TIMEFRAME = "1h"
ENTRY_TIMEFRAME = "15m"

RISK_PER_TRADE = 0.0075      # 0.75%
LEVERAGE = 3
MAX_OPEN_POSITIONS = 1
COOLDOWN_MINUTES = 60

EMA_FAST = 20
EMA_MED = 50
EMA_SLOW = 200
ATR_PERIOD = 14
VOL_MA_PERIOD = 20

MIN_TREND_STRENGTH = 0.0025   # 0.25%
MIN_VOLUME_FACTOR = 1.2
MIN_RR_FOR_ENTRY = 1.5

TP1_R = 1.0
TP2_R = 2.0
TRAILING_ATR_MULTIPLIER = 1.2

PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("TrendPullbackBot")

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")
if not BINGX_API_KEY or not BINGX_SECRET:
    raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    }
})

# =========================================================
# 2) STATE
# =========================================================
cooldowns = {}  # symbol -> timestamp
trade_state = {}  # symbol -> dict for TP1/TP2/trailing tracking
last_scan_summary = "No scans yet"

# =========================================================
# 3) HELPERS
# =========================================================
def now_ts() -> int:
    return int(time.time())


def get_active_chat_id(context: ContextTypes.DEFAULT_TYPE | None = None) -> str | None:
    if context and context.bot_data.get("chat_id"):
        return str(context.bot_data["chat_id"])
    if ENV_CHAT_ID:
        return str(ENV_CHAT_ID)
    return None


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = get_active_chat_id(context)
    if not chat_id:
        logger.info("No chat id available yet; skipping notification")
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


def is_in_cooldown(symbol: str) -> bool:
    expiry = cooldowns.get(symbol, 0)
    return now_ts() < expiry


def set_cooldown(symbol: str) -> None:
    cooldowns[symbol] = now_ts() + COOLDOWN_MINUTES * 60


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def sma(values, period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values, period: int):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period
    for price in values[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    return ema_value


def true_range(curr_high, curr_low, prev_close):
    return max(
        curr_high - curr_low,
        abs(curr_high - prev_close),
        abs(curr_low - prev_close),
    )


def atr(ohlcv, period: int):
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        prev_close = ohlcv[i - 1][4]
        curr_high = ohlcv[i][2]
        curr_low = ohlcv[i][3]
        trs.append(true_range(curr_high, curr_low, prev_close))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def average_volume(ohlcv, period: int):
    vols = [c[5] for c in ohlcv]
    return sma(vols, period)


def bullish_engulfing(c1, c0) -> bool:
    return (
        c1[4] < c1[1] and
        c0[4] > c0[1] and
        c0[4] > c1[1] and
        c0[1] <= c1[4]
    )


def bearish_engulfing(c1, c0) -> bool:
    return (
        c1[4] > c1[1] and
        c0[4] < c0[1] and
        c0[4] < c1[1] and
        c0[1] >= c1[4]
    )


def explosive_candles(candles) -> bool:
    for c in candles:
        o = c[1]
        cl = c[4]
        if o <= 0:
            continue
        body_pct = abs(cl - o) / o
        if body_pct > 0.01:
            return True
    return False


def get_market_data(symbol: str):
    try:
        trend_bars = exchange.fetch_ohlcv(symbol, timeframe=TREND_TIMEFRAME, limit=260)
        entry_bars = exchange.fetch_ohlcv(symbol, timeframe=ENTRY_TIMEFRAME, limit=120)

        if len(trend_bars) < 220 or len(entry_bars) < 60:
            logger.warning(f"{symbol}: not enough bars")
            return None

        trend_closes = [b[4] for b in trend_bars]
        entry_closes = [b[4] for b in entry_bars]

        ema20_1h = ema(trend_closes, EMA_FAST)
        ema50_1h = ema(trend_closes, EMA_MED)
        ema200_1h = ema(trend_closes, EMA_SLOW)

        ema20_15m = ema(entry_closes, EMA_FAST)
        ema50_15m = ema(entry_closes, EMA_MED)

        atr_15m = atr(entry_bars, ATR_PERIOD)
        vol_ma_15m = average_volume(entry_bars, VOL_MA_PERIOD)

        if None in (ema20_1h, ema50_1h, ema200_1h, ema20_15m, ema50_15m, atr_15m, vol_ma_15m):
            logger.warning(f"{symbol}: indicator calc returned None")
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
            "trend_bars": trend_bars,
            "entry_bars": entry_bars,
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
            "uptrend": uptrend,
            "downtrend": downtrend,
            "long_pullback": long_pullback,
            "short_pullback": short_pullback,
            "pullback_too_deep_long": pullback_too_deep_long,
            "pullback_too_deep_short": pullback_too_deep_short,
            "long_confirmation": long_confirmation,
            "short_confirmation": short_confirmation,
            "block_trade": block_trade,
            "prev_swing_high": prev_swing_high,
            "prev_swing_low": prev_swing_low,
        }
    except Exception as e:
        logger.error(f"{symbol}: market data error: {e}")
        return None


def get_signal(market: dict):
    if market["block_trade"]:
        return {"signal": "NO_TRADE", "reason": "Blocked by volatility filter"}

    if market["uptrend"]:
        if (
            market["long_pullback"] and
            not market["pullback_too_deep_long"] and
            market["long_confirmation"]
        ):
            return {"signal": "LONG", "reason": "Trend pullback long"}
        return {"signal": "WAIT", "reason": "Uptrend but no clean long setup"}

    if market["downtrend"]:
        if (
            market["short_pullback"] and
            not market["pullback_too_deep_short"] and
            market["short_confirmation"]
        ):
            return {"signal": "SHORT", "reason": "Trend pullback short"}
        return {"signal": "WAIT", "reason": "Downtrend but no clean short setup"}

    return {"signal": "NO_TRADE", "reason": "No valid trend"}


def fetch_usdt_balance() -> float:
    try:
        bal = exchange.fetch_balance()
        return safe_float(bal["USDT"]["free"])
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return 0.0


def fetch_open_positions():
    try:
        return exchange.fetch_positions()
    except Exception as e:
        logger.error(f"fetch_positions error: {e}")
        return []


def count_open_positions() -> int:
    count = 0
    for p in fetch_open_positions():
        contracts = safe_float(p.get("contracts", 0))
        if contracts != 0:
            count += 1
    return count


def get_symbol_position(symbol: str):
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if safe_float(p.get("contracts", 0)) != 0:
                return p
        return None
    except Exception as e:
        logger.error(f"{symbol}: fetch symbol position error: {e}")
        return None


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


def calculate_trade_plan(symbol: str, market: dict, side: str, balance: float):
    entry_price = market["close_15m"]
    atr_15m = market["atr_15m"]
    entry_bars = market["entry_bars"]

    if side == "LONG":
        swing_stop = min([b[3] for b in entry_bars[-3:]])
        atr_stop = entry_price - atr_15m * 1.2
        stop_loss = min(swing_stop, atr_stop)
        risk_per_unit = entry_price - stop_loss
    else:
        swing_stop = max([b[2] for b in entry_bars[-3:]])
        atr_stop = entry_price + atr_15m * 1.2
        stop_loss = max(swing_stop, atr_stop)
        risk_per_unit = stop_loss - entry_price

    if risk_per_unit <= 0:
        return None

    risk_amount = balance * RISK_PER_TRADE
    raw_amount = risk_amount / risk_per_unit
    amount = normalize_amount(symbol, raw_amount)

    if amount <= 0:
        return None

    one_r = risk_per_unit
    if side == "LONG":
        tp1 = entry_price + one_r * TP1_R
        tp2 = entry_price + one_r * TP2_R
        rr = (tp1 - entry_price) / (entry_price - stop_loss)
    else:
        tp1 = entry_price - one_r * TP1_R
        tp2 = entry_price - one_r * TP2_R
        rr = (entry_price - tp1) / (stop_loss - entry_price)

    if rr < MIN_RR_FOR_ENTRY:
        return None

    return {
        "entry": normalize_price(symbol, entry_price),
        "stop_loss": normalize_price(symbol, stop_loss),
        "tp1": normalize_price(symbol, tp1),
        "tp2": normalize_price(symbol, tp2),
        "amount": amount,
        "risk_per_unit": risk_per_unit,
        "risk_amount": risk_amount,
    }


def set_symbol_leverage(symbol: str, leverage: int):
    try:
        exchange.set_leverage(leverage, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set leverage warning: {e}")


def open_trade(symbol: str, side: str, plan: dict):
    order_side = "buy" if side == "LONG" else "sell"
    try:
        set_symbol_leverage(symbol, LEVERAGE)
        order = exchange.create_market_order(symbol, order_side, plan["amount"], params={})
        logger.info(f"{symbol}: opened {side} order -> {order.get('id')}")
        trade_state[symbol] = {
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
        logger.error(f"{symbol}: open trade error: {e}")
        return False


def close_position(symbol: str, position, portion: float = 1.0, reduce_only: bool = True):
    try:
        contracts = safe_float(position.get("contracts", 0))
        if contracts <= 0:
            return False

        close_amount = contracts * portion
        close_amount = normalize_amount(symbol, close_amount)
        if close_amount <= 0:
            return False

        side = position.get("side", "").lower()
        close_side = "sell" if side == "long" else "buy"

        exchange.create_market_order(
            symbol,
            close_side,
            close_amount,
            params={"reduceOnly": reduce_only}
        )
        return True
    except Exception as e:
        logger.error(f"{symbol}: close position error: {e}")
        return False


# =========================================================
# 4) POSITION MANAGER
# =========================================================
async def monitor_positions(context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_open_positions()
    if not positions:
        return

    for pos in positions:
        contracts = safe_float(pos.get("contracts", 0))
        if contracts == 0:
            continue

        symbol = pos["symbol"]
        side = pos.get("side", "").lower()
        entry_price = safe_float(pos.get("entryPrice", 0))
        mark_price = safe_float(pos.get("markPrice", 0))

        if entry_price <= 0 or mark_price <= 0:
            continue

        state = trade_state.get(symbol)
        if not state:
            trade_state[symbol] = {
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

        market = get_market_data(symbol)
        if not market:
            continue

        atr_15m = market["atr_15m"]
        if atr_15m is None:
            continue

        if side == "long":
            if mark_price <= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    await notify(
                        context,
                        (
                            f"🛑 تم إغلاق LONG\n"
                            f"{symbol}\n"
                            f"السبب: Stop Loss\n"
                            f"Entry: {entry_price}\n"
                            f"Exit: {mark_price}"
                        )
                    )
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price >= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        (
                            f"✅ TP1 LONG\n"
                            f"{symbol}\n"
                            f"تم قفل 50%\n"
                            f"ونقل الوقف إلى نقطة الدخول"
                        )
                    )
                    continue

            refreshed_pos = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price >= state["tp2"]:
                if close_position(symbol, refreshed_pos, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        (
                            f"🚀 TP2 LONG\n"
                            f"{symbol}\n"
                            f"تم تفعيل Trailing Stop"
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
                    refreshed_pos = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed_pos, 1.0):
                        await notify(
                            context,
                            (
                                f"📉 Trailing Stop LONG\n"
                                f"{symbol}\n"
                                f"Exit: {mark_price}"
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
                            f"🛑 تم إغلاق SHORT\n"
                            f"{symbol}\n"
                            f"السبب: Stop Loss\n"
                            f"Entry: {entry_price}\n"
                            f"Exit: {mark_price}"
                        )
                    )
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price <= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        (
                            f"✅ TP1 SHORT\n"
                            f"{symbol}\n"
                            f"تم قفل 50%\n"
                            f"ونقل الوقف إلى نقطة الدخول"
                        )
                    )
                    continue

            refreshed_pos = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price <= state["tp2"]:
                if close_position(symbol, refreshed_pos, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        (
                            f"🚀 TP2 SHORT\n"
                            f"{symbol}\n"
                            f"تم تفعيل Trailing Stop"
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
                    refreshed_pos = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed_pos, 1.0):
                        await notify(
                            context,
                            (
                                f"📈 Trailing Stop SHORT\n"
                                f"{symbol}\n"
                                f"Exit: {mark_price}"
                            )
                        )
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue


# =========================================================
# 5) SCANNER / ENTRY ENGINE
# =========================================================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    global last_scan_summary

    await monitor_positions(context)

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

            existing = get_symbol_position(symbol)
            if existing:
                scan_lines.append(f"{symbol}: already open")
                continue

            market = get_market_data(symbol)
            if not market:
                scan_lines.append(f"{symbol}: no data")
                continue

            signal_info = get_signal(market)
            signal = signal_info["signal"]
            reason = signal_info["reason"]

            scan_lines.append(f"{symbol}: {signal} | {reason}")

            if signal not in ("LONG", "SHORT"):
                continue

            balance = fetch_usdt_balance()
            if balance <= 5:
                logger.warning("Balance too low.")
                continue

            plan = calculate_trade_plan(symbol, market, signal, balance)
            if not plan:
                logger.info(f"{symbol}: plan rejected (RR/amount/stop invalid)")
                continue

            ok = open_trade(symbol, signal, plan)
            if ok:
                await notify(
                    context,
                    (
                        f"🚀 دخول صفقة {signal}\n"
                        f"{symbol}\n"
                        f"Reason: {reason}\n"
                        f"Entry: {plan['entry']}\n"
                        f"SL: {plan['stop_loss']}\n"
                        f"TP1: {plan['tp1']}\n"
                        f"TP2: {plan['tp2']}\n"
                        f"Amount: {plan['amount']}"
                    )
                )
                break

        except Exception as e:
            logger.error(f"{symbol}: trading_job error: {e}")

    last_scan_summary = "\n".join(scan_lines[-10:]) if scan_lines else "No symbols scanned."
    logger.info(f"Scan summary:\n{last_scan_summary}")


# =========================================================
# 6) DASHBOARD
# =========================================================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 الرصيد", callback_data="btn_bal"),
            InlineKeyboardButton("📡 الرادار", callback_data="btn_radar"),
        ],
        [
            InlineKeyboardButton("📂 الصفقات", callback_data="btn_positions"),
            InlineKeyboardButton("🚨 إغلاق الكل", callback_data="close_all"),
        ],
    ])


async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_bal":
        bal = fetch_usdt_balance()
        await query.message.reply_text(f"💳 الرصيد المتاح: {bal:.2f} USDT")

    elif query.data == "btn_radar":
        await query.message.reply_text(f"📡 آخر فحص:\n{last_scan_summary}")

    elif query.data == "btn_positions":
        positions = fetch_open_positions()
        lines = []
        for p in positions:
            contracts = safe_float(p.get("contracts", 0))
            if contracts == 0:
                continue
            lines.append(
                f"{p['symbol']} | {p.get('side')} | "
                f"Entry: {safe_float(p.get('entryPrice', 0))} | "
                f"Mark: {safe_float(p.get('markPrice', 0))} | "
                f"PnL: {safe_float(p.get('unrealizedPnl', 0)):.4f}"
            )
        if not lines:
            await query.message.reply_text("لا توجد صفقات مفتوحة.")
        else:
            await query.message.reply_text("📂 الصفقات المفتوحة:\n" + "\n".join(lines[:10]))

    elif query.data == "close_all":
        positions = fetch_open_positions()
        count = 0
        for p in positions:
            contracts = safe_float(p.get("contracts", 0))
            if contracts == 0:
                continue
            if close_position(p["symbol"], p, 1.0):
                trade_state.pop(p["symbol"], None)
                set_cooldown(p["symbol"])
                count += 1
        await query.message.reply_text(f"✅ تم طلب إغلاق {count} صفقة.")


# =========================================================
# 7) COMMANDS
# =========================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        context.bot_data["chat_id"] = str(update.effective_chat.id)
    await update.message.reply_text(
        "🎯 Trend Pullback Bot نشط.\n"
        "استخدم اللوحة أو الأوامر.",
        reply_markup=get_main_keyboard()
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_usdt_balance()
    open_count = count_open_positions()
    await update.message.reply_text(
        f"📊 الحالة الحالية\n"
        f"Balance: {bal:.2f} USDT\n"
        f"Open positions: {open_count}\n"
        f"Max positions: {MAX_OPEN_POSITIONS}\n"
        f"Cooldown minutes: {COOLDOWN_MINUTES}\n\n"
        f"Last scan:\n{last_scan_summary}"
    )


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_open_positions()
    lines = []
    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue
        lines.append(
            f"{p['symbol']} | {p.get('side')} | "
            f"Entry: {safe_float(p.get('entryPrice', 0))} | "
            f"Mark: {safe_float(p.get('markPrice', 0))} | "
            f"UPnL: {safe_float(p.get('unrealizedPnl', 0)):.4f}"
        )
    if not lines:
        await update.message.reply_text("لا توجد صفقات مفتوحة.")
    else:
        await update.message.reply_text("📂 الصفقات المفتوحة:\n" + "\n".join(lines[:10]))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_usdt_balance()
    await update.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT")


# =========================================================
# 8) HEALTH SERVER
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"TREND_PULLBACK_BOT_LIVE")


# =========================================================
# 9) RUN
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

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CallbackQueryHandler(dashboard_handler))

    if app.job_queue is None:
        raise RuntimeError(
            "Job queue is not available. Install python-telegram-bot[job-queue]."
        )

    chat_id = ENV_CHAT_ID
    if chat_id:
        app.bot_data["chat_id"] = str(chat_id)

    app.job_queue.run_repeating(trading_job, interval=60, first=10)

    logger.info("Starting Trend Pullback Bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
