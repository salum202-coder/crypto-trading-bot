import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import ccxt
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= 🔑 1. CONFIG =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]

# ================= ⚙️ 2. PRO SETTINGS =================
RISK_PER_TRADE = 0.01      
LEVERAGE = 5               
STOP_LOSS_PCT = 0.02       
TAKE_PROFIT_PCT = 0.04     
TIMEFRAME = "15m"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("V7_Pro_Fortress")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "options": {"defaultType": "swap"},
    "enableRateLimit": True
})

# ================= 📊 3. INDICATORS =================
def calculate_indicators(closes):
    # RSI
    period = 14
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rsi = 100 - (100 / (1 + (avg_gain/avg_loss))) if avg_loss != 0 else 100
    
    # Bollinger Bands
    sma_20 = sum(closes[-20:]) / 20
    std = (sum((x - sma_20) ** 2 for x in closes[-20:]) / 20) ** 0.5
    upper, lower = sma_20 + (2 * std), sma_20 - (2 * std)
    
    # EMA 200 (Trend Confirmation)
    ema_200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else sma_20
    
    return rsi, upper, lower, ema_200

# ================= 🚪 4. EXIT STRATEGY (الإغلاق التلقائي) =================
async def manage_exits(context: ContextTypes.DEFAULT_TYPE):
    """وظيفة تراقب الصفقات وتغلقها عند الهدف أو الوقف"""
    try:
        positions = exchange.fetch_positions()
        for pos in positions:
            contracts = float(pos.get('contracts', 0))
            if contracts == 0: continue
            
            symbol = pos['symbol']
            entry_price = float(pos['entryPrice'])
            current_price = float(pos['markPrice'])
            side = pos['side'] # long or short
            
            # حساب الربح/الخسارة بالنسبة المئوية
            pnl_pct = (current_price - entry_price) / entry_price if side == 'long' else (entry_price - current_price) / entry_price
            
            should_close = False
            reason = ""
            
            if pnl_pct >= TAKE_PROFIT_PCT:
                should_close, reason = True, "✅ Take Profit Hit"
            elif pnl_pct <= -STOP_LOSS_PCT:
                should_close, reason = True, "❌ Stop Loss Hit"
            
            if should_close:
                close_side = 'sell' if side == 'long' else 'buy'
                exchange.create_market_order(symbol, close_side, contracts, params={'reduceOnly': True})
                await context.bot.send_message(chat_id=ENV_CHAT_ID, text=f"🔔 **إغلاق صفقة:** {symbol}\nالسبب: {reason}\nالربح/الخسارة: {pnl_pct*100:.2f}%")
    except Exception as e:
        logger.error(f"Error in Exit Manager: {e}")

# ================= 🤖 5. TRADING LOGIC =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    # أولاً: تشغيل مدير الإغلاق
    await manage_exits(context)

    for sym in SYMBOLS:
        try:
            bars = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=250)
            closes = [b[4] for b in bars]
            price = closes[-1]
            
            rsi, upper, lower, ema_200 = calculate_indicators(closes)
            
            # --- شروط الدخول المطورة (V7) ---
            signal = None
            # LONG: السعر عند الحد السفلي + RSI تشبع + السعر فوق EMA 200 (ترند صاعد)
            if price <= lower and rsi < 35 and price > ema_200:
                signal = "LONG"
            # SHORT: السعر عند الحد العلوي + RSI تضخم + السعر تحت EMA 200 (ترند هابط)
            elif price >= upper and rsi > 65 and price < ema_200:
                signal = "SHORT"
            
            if signal:
                # التأكد من عدم وجود صفقة مفتوحة
                pos = exchange.fetch_positions([sym])
                if any(float(p.get('contracts', 0)) != 0 for p in pos): continue
                
                balance = exchange.fetch_balance()['USDT']['free']
                qty = (balance * RISK_PER_TRADE * LEVERAGE) / price
                
                exchange.create_market_order(sym, 'buy' if signal == "LONG" else 'sell', qty, params={'positionSide': signal})
                await context.bot.send_message(chat_id=chat_id, text=f"🚀 **دخول V7:** {sym} ({signal})\nالسعر: {price}\nتأكيد الترند: ✅")
        except Exception as e:
            logger.error(f"Error processing {sym}: {e}")

# ================= 📱 6. DASHBOARD COMMANDS =================
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()['USDT']['free']
        pos = exchange.fetch_positions()
        active_trades = [p['symbol'] for p in pos if float(p.get('contracts', 0)) != 0]
        
        msg = (
            f"📊 **لوحة تحكم القناص V7**\n\n"
            f"💰 الرصيد المتاح: {balance:.2f} USDT\n"
            f"📦 الصفقات المفتوحة: {len(active_trades)}\n"
            f"🔗 العملات النشطة: {', '.join(active_trades) if active_trades else 'لا يوجد'}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في جلب البيانات: {e}")

# ================= 🚀 7. RUNNING =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"PRO V7 IS LIVE")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🎯 V7 Pro Fortress Activated!")))
    app.add_handler(CommandHandler("status", status))
    
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
