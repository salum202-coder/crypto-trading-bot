import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= 🔑 1. CONFIG (المفاتيح) =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

# قائمة العملات التي سيراقبها البوت
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]

# ================= ⚙️ 2. SETTINGS (إعدادات الخبير) =================
RISK_PER_TRADE = 0.01      # 1% مخاطرة من رأس المال (بناءً على نصيحة الخبير)
LEVERAGE = 5               # الرافعة المالية
TIMEFRAME = "15m"          # فريم 15 دقيقة (للحصول على صفقات أكثر)

# إعداد اللوغز (عشان نشوف كل صغيرة وكبيرة في Render)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("V6.2_Final")

# إعداد المنصة
exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

# ================= 📊 3. INDICATORS (المؤشرات) =================
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_bollinger(data, period=20, std_dev=2):
    if len(data) < period: return None, None, None
    sma = sum(data[-period:]) / period
    std = (sum((x - sma) ** 2 for x in data[-period:]) / period) ** 0.5
    return sma + (std_dev * std), sma, sma - (std_dev * std)

# ================= 🧰 4. HELPERS (أدوات الحماية) =================
def has_open_position(symbol):
    """فحص لو فيه صفقة مفتوحة عشان ما نفتح ثانية على نفس العملة"""
    try:
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if float(p.get('contracts', 0)) != 0:
                return True
        return False
    except Exception as e:
        logger.error(f"❌ خطأ في فحص الصفقات لـ {symbol}: {e}")
        return True

# ================= 🤖 5. TRADING LOGIC (قلب البوت) =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    for sym in SYMBOLS:
        try:
            # 1. جلب البيانات
            bars = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=50)
            closes = [b[4] for b in bars]
            price = closes[-1]
            
            # 2. فحص الصفقات الحالية
            if has_open_position(sym):
                continue

            # 3. حساب المؤشرات (استراتيجية البولينجر)
            upper, mid, lower = calculate_bollinger(closes)
            rsi_val = calculate_rsi(closes)
            
            # 4. شروط الدخول (V6 الصارمة)
            signal = None
            if price <= lower and rsi_val < 35:
                signal = "LONG"
            elif price >= upper and rsi_val > 65:
                signal = "SHORT"
            
            if signal:
                # 5. إدارة رأس المال وحجم الصفقة
                balance_data = exchange.fetch_balance()
                balance = balance_data['USDT']['free']
                
                if balance < 10: # حماية لو الرصيد قليل
                    continue
                
                # معادلة حساب الكمية (الرصيد * المخاطرة * الرافعة) / السعر
                qty = (balance * RISK_PER_TRADE * LEVERAGE) / price
                
                # 6. تنفيذ الأوردر الحقيقي
                exchange.set_leverage(LEVERAGE, sym)
                side = 'buy' if signal == "LONG" else 'sell'
                
                # فتح الصفقة بماركت أوردر
                order = exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                
                # إرسال التنبيه للتيليجرام
                msg = (
                    f"🚀 **دخول صفقة حقيقية (V6.2)**\n"
                    f"🔹 العملة: {sym}\n"
                    f"🔹 النوع: {signal}\n"
                    f"🔹 السعر: {price}\n"
                    f"🔹 الكمية: {qty:.4f}\n"
                    f"📊 RSI: {rsi_val:.1f}"
                )
                await context.bot.send_message(chat_id=chat_id, text=msg)
                logger.info(f"✅ تم تنفيذ صفقة {signal} على {sym}")

        except Exception as e:
            logger.error(f"❌ خطأ أثناء معالجة {sym}: {str(e)}")

# ================= 📱 6. TELEGRAM HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الرد على أمر البداية وتخزين Chat ID"""
    chat_id = update.effective_chat.id
    context.bot_data["chat_id"] = chat_id
    logger.info(f"✅ تم تفعيل البوت من Chat ID: {chat_id}")
    await update.message.reply_text("🎯 **تم تفعيل القناص V6.2 بنجاح!**\nالاستراتيجية: Bollinger Bands + RSI\nالفريم: 15 دقيقة.")

# ================= 🚀 7. RUNNING =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"V6.2 PRO IS LIVE")

def main():
    # سيرفر الصحة لمنع Render من إيقاف البوت
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    
    # بناء تطبيق التيليجرام
    app = ApplicationBuilder().token(TOKEN).build()
    
    # إضافة الأوامر
    app.add_handler(CommandHandler("start", start))
    
    # جدولة فحص السوق كل دقيقة
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    
    print("🚀 البوت بدأ العمل في وضع القنص الاحترافي...")
    
    # الحل السحري لمشكلة الـ Conflict: drop_pending_updates=True
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
