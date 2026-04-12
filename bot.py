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

# ================= ⚙️ 2. SETTINGS (Expert Adjusted) =================
RISK_PER_TRADE = 0.01      # 1% مخاطرة بناءً على نصيحة الخبير
LEVERAGE = 5               
STOP_LOSS_PCT = 0.02       # 2% وقف خسارة
TAKE_PROFIT_PCT = 0.04     # 4% هدف ربح
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("pro_sniper_v6")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
})

# ================= 📊 3. INDICATORS =================
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
    sma = sum(data[-period:]) / period
    std = (sum((x - sma) ** 2 for x in data[-period:]) / period) ** 0.5
    return sma + (std_dev * std), sma, sma - (std_dev * std)

# ================= 🧰 4. HELPERS =================
def has_open_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        return any(float(p.get('contracts', 0)) != 0 for p in positions)
    except Exception as e:
        logger.error(f"Error checking positions for {symbol}: {e}")
        return True # للأمان نفترض وجود صفقة عند الخطأ

# ================= 🤖 5. TRADING JOB =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    for sym in SYMBOLS:
        try:
            # 1. جلب البيانات
            bars = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=50)
            closes = [b[4] for b in bars]
            price = closes[-1]
            
            # 2. فحص لو فيه صفقة مفتوحة
            if has_open_position(sym): continue

            # 3. حساب المؤشرات
            upper, mid, lower = calculate_bollinger(closes)
            rsi_val = calculate_rsi(closes)
            
            # 4. اتخاذ القرار
            signal = None
            if price <= lower and rsi_val < 35: signal = "LONG"
            elif price >= upper and rsi_val > 65: signal = "SHORT"
            
            if signal:
                # 5. حساب الكمية بناءً على الرصيد الفعلي
                balance = exchange.fetch_balance()['USDT']['free']
                if balance < 10: continue
                
                # الكمية = (الرصيد * نسبة المخاطرة * الرافعة) / السعر
                qty = (balance * RISK_PER_TRADE * LEVERAGE) / price
                
                # 6. تنفيذ الأوردر الفعلي
                exchange.set_leverage(LEVERAGE, sym)
                side = 'buy' if signal == "LONG" else 'sell'
                order = exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚀 **V6.1 ENTRY: {sym}**\nSignal: {signal}\nPrice: {price}\nQty: {qty:.4f}\nADX/RSI: {rsi_val:.1f}"
                )
        except Exception as e:
            logger.error(f"Error in {sym}: {str(e)}")

# ================= 🚀 6. RUNNING =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"PRO V6.1 IS LIVE")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    print("🚀 PRO SNIPER V6.1 STARTED...")
    app.run_polling()

if __name__ == "__main__":
    main()
