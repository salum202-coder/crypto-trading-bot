import os
import json
import math
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, date
from typing import Dict, Any, List, Optional, Tuple

import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# 1) CONFIG
# =========================================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")

TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()  # paper only in this version
START_BALANCE = float(os.getenv("START_BALANCE", "100"))
STATE_FILE = Path(os.getenv("STATE_FILE", "sar_pro_state.json"))

TIMEFRAME_ENTRY = "15m"
TIMEFRAME_TREND = "1h"

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "DOGE/USDT:USDT",
    "LINK/USDT:USDT",
]

# Strategy settings
EMA_PERIOD = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
SAR_STEP = 0.02
SAR_MAX = 0.2

# Risk settings
NORMAL_RISK_PCT = 0.005       # 0.5%
AGGRESSIVE_RISK_PCT = 0.0075  # 0.75%
DEFAULT_RISK_MODE = "normal"

ATR_SL_MULT = 1.5
TP1_R = 1.0
TP2_R = 2.2
TP1_CLOSE_PCT = 0.50

# Quality filters
MIN_ATR_PCT = 0.0015
MAX_ATR_PCT = 0.035
MIN_VOLUME_RATIO = 1.05
MAX_DISTANCE_FROM_EMA = 0.055
MAX_OPEN_TRADES = 3
SCAN_SECONDS = 60

# RSI pullback logic
LONG_RSI_RECOVERY_LEVEL = 30
LONG_RSI_MAX_ENTRY = 55
SHORT_RSI_RECOVERY_LEVEL = 70
SHORT_RSI_MIN_ENTRY = 45

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("SAR_PRO_BOT")

# =========================================================
# 2) EXCHANGE
# =========================================================

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# =========================================================
# 3) STATE
# =========================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def today_key() -> str:
    return date.today().isoformat()

def default_state() -> Dict[str, Any]:
    return {
        "balance": START_BALANCE,
        "bot_running": True,
        "risk_mode": DEFAULT_RISK_MODE,
        "emergency_stop": False,
        "open_trades": [],
        "closed_trades": [],
        "last_scan": {},
        "daily": {},
    }

def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            base = default_state()
            base.update(data)
            return base
        except Exception as e:
            logger.warning(f"Could not load state file: {e}")
    return default_state()

STATE = load_state()

def save_state() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def risk_pct() -> float:
    return AGGRESSIVE_RISK_PCT if STATE.get("risk_mode") == "aggressive" else NORMAL_RISK_PCT

# =========================================================
# 4) INDICATORS
# =========================================================

def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    out = [None] * len(values)
    k = 2 / (period + 1)
    first = sum(values[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out

def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if len(values) <= period:
        return [None] * len(values)
    out = [None] * len(values)
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return out

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    if len(closes) <= period:
        return [None] * len(closes)
    trs = [0.0]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out = [None] * len(closes)
    first = sum(trs[1:period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, len(closes)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out

def parabolic_sar(highs: List[float], lows: List[float], step: float = 0.02, max_step: float = 0.2) -> List[Optional[float]]:
    n = len(highs)
    if n < 5:
        return [None] * n

    psar = [None] * n
    bull = highs[1] > highs[0]
    af = step
    ep = highs[0] if bull else lows[0]
    sar = lows[0] if bull else highs[0]

    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)

        if bull:
            sar = min(sar, lows[i - 1])
            if i >= 2:
                sar = min(sar, lows[i - 2])

            if lows[i] < sar:
                bull = False
                sar = ep
                ep = lows[i]
                af = step
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            sar = max(sar, highs[i - 1])
            if i >= 2:
                sar = max(sar, highs[i - 2])

            if highs[i] > sar:
                bull = True
                sar = ep
                ep = highs[i]
                af = step
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)

        psar[i] = sar

    return psar

# =========================================================
# 5) MARKET DATA
# =========================================================

async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 260) -> Optional[List[List[float]]]:
    try:
        data = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe, None, limit)
        if not data or len(data) < 220:
            return None
        return data
    except Exception as e:
        logger.warning(f"NO_DATA {symbol} {timeframe}: {e}")
        return None

