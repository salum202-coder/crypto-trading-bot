import ccxt
import asyncio
import nest_asyncio
from telegram.ext import Application, CommandHandler, ContextTypes

nest_asyncio.apply()

TOKEN = 'حط_التوكن_هنا'
CHAT_ID = '165888578'

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
last_signals = {symbol: "Neutral" for symbol in SYMBOLS}

virtual_wallet = {
    "USDT": 10000.0,
    "BTC/USDT": 0.0,
    "ETH/USDT": 0.0,
    "SOL/USDT": 0.0
}

TRADE_AMOUNT_USDT = 3300.0 

exchange = ccxt.kucoin()

def calculate_sma(data, period):
    if len(data) < period: return None
    return sum(data[-period:]) / period

def check_trend(highs, lows, closes):
    if closes[-1] > highs[-3]: return "Up"
    if closes[-1] < lows[-3]: return "Down"
    return "Side"

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def get_analysis(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=70)
        closed_bars = bars[:-1] 
        
        closes = [b[4] for b in closed_bars]
        highs = [b[2] for b in closed_bars]
        lows = [b[3] for b in closed_bars]
        
        current_price = closes[-1]
        sma_20 = calculate_sma(closes, 20)
        trend = check_trend(highs, lows, closes)
        rsi_14 = calculate_rsi(closes, 14)
        
        signal = "Neutral"
        if sma_20 and rsi_14:
            if current_price > sma_20 and trend == "Up" and rsi_14 > 50:
                signal = "🚀 Strong Buy"
            elif current_price < sma_20 and trend == "Down" and rsi_14 < 50:
                signal = "⚠️ Strong Sell"
            
        return current_price, signal, rsi_14
    except Exception as e:
        print(f"Error: {e}")
        return None, "Error", None

async def price_command(update, context):
    text = "📊 التحليل الفني:\n\n"
    for sym in SYMBOLS:
        price, sig, rsi = await get_analysis(sym)
        rsi_text = f"{rsi:.2f}" if rsi else "N/A"
        text += f"{sym}: {price}\nRSI: {rsi_text}\nSignal: {sig}\n\n"
    await update.message.reply_text(text)

async def wallet_command(update, context):
    text = f"💼 USDT: {virtual_wallet['USDT']:.2f}\n"
    for sym in SYMBOLS:
        text += f"{sym}: {virtual_wallet[sym]}\n"
    await update.message.reply_text(text)

async def monitor_logic(context: ContextTypes.DEFAULT_TYPE):
    global last_signals, virtual_wallet
    for sym in SYMBOLS:
        price, current_sig, rsi = await get_analysis(sym)
        if not price: continue

        if current_sig == "🚀 Strong Buy" and last_signals[sym] != "🚀 Strong Buy":
            if virtual_wallet["USDT"] >= TRADE_AMOUNT_USDT and virtual_wallet[sym] == 0:
                amount = TRADE_AMOUNT_USDT / price
                virtual_wallet[sym] = amount
                virtual_wallet["USDT"] -= TRADE_AMOUNT_USDT
                await context.bot.send_message(chat_id=CHAT_ID, text=f"BUY {sym} @ {price}")
                last_signals[sym] = "🚀 Strong Buy"

        elif current_sig == "⚠️ Strong Sell" and last_signals[sym] != "⚠️ Strong Sell":
            if virtual_wallet[sym] > 0:
                value = virtual_wallet[sym] * price
                virtual_wallet["USDT"] += value
                virtual_wallet[sym] = 0.0
                await context.bot.send_message(chat_id=CHAT_ID, text=f"SELL {sym} @ {price}")
                last_signals[sym] = "⚠️ Strong Sell"

async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("wallet", wallet_command))

    # ✅ الحل هنا
    job_queue = application.job_queue
    job_queue.run_repeating(monitor_logic, interval=60, first=10)

    print("Bot is running...")
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())