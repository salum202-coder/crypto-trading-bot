import subprocess
import sys

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

# تثبيت المكتبات الناقصة إجبارياً عند بدء التشغيل
try:
    import pandas_ta
except ImportError:
    install('pandas_ta')
import pandas_ta as ta
import pandas as pd
import time

# ==========================================
# ⚙️ 1. الإعدادات المحدثة (إدارة المخاطر الجديدة)
# ==========================================
LEVERAGE = 10
POSITION_SIZE = 0.15
TAKE_PROFIT = 0.015         # هدف الربح (15% ROE)
STOP_LOSS = 0.03            # حزام الأمان (30% ROE) - نصيحة الخبير
ADX_THRESHOLD = 25
EMA_PERIOD = 200

# إعدادات SAR الجديدة (أقل حساسية للتذبذب)
SAR_STEP = 0.015            # قللناها من 0.02 لتكون أبعد عن السعر قليلاً
SAR_MAX = 0.2

# نظام الكولداون (تجنب الدخول المتكرر بعد الخسارة)
COOLDOWN_PERIOD = 3600      # ساعة كاملة راحة للعملة الخاسرة
loss_tracker = {}           # لتسجيل وقت آخر خسارة لكل عملة

# ==========================================
# 🧠 2. منطق تحليل السوق (مع فلتر الشمعة الكبيرة)
# ==========================================
def check_entry_signal(symbol, df):
    # حساب المؤشرات
    df.ta.adx(append=True)
    df.ta.ema(length=EMA_PERIOD, append=True)
    df.ta.psar(step=SAR_STEP, max_step=SAR_MAX, append=True)
    
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    
    current_price = last_row['close']
    adx_value = last_row['ADX_14']
    ema_value = last_row[f'EMA_{EMA_PERIOD}']
    
    # تحديد قيمة SAR الحالية (سواء كانت صاعدة أو هابطة)
    sar_long = last_row['PSARl_0.015_0.2']
    sar_short = last_row['PSARs_0.015_0.2']
    is_sar_bullish = pd.notna(sar_long)

    # --- فلتر الكولداون ---
    if symbol in loss_tracker:
        if time.time() - loss_tracker[symbol] < COOLDOWN_PERIOD:
            return "COOLDOWN"

    # --- فلتر الشمعة الانتحارية (نصيحة الخبير) ---
    candle_body_pct = abs(last_row['close'] - last_row['open']) / last_row['open']
    if candle_body_pct > 0.02: # إذا الشمعة الوحدة تحركت أكثر من 2% لا تدخل
        return "CANDLE_TOO_BIG"

    # --- فلتر السيولة (ADX) ---
    if adx_value < ADX_THRESHOLD:
        return "LOW_VOLATILITY"

    # --- منطق الدخول (تطابق الشروط) ---
    if current_price > ema_value and is_sar_bullish:
        return "LONG"
    elif current_price < ema_value and not is_sar_bullish:
        return "SHORT"
        
    return "WAIT"

# ==========================================
# 🛡️ 3. منطق الخروج (مع عرض النسبة والتحكم اليدوي)
# ==========================================
def check_exit_signal(position):
    symbol = position['symbol']
    side = position['side']
    entry_price = float(position['entryPrice'])
    current_price = float(position['markPrice'])
    
    # حساب النسبة المئوية الحالية (التي ستظهر لك في التيليجرام)
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    if side == "SHORT":
        pnl_pct = -pnl_pct
    
    # 1. ضرب الهدف (Take Profit)
    if (side == "LONG" and current_price >= entry_price * (1 + TAKE_PROFIT)) or \
       (side == "SHORT" and current_price <= entry_price * (1 - TAKE_PROFIT)):
        return "TP_HIT", pnl_pct

    # 2. حزام الأمان (Hard Stop Loss)
    if (side == "LONG" and current_price <= entry_price * (1 - STOP_LOSS)) or \
       (side == "SHORT" and current_price >= entry_price * (1 + STOP_LOSS)):
        loss_tracker[symbol] = time.time() # تفعيل الكولداون
        return "STOP_LOSS_HIT", pnl_pct

    # 3. خروج SAR الذكي (انعكاس النقاط الزرقاء)
    # ملاحظة: يتم جلب الـ SAR المحدث من البيانات اللحظية
    if side == "LONG" and is_sar_reversed_to_short:
        return "SAR_EXIT", pnl_pct
    if side == "SHORT" and is_sar_reversed_to_long:
        return "SAR_EXIT", pnl_pct

    return "HOLD", pnl_pct

# ==========================================
# 🎮 4. واجهة التحكم في التيليجرام (أزرار التحكم)
# ==========================================
# (هذا الجزء يوضح لك شكل الأزرار التي ستظهر في رسالتك القادمة)
# [ إغلاق جميع الصفقات 🔴 ]
# [ إغلاق الرابحة فقط 🟢 ]  [ إغلاق الخاسرة فقط 🟡 ]
