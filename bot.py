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

# ================= CONFIG =================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# توسيع القائمة إلى 10 عملات من أقوى مشاريع السوق
SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 
    'XRP/USDT', 'ADA/USDT', 'AVAX/USDT', 'DOGE/USDT', 
    'LINK/USDT', 'DOT/USDT'
]

exchange = ccxt.kucoin({
    "enableRateLimit": True
})

# المحفظة الواقعية 130 دولار مع دخول بـ 15% للصفقة
virtual_wallet = {"USDT": 130.0}
positions = {} # شكلها الجديد: {'BTC/USDT': {'qty': 0.5, 'entry': 70000, 'type': 'LONG'}}

trade_history = []
wins = 0
losses = 0

RISK_PER_TRADE = 0.15  # 15% من الـ 130 دولار
STOP_LOSS = 0.015
TAKE_PROFIT = 0.03

# ================= INDICATORS =================
def ema(data, period):
    if len(data) < period: return data[-1]
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

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
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        bars = bars[:-1] 

        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        
        price = closes[-1]
        ema50 = ema(closes, 50)
        ema20 = ema(closes, 20) 
        sar_val = calculate_sar(highs, lows)

        # شروط صفقة الشراء (LONG)
        if price > ema50 and ema20 > ema50 and sar_val < price:
            return price, "LONG", sar_val, ema50

        # شروط صفقة البيع (SHORT) - التربح من الهبوط
        if price < ema50 and ema20 < ema50 and sar_val > price:
            return price, "SHORT", sar_val, ema50

        return price, "HOLD", sar_val, ema50

    except Exception as e:
        print(f"Error for {symbol}: {e}")
        return None, "HOLD", None, None

# ================= TRADING =================
def position_size(price):
    return (virtual_wallet["USDT"] * RISK_PER_TRADE) / price

def close_trade(symbol, price):
    global wins, losses
    pos = positions[symbol]
    qty = pos['qty']
    entry = pos['entry']
    pos_type = pos['type']
    
    # حساب الربح/الخسارة بناءً على نوع الصفقة
    if pos_type == 'LONG':
        pnl = (price - entry) * qty
    else: # SHORT
        pnl = (entry - price) * qty
        
    cost = qty * entry
    virtual_wallet["USDT"] += (cost + pnl) # إعادة الكاش للمحفظة مع الأرباح/الخسائر
    
    if pnl > 0: wins += 1
    else: losses += 1
        
    trade_history.append(pnl)
    positions.pop(symbol)
    return pnl

