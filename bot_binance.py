import os
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Any, List, Optional

import ccxt
import pandas as pd
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PORT = int(os.getenv("PORT", "8080"))

TRADING_MODE = os.getenv("TRADING_MODE", "PAPER").upper()
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "1000"))

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "paper_state.json"
JOURNAL_FILE = DATA_DIR / "paper_journal.jsonl"

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")

if TRADING_MODE != "PAPER":
    raise RuntimeError("This bot is PAPER only. Set TRADING_MODE=PAPER")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "LTC/USDT",
]

TIMEFRAME = os.getenv("TIMEFRAME", "1h")
SCAN_INTERVAL_SECONDS = 60

EMA_PERIOD = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
SAR_STEP = 0.02
SAR_MAX = 0.2

RISK_NORMAL = 0.01
RISK_AGGRESSIVE = 0.02
LEVERAGE_NORMAL = 3
LEVERAGE_AGGRESSIVE = 5

ATR_SL_MULTIPLIER = 1.5
MAX_RSI_CONFIRM_LONG = 55
MIN_RSI_CONFIRM_SHORT = 45
MAX_DISTANCE_FROM_EMA = 0.035

bot_paused = False
risk_mode = "NORMAL"
last_scan_summary = "No scan yet."

state: Dict[str, Any] = {
    "balance": PAPER_START_BALANCE,
    "positions": {},
    "closed_trades": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl": 0.0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("OKX_PAPER_BOT")

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state():
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"save_state error: {e}")


def load_state():
    global state
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if loaded:
                state.update(loaded)
    except Exception as e:
        logger.error(f"load_state error: {e}")


def journal(event: str, payload: dict):
    try:
        row = {"ts": now_iso(), "event": event, **payload}
        with JOURNAL_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"journal error: {e}")


def parabolic_sar(df: pd.DataFrame, step: float = SAR_STEP, max_step: float = SAR_MAX) -> pd.Series:
    high = df["high"].values
    low = df["low"].values

    sar = [0.0] * len(df)
    bull = True
    af = step
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, len(df)):
        prev_sar = sar[i - 1]

        if bull:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i - 1])
            if i > 1:
                sar[i] = min(sar[i], low[i - 2])

            if low[i] < sar[i]:
                bull = False
                sar[i] = ep
                ep = low[i]
                af = step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_step)
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i - 1])
            if i > 1:
                sar[i] = max(sar[i], high[i - 2])

            if high[i] > sar[i]:
                bull = True
                sar[i] = ep
                ep = high[i]
                af = step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_step)

    return pd.Series(sar, index=df.index)


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema200"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()

    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    df["sar"] = parabolic_sar(df)
    return df


def fetch_df(symbol: str, limit: int = 260) -> Optional[pd.DataFrame]:
    for attempt in range(3):
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
            if not bars:
                return None

            df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna()

            if len(df) < EMA_PERIOD + 5:
                return None

            return calculate_indicators(df)

        except Exception as e:
            logger.error(f"{symbol} fetch attempt {attempt + 1}/3 error: {e}")
            time.sleep(1)

    return None


def get_price(symbol: str) -> float:
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker["last"])


def check_signal(symbol: str) -> dict:
    df = fetch_df(symbol)

    if df is None or len(df) < EMA_PERIOD + 5:
        return {"signal": False, "reason": "NO_DATA"}

    prev = df.iloc[-2]
    cur = df.iloc[-1]

    close = float(cur["close"])
    prev_close = float(prev["close"])
    ema200 = float(cur["ema200"])
    rsi_now = float(cur["rsi"])
    rsi_prev = float(prev["rsi"])
    sar_now = float(cur["sar"])
    sar_prev = float(prev["sar"])
    atr = float(cur["atr"])

    if pd.isna(ema200) or pd.isna(rsi_now) or pd.isna(sar_now) or pd.isna(atr):
        return {"signal": False, "reason": "INDICATORS_NOT_READY"}

    distance_from_ema = abs(close - ema200) / ema200

    if distance_from_ema > MAX_DISTANCE_FROM_EMA:
        return {"signal": False, "reason": f"Anti-FOMO: far from EMA {distance_from_ema * 100:.2f}%"}

    long_trend = close > ema200
    long_rsi_recovery = rsi_prev < 30 and rsi_now > 30
    long_recent_recovery = df["rsi"].iloc[-5:].min() < 30 and rsi_now > rsi_prev
    long_sar_flip = sar_prev > prev_close and sar_now < close

    if long_trend and (long_rsi_recovery or long_recent_recovery) and long_sar_flip:
        if rsi_now > MAX_RSI_CONFIRM_LONG:
            return {"signal": False, "reason": f"LONG rejected: RSI too high {rsi_now:.2f}"}

        stop_loss = close - (atr * ATR_SL_MULTIPLIER)

        return {
            "signal": True,
            "side": "LONG",
            "entry": close,
            "stop_loss": stop_loss,
            "sar": sar_now,
            "atr": atr,
            "rsi": rsi_now,
            "ema200": ema200,
            "reason": "LONG: EMA200 trend + RSI recovery + SAR flip",
        }

    short_trend = close < ema200
    short_rsi_rejection = rsi_prev > 70 and rsi_now < 70
    short_recent_rejection = df["rsi"].iloc[-5:].max() > 70 and rsi_now < rsi_prev
    short_sar_flip = sar_prev < prev_close and sar_now > close

    if short_trend and (short_rsi_rejection or short_recent_rejection) and short_sar_flip:
        if rsi_now < MIN_RSI_CONFIRM_SHORT:
            return {"signal": False, "reason": f"SHORT rejected: RSI too low {rsi_now:.2f}"}

        stop_loss = close + (atr * ATR_SL_MULTIPLIER)

        return {
            "signal": True,
            "side": "SHORT",
            "entry": close,
            "stop_loss": stop_loss,
            "sar": sar_now,
            "atr": atr,
            "rsi": rsi_now,
            "ema200": ema200,
            "reason": "SHORT: EMA200 downtrend + RSI rejection + SAR flip",
        }

    return {
        "signal": False,
        "reason": f"No setup | RSI {rsi_now:.2f} | Close {'above' if close > ema200 else 'below'} EMA200",
    }


