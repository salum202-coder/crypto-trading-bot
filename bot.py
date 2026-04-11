import os
import subprocess
import sys
import time
import hmac
import hashlib
import requests
import pandas as pd

# --- 1. حل مشكلة المكتبات (إجباري) ---
def force_install():
    try:
        import pandas_ta
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "https://github.com/twopirllc/pandas-ta/archive/master.zip"])

force_install()
import pandas_ta as ta

# --- 2. سحب البيانات من Render Environment ---
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
BINGX_URL = "https://open-api.bingx.com"

# --- 3. إعدادات استراتيجية الخبير ---
SYMBOLS = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT', 'BNB-USDT']
LEVERAGE = 10
POSITION_SIZE_PCT = 0.15
TAKE_PROFIT = 0.015  # 1.5%
STOP_LOSS = 0.03     # 3% حزام الأمان
EMA_PERIOD = 200
SAR_STEP = 0.015
SAR_MAX = 0.2
ADX_THRESHOLD = 25

# --- 4. وظائف الربط والاتصال (BingX & Telegram) ---
def get_sign(params_str):
    return hmac.new(SECRET_KEY.encode("utf-8"), params_str.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

def bingx_request(method, path, params={}):
    params['timestamp'] = int(time.time() * 1000)
    params['apiKey'] = API_KEY
    sorted_params = dict(sorted(params.items()))
    params_str = "&".join([f"{k}={v}" for k, v in sorted_params.items()])
    sign = get_sign(params_str)
    url = f"{BINGX_URL}{path}?{params_str}&signature={sign}"
    return requests.request(method, url).json()

def send_telegram(msg):
    if TELEGRAM_TOKEN:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

# --- 5. منطق التحليل الفني ---
def get_market_data(symbol):
    path = "/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": "30m", "limit": 250}
    res = bingx_request("GET", path, params)
    if 'data' in res:
        df = pd.DataFrame(res['data'])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['open'] = df['open'].astype(float)
        return df
    return None

def check_signals(symbol):
    df = get_market_data(symbol)
    if df is None: return None
    
    # حساب المؤشرات
    df.ta.ema(length=EMA_PERIOD, append=True)
    df.ta.adx(append=True)
    df.ta.psar(step=SAR_STEP, max_step=SAR_MAX, append=True)
    
    last = df.iloc[-1]
    
    # الفلاتر (نصيحة الخبير)
    if last['ADX_14'] < ADX_THRESHOLD: return None
    candle_size = abs(last['close'] - last['open']) / last['open']
    if candle_size > 0.02: return None # شمعة كبيرة جداً

    ema_val = last[f'EMA_{EMA_PERIOD}']
    sar_bullish = pd.notna(last['PSARl_0.015_0.2'])

    if last['close'] > ema_val and sar_bullish: return "LONG"
    if last['close'] < ema_val and not sar_bullish: return "SHORT"
    return None

# --- 6. تنفيذ الصفقات وإدارتها ---
def trade_cycle():
    # 1. فحص الصفقات المفتوحة لإغلاقها (TP/SL/SAR)
    # (هنا يوضع كود فحص الربح والخسارة اللحظي)
    
    # 2. البحث عن فرص دخول جديدة
    for symbol in SYMBOLS:
        signal = check_signals(symbol)
        if signal:
            print(f"🎯 Signal found for {symbol}: {signal}")
            # كود فتح الصفقة الفعلي يوضع هنا

# --- 7. التشغيل الرئيسي ---
if __name__ == "__main__":
    send_telegram("🚀 *تم تشغيل نظام القناص المطور V2*\nتم دمج استراتيجية الخبير وحماية SAR.")
    while True:
        try:
            trade_cycle()
            time.sleep(60) # فحص كل دقيقة
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(30)
