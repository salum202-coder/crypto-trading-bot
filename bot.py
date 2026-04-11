import os
import ccxt
import time
import threading
import numpy as np
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler
)

# ================= CONFIG & API (سحب المفاتيح من Render) =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

# القائمة (11 عملة)
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'DOGE/USDT:USDT', 
    'LINK/USDT:USDT', 'DOT/USDT:USDT', 'ZEC/USDT:USDT'
]

# ================= TRADING SETTINGS (تعديلات الخبير) =================
RISK_PER_TRADE = 0.15  
LEVERAGE = 10          
STOP_LOSS_PCT = 0.03        # حزام أمان 3% (نصيحة الخبير)
TAKE_PROFIT_PCT = 0.015     # هدف الربح 1.5% (تعديلنا السابق)
ADX_THRESHOLD = 25     
COOLDOWN_TIME = 3600        # ساعة راحة بعد الخسارة

trade_history = []
wins = 0
losses = 0
positions_virtual = {} 
cooldown_tracker = {}

try:
    exchange = ccxt.bingx({
        'apiKey': BINGX_API_KEY,
        'secret': BINGX_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    exchange.load_markets() 
    print("✅ Successfully connected to BingX API")
except Exception as e:
    print(f"❌ Failed to connect to BingX: {e}")

# ================= INDICATORS (المعادلات اليدوية المضمونة) =================
def ema(data, period):
    if len(data) < period: return data[-1]
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 0
    tr, up_move, down_move = [], [], []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        dm_plus = highs[i] - highs[i-1] if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
        dm_minus = lows[i-1] - lows[i] if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
        up_move.append(max(dm_plus, 0))
        down_move.append(max(dm_minus, 0))
    atr = sum(tr[:period]) / period
    if atr == 0: atr = 0.0001
    plus_di = 100 * (sum(up_move[:period]) / period) / atr
    minus_di = 100 * (sum(down_move[:period]) / period) / atr
    if (plus_di + minus_di) == 0: return 0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx

def calculate_sar(highs, lows, af=0.015, max_af=0.2): # تعديل حساسية SAR
    if len(highs) < 2: return lows[-1]
    sar = [0.0] * len(highs)
    is_long, ep, cur_af = True, highs[0], af
    sar[0] = lows[0] - (highs[0] - lows[0])
    for i in range(1, len(highs)):
        sar[i] = sar[i-1] + cur_af * (ep - sar[i-1])
        if is_long:
            if lows[i] < sar[i]: is_long, sar[i], ep, cur_af = False, ep, lows[i], af
            else:
                if highs[i] > ep: ep, cur_af = highs[i], min(cur_af + af, max_af)
                sar[i] = min(sar[i], lows[i-1])
                if i > 1: sar[i] = min(sar[i], lows[i-2])
        else:
            if highs[i] > sar[i]: is_long, sar[i], ep, cur_af = True, ep, highs[i], af
            else:
                if lows[i] < ep: ep, cur_af = lows[i], min(cur_af + af, max_af)
                sar[i] = max(sar[i], highs[i-1])
                if i > 1: sar[i] = max(sar[i], highs[i-2])
    return sar[-1]

# ================= JOB QUEUE & LOGIC =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    active_chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not active_chat_id: return
    
    try:
        balance = exchange.fetch_balance()
        real_usdt = balance['free'].get('USDT', 0)
    except: return

    for sym in SYMBOLS:
        try:
            bars = exchange.fetch_ohlcv(sym, timeframe='30m', limit=100)
            bars = bars[:-1]
            closes, highs, lows = [b[4] for b in bars], [b[2] for b in bars], [b[3] for b in bars]
            price = closes[-1]
            
            # حساب المؤشرات يدوياً
            ema200 = ema(closes, 200)
            adx_val = calculate_adx(highs, lows, closes)
            sar_val = calculate_sar(highs, lows)

            # 1. إدارة الصفقات المفتوحة (الخروج)
            if sym in positions_virtual:
                pos = positions_virtual[sym]
                entry, p_type, qty = pos['entry'], pos['type'], pos['qty']
                pnl_pct = ((price - entry) / entry) * 100 * (1 if p_type == "LONG" else -1)
                
                close_signal = False
                reason = ""
                
                if pnl_pct >= (TAKE_PROFIT_PCT * 100): close_signal, reason = True, "🎯 TP HIT"
                elif pnl_pct <= -(STOP_LOSS_PCT * 100): 
                    close_signal, reason = True, "🛑 STOP LOSS"
                    cooldown_tracker[sym] = time.time() # كولداون بعد الخسارة
                elif (p_type == "LONG" and sar_val > price) or (p_type == "SHORT" and sar_val < price):
                    close_signal, reason = True, "⚠️ SAR EXIT"

                if close_signal:
                    side = 'sell' if p_type == 'LONG' else 'buy'
                    exchange.create_market_order(sym, side, qty, params={'positionSide': p_type})
                    pnl_cash = (price - entry) * qty if p_type == 'LONG' else (entry - price) * qty
                    global wins, losses
                    if pnl_cash > 0: wins += 1
                    else: losses += 1
                    trade_history.append(pnl_cash)
                    positions_virtual.pop(sym)
                    await context.bot.send_message(chat_id=active_chat_id, text=f"{reason}\n✅ Closed: {sym.split(':')[0]}\n💰 PnL: {pnl_cash:.2f} $")

            # 2. البحث عن فرص جديدة (الدخول)
            else:
                # فلتر الكولداون
                if sym in cooldown_tracker and (time.time() - cooldown_tracker[sym] < COOLDOWN_TIME): continue
                
                # فلتر الشمعة الكبيرة
                candle_size = abs(closes[-1] - bars[-1][1]) / bars[-1][1]
                if candle_size > 0.02: continue

                if adx_val > ADX_THRESHOLD:
                    signal = None
                    if price > ema200 and sar_val < price: signal = "LONG"
                    elif price < ema200 and sar_val > price: signal = "SHORT"
                    
                    if signal and real_usdt > 10:
                        margin = real_usdt * RISK_PER_TRADE
                        qty = (margin * LEVERAGE) / price
                        try:
                            exchange.set_leverage(LEVERAGE, sym)
                            side = 'buy' if signal == 'LONG' else 'sell'
                            exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                            positions_virtual[sym] = {'qty': qty, 'entry': price, 'type': signal}
                            await context.bot.send_message(chat_id=active_chat_id, text=f"🚀 **{signal} ENTRY**\n🪙 Coin: {sym.split(':')[0]}\n🔥 ADX: {adx_val:.1f}")
                        except Exception as e: print(f"Error opening {sym}: {e}")
        except: continue

# ================= SERVER & UI =================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SMART AI BOT V3 IS RUNNING!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(('0.0.0.0', port), DummyHandler).serve_forever()

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 رادار", callback_data="scan"), InlineKeyboardButton("💰 رصيد", callback_data="balance")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="stats"), InlineKeyboardButton("💼 صفقات", callback_data="positions")]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # (هنا يوضع كود التعامل مع الأزرار كما في كودك الأصلي...)
    await query.message.reply_text("جاري التحديث...", reply_markup=get_main_keyboard())

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.bot_data["chat_id"] = update.effective_chat.id
        await update.message.reply_text("🚀 **تم تشغيل النسخة المدمجة (V3)!**\nلا حاجة لمكتبات خارجية | حماية كاملة.", reply_markup=get_main_keyboard())
        
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    print("🚀 SMART SNIPER BOT STARTED...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