def candles_to_arrays(candles: List[List[float]]) -> Tuple[List[float], List[float], List[float], List[float]]:
    highs = [safe_float(c[2]) for c in candles]
    lows = [safe_float(c[3]) for c in candles]
    closes = [safe_float(c[4]) for c in candles]
    volumes = [safe_float(c[5]) for c in candles]
    return highs, lows, closes, volumes

def has_open_trade(symbol: str) -> bool:
    return any(t["symbol"] == symbol and t["status"] == "open" for t in STATE["open_trades"])

def volume_ratio(volumes: List[float], lookback: int = 20) -> float:
    if len(volumes) < lookback + 1:
        return 0.0
    avg = sum(volumes[-lookback-1:-1]) / lookback
    if avg <= 0:
        return 0.0
    return volumes[-1] / avg

async def build_signal(symbol: str) -> Dict[str, Any]:
    candles_15m = await fetch_ohlcv(symbol, TIMEFRAME_ENTRY)
    candles_1h = await fetch_ohlcv(symbol, TIMEFRAME_TREND)

    if not candles_15m or not candles_1h:
        return {"symbol": symbol, "signal": "NO_DATA", "reason": "Could not fetch enough candles"}

    h15, l15, c15, v15 = candles_to_arrays(candles_15m)
    h1, l1, c1, v1 = candles_to_arrays(candles_1h)

    ema200_15 = ema(c15, EMA_PERIOD)[-1]
    ema200_1h = ema(c1, EMA_PERIOD)[-1]
    rsi_values = rsi(c15, RSI_PERIOD)
    atr15 = atr(h15, l15, c15, ATR_PERIOD)[-1]
    sar_values = parabolic_sar(h15, l15, SAR_STEP, SAR_MAX)

    if None in (ema200_15, ema200_1h, rsi_values[-1], rsi_values[-2], atr15, sar_values[-1], sar_values[-2]):
        return {"symbol": symbol, "signal": "NO_DATA", "reason": "Indicators not ready"}

    close = c15[-1]
    prev_close = c15[-2]
    rsi_now = safe_float(rsi_values[-1])
    rsi_prev = safe_float(rsi_values[-2])
    sar_now = safe_float(sar_values[-1])
    sar_prev = safe_float(sar_values[-2])
    atr_pct = atr15 / close if close else 999
    vol_ratio = volume_ratio(v15)
    distance_ema = abs(close - ema200_15) / close if close else 999

    trend_long = close > ema200_15 and c1[-1] > ema200_1h
    trend_short = close < ema200_15 and c1[-1] < ema200_1h

    sar_flip_long = sar_prev > prev_close and sar_now < close
    sar_flip_short = sar_prev < prev_close and sar_now > close

    base = {
        "symbol": symbol,
        "close": close,
        "ema200_15m": ema200_15,
        "ema200_1h": ema200_1h,
        "rsi": rsi_now,
        "rsi_prev": rsi_prev,
        "atr": atr15,
        "atr_pct": atr_pct,
        "sar": sar_now,
        "volume_ratio": vol_ratio,
        "distance_ema": distance_ema,
        "time": utc_now(),
    }

    # Hard filters first
    if has_open_trade(symbol):
        return {**base, "signal": "NO_TRADE", "reason": "Already has open trade"}

    if len(STATE["open_trades"]) >= MAX_OPEN_TRADES:
        return {**base, "signal": "NO_TRADE", "reason": "Max open trades reached"}

    if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
        return {**base, "signal": "NO_TRADE", "reason": f"ATR not suitable {atr_pct:.4f}"}

    if vol_ratio < MIN_VOLUME_RATIO:
        return {**base, "signal": "WAIT", "reason": f"Weak volume {vol_ratio:.2f}x"}

    if distance_ema > MAX_DISTANCE_FROM_EMA:
        return {**base, "signal": "WAIT", "reason": "Price too far from EMA200"}

    # LONG
    if trend_long:
        rsi_recovery = rsi_prev < LONG_RSI_RECOVERY_LEVEL and rsi_now > LONG_RSI_RECOVERY_LEVEL
        not_fomo = rsi_now <= LONG_RSI_MAX_ENTRY

        if rsi_recovery and sar_flip_long and not_fomo:
            return {
                **base,
                "signal": "LONG",
                "side": "LONG",
                "reason": "LONG: Trend above EMA200 + RSI recovery + SAR flip",
            }

        return {
            **base,
            "signal": "WATCH",
            "side": "LONG",
            "reason": f"LONG trend exists | RSI {rsi_now:.1f} | SAR flip={sar_flip_long}",
        }

    # SHORT
    if trend_short:
        rsi_recovery = rsi_prev > SHORT_RSI_RECOVERY_LEVEL and rsi_now < SHORT_RSI_RECOVERY_LEVEL
        not_fomo = rsi_now >= SHORT_RSI_MIN_ENTRY

        if rsi_recovery and sar_flip_short and not_fomo:
            return {
                **base,
                "signal": "SHORT",
                "side": "SHORT",
                "reason": "SHORT: Trend below EMA200 + RSI recovery + SAR flip",
            }

        return {
            **base,
            "signal": "WATCH",
            "side": "SHORT",
            "reason": f"SHORT trend exists | RSI {rsi_now:.1f} | SAR flip={sar_flip_short}",
        }

    return {**base, "signal": "NO_TRADE", "reason": "No EMA200 trend alignment"}

