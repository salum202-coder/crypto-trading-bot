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

# استخدام المتغيرات اللي كتبناها مع بعض في Render
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") 

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

# رجعناها لمنصة بينانس (Binance)
exchange = ccxt.binance()

virtual_wallet = {"USDT": 10000.0}
for s in SYMBOLS:
    virtual_wallet[s] = 0.0

entry_price = {s: None for s in SYMBOLS}
highest_price = {s: None for s in SYMBOLS}
last_signal = {s: None for s in SYMBOLS}

RISK_PER_TRADE = 0.02
TRAILING_STOP = 0.03

# ================= INDICATORS =================

def ema(data, period):
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def macd(closes):
    return ema(closes, 12) - ema(closes, 26)

def calculate_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period if period > 0 else 0
    avg_loss = sum(losses[:period]) / period if period > 0 else 0

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= ANALYSIS =================

def get_analysis(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        bars = bars[:-1]

        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]

        price = closes[-1]

        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)
        rsi = calculate_rsi(closes)
        macd_val = macd(closes)

        vol_avg = sum(volumes[-20:]) / 20

        if ema20 > ema50 and macd_val > 0 and rsi > 50 and volumes[-1] > vol_avg:
            return price, "BUY"
        elif ema20 < ema50 and macd_val < 0 and rsi < 50:
            return price, "SELL"

        return price, "NEUTRAL"
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None, "NEUTRAL"

# ================= RISK =================

def position_size(price):
    risk_amount = virtual_wallet["USDT"] * RISK_PER_TRADE
    return risk_amount / price

def close_trade(symbol, price):
    value = virtual_wallet[symbol] * price
    virtual_wallet["USDT"] += value
    virtual_wallet[symbol] = 0
    entry_price[symbol] = None
    highest_price[symbol] = None

# ================= BOT LOGIC (Job Queue) =================

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
        
    for sym in SYMBOLS:
        price, signal = get_analysis(sym)
        if not price:
            continue

        if signal == last_signal[sym]:
            continue

        if signal == "BUY" and virtual_wallet[sym] == 0:
            qty = position_size(price)
            cost = qty * price

            if virtual_wallet["USDT"] >= cost:
                virtual_wallet[sym] = qty
                virtual_wallet["USDT"] -= cost
                entry_price[sym] = price
                highest_price[sym] = price

                await context.bot.send_message(chat_id=CHAT_ID, text=f"🚀 BUY {sym} @ {price}")

        elif signal == "SELL" and virtual_wallet[sym] > 0:
            close_trade(sym, price)
            await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ SELL {sym} @ {price}")

        last_signal[sym] = signal

# ================= COMMANDS & BUTTONS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🤖 BOT READY - تم التفعيل بنجاح", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "stats":
        balance = virtual_wallet["USDT"]
        await query.edit_message_text(f"💰 Balance: {balance:.2f} USDT")
    
    elif query.data == "positions":
        msg = "📊 Positions:\n"
        for sym in SYMBOLS:
            msg += f"{sym}: {virtual_wallet[sym]}\n"
        await query.edit_message_text(msg)

# ================= MAIN =================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN Not Found!")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    # تشغيل حلقة التداول بأمان في الخلفية كل 60 ثانية
    job_queue = app.job_queue
    job_queue.run_repeating(trading_job, interval=60, first=5)

    print("BOT V2 RUNNING 🚀")
    
    # التشغيل بالطريقة العادية والآمنة
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
