import os
import ccxt
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler
)

# ================= CONFIG =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

# التعديل السحري: غيرنا المنصة إلى KuCoin لتجنب حظر السيرفرات الأمريكية
exchange = ccxt.kucoin({
    "enableRateLimit": True
})

virtual_wallet = {"USDT": 10000.0}
positions = {}
entry_price = {}

trade_history = []
wins = 0
losses = 0

RISK_PER_TRADE = 0.05
STOP_LOSS = 0.015
TAKE_PROFIT = 0.03

# ================= INDICATORS =================
def ema(data, period):
    if len(data) < period: return data[-1]
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_sar(highs, lows, af=0.02, max_af=0.2):
    if len(highs) < 2: return lows[-1]
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
        bars = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=100)
        bars = bars[:-1] 

        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        
        price = closes[-1]
        ema50 = ema(closes, 50)
        sar_val = calculate_sar(highs, lows)

        if price > ema50 and sar_val < price:
            return price, "BUY", sar_val, ema50

        if sar_val > price:
            return price, "SELL", sar_val, ema50

        return price, "HOLD", sar_val, ema50

    except Exception as e:
        print(f"Error for {symbol}: {e}")
        return None, "HOLD", None, None

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
    active_chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    
    if not active_chat_id:
        return

    for sym in SYMBOLS:
        price, signal, sar_val, ema50 = get_signal(sym)
        if not price:
            continue

        if signal == "BUY" and sym not in positions:
            qty = position_size(price)
            cost = qty * price
            
            if virtual_wallet["USDT"] >= cost:
                positions[sym] = qty
                entry_price[sym] = price
                virtual_wallet["USDT"] -= cost
                
                msg = f"🟢 **TEST BUY OPENED** 🟢\n🪙 Coin: {sym}\n💵 Price: {price:.2f} $\n🎯 TP: {(price * (1 + TAKE_PROFIT)):.2f} $\n🛑 SL: {(price * (1 - STOP_LOSS)):.2f} $"
                await context.bot.send_message(chat_id=active_chat_id, text=msg)

        if sym in positions:
            entry = entry_price[sym]
            
            if price <= entry * (1 - STOP_LOSS):
                pnl = close_trade(sym, price)
                await context.bot.send_message(chat_id=active_chat_id, text=f"🛑 **STOP LOSS HIT**\n🪙 {sym} closed at {price:.2f} $\n📉 PnL: {pnl:.2f} $")
            
            elif price >= entry * (1 + TAKE_PROFIT):
                pnl = close_trade(sym, price)
                await context.bot.send_message(chat_id=active_chat_id, text=f"🎯 **TAKE PROFIT HIT**\n🪙 {sym} closed at {price:.2f} $\n📈 PnL: {pnl:.2f} $")
            
            elif signal == "SELL":
                pnl = close_trade(sym, price)
                icon = "📈" if pnl > 0 else "📉"
                await context.bot.send_message(chat_id=active_chat_id, text=f"⚠️ **TREND REVERSED (SAR)**\n🪙 {sym} closed at {price:.2f} $\n{icon} PnL: {pnl:.2f} $")

# ================= COMMANDS & BUTTONS =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 فحص السوق الآن", callback_data="scan")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "✅ **تم الربط بنجاح!**\nالبوت مستقر الآن. اضغط على 'فحص السوق الآن' لترى المؤشرات مباشرة:",
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "scan":
        msg = "📡 **رادار السوق المباشر (1m):**\n\n"
        for sym in SYMBOLS:
            price, signal, sar_val, ema50 = get_signal(sym)
            if price:
                status = "✅ يشتري الآن" if signal == "BUY" else "⏳ ننتظر الفرصة"
                msg += f"🪙 {sym}\n💵 السعر: {price:.2f}\n📊 خط EMA50: {ema50:.2f}\n🎯 نقطة SAR: {sar_val:.2f}\n🤖 حالة البوت: {status}\n---\n"
            else:
                msg += f"⚠️ {sym}: جاري تحميل البيانات...\n---\n"
        
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "stats":
        total_trades = wins + losses
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        pnl = sum(trade_history)
        msg = f"💰 Balance: {virtual_wallet['USDT']:.2f} USDT\n📊 Trades: {total_trades}\n✅ Wins: {wins} | ❌ Losses: {losses}\n💵 Net PnL: {pnl:.2f} $"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "positions":
        if not positions:
            await query.edit_message_text("📭 لا توجد صفقات مفتوحة حالياً.", reply_markup=get_main_keyboard())
            return
        msg = "💼 **Active Positions:**\n\n"
        for sym, qty in positions.items():
            msg += f"🪙 {sym}\n💵 Entry: {entry_price[sym]:.2f} $\n---\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

# ================= DUMMY SERVER =================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# ================= MAIN =================
def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return

    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(trading_job, interval=60, first=5)

    print("🚀 BOT STARTED SUCCESSFULLY (KuCoin)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
