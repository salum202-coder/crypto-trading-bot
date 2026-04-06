import ccxt
import os
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

exchange = ccxt.binance({
    "enableRateLimit": True
})

# ================= WALLET =================

virtual_wallet = {"USDT": 10000.0}
positions = {}
entry_price = {}

trade_history = []
wins = 0
losses = 0

RISK_PER_TRADE = 0.02
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04

# ================= INDICATORS =================

def ema(data, period):
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= STRATEGY =================

def get_signal(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=100)
        bars = bars[:-1]

        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]

        price = closes[-1]

        ema50 = ema(closes[-50:], 50)
        ema200 = ema(closes[-100:], 100)

        rsi_val = rsi(closes)
        vol_avg = sum(volumes[-20:]) / 20

        # BUY
        if price > ema50 and ema50 > ema200:
            if abs(price - ema50) / ema50 < 0.003:
                if 45 < rsi_val < 60 and volumes[-1] > vol_avg:
                    return price, "BUY"

        # SELL (trend break)
        if price < ema50:
            return price, "SELL"

        return price, "HOLD"

    except Exception as e:
        print(f"Error: {e}")
        return None, "HOLD"

# ================= TRADING =================

def position_size(price):
    return (virtual_wallet["USDT"] * RISK_PER_TRADE) / price

def close_trade(symbol, price):
    global wins, losses

    qty = positions[symbol]
    entry = entry_price[symbol]

    pnl = (price - entry) * qty
    virtual_wallet["USDT"] += qty * price

    if pnl > 0:
        wins += 1
    else:
        losses += 1

    trade_history.append(pnl)

    positions.pop(symbol)
    entry_price.pop(symbol)

# ================= JOB =================

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return

    for sym in SYMBOLS:
        price, signal = get_signal(sym)
        if not price:
            continue

        # BUY
        if signal == "BUY" and sym not in positions:
            qty = position_size(price)
            cost = qty * price

            if virtual_wallet["USDT"] >= cost:
                positions[sym] = qty
                entry_price[sym] = price
                virtual_wallet["USDT"] -= cost

                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🚀 BUY {sym}\n💰 Price: {price}"
                )

        # SELL / SL / TP
        if sym in positions:
            entry = entry_price[sym]

            if price <= entry * (1 - STOP_LOSS):
                close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"🛑 STOP LOSS {sym}")

            elif price >= entry * (1 + TAKE_PROFIT):
                close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"🎯 TAKE PROFIT {sym}")

            elif signal == "SELL":
                close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ SELL {sym}")

# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ]
    await update.message.reply_text(
        "🤖 BOT V3 RUNNING",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "stats":
        total_trades = wins + losses
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        pnl = sum(trade_history)

        msg = (
            f"💰 Balance: {virtual_wallet['USDT']:.2f} USDT\n"
            f"📊 Trades: {total_trades}\n"
            f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
            f"📈 PnL: {pnl:.2f} USDT\n"
            f"🎯 WinRate: {winrate:.1f}%"
        )
        await query.edit_message_text(msg)

    elif query.data == "positions":
        if not positions:
            await query.edit_message_text("📭 No Open Positions")
            return

        msg = "💼 Positions:\n"
        for sym, qty in positions.items():
            msg += f"{sym}: {qty:.4f}\n"
        await query.edit_message_text(msg)

# ================= MAIN =================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(trading_job, interval=60, first=5)

    print("BOT V3 RUNNING 🚀")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
