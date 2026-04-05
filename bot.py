import ccxt
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler
)

# ================= CONFIG =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

exchange = ccxt.binance()

virtual_wallet = {"USDT": 10000.0}
for s in SYMBOLS:
    virtual_wallet[s] = 0.0

entry_price = {s: None for s in SYMBOLS}
highest_price = {s: None for s in SYMBOLS}
last_signal = {s: None for s in SYMBOLS}

# 🔥 إدارة المخاطر
RISK_PER_TRADE = 0.02
STOP_LOSS = 0.01
TAKE_PROFIT = 0.02
TRAILING_STOP = 0.03

# 📊 سجل الصفقات
trade_history = []

# ================= INDICATORS =================

def ema(data, period):
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= ANALYSIS =================

def get_analysis(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=200)
        bars = bars[:-1]

        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]

        price = closes[-1]

        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)
        ema100 = ema(closes[-100:], 100)
        ema200 = ema(closes[-200:], 200)

        rsi = calculate_rsi(closes)

        sar = min(lows[-2:])

        # BUY
        if ema20 > ema50 > ema100 > ema200 and price > ema20 and sar < price and 50 < rsi < 65:
            return price, "BUY"

        # SELL
        elif ema20 < ema50 < ema100 < ema200 and price < ema20 and sar > price and 35 < rsi < 50:
            return price, "SELL"

        return price, "NEUTRAL"

    except Exception as e:
        print(f"ANALYSIS ERROR {symbol}: {e}")
        return None, "NEUTRAL"

# ================= TRADE =================

def position_size(price):
    risk_amount = virtual_wallet["USDT"] * RISK_PER_TRADE
    return risk_amount / price

def record_trade(symbol, entry, exit_price):
    profit = (exit_price - entry) / entry
    trade_history.append({
        "symbol": symbol,
        "entry": entry,
        "exit": exit_price,
        "profit": profit,
        "time": datetime.now().strftime("%H:%M")
    })

def close_trade(symbol, price):
    entry = entry_price[symbol]

    value = virtual_wallet[symbol] * price
    virtual_wallet["USDT"] += value
    virtual_wallet[symbol] = 0

    record_trade(symbol, entry, price)

    entry_price[symbol] = None
    highest_price[symbol] = None

# ================= BOT =================

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not CHAT_ID:
            return

        for sym in SYMBOLS:
            price, signal = get_analysis(sym)
            if not price:
                continue

            # 🔥 إدارة الصفقة
            if virtual_wallet[sym] > 0:
                entry = entry_price[sym]

                # تحديث أعلى سعر
                if highest_price[sym] is None or price > highest_price[sym]:
                    highest_price[sym] = price

                # Stop Loss
                if price <= entry * (1 - STOP_LOSS):
                    close_trade(sym, price)
                    await context.bot.send_message(chat_id=CHAT_ID, text=f"❌ STOP LOSS {sym}")
                    continue

                # Take Profit
                if price >= entry * (1 + TAKE_PROFIT):
                    close_trade(sym, price)
                    await context.bot.send_message(chat_id=CHAT_ID, text=f"💰 TAKE PROFIT {sym}")
                    continue

                # Trailing Stop
                if price < highest_price[sym] * (1 - TRAILING_STOP):
                    close_trade(sym, price)
                    await context.bot.send_message(chat_id=CHAT_ID, text=f"🔻 TRAILING STOP {sym}")
                    continue

            if signal == last_signal[sym]:
                continue

            # BUY
            if signal == "BUY" and virtual_wallet[sym] == 0:
                qty = position_size(price)
                cost = qty * price

                if virtual_wallet["USDT"] >= cost:
                    virtual_wallet[sym] = qty
                    virtual_wallet["USDT"] -= cost
                    entry_price[sym] = price
                    highest_price[sym] = price

                    await context.bot.send_message(chat_id=CHAT_ID, text=f"🚀 BUY {sym} @ {price}")

            # SELL
            elif signal == "SELL" and virtual_wallet[sym] > 0:
                close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ SELL {sym}")

            last_signal[sym] = signal

    except Exception as e:
        print("JOB ERROR:", e)

# ================= STATS =================

def get_stats():
    total = len(trade_history)
    wins = sum(1 for t in trade_history if t["profit"] > 0)
    losses = total - wins

    pnl = sum(t["profit"] for t in trade_history) * 100

    winrate = (wins / total * 100) if total > 0 else 0

    return total, wins, losses, pnl, winrate

# ================= UI =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ]
    await update.message.reply_text("🤖 BOT PRO READY 🚀", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "stats":
        total, wins, losses, pnl, winrate = get_stats()
        balance = virtual_wallet["USDT"]

        text = (
            f"💰 Balance: {balance:.2f} USDT\n"
            f"📊 Trades: {total}\n"
            f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
            f"📈 PnL: {pnl:.2f}%\n"
            f"🎯 WinRate: {winrate:.1f}%"
        )
        await query.edit_message_text(text)

    elif query.data == "positions":
        msg = "📊 Positions:\n"
        for sym in SYMBOLS:
            msg += f"{sym}: {virtual_wallet[sym]:.4f}\n"
        await query.edit_message_text(msg)

# ================= MAIN =================

def main():
    try:
        if not TOKEN:
            print("❌ TOKEN NOT FOUND")
            return

        app = ApplicationBuilder().token(TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(button_handler))

        app.job_queue.run_repeating(trading_job, interval=60, first=5)

        print("✅ BOT PRO RUNNING")

        app.run_polling(drop_pending_updates=True)

    except Exception as e:
        print("🔥 CRASH:", e)

if __name__ == "__main__":
    main()