def current_risk_pct() -> float:
    return RISK_AGGRESSIVE if risk_mode == "AGGRESSIVE" else RISK_NORMAL


def current_leverage() -> int:
    return LEVERAGE_AGGRESSIVE if risk_mode == "AGGRESSIVE" else LEVERAGE_NORMAL


def calc_amount(entry: float, stop_loss: float) -> float:
    risk_usdt = float(state["balance"]) * current_risk_pct()
    risk_per_unit = abs(entry - stop_loss)

    if risk_per_unit <= 0:
        return 0.0

    return risk_usdt / risk_per_unit


def open_paper_position(symbol: str, signal: dict) -> bool:
    if symbol in state["positions"]:
        return False

    entry = float(signal["entry"])
    stop_loss = float(signal["stop_loss"])
    amount = calc_amount(entry, stop_loss)

    if amount <= 0:
        return False

    position = {
        "symbol": symbol,
        "side": signal["side"],
        "entry": entry,
        "amount": amount,
        "stop_loss": stop_loss,
        "initial_stop": stop_loss,
        "trailing_stop": stop_loss,
        "opened_at": now_iso(),
        "rsi": signal["rsi"],
        "ema200": signal["ema200"],
        "atr": signal["atr"],
        "leverage": current_leverage(),
        "risk_pct": current_risk_pct(),
    }

    state["positions"][symbol] = position
    save_state()
    journal("OPEN", position)
    return True


def close_paper_position(symbol: str, exit_price: float, reason: str) -> Optional[dict]:
    pos = state["positions"].get(symbol)

    if not pos:
        return None

    entry = float(pos["entry"])
    amount = float(pos["amount"])
    side = pos["side"]

    if side == "LONG":
        pnl = (exit_price - entry) * amount
        pnl_pct = ((exit_price - entry) / entry) * 100
    else:
        pnl = (entry - exit_price) * amount
        pnl_pct = ((entry - exit_price) / entry) * 100

    state["balance"] = float(state["balance"]) + pnl
    state["closed_trades"] += 1
    state["realized_pnl"] += pnl

    if pnl >= 0:
        state["wins"] += 1
    else:
        state["losses"] += 1

    state["positions"].pop(symbol, None)
    save_state()

    payload = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "amount": amount,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "balance": state["balance"],
    }

    journal("CLOSE", payload)
    return payload


def update_open_positions() -> List[dict]:
    updates = []

    for symbol, pos in list(state["positions"].items()):
        df = fetch_df(symbol)

        if df is None or len(df) < 5:
            continue

        cur = df.iloc[-1]
        close = float(cur["close"])
        sar = float(cur["sar"])
        side = pos["side"]
        current_sl = float(pos["trailing_stop"])

        if side == "LONG":
            if sar > current_sl and sar < close:
                pos["trailing_stop"] = sar
                updates.append({"type": "SL_UPDATE", "symbol": symbol, "side": side, "new_sl": sar, "price": close})
                journal("SL_UPDATE", {"symbol": symbol, "side": side, "new_sl": sar, "price": close})

            if close <= float(pos["trailing_stop"]):
                closed = close_paper_position(symbol, close, "LONG Trailing Stop / Stop Loss")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

            if sar > close:
                closed = close_paper_position(symbol, close, "LONG SAR Exit Signal")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

        else:
            if sar < current_sl and sar > close:
                pos["trailing_stop"] = sar
                updates.append({"type": "SL_UPDATE", "symbol": symbol, "side": side, "new_sl": sar, "price": close})
                journal("SL_UPDATE", {"symbol": symbol, "side": side, "new_sl": sar, "price": close})

            if close >= float(pos["trailing_stop"]):
                closed = close_paper_position(symbol, close, "SHORT Trailing Stop / Stop Loss")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

            if sar < close:
                closed = close_paper_position(symbol, close, "SHORT SAR Exit Signal")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

    save_state()
    return updates


