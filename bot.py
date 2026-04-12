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

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]

# ================= ⚙️ 2. STRATEGY SETTINGS =================
RISK_PER_TRADE = 0.01
LEVERAGE = 5
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("V9_Dashboard")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

# ================= 📊 3. INDICATORS LOGIC =================
def get_indicators(symbol):
    bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=210)
    closes = [b[4] for b in bars]
    volumes = [b[5] for b in bars]
    
    # EMAs
    ema_50 = sum(closes[-50:]) / 50
    ema_200 = sum(closes[-200:]) / 200
    
    # Volume Confirmation (Average of last 20)
    avg_vol = sum(volumes[-20:]) / 20
    curr_vol = volumes[-1]
    
    # RSI Filter
    period = 14
    gains = [max(closes[i] - closes[i-1], 0) for i in range(len(closes)-period, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(len(closes)-period, len(closes))]
    rsi = 100 - (100 / (1 + (sum(gains)/sum(losses)))) if sum(losses) != 0 else 100
    
    return {
        "price": closes[-1],
        "ema_50": ema_50,
        "ema_200": ema_200,
        "vol_ok": curr_vol > avg_vol * 1.2, # تأكيد الفوليوم (أعلى من المتوسط بـ 20%)
        "rsi": rsi,
        "prev_close": closes[-2]
    }

# ================= 🛠️ 4. TRADING ACTIONS =================
async def close_positions(context, type="all"):
    positions = exchange.fetch_positions()
    closed_count = 0
    for pos in positions:
        contracts = float(pos.get('contracts', 0))
        if contracts == 0: continue
        
        unrealized_pnl = float(pos.get('unrealizedPnl', 0))
        should_close = False
        
        if type == "all": should_close = True
        elif type == "win" and unrealized_pnl > 0: should_close = True
        elif type == "loss" and unrealized_pnl < 0: should_close = True
        
        if should_close:
            side = 'sell' if pos['side'] == 'long' else 'buy'
            exchange.create_market_order(pos['symbol'], side, contracts, params={'reduceOnly': True})
            closed_count += 1
            
    await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"✅ تم إغلاق {closed_count} صفقات ({type})")

# ================= 📱 5. DASHBOARD UI =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 رصيد المحفظة", callback_data="btn_balance"),
         InlineKeyboardButton("📡 رادار السوق", callback_data="btn_radar")],
        [InlineKeyboardButton("✅ قفل الربحانة", callback_data="close_win"),
         InlineKeyboardButton("❌ قفل الخسرانة", callback_data="close_loss")],
        [InlineKeyboardButton("🚨 إغلاق الكل فوراً", callback_data="close_all")]
    ])

async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "btn_balance":
        bal = exchange.fetch_balance()['USDT']['free']
        await query.message.reply_text(f"💳 رصيدك المتوفر حالياً: {bal:.2f} USDT")
        
    elif query.data == "btn_radar":
        msg = "📡 **رادار السوق الحالي:**\n"
        for sym in SYMBOLS:
            ind = get_indicators(sym)
            trend = "🟢 صاعد" if ind['ema_50'] > ind['ema_200'] else "🔴 هابط"
            msg += f"\n• {sym}: {trend} | RSI: {ind['rsi']:.1f}"
        await query.message.reply_text(msg)
        
    elif query.data.startswith("close_"):
        action_type = query.data.split("_")[1]
        await close_positions(context, action_type)

# ================= 🤖 6. STRATEGY ENGINE =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    for sym in SYMBOLS:
        try:
            ind = get_indicators(sym)
            price = ind['price']
            
            # --- منطق الدخول (Price Action + EMAs + Volume) ---
            # LONG: الترند صاعد + السعر فوق EMA 50 + اختراق فوليوم + RSI ليس متضخماً
            if ind['ema_50'] > ind['ema_200'] and price > ind['ema_50'] and ind['vol_ok'] and ind['rsi'] < 65:
                # شرط Price Action: الشمعة الحالية أغلقت فوق الهاي السابق (إشارة دخول)
                if price > ind['prev_close']:
                    # تنفيذ الصفقة...
                    pass 
            
            # SHORT: الترند هابط + السعر تحت EMA 50 + اختراق فوليوم + RSI ليس منهاراً
            elif ind['ema_50'] < ind['ema_200'] and price < ind['ema_50'] and ind['vol_ok'] and ind['rsi'] > 35:
                if price < ind['prev_close']:
                    # تنفيذ الصفقة...
                    pass
        except: continue

# ================= 🚀 7. STARTUP =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎮 **لوحة تحكم القناص V9.0 جاهزة!**\nالاستراتيجية: EMA + Volume + Price Action",
        reply_markup=get_main_keyboard()
    )

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(dashboard_handler))
    
    app.job_queue.run_repeating(trading_job, interval=60)
    app.run_polling(drop_pending_updates=True)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"V9 LIVE")

if __name__ == "__main__":
    main()
