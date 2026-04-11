import os
import ccxt
import time
import threading
import numpy as np
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler
)

# ================= 🔑 1. CONFIG & API =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

# القائمة الكاملة (11 عملة)
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'DOGE/USDT:USDT', 
    'LINK/USDT:USDT', 'DOT/USDT:USDT', 'ZEC/USDT:USDT'
]

# ================= ⚙️ 2. TRADING SETTINGS =================
RISK_PER_TRADE = 0.15     # 15% من الكاش
LEVERAGE = 10             # رافعة 10
STOP_LOSS_PCT = 0.03      # 30% ROE (إيقاف خسارة)
TAKE_PROFIT_PCT = 0.015   # 15% ROE (هدف الربح)
ADX_THRESHOLD = 25        # فلتر قوة الاتجاه
EMA_PERIOD = 200          # فلتر الاتجاه العام
COOLDOWN_TIME = 3600      # ساعة راحة بعد أي خسارة

trade_history = []
wins, losses = 0, 0
positions_virtual = {}    # لمتابعة الصفقات المفتوحة
cooldown_tracker = {}     # لمتابعة تجميد العملات

# ربط المنصة
try:
    exchange = ccxt.bingx({
        'apiKey': BINGX_API_KEY,
        'secret': BINGX_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    print("✅ Successfully connected to BingX API")
except Exception as e:
    print(f"❌ Connection Error: {e}")

# ================= 📊 3. MANUAL INDICATORS =================
def calculate_ema(data, period):
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
        dm_p = highs[i] - highs[i-1] if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
        dm_m = lows[i-1] - lows[i] if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
        up_move.append(max(dm_p, 0))
        down_move.append(max(dm_m, 0))
    atr = sum(tr[:period]) / period if period > 0 else 1
    plus_di = 100 * (sum(up_move[:period]) / period) / atr
    minus_di = 100 * (sum(down_move[:period]) / period) / atr
    if (plus_di + minus_di) == 0: return 0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx

def calculate_sar(highs, lows, af=0.015, max_af=0.2):
    if len(highs) < 2: return lows[-1]
    sar = [0.0] * len(highs)
    is_long, ep, cur_af = True, highs[0], af
    sar[0] = lows[0]
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

# ================= 📡 4. ANALYSIS & TRADING =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    try:
        balance = exchange.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0)
    except: return

    for sym in SYMBOLS:
        try:
            # جلب البيانات
            bars = exchange.fetch_ohlcv(sym, timeframe='30m', limit=210)
            closes = [b[4] for b in bars[:-1]]
            highs = [b[2] for b in bars[:-1]]
            lows = [b[3] for b in bars[:-1]]
            price = closes[-1]

            # حساب المؤشرات
            ema_val = calculate_ema(closes, EMA_PERIOD)
            adx_val = calculate_adx(highs, lows, closes)
            sar_val = calculate_sar(highs, lows)

            # --- إدارة الصفقات المفتوحة ---
            if sym in positions_virtual:
                pos = positions_virtual[sym]
                pnl_pct = ((price - pos['entry']) / pos['entry']) * 100 * (1 if pos['type'] == "LONG" else -1)
                
                close_it, reason = False, ""
                if pnl_pct >= (TAKE_PROFIT_PCT * 100): close_it, reason = True, "🎯 TP HIT"
                elif pnl_pct <= -(STOP_LOSS_PCT * 100): 
                    close_it, reason = True, "🛑 STOP LOSS"
                    cooldown_tracker[sym] = time.time()
                elif (pos['type'] == "LONG" and sar_val > price) or (pos['type'] == "SHORT" and sar_val < price):
                    close_it, reason = True, "⚠️ SAR REVERSE"

                if close_it:
                    side = 'sell' if pos['type'] == 'LONG' else 'buy'
                    exchange.create_market_order(sym, side, pos['qty'], params={'positionSide': pos['type']})
                    pnl_cash = (price - pos['entry']) * pos['qty'] if pos['type'] == 'LONG' else (pos['entry'] - price) * pos['qty']
                    global wins, losses
                    if pnl_cash > 0: wins += 1
                    else: losses += 1
                    trade_history.append(pnl_cash)
                    positions_virtual.pop(sym)
                    await context.bot.send_message(chat_id=chat_id, text=f"{reason}\n✅ Closed: {sym.split(':')[0]}\n💰 PnL: {pnl_cash:.2f} $")

            # --- فتح صفقات جديدة ---
            else:
                if sym in cooldown_tracker and (time.time() - cooldown_tracker[sym] < COOLDOWN_TIME): continue
                if adx_val > ADX_THRESHOLD:
                    signal = None
                    if price > ema_val and sar_val < price: signal = "LONG"
                    elif price < ema_val and sar_val > price: signal = "SHORT"

                    if signal and usdt_free > 10:
                        margin = usdt_free * RISK_PER_TRADE
                        qty = (margin * LEVERAGE) / price
                        try:
                            exchange.set_leverage(LEVERAGE, sym)
                            side = 'buy' if signal == 'LONG' else 'sell'
                            exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                            positions_virtual[sym] = {'qty': qty, 'entry': price, 'type': signal}
                            await context.bot.send_message(chat_id=chat_id, text=f"🚀 **{signal} ENTRY**\n🪙 {sym.split(':')[0]}\n🔥 ADX: {adx_val:.1f}")
                        except: pass
        except: continue

# ================= 📱 5. UI & HANDLERS =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 رادار السوق", callback_data="scan"), InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"), InlineKeyboardButton("💼 الصفقات", callback_data="positions")]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "scan":
        msg = "📡 **فحص الرادار (30m):**\n\n"
        for sym in SYMBOLS:
            try:
                bars = exchange.fetch_ohlcv(sym, timeframe='30m', limit=210)
                closes = [b[4] for b in bars[:-1]]
                price = closes[-1]
                ema_v = calculate_ema(closes, EMA_PERIOD)
                if price > ema_v: st = "🟢 LONG"
                else: st = "🔴 SHORT"
                msg += f"🪙 {sym.split(':')[0]} | {st}\n"
            except: continue
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "balance":
        try:
            bal = exchange.fetch_balance()
            msg = f"🏦 **المحفظة:**\n💰 الإجمالي: {bal['total'].get('USDT', 0):.2f} $"
        except: msg = "❌ خطأ في الاتصال"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "stats":
        msg = f"📊 **إحصائيات:**\n✅ ربح: {wins} | ❌ خسارة: {losses}\n💸 الصافي: {sum(trade_history):.2f} $"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "positions":
        if not positions_virtual: msg = "📭 لا توجد صفقات."
        else:
            msg = "💼 **المفتوحة:**\n"
            for s, p in positions_virtual.items():
                msg += f"• {s.split(':')[0]} [{p['type']}] @ {p['entry']:.4f}\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

# ================= 🚀 6. SERVER & RUN =================
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BOT V3 RUNNING...")

def main():
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), WebHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    
    async def start(u, c):
        c.bot_data["chat_id"] = u.effective_chat.id
        await u.message.reply_text("🚀 **تم تفعيل القناص المطور V3!**", reply_markup=get_main_keyboard())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    print("🚀 SMART SNIPER STARTED...")
    app.run_polling()

if __name__ == "__main__":
    main()