def keyboard():
    return ReplyKeyboardMarkup(
        [
            ["الرصيد 💰", "الصفقات 📁"],
            ["الرادار 📡", "حالة النظام 🟢"],
            ["تشغيل ▶️", "إيقاف ⏸️"],
            ["Normal 3x 🧠", "Aggressive 5x 🔥"],
            ["إغلاق الكل 🛑", "إغلاق الرابحة ✅", "إغلاق الخاسرة ❌"],
            ["طوارئ 🚨"],
        ],
        resize_keyboard=True,
    )


def allowed(update: Update) -> bool:
    if not ENV_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(ENV_CHAT_ID)


async def send_to_user(context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = ENV_CHAT_ID or context.bot_data.get("chat_id")
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return

    context.bot_data["chat_id"] = str(update.effective_chat.id)

    await update.message.reply_text(
        "🤖 Binance Paper Trading Bot جاهز\n"
        "الوضع: تجريبي فقط\n"
        "مصدر البيانات: OKX\n"
        f"الرصيد الوهمي: {state['balance']:.2f} USDT",
        reply_markup=keyboard(),
    )


def balance_text() -> str:
    unrealized = 0.0

    for symbol, pos in state["positions"].items():
        try:
            price = get_price(symbol)
            if pos["side"] == "LONG":
                unrealized += (price - float(pos["entry"])) * float(pos["amount"])
            else:
                unrealized += (float(pos["entry"]) - price) * float(pos["amount"])
        except Exception:
            pass

    equity = float(state["balance"]) + unrealized

    return (
        f"💰 الرصيد التجريبي\n"
        f"Balance: {state['balance']:.2f} USDT\n"
        f"Unrealized PnL: {unrealized:.2f} USDT\n"
        f"Equity: {equity:.2f} USDT\n"
        f"Start Balance: {PAPER_START_BALANCE:.2f} USDT\n"
        f"Mode: {risk_mode} ({current_leverage()}x)\n"
        f"Risk: {current_risk_pct() * 100:.2f}%\n"
        f"Market Data: OKX"
    )


def positions_text() -> str:
    if not state["positions"]:
        return "📁 لا توجد صفقات مفتوحة."

    lines = ["📁 الصفقات المفتوحة:"]

    for symbol, pos in state["positions"].items():
        try:
            price = get_price(symbol)

            if pos["side"] == "LONG":
                pnl = (price - float(pos["entry"])) * float(pos["amount"])
                pnl_pct = ((price - float(pos["entry"])) / float(pos["entry"])) * 100
            else:
                pnl = (float(pos["entry"]) - price) * float(pos["amount"])
                pnl_pct = ((float(pos["entry"]) - price) / float(pos["entry"])) * 100

        except Exception:
            price, pnl, pnl_pct = 0, 0, 0

        lines.append(
            f"\n{symbol}\n"
            f"Side: {pos['side']}\n"
            f"Entry: {pos['entry']:.6f}\n"
            f"Now: {price:.6f}\n"
            f"SL: {pos['trailing_stop']:.6f}\n"
            f"PnL: {pnl:.2f} USDT ({pnl_pct:.2f}%)"
        )

    return "\n".join(lines)


async def scan_now(context: ContextTypes.DEFAULT_TYPE) -> str:
    global last_scan_summary

    if bot_paused:
        last_scan_summary = "Bot paused."
        return last_scan_summary

    lines = []

    for symbol in SYMBOLS:
        if symbol in state["positions"]:
            lines.append(f"{symbol}: ALREADY_OPEN")
            continue

        signal = check_signal(symbol)

        if signal["signal"]:
            opened = open_paper_position(symbol, signal)

            if opened:
                msg = (
                    f"🚀 صفقة وهمية جديدة\n"
                    f"{symbol}\n"
                    f"Side: {signal['side']}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"SL: {signal['stop_loss']:.6f}\n"
                    f"RSI: {signal['rsi']:.2f}\n"
                    f"Market Data: OKX\n"
                    f"Reason: {signal['reason']}"
                )
                await send_to_user(context, msg)
                lines.append(f"{symbol}: OPENED {signal['side']}")
            else:
                lines.append(f"{symbol}: SIGNAL BUT NOT OPENED")
        else:
            lines.append(f"{symbol}: NO_TRADE | {signal['reason']}")

    last_scan_summary = "\n".join(lines[-12:])
    return last_scan_summary


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused, risk_mode

    if not allowed(update):
        return

    text = update.message.text.strip()

    if text == "الرصيد 💰":
        await update.message.reply_text(balance_text())

    elif text == "الصفقات 📁":
        await update.message.reply_text(positions_text())

    elif text == "الرادار 📡":
        await update.message.reply_text("📡 جاري الفحص...")
        result = await scan_now(context)
        await update.message.reply_text(result)

    elif text == "حالة النظام 🟢":
        try:
            start = time.time()
            exchange.fetch_time()
            latency = (time.time() - start) * 1000
            await update.message.reply_text(
                f"🟢 النظام شغال\n"
                f"Market Data: OKX\n"
                f"Latency: {latency:.0f} ms\n"
                f"Mode: PAPER"
            )
        except Exception as e:
            await update.message.reply_text(f"🔴 مشكلة اتصال OKX:\n{e}")

    elif text == "تشغيل ▶️":
        bot_paused = False
        await update.message.reply_text("▶️ تم تشغيل البوت.")

    elif text == "إيقاف ⏸️":
        bot_paused = True
        await update.message.reply_text("⏸️ تم إيقاف استقبال صفقات جديدة.")

    elif text == "Normal 3x 🧠":
        risk_mode = "NORMAL"
        await update.message.reply_text("🧠 تم تفعيل Normal 3x | Risk 1%")

    elif text == "Aggressive 5x 🔥":
        risk_mode = "AGGRESSIVE"
        await update.message.reply_text("🔥 تم تفعيل Aggressive 5x | Risk 2%")

    elif text == "إغلاق الكل 🛑":
        count = 0
        for symbol in list(state["positions"].keys()):
            price = get_price(symbol)
            close_paper_position(symbol, price, "Manual Close All")
            count += 1
        await update.message.reply_text(f"🛑 تم إغلاق {count} صفقة وهمية.")

    elif text in ("إغلاق الرابحة ✅", "إغلاق الخاسرة ❌"):
        winners = text == "إغلاق الرابحة ✅"
        count = 0

        for symbol, pos in list(state["positions"].items()):
            price = get_price(symbol)
            if pos["side"] == "LONG":
                pnl = (price - float(pos["entry"])) * float(pos["amount"])
            else:
                pnl = (float(pos["entry"]) - price) * float(pos["amount"])

            if (winners and pnl > 0) or ((not winners) and pnl < 0):
                close_paper_position(symbol, price, "Manual Close Winners/Losers")
                count += 1

        await update.message.reply_text(f"تم إغلاق {count} صفقة.")

    elif text == "طوارئ 🚨":
        count = 0
        for symbol in list(state["positions"].keys()):
            price = get_price(symbol)
            close_paper_position(symbol, price, "Emergency Close")
            count += 1

        bot_paused = True
        await update.message.reply_text(f"🚨 تم إغلاق كل الصفقات وإيقاف البوت. العدد: {count}")

    else:
        await update.message.reply_text("استخدم الأزرار بالأسفل.", reply_markup=keyboard())


async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    if bot_paused:
        return

    updates = update_open_positions()

    for u in updates:
        if u["type"] == "SL_UPDATE":
            await send_to_user(
                context,
                f"🔁 تحديث وقف الخسارة\n"
                f"{u['symbol']}\n"
                f"Side: {u['side']}\n"
                f"New SL: {u['new_sl']:.6f}\n"
                f"Price: {u['price']:.6f}"
            )

        elif u["type"] == "CLOSE":
            await send_to_user(
                context,
                f"📌 إغلاق صفقة وهمية\n"
                f"{u['symbol']}\n"
                f"Side: {u['side']}\n"
                f"Reason: {u['reason']}\n"
                f"Entry: {u['entry']:.6f}\n"
                f"Exit: {u['exit']:.6f}\n"
                f"PnL: {u['pnl']:.2f} USDT ({u['pnl_pct']:.2f}%)\n"
                f"Balance: {u['balance']:.2f} USDT"
            )

    await scan_now(context)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OKX_MARKET_DATA_PAPER_BOT_LIVE")

    def log_message(self, format, *args):
        return


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


def main():
    load_state()

    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    if ENV_CHAT_ID:
        app.bot_data["chat_id"] = str(ENV_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(trading_job, interval=SCAN_INTERVAL_SECONDS, first=10)

    logger.info(
        f"Starting Paper Trading Bot | LONG+SHORT | Market Data=OKX | DATA_DIR={DATA_DIR} | Balance={state['balance']}"
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