# =========================================================
# 6) PAPER TRADING
# =========================================================

def calc_position_size(entry: float, sl: float) -> float:
    balance = safe_float(STATE["balance"], START_BALANCE)
    risk_amount = balance * risk_pct()
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    qty = risk_amount / risk_per_unit
    return max(qty, 0.0)

def create_trade(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    side = signal["side"]
    entry = safe_float(signal["close"])
    atr_value = safe_float(signal["atr"])

    if side == "LONG":
        sl = entry - (atr_value * ATR_SL_MULT)
        tp1 = entry + (entry - sl) * TP1_R
        tp2 = entry + (entry - sl) * TP2_R
    else:
        sl = entry + (atr_value * ATR_SL_MULT)
        tp1 = entry - (sl - entry) * TP1_R
        tp2 = entry - (sl - entry) * TP2_R

    qty = calc_position_size(entry, sl)
    if qty <= 0:
        return None

    trade = {
        "id": f"{signal['symbol']}-{int(time.time())}",
        "symbol": signal["symbol"],
        "side": side,
        "entry": entry,
        "qty": qty,
        "sl": sl,
        "initial_sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp1_done": False,
        "remaining_pct": 1.0,
        "opened_at": utc_now(),
        "status": "open",
        "reason": signal.get("reason", ""),
        "risk_pct": risk_pct(),
        "atr": atr_value,
        "sar": signal.get("sar"),
    }
    STATE["open_trades"].append(trade)
    save_state()
    return trade

def pnl_for_trade(trade: Dict[str, Any], exit_price: float, close_pct: float) -> float:
    side = trade["side"]
    qty = safe_float(trade["qty"]) * close_pct
    entry = safe_float(trade["entry"])
    if side == "LONG":
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty

def record_closed(trade: Dict[str, Any], exit_price: float, reason: str, close_pct: float) -> float:
    pnl = pnl_for_trade(trade, exit_price, close_pct)
    STATE["balance"] = safe_float(STATE["balance"], START_BALANCE) + pnl

    closed = dict(trade)
    closed.update({
        "exit": exit_price,
        "closed_at": utc_now(),
        "close_reason": reason,
        "close_pct": close_pct,
        "pnl": pnl,
        "pnl_pct_balance": (pnl / max(START_BALANCE, 1)) * 100,
        "status": "closed",
    })
    STATE["closed_trades"].append(closed)

    dkey = today_key()
    STATE["daily"].setdefault(dkey, {"closed": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    STATE["daily"][dkey]["closed"] += 1
    STATE["daily"][dkey]["pnl"] += pnl
    if pnl >= 0:
        STATE["daily"][dkey]["wins"] += 1
    else:
        STATE["daily"][dkey]["losses"] += 1

    return pnl

async def update_open_trades() -> List[str]:
    messages = []
    still_open = []

    for trade in list(STATE["open_trades"]):
        symbol = trade["symbol"]
        candles = await fetch_ohlcv(symbol, TIMEFRAME_ENTRY, limit=260)
        if not candles:
            still_open.append(trade)
            continue

        h, l, c, v = candles_to_arrays(candles)
        close = c[-1]
        sar_now = parabolic_sar(h, l, SAR_STEP, SAR_MAX)[-1]

        side = trade["side"]
        exit_now = False
        exit_reason = ""
        exit_price = close

        # SAR trailing stop update
        if sar_now:
            if side == "LONG":
                trade["sl"] = max(safe_float(trade["sl"]), safe_float(sar_now))
            else:
                trade["sl"] = min(safe_float(trade["sl"]), safe_float(sar_now))

        # TP1 partial close
        if not trade.get("tp1_done"):
            if side == "LONG" and close >= trade["tp1"]:
                pnl = record_closed(trade, trade["tp1"], "TP1 partial", TP1_CLOSE_PCT)
                trade["tp1_done"] = True
                trade["remaining_pct"] = 1.0 - TP1_CLOSE_PCT
                trade["sl"] = trade["entry"]  # breakeven after TP1
                messages.append(f"✅ TP1 LONG {symbol}\nClosed 50% | PnL: {pnl:.3f} USDT\nSL moved to breakeven")
            elif side == "SHORT" and close <= trade["tp1"]:
                pnl = record_closed(trade, trade["tp1"], "TP1 partial", TP1_CLOSE_PCT)
                trade["tp1_done"] = True
                trade["remaining_pct"] = 1.0 - TP1_CLOSE_PCT
                trade["sl"] = trade["entry"]
                messages.append(f"✅ TP1 SHORT {symbol}\nClosed 50% | PnL: {pnl:.3f} USDT\nSL moved to breakeven")

        # TP2 full close
        if side == "LONG" and close >= trade["tp2"]:
            exit_now = True
            exit_price = trade["tp2"]
            exit_reason = "TP2"
        elif side == "SHORT" and close <= trade["tp2"]:
            exit_now = True
            exit_price = trade["tp2"]
            exit_reason = "TP2"

        # SL / trailing
        if not exit_now:
            if side == "LONG" and close <= trade["sl"]:
                exit_now = True
                exit_price = trade["sl"]
                exit_reason = "SL / Trailing SAR"
            elif side == "SHORT" and close >= trade["sl"]:
                exit_now = True
                exit_price = trade["sl"]
                exit_reason = "SL / Trailing SAR"

        if exit_now:
            close_pct = safe_float(trade.get("remaining_pct", 1.0), 1.0)
            pnl = record_closed(trade, exit_price, exit_reason, close_pct)
            messages.append(
                f"🔚 Closed {side} {symbol}\n"
                f"Reason: {exit_reason}\n"
                f"Exit: {exit_price:.6f}\n"
                f"PnL: {pnl:.3f} USDT\n"
                f"Balance: {STATE['balance']:.2f} USDT"
            )
        else:
            still_open.append(trade)

    STATE["open_trades"] = still_open
    save_state()
    return messages

# =========================================================
# 7) TELEGRAM UI
# =========================================================

def main_keyboard() -> InlineKeyboardMarkup:
    running = "⏸ إيقاف" if STATE.get("bot_running") else "▶️ تشغيل"
    risk = "🔥 Aggressive" if STATE.get("risk_mode") == "aggressive" else "🧠 Normal"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 الرصيد", callback_data="balance"),
            InlineKeyboardButton("📁 الصفقات", callback_data="trades"),
        ],
        [
            InlineKeyboardButton("📡 الرادار", callback_data="radar"),
            InlineKeyboardButton(running, callback_data="toggle_running"),
        ],
        [
            InlineKeyboardButton(risk, callback_data="toggle_risk"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("✅ إغلاق الرابحة", callback_data="close_winners"),
            InlineKeyboardButton("❌ إغلاق الخاسرة", callback_data="close_losers"),
        ],
        [
            InlineKeyboardButton("🛑 إغلاق الكل", callback_data="close_all"),
            InlineKeyboardButton("🚨 طوارئ", callback_data="emergency"),
        ],
    ])

def dashboard_text() -> str:
    return (
        "🤖 SAR Pro Paper Bot\n"
        "━━━━━━━━━━━━━━\n"
        f"الاستراتيجية: Trend Pullback + SAR\n"
        f"المصدر: OKX\n"
        f"الوضع: {'شغال ✅' if STATE.get('bot_running') else 'متوقف ⏸'}\n"
        f"الطوارئ: {'مفعلة 🚨' if STATE.get('emergency_stop') else 'غير مفعلة ✅'}\n"
        f"المخاطرة: {STATE.get('risk_mode')} ({risk_pct()*100:.2f}%)\n"
        f"الرصيد: {safe_float(STATE.get('balance')):.2f} USDT\n"
        f"الصفقات المفتوحة: {len(STATE.get('open_trades', []))}\n"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), reply_markup=main_keyboard())

