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

# ================= CONFIG & API =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

# القائمة المحدثة (تم إضافة ZEC)
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'DOGE/USDT:USDT', 
    'LINK/USDT:USDT', 'DOT/USDT:USDT', 'ZEC/USDT:USDT'
]

# ================= TRADING SETTINGS =================
RISK_PER_TRADE = 0.15  
LEVERAGE = 10          
STOP_LOSS = 0.015      
TAKE_PROFIT = 0.03     
ADX_THRESHOLD = 25     # لا يدخل أي صفقة إلا إذا كانت قوة الترند فوق 25

trade_history = []
wins = 0
losses = 0
positions_virtual = {} 

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

# ================= INDICATORS =================
def ema(data, period):
    if len(data) < period: return data[-1]
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 0
    tr = []
    up_move = []
    down_move = []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        dm_plus = highs[i] - highs[i-1] if (highs[i] - highs[i-1]) > (lows[i-1] - lows[i]) else 0
        dm_minus = lows[i-1] - lows[i] if (lows[i-1] - lows[i]) > (highs[i] - highs[i-1]) else 0
        up_move.append(max(dm_plus, 0))
        down_move.append(max(dm_minus, 0))
    
    atr = sum(tr[:period]) / period if period > 0 else 1 # حماية من القسمة على صفر
    if atr == 0: atr = 0.0001
    
    plus_di = 100 * (sum(up_move[:period]) / period) / atr
    minus_di = 100 * (sum(down_move[:period]) / period) / atr
    
    if (plus_di + minus_di) == 0: return 0
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx

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
        bars = exchange.fetch_ohlcv(symbol, timeframe='30m', limit=100)
        bars = bars[:-1] 
        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        
        price = closes[-1]
        ema50 = ema(closes, 50)
        ema20 = ema(closes, 20) 
        sar_val = calculate_sar(highs, lows)
        adx_val = calculate_adx(highs, lows, closes)

        # فلترة ذكية: الدخول فقط إذا كان هناك ترند واضح (ADX > 25)
        if adx_val > ADX_THRESHOLD:
            if price > ema50 and ema20 > ema50 and sar_val < price:
                return price, "LONG", adx_val, sar_val
            if price < ema50 and ema20 < ema50 and sar_val > price:
                return price, "SHORT", adx_val, sar_val

        return price, "HOLD", adx_val, sar_val
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None, "HOLD", 0, 0

# ================= COMMANDS KEYBOARD =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 رادار السوق", callback_data="scan"), InlineKeyboardButton("💰 رصيد المحفظة", callback_data="balance")],
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="stats"), InlineKeyboardButton("💼 صفقات مفتوحة", callback_data="positions")]
    ])