# ================= JOB QUEUE =================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    active_chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not active_chat_id: return

    for sym in SYMBOLS:
        price, signal, sar_val, ema50 = get_signal(sym)
        if not price: continue

        # إذا كنا نملك صفقة في هذه العملة
        if sym in positions:
            pos = positions[sym]
            entry = pos['entry']
            pos_type = pos['type']
            
            if pos_type == "LONG":
                if price <= entry * (1 - STOP_LOSS):
                    pnl = close_trade(sym, price)
                    await context.bot.send_message(chat_id=active_chat_id, text=f"🛑 **SL HIT (LONG)**\n🪙 {sym} closed at {price:.4f} $\n📉 PnL: {pnl:.2f} $")
                elif price >= entry * (1 + TAKE_PROFIT):
                    pnl = close_trade(sym, price)
                    await context.bot.send_message(chat_id=active_chat_id, text=f"🎯 **TP HIT (LONG)**\n🪙 {sym} closed at {price:.4f} $\n📈 PnL: {pnl:.2f} $")
                elif sar_val > price:
                    pnl = close_trade(sym, price)
                    icon = "📈" if pnl > 0 else "📉"
                    await context.bot.send_message(chat_id=active_chat_id, text=f"⚠️ **TREND REVERSED (Closed LONG)**\n🪙 {sym} closed at {price:.4f} $\n{icon} PnL: {pnl:.2f} $")

            elif pos_type == "SHORT":
                if price >= entry * (1 + STOP_LOSS): # السعر ارتفع (خسارة للشورت)
                    pnl = close_trade(sym, price)
                    await context.bot.send_message(chat_id=active_chat_id, text=f"🛑 **SL HIT (SHORT)**\n🪙 {sym} closed at {price:.4f} $\n📉 PnL: {pnl:.2f} $")
                elif price <= entry * (1 - TAKE_PROFIT): # السعر انخفض (ربح للشورت)
                    pnl = close_trade(sym, price)
                    await context.bot.send_message(chat_id=active_chat_id, text=f"🎯 **TP HIT (SHORT)**\n🪙 {sym} closed at {price:.4f} $\n📈 PnL: {pnl:.2f} $")
                elif sar_val < price:
                    pnl = close_trade(sym, price)
                    icon = "📈" if pnl > 0 else "📉"
                    await context.bot.send_message(chat_id=active_chat_id, text=f"⚠️ **TREND REVERSED (Closed SHORT)**\n🪙 {sym} closed at {price:.4f} $\n{icon} PnL: {pnl:.2f} $")

        # إذا لم يكن لدينا صفقة، نبحث عن فرصة
        else:
            if signal in ["LONG", "SHORT"]:
                qty = position_size(price)
                cost = qty * price
                
                if virtual_wallet["USDT"] >= cost:
                    positions[sym] = {'qty': qty, 'entry': price, 'type': signal}
                    virtual_wallet["USDT"] -= cost
                    
                    if signal == "LONG":
                        tp_price = price * (1 + TAKE_PROFIT)
                        sl_price = price * (1 - STOP_LOSS)
                        icon = "🟢 **LONG OPENED**"
                    else:
                        tp_price = price * (1 - TAKE_PROFIT)
                        sl_price = price * (1 + STOP_LOSS)
                        icon = "🔴 **SHORT OPENED**"
                        
                    msg = f"{icon}\n🪙 Coin: {sym}\n💵 Entry: {price:.4f} $\n🎯 TP: {tp_price:.4f} $\n🛑 SL: {sl_price:.4f} $"
                    await context.bot.send_message(chat_id=active_chat_id, text=msg)

# ================= COMMANDS & BUTTONS =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 فحص السوق الآن", callback_data="scan")],
        [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("💼 Positions", callback_data="positions")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "✅ **تم تحديث الوحش!**\nالبوت الآن يراقب 10 عملات ويفتح صفقات (LONG & SHORT).\n💰 الرصيد المبدئي: 130 USDT",
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "scan":
        msg = "📡 **رادار السوق المباشر (10 عملات):**\n\n"
        for sym in SYMBOLS:
            price, signal, sar_val, ema50 = get_signal(sym)
            if price:
                if signal == "LONG": status = "🟢 فرصة صعود (LONG)"
                elif signal == "SHORT": status = "🔴 فرصة هبوط (SHORT)"
                else: status = "⏳ انتظار"
                msg += f"🪙 {sym} | 🤖 {status}\n"
            else:
                msg += f"⚠️ {sym}: جاري التحميل...\n"
        
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "stats":
        total_trades = wins + losses
        pnl = sum(trade_history)
        
        # حساب قيمة المحفظة الإجمالية (الكاش المتاح + قيمة الصفقات المفتوحة)
        locked_margin = sum([pos['qty'] * pos['entry'] for pos in positions.values()])
        total_value = virtual_wallet['USDT'] + locked_margin + pnl
        
        msg = f"💰 **Total Value:** {total_value:.2f} USDT\n💵 **Free Cash:** {virtual_wallet['USDT']:.2f} USDT\n📊 Trades: {total_trades}\n✅ Wins: {wins} | ❌ Losses: {losses}\n💸 Net PnL: {pnl:.2f} $"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

    elif query.data == "positions":
        if not positions:
            await query.edit_message_text("📭 لا توجد صفقات مفتوحة حالياً.", reply_markup=get_main_keyboard())
            return
        msg = "💼 **Active Positions:**\n\n"
        for sym, pos in positions.items():
            icon = "🟢" if pos['type'] == "LONG" else "🔴"
            msg += f"{icon} {sym} [{pos['type']}]\n💵 Entry: {pos['entry']:.4f} $\n---\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

# ================= DUMMY SERVER =================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Hedge Fund Bot is running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# ================= MAIN =================
def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return

    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(trading_job, interval=60, first=5)

    print("🚀 BOT STARTED SUCCESSFULLY (LONG/SHORT - 10 COINS)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