async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), reply_markup=main_keyboard())

def open_trades_text() -> str:
    trades = STATE.get("open_trades", [])
    if not trades:
        return "لا توجد صفقات مفتوحة حالياً."
    lines = ["📁 الصفقات المفتوحة:"]
    for t in trades:
        lines.append(
            f"\n{t['side']} {t['symbol']}\n"
            f"Entry: {t['entry']:.6f}\n"
            f"SL: {t['sl']:.6f}\n"
            f"TP1: {t['tp1']:.6f} | TP2: {t['tp2']:.6f}\n"
            f"Remaining: {t.get('remaining_pct', 1.0)*100:.0f}%"
        )
    return "\n".join(lines)

def stats_text() -> str:
    closed = STATE.get("closed_trades", [])
    if not closed:
        return "📊 لا توجد صفقات مغلقة حتى الآن."

    wins = [t for t in closed if safe_float(t.get("pnl")) >= 0]
    losses = [t for t in closed if safe_float(t.get("pnl")) < 0]
    total_pnl = sum(safe_float(t.get("pnl")) for t in closed)
    winrate = (len(wins) / len(closed)) * 100 if closed else 0
    best = max([safe_float(t.get("pnl")) for t in closed], default=0)
    worst = min([safe_float(t.get("pnl")) for t in closed], default=0)

    d = STATE.get("daily", {}).get(today_key(), {"closed": 0, "wins": 0, "losses": 0, "pnl": 0.0})

    return (
        "📊 الإحصائيات\n"
        "━━━━━━━━━━━━━━\n"
        f"إجمالي الصفقات المغلقة: {len(closed)}\n"
        f"الرابحة: {len(wins)} | الخاسرة: {len(losses)}\n"
        f"نسبة النجاح: {winrate:.2f}%\n"
        f"إجمالي PnL: {total_pnl:.3f} USDT\n"
        f"أفضل صفقة: {best:.3f}\n"
        f"أسوأ صفقة: {worst:.3f}\n\n"
        f"اليوم {today_key()}:\n"
        f"مغلقة: {d['closed']} | ربح: {d['wins']} | خسارة: {d['losses']} | PnL: {d['pnl']:.3f}"
    )

