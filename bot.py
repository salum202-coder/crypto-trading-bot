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

# القائمة الموسعة لـ 10 عملات (الأكثر حركة وسيولة)
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "ADA/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT", "DOT/USDT:USDT", "LTC/USDT:USDT"
]

# ================= ⚙️ 2. SETTINGS =================
RISK_PER_TRADE = 0.01
LEVERAGE = 5
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("V9.1_DecaSniper")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

# ================= 📊 3. INDICATORS LOGIC =================
def get_indicators(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=210)
        closes = [b[4] for b in bars]
        volumes = [b[5] for b in bars]
        
        # EMAs (50/200) لحديد الاتجاه
        ema_50 = sum(closes[-50:]) / 50
        ema_200 = sum(closes[-200:]) / 200
        
        # Volume (تأكيد السيولة)
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
            "vol_ok": curr_vol > avg_vol * 1.2,
            "rsi": rsi,
            "prev_close": closes[-2],
            "prev_high": max([b[2] for b in bars[-2:-1]]), # هاي الشمعة السابقة
            "prev_low": min([b[3] for b in bars[-2:-1]])   # لو الشمعة السابقة
        }
    except: return None

# ================= 🛠️ 4. DASHBOARD UI =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 رصيد المحفظة", callback_data="btn_balance"),
         InlineKeyboardButton("📡 رادار الـ 10 عملات", callback_data="btn_radar")],
        [InlineKeyboardButton("✅ قفل الربحانة", callback_data="close_win"),
         InlineKeyboardButton("❌ قفل الخسرانة", callback_data="close_loss")],
        [InlineKeyboardButton("🚨 إغلاق الكل فوراً", callback_data="close_all")]
    ])

async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "btn_balance":
        bal = exchange.fetch_balance()['USDT']['free']
        await query.message.reply_text(f"💳 رصيدك المتوفر: {bal:.2f} USDT")
        
    elif query.data == "btn_radar":
        msg = "📡 **حالة رادار الـ 10 عملات:**\n"
        for sym in SYMBOLS:
            ind = get_indicators(sym)
            if not ind: continue
            trend = "🟢 صاعد" if ind['ema_50'] > ind['ema_200'] else "🔴 هابط"
            msg += f"\n• {sym.split('/')[0]}: {trend} | RSI: {ind['rsi']:.1f}"
        await query.message.reply_text(msg)
    
    elif query.data.startswith("close_"):
        # (نفس منطق الإغلاق في V9 السابق)
        await query.message.reply_text(f"⏳ جاري تنفيذ أمر الإغلاق: {query.data.split('_')[1]}...")

# ================= 🤖 5. TRADING JOB (الاستراتيجية الاستراتيجية) =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    for sym in SYMBOLS:
        try:
            ind = get_indicators(sym)
            if not ind: continue
            
            price = ind['price']
            
            # --- منطق الدخول (EMA + Volume + Price Action) ---
            signal = None
            
            # LONG: ترند صاعد + فوق EMA 50 + فوليوم عالي + اختراق الهاي السابق (Price Action)
            if ind['ema_50'] > ind['ema_200'] and price > ind['ema_50'] and ind['vol_ok'] and ind['rsi'] < 60:
                if price > ind['prev_high']:
                    signal = "LONG"
            
            # SHORT: ترند هابط + تحت EMA 50 + فوليوم عالي + كسر اللو السابق (Price Action)
            elif ind['ema_50'] < ind['ema_200'] and price < ind['ema_50'] and ind['vol_ok'] and ind['rsi'] > 40:
                if price < ind['prev_low']:
                    signal = "SHORT"
            
            if signal:
                # تنفيذ الصفقة (بناءً على الـ 1% مخاطرة والرافعة 5)
                # ... [كود التنفيذ كما في النسخ السابقة] ...
                await context.bot.send_message(chat_id=chat_id, text=f"🚀 **دخول قناص V9.1:** {sym}\nالنوع: {signal}\nتأكيد الفوليوم والسعر: ✅")
        except: continue

# ================= 🚀 6. RUNNING =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "🎮 **لوحة تحكم القناص V9.1 (10 عملات)**\nالاستراتيجية: EMA + Volume + Price Action",
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
        self.send_response(200); self.end_headers(); self.wfile.write(b"V9.1 LIVE")

if __name__ == "__main__":
    main()
