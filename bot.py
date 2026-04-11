import os
import time
import hmac
import hashlib
import requests
import pandas as pd
import pandas_ta as ta  # سيتم تحميلها عبر requirements.txt

# ==========================================
# 🔑 1. جلب المفاتيح من إعدادات Render
# ==========================================
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
BINGX_URL = "https://open-api.bingx.com"

# ==========================================
# ⚙️ 2. إعدادات استراتيجية الخبير (المطورة)
# ==========================================
SYMBOLS = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT', 'BNB-USDT', 'DOGE-USDT']
LEVERAGE = 10
POSITION_SIZE_PCT = 0.15    # 15% من رأس المال
TAKE_PROFIT = 0.015         # هدف الربح 1.5% (15% ROE)
STOP_LOSS = 0.03            # حزام الأمان 3% (30% ROE)
EMA_PERIOD = 200
SAR_STEP = 0.015            # حساسية SAR موزونة
SAR_MAX = 0.2
ADX_THRESHOLD = 25
COOLDOWN_TIME = 3600        # ساعة راحة للعملة الخاسرة

cooldown_tracker = {}

# ==========================================
# 📡 3. وظائف الاتصال (API Helpers)
# ==========================================
def get_sign(params_str):
    return hmac.new(SECRET_KEY.encode("utf-8"), params_str.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

def bingx_request(method, path, params={}):
    params['timestamp'] = int(time.time() * 1000)
    params['apiKey'] = API_KEY
    sorted_params = dict(sorted(params.items()))
    params_str = "&".join([f"{k}={v}" for k, v in sorted_params.items()])
    sign = get_sign(params_str)
    url = f"{BINGX_URL}{path}?{params_str}&signature={sign}"
    try:
        response = requests.request(method, url)
        return response.json()
    except Exception as e:
        print(f"Connection Error: {e}")
        return None

def send_telegram(msg):
    if TELEGRAM_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try: requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        except: pass

# ==========================================
# 📊 4. محرك التحليل الفني
# ==========================================
def get_data_and_analyze(symbol):
    path = "/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": "30m", "limit": 250}
    res = bingx_request("GET", path, params)
    
    if res and 'data' in res:
        df = pd.DataFrame(res['data'])
        for col in ['close', 'high', 'low', 'open']:
            df[col] = df[col].astype(float)
        
        # حساب المؤشرات (الخبير)
        df.ta.ema(length=EMA_PERIOD, append=True)
        df.ta.adx(append=True)
        df.ta.psar(step=SAR_STEP, max_step=SAR_MAX, append=True)
        
        last = df.iloc[-1]
        
        # الفلاتر الدفاعية
        if last['ADX_14'] < ADX_THRESHOLD: return None
        candle_size = abs(last['close'] - last['open']) / last['open']
        if candle_size > 0.02: return None # تجنب الشموع الضخمة

        # تحديد الاتجاه
        ema_val = last[f'EMA_{EMA_PERIOD}']
        sar_bullish = pd.notna(last['PSARl_0.015_0.2']) # النقطة الزرقاء تحت السعر

        if last['close'] > ema_val and sar_bullish: return "LONG"
        if last['close'] < ema_val and not sar_bullish: return "SHORT"
    
    return None

# ==========================================
# 🛡️ 5. إدارة الصفقات (إغلاق وفتح)
# ==========================================
def manage_trades():
    # هنا يتم جلب الصفقات المفتوحة حالياً من BingX
    path = "/openApi/swap/v2/user/positions"
    pos_res = bingx_request("GET", path)
    
    if pos_res and 'data' in pos_res:
        for pos in pos_res['data']:
            symbol = pos['symbol']
            entry = float(pos['entryPrice'])
            mark = float(pos['markPrice'])
            side = pos['positionSide'] # LONG or SHORT
            
            # حساب الربح اللحظي ROE
            pnl = ((mark - entry) / entry) * 100 * (1 if side == "LONG" else -1)
            
            # 1. هدف الربح (15% ROE)
            if pnl >= (TAKE_PROFIT * 100):
                print(f"🎯 Target Hit for {symbol}!")
                # كود إغلاق الصفقة
            
            # 2. وقف الخسارة الثابت (30% ROE)
            elif pnl <= -(STOP_LOSS * 100):
                cooldown_tracker[symbol] = time.time()
                print(f"🛑 Stop Loss Hit for {symbol}!")
                # كود إغلاق الصفقة

# ==========================================
# 🚀 6. الحلقة الرئيسية (The Master Loop)
# ==========================================
if __name__ == "__main__":
    print("🚀 Bot is initializing...")
    send_telegram("🤖 *تم تشغيل القناص المطور V2 بنجاح!*\n\n• الاستراتيجية: EMA 200 + SAR + ADX\n• الحماية: وقف ثابت 30% + كولداون.")
    
    while True:
        try:
            manage_trades() # فحص الصفقات المفتوحة
            
            for symbol in SYMBOLS:
                # التأكد من الكولداون
                if symbol in cooldown_tracker:
                    if time.time() - cooldown_tracker[symbol] < COOLDOWN_TIME:
                        continue

                signal = get_data_and_analyze(symbol)
                if signal:
                    print(f"🔥 Signal Detected: {symbol} -> {signal}")
                    # كود فتح الصفقة الفعلي يوضع هنا بناءً على الـ API
            
            time.sleep(60) # فحص كل دقيقة
        except Exception as e:
            print(f"System Error: {e}")
            time.sleep(30)
