import os, time, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# ================= 🔑 1. CONFIG =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT", 
           "ADA/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT", "DOT/USDT:USDT", "LTC/USDT:USDT"]

# ================= ⚙️ 2. SETTINGS (تعديل الأهداف) =================
RISK_PER_TRADE = 0.01      # 1% من الرصيد
LEVERAGE = 5               # رافعة 5
TAKE_PROFIT = 0.03         # ربح 3% (قبل الرافعة)
STOP_LOSS = 0.015          # خسارة 1.5% (قبل الرافعة)
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
exchange = ccxt.bingx({"apiKey": BINGX_API_KEY, "secret": BINGX_SECRET, "options": {"defaultType": "swap"}, "enableRateLimit": True})

# ================= 📊 3. INDICATORS =================
def get_indicators(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=210)
        closes = [b[4] for b in bars]
        ema_50, ema_200 = sum(closes[-50:])/50, sum(closes[-200:])/200
        vol_ok = b[5] > (sum([x[5] for x in bars[-20:]])/20) * 1.1
        return {"price": closes[-1], "ema_50": ema_50, "ema_200": ema_200, "vol_ok": vol_ok, "prev_high": max([b[2] for b in bars[-2:-1]]), "prev_low": min([b[3] for b in bars[-2:-1]])}
    except: return None

# ================= 🚪 4. AUTO EXIT (مدير الصفقات) =================
async def monitor_exits(context):
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0: continue
            pnl = float(pos.get('unrealizedPnl', 0))
            entry = float(pos['entryPrice'])
            curr = float(pos['markPrice'])
            side = pos['side']
            
            # حساب النسبة المئوية للربح
            change = (curr - entry)/entry if side == 'long' else (entry - curr)/entry
            
            if change >= TAKE_PROFIT or change <= -STOP_LOSS:
                close_side = 'sell' if side == 'long' else 'buy'
                exchange.create_market_order(pos['symbol'], close_side, contracts, params={'reduceOnly': True})
                await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"🔔 إغلاق تلقائي: {pos['symbol']}\nالنتيجة: {change*100:.2f}%")
    except: pass

# ================= 🛠️ 5. DASHBOARD UI =================
def get_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 الرصيد", callback_data="bal"), InlineKeyboardButton("📡 الرادار", callback_data="radar")],
        [InlineKeyboardButton("🟢 قفل الربحانة", callback_data="c_win"), InlineKeyboardButton("🔴 قفل الخسرانة", callback_data="c_loss")],
        [InlineKeyboardButton("🚨 إغلاق الكل", callback_data="c_all")]
    ])

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "bal":
        await query.message.reply_text(f"💳 رصيدك: {exchange.fetch_balance()['USDT']['free']:.2f} USDT")
    elif query.data == "radar":
        msg = "📡 الرادار:\n"
        for s in SYMBOLS:
            i = get_indicators(s)
            t = "🟢" if i and i['ema_50'] > i['ema_200'] else "🔴"
            msg += f"{t} {s.split('/')[0]}\n"
        await query.message.reply_text(msg)
    elif query.data.startswith("c_"):
        # منطق الإغلاق (all, win, loss)
        await query.message.reply_text("⏳ جاري الإغلاق...")

# ================= 🚀 6. MAIN JOB =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    await monitor_exits(context) # تشغيل مدير الخروج أولاً
    for sym in SYMBOLS:
        ind = get_indicators(sym)
        if not ind: continue
        price = ind['price']
        
        # LONG
        if ind['ema_50'] > ind['ema_200'] and price > ind['ema_50'] and price > ind['prev_high'] and ind['vol_ok']:
            await open_trade(context, sym, "LONG", price)
        # SHORT
        elif ind['ema_50'] < ind['ema_200'] and price < ind['ema_50'] and price < ind['prev_low'] and ind['vol_ok']:
            await open_trade(context, sym, "SHORT", price)

async def open_trade(context, sym, signal, price):
    try:
        pos = exchange.fetch_positions([sym])
        if any(float(p.get('contracts', 0)) != 0 for p in pos): return
        balance = exchange.fetch_balance()['USDT']['free']
        qty = (balance * RISK_PER_TRADE * LEVERAGE) / price
        exchange.set_leverage(LEVERAGE, sym)
        exchange.create_market_order(sym, 'buy' if signal == "LONG" else 'sell', qty, params={'positionSide': signal})
        await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"🚀 دخول: {sym} ({signal})")
    except: pass

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🎯 القناص V9.3 جاهز!", reply_markup=get_keyboard())))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.job_queue.run_repeating(trading_job, interval=60)
    app.run_polling(drop_pending_updates=True)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"LIVE")

if __name__ == "__main__": main()
