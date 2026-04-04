import ccxt
import time
import requests
import os

# ================= Config =================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

virtual_wallet = {"USDT": 10000.0}
for s in SYMBOLS:
    virtual_wallet[s] = 0.0

entry_price = {s: None for s in SYMBOLS}
highest_price = {s: None for s in SYMBOLS}

RISK_PER_TRADE = 0.02   # 2%
TRAILING_STOP = 0.03    # 3%

exchange = ccxt.kucoin()

last_signal = {s: None for s in SYMBOLS}

# ================= Telegram =================

def send_message(text):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    except:
        pass

# ================= Indicators =================

def ema(data, period):
    k = 2 / (period + 1)
    ema_val = data[0]
    for price in data:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def calculate_rsi(closes, period=14):
    gains, losses = [], []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(closes):
    return ema(closes, 12) - ema(closes, 26)

# ================= Analysis =================

def get_analysis(symbol):
    bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
    bars = bars[:-1]

    closes = [b[4] for b in bars]
    volumes = [b[5] for b in bars]

    price = closes[-1]

    ema20 = ema(closes[-20:], 20)
    ema50 = ema(closes[-50:], 50)
    rsi = calculate_rsi(closes)
    macd_val = macd(closes)

    vol_avg = sum(volumes[-20:]) / 20

    if ema20 > ema50 and macd_val > 0 and rsi > 50 and volumes[-1] > vol_avg:
        return price, "BUY"

    elif ema20 < ema50 and macd_val < 0 and rsi < 50:
        return price, "SELL"

    return price, "Neutral"

# ================= Risk =================

def position_size(price):
    risk_amount = virtual_wallet["USDT"] * RISK_PER_TRADE
    return risk_amount / price

def trailing_stop_check(symbol, price):
    if virtual_wallet[symbol] == 0:
        return False

    if highest_price[symbol] is None or price > highest_price[symbol]:
        highest_price[symbol] = price

    if price <= highest_price[symbol] * (1 - TRAILING_STOP):
        close_trade(symbol, price, "🔻 TRAILING STOP")
        return True

    return False

def close_trade(symbol, price, reason):
    value = virtual_wallet[symbol] * price
    virtual_wallet["USDT"] += value

    virtual_wallet[symbol] = 0
    entry_price[symbol] = None
    highest_price[symbol] = None

    send_message(f"{reason} {symbol} @ {price}")

# ================= Dashboard =================

def report():
    msg = f"💼 Balance: {virtual_wallet['USDT']:.2f} USDT"
    send_message(msg)

# ================= Main =================

def run_bot():
    print("PRO BOT 🔥")

    counter = 0

    while True:
        for sym in SYMBOLS:
            price, signal = get_analysis(sym)

            if trailing_stop_check(sym, price):
                continue

            if signal == last_signal[sym]:
                continue

            if signal == "BUY" and virtual_wallet[sym] == 0:
                qty = position_size(price)
                cost = qty * price

                if virtual_wallet["USDT"] >= cost:
                    virtual_wallet[sym] = qty
                    virtual_wallet["USDT"] -= cost
                    entry_price[sym] = price
                    highest_price[sym] = price

                    send_message(f"🚀 BUY {sym} @ {price}")

            elif signal == "SELL" and virtual_wallet[sym] > 0:
                close_trade(sym, price, "⚠️ SELL")

            last_signal[sym] = signal

        counter += 1

        if counter % 30 == 0:
            report()

        time.sleep(60)

if __name__ == "__main__":
    run_bot()