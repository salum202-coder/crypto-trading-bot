import os
import ccxt
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

RISK_PER_TRADE = 0.05  # الدخول بـ 5% من المحفظة
STOP_LOSS = 0.015      # وقف الخسارة 1.5%
TAKE_PROFIT = 0.03     # أخذ الربح 3%

# ================= INDICATORS =================

def ema(data, period):
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def macd(closes):
    macd_line = []
    ema12 = [ema(closes[:i+1], 12) for i in range(len(closes))]
    ema26 = [ema(closes[:i+1], 26) for i in range(len(closes))]
    
    for i in range(len(closes)):
        macd_line.append(ema12[i] - ema26[i])
        
    signal_line = [ema(macd_line[:i+1], 9) for i in range(len(macd_line))]
    return macd_line[-1], signal_line[-1]

def calculate_sar(highs, lows, af=0.02, max_af=0.2):
    sar = [0.0] * len(highs)
    is_long = True
    ep = highs[0]
    cur_af = af
    sar[0] = lows[0] - (highs[0] - lows[0])
    
    for i in range(1, len(highs)):
        sar[i] = sar[i-1] + cur_af * (ep - sar[i-1])
        if is_long:
            if lows[i] < sar[i]:
                is_long = False
                sar[i] = ep
                ep = lows[i]
                cur_af = af
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    cur_af = min(cur_af + af, max_af)
                sar[i] = min(sar[i], lows[i-1])
                if i > 1: sar[i] = min(sar[i], lows[i-2])
        else:
            if highs[i] > sar[i]:
                is_long = True
                sar[i] = ep
                ep = highs[i]
                cur_af = af
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    cur_af = min(cur_af + af, max_af)
                sar[i] = max(sar[i], highs[i-1])
                if i > 1: sar[i] = max(sar[i], highs[i-2])
    return sar[-1]

# ================= STRATEGY =================

def get_signal(symbol):
    try:
        # فريم 1 دقيقة لفتح صفقات سريعة للتجربة
        bars = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=100)
        bars = bars[:-1] 

        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        
        price = closes[-1]
        
        ema50 = ema(closes, 50)
        sar_val = calculate_sar(highs, lows)

        # ====== شروط الشراء (مخففة جداً للتجربة) ======
        # السعر أعلى من الـ EMA 50 و نقطة الـ SAR أسفل السعر
        if price > ema50 and sar_val < price:
            return price, "BUY", sar_val

        # ====== شروط البيع / الخروج ======
        # نقطة الـ SAR أصبحت أعلى السعر
        if sar_val > price:
            return price, "SELL", sar_val

        return price, "HOLD", sar_val

    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None, "HOLD", None

# ================= TRADING =================

def position_size(price):
    return (virtual_wallet["USDT"] * RISK_PER_TRADE) / price

def close_trade(symbol, price):
    global wins, losses
    qty = positions[symbol]
    entry = entry_price[symbol]
    
    pnl = (price - entry) * qty
    virtual_wallet["USDT"] += qty * price
    
    if pnl > 0: wins += 1
    else: losses += 1
        
    trade_history.append(pnl)
    positions.pop(symbol)
    entry_price.pop(symbol)
    return pnl

# ================= JOB QUEUE =================

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return

    for sym in SYMBOLS:
        price, signal, sar_val = get_signal(sym)
        if not price:
            continue

        if signal == "BUY" and sym not in positions:
            qty = position_size(price)
            cost = qty * price
            
            if virtual_wallet["USDT"] >= cost:
                positions[sym] = qty
                entry_price[sym] = price
                virtual_wallet["USDT"] -= cost
                
                msg = f"🟢 **TEST BUY OPENED** 🟢\n" \
                      f"🪙 Coin: {sym}\n" \
                      f"💵 Price: {price:.2f} $\n" \
                      f"🎯 TP: {(price * (1 + TAKE_PROFIT)):.2f} $\n" \
                      f"🛑 SL: {(price * (1 - STOP_LOSS)):.2f} $"
                await context.bot.send_message(chat_id=CHAT_ID, text=msg)

        if sym in positions:
            entry = entry_price[sym]
            
            if price <= entry * (1 - STOP_LOSS):
                pnl = close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"🛑 **STOP LOSS HIT**\n🪙 {sym} closed at {price:.2f} $\n📉 PnL: {pnl:.2f} $")
            
            elif price >= entry * (1 + TAKE_PROFIT):
                pnl = close_trade(sym, price)
                await context.bot.send_message(chat_id=CHAT_ID, text=f"🎯 **TAKE PROFIT HIT**\n🪙 {sym} closed at {price:.2f} $\n📈 PnL: {pnl:.2f} $")
            
            elif signal == "SELL":
                pnl = close_trade(sym, price)
                icon = "📈" if pnl > 0 else "📉"
                await context.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ **TREND REVERSED (SAR)**\n🪙 {sym} closed at {price:.2f} $\n{icon} PnL: {pnl:.2f} $")

# ================= COMMANDS & BUTTONS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
        [InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ]
    await update.message.reply_text(
        "🤖 **Fast Test Bot Running! (1m timeframe)**\nالبوت يعمل الآن بالوضع السريع للتجربة، ترقب الإشارات...",
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
            f"💵 Net PnL: {pnl:.2f} $\n"
            f"🎯 WinRate: {winrate:.1f}%"
        )
        await query.edit_message_text(msg)

    elif query.data == "positions":
        if not positions:
            await query.edit_message_text("📭 لا توجد صفقات مفتوحة حالياً.")
            return

        msg = "💼 **Active Positions:**\n\n"
        for sym, qty in positions.items():
            entry = entry_price[sym]
            msg += f"🪙 {sym}\n🔸 Qty: {qty:.4f}\n💵 Entry: {entry:.2f} $\n---\n"
        await query.edit_message_text(msg)

# ================= MAIN =================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    # فحص السوق كل دقيقة ليتناسب مع التعديل السريع
    app.job_queue.run_repeating(trading_job, interval=60, first=5)

    print("BOT V4 (FAST TEST MODE) RUNNING 🚀")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
