import os
import time
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ================= 🔑 1. CONFIG (Environment Variables) =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "DOGE/USDT:USDT",
    "LINK/USDT:USDT", "DOT/USDT:USDT", "ZEC/USDT:USDT",
]

# ================= ⚙️ 2. SETTINGS =================

RISK_PER_TRADE = 0.02      
LEVERAGE = 5               

STOP_LOSS_PRICE_MOVE  = 0.02   
TAKE_PROFIT_PRICE_MOVE = 0.04  

ADX_THRESHOLD = 25
RSI_PERIOD = 14            
EMA_FAST_PERIOD = 50       
EMA_SLOW_PERIOD = 200
COOLDOWN_TIME = 3600
TIMEFRAME = "30m"

MAX_LAST_CANDLE_BODY_RATIO = 0.012

# ================= 🧾 3. GLOBAL STATE =================

trade_history = []
wins = 0
losses = 0
open_positions = {}
cooldown_tracker = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ai_sniper_bot")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# ================= 📊 4. INDICATORS =================

def calculate_ema(data, period):
    if len(data) < period:
        return data[-1]
    k = 2 / (period + 1)
    ema_val = sum(data[:period]) / period  
    for price in data[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses_list = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses_list.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses_list[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 0.0
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    atr = sum(tr[-period:]) / period
    if atr == 0: return 0.0
    p_di = 100 * (sum(plus_dm[-period:]) / period) / atr
    m_di = 100 * (sum(minus_dm[-period:]) / period) / atr
    return 100 * abs(p_di - m_di) / (p_di + m_di + 0.001)

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

# ================= 🧰 5. HELPERS =================

def fetch_actual_positions():
    synced = {}
    try:
        positions = exchange.fetch_positions(SYMBOLS)
        for p in positions:
            contracts = float(p.get("contracts", 0))
            if contracts != 0:
                synced[p['symbol']] = {
                    "qty": abs(contracts),
                    "entry": float(p.get("entryPrice", 0)),
                    "type": "LONG" if p['side'] == 'long' else "SHORT"
                }
    except Exception as e:
        logger.error(f"Sync positions error: {e}")
    return synced

def get_free_balance():
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("free", {}).get("USDT", 0))
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return 0.0

def close_position(sym, pos, price):
    try:
        side = 'sell' if pos['type'] == 'LONG' else 'buy'
        order = exchange.create_market_order(sym, side, pos['qty'], params={'positionSide': pos['type']})
        if order and order.get('status') in ('closed', 'filled'):
            return True
        return True
    except Exception as e:
        logger.error(f"Close position error {sym}: {e}")
        return False

# ================= 🤖 6. TRADING JOB =================

async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    global open_positions, wins, losses
    chat_id = context.bot_data.get("chat_id") or ENV_CHAT_ID
    if not chat_id: return

    open_positions = fetch_actual_positions()

    for sym in SYMBOLS:
        try:
            bars = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=250)
            if not bars or len(bars) < 210: continue

            closes = [b[4] for b in bars[:-1]]
            highs  = [b[2] for b in bars[:-1]]
            lows   = [b[3] for b in bars[:-1]]
            price  = closes[-1]

            ema_slow = calculate_ema(closes, EMA_SLOW_PERIOD)
            ema_fast = calculate_ema(closes, EMA_FAST_PERIOD)
            adx_val  = calculate_adx(highs, lows, closes)
            sar_val  = calculate_sar(highs, lows)
            rsi_val  = calculate_rsi(closes, RSI_PERIOD)

            open_price = bars[-2][1]
            body_ratio = abs(closes[-1] - open_price) / open_price if open_price != 0 else 0

            if sym in open_positions:
                pos = open_positions[sym]
                pnl_pct = ((price - pos['entry']) / pos['entry']) * (1 if pos['type'] == "LONG" else -1)
                close_it, reason = False, ""
                if pnl_pct >= TAKE_PROFIT_PRICE_MOVE: close_it, reason = True, "🎯 TAKE PROFIT"
                elif pnl_pct <= -STOP_LOSS_PRICE_MOVE: 
                    close_it, reason = True, "🛑 STOP LOSS"
                    cooldown_tracker[sym] = time.time()
                elif (pos['type'] == "LONG" and sar_val > price) or (pos['type'] == "SHORT" and sar_val < price):
                    close_it, reason = True, "⚠️ SAR REVERSE"

                if close_it:
                    success = close_position(sym, pos, price)
                    if success:
                        pnl_cash = (price - pos['entry']) * pos['qty'] if pos['type'] == 'LONG' else (pos['entry'] - price) * pos['qty']
                        if pnl_cash > 0: wins += 1
                        else: losses += 1
                        trade_history.append(pnl_cash)
                        await context.bot.send_message(chat_id=chat_id, text=f"{reason}\n✅ Closed: {sym.split('/')[0]}\n💰 PnL: {pnl_cash:.2f} $")

            else:
                if sym in cooldown_tracker and (time.time() - cooldown_tracker[sym] < COOLDOWN_TIME): continue
                if body_ratio > MAX_LAST_CANDLE_BODY_RATIO: continue
                if adx_val <= ADX_THRESHOLD: continue

                signal = None
                if price > ema_slow and ema_fast > ema_slow and sar_val < price and 45 < rsi_val < 65:
                    signal = "LONG"
                elif price < ema_slow and ema_fast < ema_slow and sar_val > price and 35 < rsi_val < 55:
                    signal = "SHORT"

                if signal:
                    usdt_free = get_free_balance()
                    if usdt_free < 10: continue
                    qty = (usdt_free * RISK_PER_TRADE * LEVERAGE) / price
                    try:
                        exchange.set_leverage(LEVERAGE, sym)
                        side = 'buy' if signal == 'LONG' else 'sell'
                        exchange.create_market_order(sym, side, qty, params={'positionSide': signal})
                        await context.bot.send_message(chat_id=chat_id, text=f"🚀 **{signal} ENTRY**\n🪙 {sym.split('/')[0]}\n🔥 ADX: {adx_val:.1f} | RSI: {rsi_val:.1f}")
                    except Exception as e: logger.error(f"Entry error {sym}: {e}")

        except Exception as e: logger.error(f"Loop error {sym}: {e}")