def radar_text() -> str:
    scan = STATE.get("last_scan", {})
    if not scan:
        return "📡 لا يوجد فحص حتى الآن."
    lines = ["📡 آخر فحص:"]
    for sym, item in scan.items():
        lines.append(f"{sym}: {item.get('signal')} | {item.get('reason')}")
    return "\n".join(lines)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "balance":
        text = f"💰 الرصيد الحالي: {safe_float(STATE.get('balance')):.2f} USDT"
    elif data == "trades":
        text = open_trades_text()
    elif data == "stats":
        text = stats_text()
    elif data == "radar":
        text = radar_text()
    elif data == "toggle_running":
        STATE["bot_running"] = not STATE.get("bot_running", True)
        save_state()
        text = dashboard_text()
    elif data == "toggle_risk":
        STATE["risk_mode"] = "aggressive" if STATE.get("risk_mode") == "normal" else "normal"
        save_state()
        text = dashboard_text()
    elif data == "emergency":
        STATE["emergency_stop"] = True
        STATE["bot_running"] = False
        save_state()
        text = "🚨 تم تفعيل وضع الطوارئ. البوت توقف ولن يفتح صفقات جديدة."
    elif data in ("close_all", "close_winners", "close_losers"):
        text = await manual_close(data)
    else:
        text = dashboard_text()

    await q.edit_message_text(text, reply_markup=main_keyboard())

