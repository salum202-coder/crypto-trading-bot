import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# ================= 🔑 1. CONFIG =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "ADA/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT", "DOT/USDT:USDT", "LTC/USDT:USDT"
]

# ================= ⚙️ 2. SETTINGS =================
RISK_PER_TRADE = 0.01      # المخاطرة 1%
LEVERAGE = 5               # الرافعة 5x
TAKE_PROFIT = 0.03         # هدف الربح 3%
STOP_LOSS = 0.015          # وقف الخسارة 1.5%
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("V9.3_Final")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

# ================= 📊 3. INDICATORS =================
def get_indicators(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=210)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        
        ema_50 = sum(closes[-50:]) / 50
        ema_200 = sum(closes[-200:]) / 200
        vol_ok = volumes[-1] > (sum([v[5] for v in bars[-20:]]) / 20) * 1.1
        
        return {
            "price": closes[-1],
            "ema_50": ema_50,
            "ema_200": ema_200,
            "vol_ok": vol_ok,
            "prev_high": max([b[2] for b in bars[-2:-1]]),
            "prev_low": min([b[3] for b in bars[-2:-1]])
        }
    except: return None

# ================= 🚪 4. EXIT MANAGER (إدارة الخروج) =================
async def monitor_exits(context):
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0: continue
            
            symbol = pos['symbol']
            side = pos['side']
            entry = float(pos['entryPrice'])
            mark = float(pos['markPrice'])
            
            # حساب نسبة الربح/الخسارة الحالية
            pnl_pct = (mark - entry) / entry if side == 'long' else (entry - mark) / entry
            
            if pnl_pct >= TAKE_PROFIT or pnl_pct <= -STOP_LOSS:
                close_side = 'sell' if side == 'long' else 'buy'
                exchange.create_market_order(symbol, close_side, contracts, params={'reduceOnly': True})
                await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"🔔 **إغلاق تلقائي:** {symbol}\nالنتيجة: {pnl_pct*100:.2f}%")
    except Exception as e:
        logger.error(f"Exit Manager Error: {e}")

# ================= 🛠️ 5. DASHBOARD UI =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 الرصيد", callback_data="btn_bal"), InlineKeyboardButton("📡 الرادار", callback_data="btn_radar")],
        [InlineKeyboardButton("🟢 قفل الربحانة", callback_data="close_win"), InlineKeyboardButton("🔴 قفل الخسرانة", callback_data="close_loss")],
        [InlineKeyboardButton("🚨 إغلاق الكل", callback_data="close_all")]
    ])

async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "btn_bal":
        bal = exchange.fetch_balance()['USDT']['free']
        await query.message.reply_text(f"💳 رصيدك المتوفر: {bal:.2f} USDT")
        
    elif query.data == "btn_radar":
        msg = "📡 **حالة السوق الحالية:**\n"
        for s in SYMBOLS:
            ind = get_indicators(s)
            t = "🟢" if ind and ind['ema_50'] > ind['ema_200'] else "🔴"
            msg += f"{t} {s.split('/')[0]}\n"
        await query.message.reply_text(msg)
        
    elif query.data.startswith("close_"):
        await handle_manual_close(query.data.split("_")[1], context)

async def handle_manual_close(mode, context):
    positions = exchange.fetch_positions()
    count = 0
    for pos in positions:
        contracts = float(pos.get('contracts', 0))
        if contracts == 0: continue
        pnl = float(pos.get('unrealizedPnl', 0))
        
        if mode == "all" or (mode == "win" and pnl > 0) or (mode == "loss" and pnl < 0):
            side = 'sell' if pos['side'] == 'long' else 'buy'
            exchange.create_market_order(pos['symbol'], side, contracts, params={'reduceOnly': True})
            count += 1
    await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"✅ تم إغلاق {count} صفقات بنجاح.")

# ================= 🤖 6. TRADING ENGINE =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    await monitor_exits(context) # أولاً: التأكد من الصفقات المفتوحة
    
    for sym in SYMBOLS:
        try:
            ind = get_indicators(sym)
            if not ind: continue
            
            # فحص لو فيه صفقة مفتوحة للعملة
            pos = exchange.fetch_positions([sym])
            if any(float(p.get('contracts', 0)) != 0 for p in pos): continue
            
            price = ind['price']
            signal = None
            
            # LONG
            if ind['ema_50'] > ind['ema_200'] and price > ind['ema_50'] and price > ind['prev_high'] and ind['vol_ok']:
                signal = "LONG"
            # SHORT
            elif ind['ema_50'] < ind['ema_200'] and price < ind['ema_50'] and price < ind['prev_low'] and ind['vol_ok']:
                signal = "SHORT"
            
            if signal:
                balance = exchange.fetch_balance()['USDT']['free']
                qty = (balance * RISK_PER_TRADE * LEVERAGE) / price
                exchange.set_leverage(LEVERAGE, sym)
                exchange.create_market_order(sym, 'buy' if signal == "LONG" else 'sell', qty, params={'positionSide': signal})
                await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"🚀 **دخول صفقة:** {sym} ({signal})\nالسعر: {price}")
        except: continue

# ================= 🚀 7. RUNNING =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 **القناص V9.3 نشط!**\nلوحة التحكم جاهزة:", reply_markup=get_main_keyboard())

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(dashboard_handler))
    app.job_queue.run_repeating(trading_job, interval=60)
    app.run_polling(drop_pending_updates=True)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"V9.3 LIVE")

if __name__ == "__main__": main()