# ================= 📱 7. UI =================

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 الرادار", callback_data="scan"), InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"), InlineKeyboardButton("💼 الصفقات", callback_data="positions")]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "scan":
        msg = "📡 **فحص الرادار (V5):**\n\n"
        for sym in SYMBOLS:
            try:
                bars = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=210)
                price = bars[-1][4]
                ema_s = calculate_ema([b[4] for b in bars[:-1]], EMA_SLOW_PERIOD)
                ema_f = calculate_ema([b[4] for b in bars[:-1]], EMA_FAST_PERIOD)
                trend = "🟢 LONG" if (price > ema_s and ema_f > ema_s) else "🔴 SHORT" if (price < ema_s and ema_f < ema_s) else "⚪ محايد"
                msg += f"🪙 {sym.split('/')[0]} | {trend}\n"
            except: continue
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())
    elif query.data == "balance":
        bal = get_free_balance()
        await query.edit_message_text(f"🏦 **رصيدك المتاح:**\n💰 {bal:.2f} USDT", reply_markup=get_main_keyboard())
    elif query.data == "stats":
        total = wins + losses
        total_pnl = sum(trade_history)
        msg = f"📊 **الإحصائيات:**\n✅ ربح: {wins} | ❌ خسارة: {losses}\n💰 إجمالي PnL: {total_pnl:.2f} $"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())
    elif query.data == "positions":
        curr = fetch_actual_positions()
        if not curr: msg = "📭 لا توجد صفقات مفتوحة."
        else:
            msg = "💼 **الصفقات الحقيقية:**\n"
            for s, p in curr.items():
                msg += f"• {s.split('/')[0]} [{p['type']}] @ {p['entry']:.4f}\n"
        await query.edit_message_text(msg, reply_markup=get_main_keyboard())

# ================= 🚀 8. RUNNING =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"AI SNIPER V5 IS LIVE")

def main():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever(), daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    
    async def start(u, c):
        c.bot_data["chat_id"] = u.effective_chat.id
        await u.message.reply_text("🚀 **تم تفعيل النسخة الاحترافية V5!**", reply_markup=get_main_keyboard())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.job_queue.run_repeating(trading_job, interval=60, first=5)
    
    print("🚀 PRO SNIPER V5 STARTED...")
    app.run_polling()

if __name__ == "__main__":
    main()