async def manual_close(mode: str) -> str:
    if not STATE["open_trades"]:
        return "لا توجد صفقات مفتوحة."

    remaining = []
    closed_count = 0
    total_pnl = 0.0

    for trade in STATE["open_trades"]:
        candles = await fetch_ohlcv(trade["symbol"], TIMEFRAME_ENTRY, limit=50)
        if not candles:
            remaining.append(trade)
            continue
        close = safe_float(candles[-1][4])
        pnl_now = pnl_for_trade(trade, close, safe_float(trade.get("remaining_pct", 1.0), 1.0))

        should_close = (
            mode == "close_all" or
            (mode == "close_winners" and pnl_now > 0) or
            (mode == "close_losers" and pnl_now < 0)
        )

        if should_close:
            pnl = record_closed(trade, close, "Manual close", safe_float(trade.get("remaining_pct", 1.0), 1.0))
            total_pnl += pnl
            closed_count += 1
        else:
            remaining.append(trade)

    STATE["open_trades"] = remaining
    save_state()
    return f"تم إغلاق {closed_count} صفقة.\nPnL: {total_pnl:.3f} USDT\nBalance: {STATE['balance']:.2f} USDT"

# =========================================================
# 8) SCANNER JOB
# =========================================================

async def notify(context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = ENV_CHAT_ID
    if not chat_id:
        return
    try:
        await context.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")

async def scan_job(context: ContextTypes.DEFAULT_TYPE):
    # Always manage open trades even if bot is paused
    close_messages = await update_open_trades()
    for msg in close_messages:
        await notify(context, msg)

    if not STATE.get("bot_running", True) or STATE.get("emergency_stop", False):
        return

    scan_result = {}
    for symbol in SYMBOLS:
        signal = await build_signal(symbol)
        scan_result[symbol] = {
            "signal": signal.get("signal"),
            "reason": signal.get("reason"),
            "time": utc_now(),
        }

        if signal.get("signal") in ("LONG", "SHORT"):
            trade = create_trade(signal)
            if trade:
                await notify(
                    context,
                    f"🚀 New {trade['side']} Paper Trade\n"
                    f"{trade['symbol']}\n"
                    f"Entry: {trade['entry']:.6f}\n"
                    f"SL: {trade['sl']:.6f}\n"
                    f"TP1: {trade['tp1']:.6f}\n"
                    f"TP2: {trade['tp2']:.6f}\n"
                    f"Risk: {trade['risk_pct']*100:.2f}%\n"
                    f"Reason: {trade['reason']}"
                )

        await asyncio.sleep(0.2)

    STATE["last_scan"] = scan_result
    save_state()

# =========================================================
# 9) MAIN
# =========================================================

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.job_queue.run_repeating(scan_job, interval=SCAN_SECONDS, first=5)

    logger.info("Starting SAR Pro Paper Bot on OKX...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