# ================= JOB QUEUE =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    active_chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not active_chat_id: return
    
    try:
        balance = exchange.fetch_balance()
        real_usdt_balance = balance['free'].get('USDT', 0)
    except: return

    for sym in SYMBOLS:
        price, signal, adx_val, sar_val = get_signal(sym)
        if not price: continue

        # ================= الإغلاق الذكي =================
        if sym in positions_virtual:
            pos = positions_virtual[sym]
            entry = pos['entry']
            pos_type = pos['type']
            qty = pos['qty']
            
            close_signal = False
            close_reason = ""
            
            if pos_type == "LONG":
                if price <= entry * (1 - STOP_LOSS): close_signal, close_reason = True, "🛑 SL HIT (LONG)"
                elif price >= entry * (1 + TAKE_PROFIT): close_signal, close_reason = True, "🎯 TP HIT (LONG)"
                elif sar_val > price: close_signal, close_reason = True, "⚠️ SAR REVERSED (LONG)"
            elif pos_type == "SHORT":
                if price >= entry * (1 + STOP_LOSS): close_signal, close_reason = True, "🛑 SL HIT (SHORT)"
                elif price <= entry * (1 - TAKE_PROFIT): close_signal, close_reason = True, "🎯 TP HIT (SHORT)"
                elif sar_val < price: close_signal, close_reason = True, "⚠️ SAR REVERSED (SHORT)"

            if close_signal:
                try:
                    side = 'sell' if pos_type == 'LONG' else 'buy'
                    exchange.create_market_order(sym, side, qty, params={'positionSide': pos_type})
                    
                    pnl = (price - entry) * qty if pos_type == 'LONG' else (entry - price) * qty
                    global wins, losses
                    if pnl > 0: wins += 1
                    else: losses += 1
                    trade_history.append(pnl)
                    positions_virtual.pop(sym)
                    
                    await context.bot.send_message(chat_id=active_chat_id, text=f"{close_reason}\n✅ Closed: {sym.split(':')[0]}\n💵 Price: {price:.4f} $\n💰 PnL: {pnl:.2f} $")
                except Exception as e:
                    print(f"Error closing {sym}: {e}")

        # ================= الفتح الذكي (بدون سقف + فلتر ADX) =================
        else:
            if signal in ["LONG", "SHORT"] and real_usdt_balance > 10:
                trade_margin = real_usdt_balance * RISK_PER_TRADE
                position_size_usdt = trade_margin * LEVERAGE
                raw_qty = position_size_usdt / price
                
                try:
                    qty_str = exchange.amount_to_precision(sym, raw_qty)
                    qty = float(qty_str)
                except Exception:
                    qty = round(raw_qty, 3)
                    
                try:
                    try:
                        exchange.set_leverage(LEVERAGE, sym)
                    except: pass
                    
                    side = 'buy' if signal == 'LONG' else 'sell'
                    exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                    positions_virtual[sym] = {'qty': qty, 'entry': price, 'type': signal}
                    
                    icon = "🟢 **SMART LONG**" if signal == "LONG" else "🔴 **SMART SHORT**"
                    msg = f"{icon}\n🪙 Coin: {sym.split(':')[0]}\n🔥 ADX Power: {adx_val:.1f}\n💵 Entry: {price:.4f} $\n💼 Margin: {trade_margin:.2f} $"
                    await context.bot.send_message(chat_id=active_chat_id, text=msg)
                    
                    real_usdt_balance -= trade_margin 
                except Exception as e:
                    print(f"Error opening {sym}: {e}")

# ================= BUTTON HANDLER =================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "scan":
        msg = "📡 **رادار السوق الذكي (30m):**\n\n"
        for sym in SYMBOLS:
            price, signal, adx_val, sar_val = get_signal(sym)
            if price:
                if signal == "LONG": status = "🟢 صعود قوي"
                elif signal == "SHORT": status = "🔴 هبوط قوي"
                else: status = "⏳ انتظار/تذبذب"
                msg += f"🪙 {sym.split(':')[0]} | ADX: {adx_val:.1f} | {status}\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "balance":
        try:
            bal = exchange.fetch_balance()
            total = bal['total'].get('USDT', 0)
            free = bal['free'].get('USDT', 0)
            used = bal['used'].get('USDT', 0)
            msg = f"🏦 **رصيد المحفظة:**\n\n💰 الإجمالي: {total:.2f} $\n💵 كاش متاح: {free:.2f} $\n🔒 محجوز: {used:.2f} $"
        except Exception as e:
            msg = f"❌ خطأ: {e}"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "stats":
        total_trades = wins + losses
        pnl = sum(trade_history)
        msg = f"📊 **إحصائيات الذكاء الاصطناعي:**\n\n🔄 صفقات: {total_trades}\n✅ ربح: {wins} | ❌ خسارة: {losses}\n💸 صافي الربح: {pnl:.2f} $"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "positions":
        if not positions_virtual:
            await query.edit_message_text("📭 لا توجد صفقات مفتوحة حالياً.", reply_markup=get_main_keyboard())
            return
        msg = "💼 **الصفقات المفتوحة (الذكية):**\n\n"
        for sym, pos in positions_virtual.items():
            icon = "🟢" if pos['type'] == "LONG" else "🔴"
            msg += f"{icon} {sym.split(':')[0]} [{pos['type']}]\n💵 الدخول: {pos['entry']:.4f} $\n---\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

# ================= SERVER & MAIN =================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SMART AI BOT (ZEC ADDED) IS RUNNING!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(('0.0.0.0', port), DummyHandler).serve_forever()

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.bot_data["chat_id"] = update.effective_chat.id
        await update.message.reply_text(
            "🚀 **تم تفعيل الوحش القناص الذكي!**\n\n(فلتر ADX يعمل | 11 عملة منها ZEC | بدون سقف صفقات)",
            reply_markup=get_main_keyboard()
        )
        
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    print("🚀 SMART SNIPER BOT STARTED...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
