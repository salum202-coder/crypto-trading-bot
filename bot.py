import os
import time
import pandas as pd
import numpy as np
import requests

# سحب المفاتيح من Render
API_KEY = os.getenv('API_KEY')
SECRET_KEY = os.getenv('SECRET_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# --- دالة حساب EMA يدوياً ---
def calculate_ema(df, period=200):
    return df['close'].ewm(span=period, adjust=False).mean()

# --- دالة حساب ADX يدوياً ---
def calculate_adx(df, period=14):
    df = df.copy()
    df['up'] = df['high'].diff()
    df['down'] = -df['low'].diff()
    df['plus_dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
    df['minus_dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
    tr = pd.concat([df['high'] - df['low'], abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift())], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (df['plus_dm'].rolling(window=period).mean() / atr)
    minus_di = 100 * (df['minus_dm'].rolling(window=period).mean() / atr)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx.rolling(window=period).mean()

# --- دالة حساب SAR يدوياً (نسخة مبسطة وفعالة) ---
def calculate_sar(df, step=0.015, max_step=0.2):
    # حسبة الـ SAR هنا تعتمد على مقارنة السعر بالقمة/القاع السابق
    # للتبسيط، سنستخدم منطق "تقاطع السعر مع القمم السابقة" كبديل للـ SAR
    return df['high'].rolling(window=5).max() # مثال تقريبي للتبسيط

if __name__ == "__main__":
    print("🚀 SMART BOT STARTED WITHOUT PANDAS-TA!")
    if TELEGRAM_TOKEN:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": "✅ *تم تشغيل البوت بنظام المعادلات اليدوية!*", "parse_mode": "Markdown"})
    
    while True:
        try:
            # هنا يوضع منطق التداول باستخدام المعادلات اللي فوق
            time.sleep(60)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)
