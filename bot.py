import os
import json
import asyncio
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, List, Tuple

import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# =========================================================
# 1) CONFIG
# =========================================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET = os.getenv("BINGX_SECRET")
PORT = int(os.environ.get("PORT", "8080"))

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN")
if not BINGX_API_KEY or not BINGX_SECRET:
    raise RuntimeError("Missing BINGX_API_KEY or BINGX_SECRET")

# BingX Perpetual USDT-M
MARGIN_MODE = "isolated"

# Mode system
BOT_MODE = "NORMAL"  # NORMAL / AGGRESSIVE

NORMAL_LEVERAGE = 3
AGGRESSIVE_LEVERAGE = 5
DEFAULT_LEVERAGE = NORMAL_LEVERAGE

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "DOGE/USDT:USDT",
    "LINK/USDT:USDT",
    "AVAX/USDT:USDT",
    "LTC/USDT:USDT",
    "TRX/USDT:USDT",
    "BCH/USDT:USDT",
    "ATOM/USDT:USDT",
    "NEAR/USDT:USDT",
]

TF_4H = "4h"
TF_1H = "1h"
TF_15M = "15m"

MAX_OPEN_POSITIONS = 2
COOLDOWN_MINUTES = 60

# Risk can be changed from Telegram
risk_per_trade = 0.005  # 0.5%

ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST_PERIOD = 20
VOLUME_SMA_PERIOD = 20

# Ichimoku settings
ICHIMOKU_TENKAN = 9
ICHIMOKU_KIJUN = 26
ICHIMOKU_SENKOU_B = 52
ICHIMOKU_DISPLACEMENT = 26

# Smart score thresholds
ENTRY_SCORE = 82
WATCH_SCORE = 70
MIN_RR = 1.35

# Entry filters
MAX_DISTANCE_FROM_KIJUN_15M = 0.018  # 1.8%
MAX_ATR_PERCENT_15M = 0.035          # avoid crazy volatility
MIN_ATR_PERCENT_15M = 0.0015         # avoid dead market

# Exit logic
TP1_R = 0.9
TP2_R = 1.8
TRAILING_ATR_MULTIPLIER = 1.15
EARLY_EXIT_SCORE = 45
EMERGENCY_EXIT_SCORE = 35

SCAN_INTERVAL_SECONDS = 60
POSITION_CHECK_INTERVAL_SECONDS = 20

# Persistent stats file
STATS_FILE = Path("stats.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ICHIMOKU_SMART_BOT")

exchange = ccxt.bingx({
    "apiKey": BINGX_API_KEY,
    "secret": BINGX_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
    },
})

# =========================================================
# 2) STATE
# =========================================================
cooldowns: Dict[str, int] = {}
trade_state: Dict[str, Dict[str, Any]] = {}
last_scan_summary = "No scans yet."
last_signal_summary = "No signals yet."
bot_paused = False

daily_stats = {}
all_stats = {}

# =========================================================
# 3) BASIC HELPERS
# =========================================================
def now_ts() -> int:
    return int(time.time())


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def format_num(v: float, digits: int = 6) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def get_active_chat_id(context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Optional[str]:
    if context and context.bot_data.get("chat_id"):
        return str(context.bot_data["chat_id"])
    if ENV_CHAT_ID:
        return str(ENV_CHAT_ID)
    return None


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = get_active_chat_id(context)
    if not chat_id:
        logger.info("No active chat id yet; skipping notification")
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


def is_in_cooldown(symbol: str) -> bool:
    return now_ts() < cooldowns.get(symbol, 0)


def set_cooldown(symbol: str) -> None:
    cooldowns[symbol] = now_ts() + COOLDOWN_MINUTES * 60


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    val = sum(values[:period]) / period
    for x in values[period:]:
        val = x * k + val * (1 - k)
    return val


def rsi(values: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr_from_ohlcv(ohlcv: List[List[float]], period: int) -> Optional[float]:
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        prev_close = ohlcv[i - 1][4]
        curr_high = ohlcv[i][2]
        curr_low = ohlcv[i][3]
        tr = max(
            curr_high - curr_low,
            abs(curr_high - prev_close),
            abs(curr_low - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def normalize_amount(symbol: str, amount: float) -> float:
    try:
        return float(exchange.amount_to_precision(symbol, amount))
    except Exception:
        return amount


def normalize_price(symbol: str, price: float) -> float:
    try:
        return float(exchange.price_to_precision(symbol, price))
    except Exception:
        return price


# =========================================================
# 3.1) ICHIMOKU HELPERS
# =========================================================
def midpoint_high_low(ohlcv: List[List[float]], period: int, end_index: int) -> Optional[float]:
    start = end_index - period + 1
    if start < 0:
        return None
    window = ohlcv[start:end_index + 1]
    high = max(c[2] for c in window)
    low = min(c[3] for c in window)
    return (high + low) / 2


def ichimoku_snapshot(ohlcv: List[List[float]]) -> Optional[dict]:
    """
    Practical Ichimoku snapshot for bot decisions.
    For current cloud, we compare price with the cloud values projected from 26 candles ago.
    """
    required = ICHIMOKU_SENKOU_B + ICHIMOKU_DISPLACEMENT + 5
    if len(ohlcv) < required:
        return None

    last = len(ohlcv) - 1
    cloud_source = last - ICHIMOKU_DISPLACEMENT

    tenkan_now = midpoint_high_low(ohlcv, ICHIMOKU_TENKAN, last)
    kijun_now = midpoint_high_low(ohlcv, ICHIMOKU_KIJUN, last)

    tenkan_cloud = midpoint_high_low(ohlcv, ICHIMOKU_TENKAN, cloud_source)
    kijun_cloud = midpoint_high_low(ohlcv, ICHIMOKU_KIJUN, cloud_source)
    span_b_current = midpoint_high_low(ohlcv, ICHIMOKU_SENKOU_B, cloud_source)

    if tenkan_now is None or kijun_now is None or tenkan_cloud is None or kijun_cloud is None or span_b_current is None:
        return None

    span_a_current = (tenkan_cloud + kijun_cloud) / 2
    cloud_top = max(span_a_current, span_b_current)
    cloud_bottom = min(span_a_current, span_b_current)
    close = ohlcv[-1][4]

    # Future cloud from current candle.
    span_a_future = (tenkan_now + kijun_now) / 2
    span_b_future = midpoint_high_low(ohlcv, ICHIMOKU_SENKOU_B, last)
    if span_b_future is None:
        return None

    if close > cloud_top:
        price_vs_cloud = "ABOVE"
    elif close < cloud_bottom:
        price_vs_cloud = "BELOW"
    else:
        price_vs_cloud = "INSIDE"

    tk_bias = "BULL" if tenkan_now > kijun_now else "BEAR" if tenkan_now < kijun_now else "FLAT"
    future_cloud_bias = "BULL" if span_a_future > span_b_future else "BEAR" if span_a_future < span_b_future else "FLAT"
    cloud_thickness = abs(cloud_top - cloud_bottom) / close if close > 0 else 999

    return {
        "tenkan": tenkan_now,
        "kijun": kijun_now,
        "span_a_current": span_a_current,
        "span_b_current": span_b_current,
        "cloud_top": cloud_top,
        "cloud_bottom": cloud_bottom,
        "price_vs_cloud": price_vs_cloud,
        "tk_bias": tk_bias,
        "future_cloud_bias": future_cloud_bias,
        "cloud_thickness": cloud_thickness,
    }


def ichimoku_trend(ichi: dict) -> str:
    if ichi["price_vs_cloud"] == "ABOVE" and ichi["tk_bias"] == "BULL":
        return "BULL"
    if ichi["price_vs_cloud"] == "BELOW" and ichi["tk_bias"] == "BEAR":
        return "BEAR"
    if ichi["price_vs_cloud"] == "INSIDE":
        return "RANGE"
    return "MIXED"


def candle_strength(ohlcv: List[List[float]]) -> dict:
    if len(ohlcv) < 3:
        return {"body_ratio": 0.0, "direction": "FLAT"}
    o, h, l, c = ohlcv[-1][1], ohlcv[-1][2], ohlcv[-1][3], ohlcv[-1][4]
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    body_ratio = body / rng
    direction = "BULL" if c > o else "BEAR" if c < o else "FLAT"
    return {"body_ratio": body_ratio, "direction": direction}


def recent_structure(ohlcv: List[List[float]], lookback: int = 8) -> str:
    if len(ohlcv) < lookback + 3:
        return "UNKNOWN"
    highs = [c[2] for c in ohlcv[-lookback:]]
    lows = [c[3] for c in ohlcv[-lookback:]]
    if highs[-1] > highs[0] and lows[-1] > lows[0]:
        return "BULL"
    if highs[-1] < highs[0] and lows[-1] < lows[0]:
        return "BEAR"
    return "RANGE"


def volume_ok(ohlcv: List[List[float]]) -> Tuple[bool, float]:
    if len(ohlcv) < VOLUME_SMA_PERIOD + 1:
        return False, 0.0
    vols = [safe_float(c[5]) for c in ohlcv]
    avg = sma(vols[:-1], VOLUME_SMA_PERIOD)
    current = vols[-1]
    if not avg or avg <= 0:
        return False, 0.0
    ratio = current / avg
    return ratio >= 0.9, ratio


def score_setup(
    side: str,
    bars_4h: List[List[float]],
    bars_1h: List[List[float]],
    bars_15m: List[List[float]],
    ichi_4h: dict,
    ichi_1h: dict,
    ichi_15m: dict,
    atr_15m: float,
    rsi_15m: float,
) -> dict:
    close_15m = bars_15m[-1][4]
    atr_pct = atr_15m / close_15m if close_15m > 0 else 999
    candle = candle_strength(bars_15m)
    structure_15m = recent_structure(bars_15m)
    vol_ok, vol_ratio = volume_ok(bars_15m)

    score = 0
    reasons = []

    trend_4h = ichimoku_trend(ichi_4h)
    trend_1h = ichimoku_trend(ichi_1h)
    trend_15m = ichimoku_trend(ichi_15m)

    want = "BULL" if side == "LONG" else "BEAR"
    cloud_side = "ABOVE" if side == "LONG" else "BELOW"

    # 4H macro direction
    if trend_4h == want:
        score += 25
        reasons.append(f"4H {want} فوق/تحت السحابة بشكل واضح")
    elif ichi_4h["price_vs_cloud"] == cloud_side:
        score += 15
        reasons.append("4H السعر في جهة الاتجاه لكن Tenkan/Kijun غير مثالي")
    elif trend_4h == "RANGE":
        score -= 25
        reasons.append("4H داخل السحابة = سوق غير واضح")
    else:
        score -= 35
        reasons.append("4H ضد اتجاه الصفقة")

    # 1H confirmation
    if trend_1h == want:
        score += 20
        reasons.append(f"1H يؤكد الاتجاه {want}")
    elif ichi_1h["price_vs_cloud"] == cloud_side:
        score += 10
        reasons.append("1H في جهة الاتجاه لكن التأكيد متوسط")
    elif trend_1h == "RANGE":
        score -= 15
        reasons.append("1H داخل السحابة")
    else:
        score -= 25
        reasons.append("1H ضد الصفقة")

    # 15m entry condition
    if trend_15m == want:
        score += 15
        reasons.append("15m متوافق للدخول")
    elif ichi_15m["price_vs_cloud"] == cloud_side:
        score += 8
        reasons.append("15m فوق/تحت السحابة لكن الزخم متوسط")
    else:
        score -= 12
        reasons.append("15m غير مناسب للدخول")

    # Pullback quality around Kijun
    distance_kijun = abs(close_15m - ichi_15m["kijun"]) / ichi_15m["kijun"] if ichi_15m["kijun"] > 0 else 999
    if distance_kijun <= MAX_DISTANCE_FROM_KIJUN_15M:
        score += 12
        reasons.append("الدخول قريب من Kijun وليس مطاردة")
    else:
        score -= 10
        reasons.append("السعر بعيد عن Kijun، احتمال الدخول متأخر")

    # Momentum by Tenkan/Kijun and future cloud
    if ichi_1h["future_cloud_bias"] == want and ichi_15m["tk_bias"] == want:
        score += 10
        reasons.append("زخم Ichimoku داعم")
    elif ichi_15m["tk_bias"] == want:
        score += 5
        reasons.append("زخم 15m داعم جزئيًا")

    # Candle strength
    if candle["direction"] == want and candle["body_ratio"] >= 0.45:
        score += 8
        reasons.append("شمعة الدخول قوية")
    elif candle["direction"] != want:
        score -= 8
        reasons.append("شمعة الدخول عكس الاتجاه")

    # Structure
    if structure_15m == want:
        score += 6
        reasons.append("هيكل 15m داعم")
    elif structure_15m == "RANGE":
        score -= 4
        reasons.append("هيكل 15m عرضي")

    # Volume
    if vol_ok:
        score += 5
        reasons.append(f"الحجم جيد ({vol_ratio:.2f}x)")
    else:
        score -= 3
        reasons.append(f"الحجم ضعيف ({vol_ratio:.2f}x)")

    # Volatility filter
    if MIN_ATR_PERCENT_15M <= atr_pct <= MAX_ATR_PERCENT_15M:
        score += 4
        reasons.append("التذبذب مناسب")
    elif atr_pct > MAX_ATR_PERCENT_15M:
        score -= 18
        reasons.append("التذبذب عالي جدًا")
    else:
        score -= 10
        reasons.append("السوق بارد جدًا")

    # RSI sanity
    if side == "LONG":
        if 42 <= rsi_15m <= 68:
            score += 5
            reasons.append("RSI مناسب للونق")
        elif rsi_15m > 75:
            score -= 12
            reasons.append("RSI مرتفع جدًا، احتمال مطاردة")
    else:
        if 32 <= rsi_15m <= 58:
            score += 5
            reasons.append("RSI مناسب للشورت")
        elif rsi_15m < 25:
            score -= 12
            reasons.append("RSI منخفض جدًا، احتمال مطاردة")

    score = max(0, min(100, score))

    return {
        "score": score,
        "reasons": reasons,
        "atr_pct": atr_pct,
        "distance_kijun": distance_kijun,
        "trend_4h": trend_4h,
        "trend_1h": trend_1h,
        "trend_15m": trend_15m,
        "volume_ratio": vol_ratio,
        "rsi": rsi_15m,
    }


def decide_best_side(
    bars_4h: List[List[float]],
    bars_1h: List[List[float]],
    bars_15m: List[List[float]],
    ichi_4h: dict,
    ichi_1h: dict,
    ichi_15m: dict,
    atr_15m: float,
    rsi_15m: float,
) -> Tuple[Optional[str], dict, dict]:
    long_score = score_setup("LONG", bars_4h, bars_1h, bars_15m, ichi_4h, ichi_1h, ichi_15m, atr_15m, rsi_15m)
    short_score = score_setup("SHORT", bars_4h, bars_1h, bars_15m, ichi_4h, ichi_1h, ichi_15m, atr_15m, rsi_15m)

    if long_score["score"] > short_score["score"] and long_score["score"] >= WATCH_SCORE:
        return "LONG", long_score, short_score
    if short_score["score"] > long_score["score"] and short_score["score"] >= WATCH_SCORE:
        return "SHORT", short_score, long_score

    return None, long_score, short_score


# =========================================================
# 3.2) STATS PERSISTENCE
# =========================================================
def empty_daily_stats() -> dict:
    return {
        "date": utc_today(),
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
    }


def empty_all_stats() -> dict:
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "best_win": 0.0,
        "worst_loss": 0.0,
        "by_symbol": {},
    }


def save_stats_to_disk() -> None:
    try:
        payload = {
            "daily_stats": daily_stats,
            "all_stats": all_stats,
        }
        STATS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"save_stats_to_disk error: {e}")


def load_stats_from_disk() -> None:
    global daily_stats, all_stats

    if not STATS_FILE.exists():
        daily_stats = empty_daily_stats()
        all_stats = empty_all_stats()
        save_stats_to_disk()
        return

    try:
        raw = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        daily_stats = raw.get("daily_stats") or empty_daily_stats()
        all_stats = raw.get("all_stats") or empty_all_stats()
    except Exception as e:
        logger.error(f"load_stats_from_disk error: {e}")
        daily_stats = empty_daily_stats()
        all_stats = empty_all_stats()
        save_stats_to_disk()

    daily_stats.setdefault("date", utc_today())
    daily_stats.setdefault("closed_trades", 0)
    daily_stats.setdefault("wins", 0)
    daily_stats.setdefault("losses", 0)
    daily_stats.setdefault("realized_pnl", 0.0)

    all_stats.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    all_stats.setdefault("closed_trades", 0)
    all_stats.setdefault("wins", 0)
    all_stats.setdefault("losses", 0)
    all_stats.setdefault("realized_pnl", 0.0)
    all_stats.setdefault("best_win", 0.0)
    all_stats.setdefault("worst_loss", 0.0)
    all_stats.setdefault("by_symbol", {})

    save_stats_to_disk()


def reset_daily_stats_if_needed() -> None:
    global daily_stats
    today = utc_today()
    if daily_stats.get("date") != today:
        daily_stats = empty_daily_stats()
        save_stats_to_disk()


def update_symbol_stats(symbol: str, pnl_pct: float) -> None:
    symbol_stats = all_stats["by_symbol"].get(symbol, {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "best_win": 0.0,
        "worst_loss": 0.0,
    })

    symbol_stats["closed_trades"] += 1
    symbol_stats["realized_pnl"] += pnl_pct

    if pnl_pct >= 0:
        symbol_stats["wins"] += 1
        symbol_stats["best_win"] = max(safe_float(symbol_stats.get("best_win", 0.0)), pnl_pct)
    else:
        symbol_stats["losses"] += 1
        symbol_stats["worst_loss"] = min(safe_float(symbol_stats.get("worst_loss", 0.0)), pnl_pct)

    all_stats["by_symbol"][symbol] = symbol_stats


def build_today_stats_text() -> str:
    reset_daily_stats_if_needed()
    win_rate = 0.0
    if daily_stats["closed_trades"] > 0:
        win_rate = (daily_stats["wins"] / daily_stats["closed_trades"]) * 100

    return (
        f"📈 إحصائيات اليوم\n"
        f"التاريخ: {daily_stats['date']}\n"
        f"الصفقات المغلقة: {daily_stats['closed_trades']}\n"
        f"الرابحة: {daily_stats['wins']}\n"
        f"الخاسرة: {daily_stats['losses']}\n"
        f"نسبة النجاح: {win_rate:.2f}%\n"
        f"إجمالي PnL%: {daily_stats['realized_pnl']:.2f}%"
    )


def build_all_stats_text() -> str:
    total = all_stats["closed_trades"]
    wins = all_stats["wins"]
    losses = all_stats["losses"]
    pnl = all_stats["realized_pnl"]
    best_win = all_stats["best_win"]
    worst_loss = all_stats["worst_loss"]

    win_rate = 0.0
    if total > 0:
        win_rate = (wins / total) * 100

    return (
        f"📊 الإحصائيات الكاملة\n"
        f"منذ: {all_stats['started_at'][:10]}\n"
        f"إجمالي الصفقات المغلقة: {total}\n"
        f"الرابحة: {wins}\n"
        f"الخاسرة: {losses}\n"
        f"نسبة النجاح: {win_rate:.2f}%\n"
        f"إجمالي PnL%: {pnl:.2f}%\n"
        f"أفضل صفقة: {best_win:.2f}%\n"
        f"أسوأ صفقة: {worst_loss:.2f}%"
    )


def build_best_symbols_text(limit: int = 5) -> str:
    stats_by_symbol = all_stats.get("by_symbol", {})
    if not stats_by_symbol:
        return "📌 لا توجد بيانات صفقات كافية بعد."

    ranked = sorted(
        stats_by_symbol.items(),
        key=lambda item: safe_float(item[1].get("realized_pnl", 0.0)),
        reverse=True,
    )[:limit]

    lines = ["🏆 أفضل العملات أداءً:"]
    for symbol, data in ranked:
        total = int(data.get("closed_trades", 0))
        pnl = safe_float(data.get("realized_pnl", 0.0))
        wins = int(data.get("wins", 0))
        losses = int(data.get("losses", 0))
        win_rate = (wins / total * 100) if total > 0 else 0.0
        lines.append(
            f"{symbol} | Trades: {total} | WinRate: {win_rate:.1f}% | PnL: {pnl:.2f}%"
        )
    return "\n".join(lines)


# =========================================================
# 4) EXCHANGE WRAPPERS
# =========================================================
def fetch_balance_usdt() -> float:
    try:
        bal = exchange.fetch_balance({"type": "swap"})
        if isinstance(bal.get("USDT"), dict):
            return safe_float(bal["USDT"].get("free", 0.0))
        if isinstance(bal.get("free"), dict):
            return safe_float(bal["free"].get("USDT", 0.0))
        return 0.0
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return 0.0


def fetch_positions() -> List[dict]:
    try:
        return exchange.fetch_positions(params={"type": "swap"})
    except Exception as e:
        logger.error(f"fetch_positions error: {e}")
        return []


def get_symbol_position(symbol: str) -> Optional[dict]:
    try:
        positions = exchange.fetch_positions([symbol], params={"type": "swap"})
        for p in positions:
            if safe_float(p.get("contracts", 0)) != 0:
                return p
        return None
    except Exception as e:
        logger.error(f"{symbol}: get_symbol_position error: {e}")
        return None


def count_open_positions() -> int:
    count = 0
    for pos in fetch_positions():
        if safe_float(pos.get("contracts", 0)) != 0:
            count += 1
    return count


def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[List[List[float]]]:
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        logger.error(f"{symbol} {timeframe} fetch_ohlcv error: {e}")
        return None


def set_leverage_and_margin(symbol: str, leverage: int) -> None:
    try:
        exchange.set_position_mode(False, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set position mode warning: {e}")

    try:
        exchange.set_margin_mode(MARGIN_MODE, symbol)
    except Exception as e:
        logger.warning(f"{symbol}: set margin mode warning: {e}")

    try:
        exchange.set_leverage(leverage, symbol, {"side": "BOTH"})
    except Exception as e:
        logger.warning(f"{symbol}: set leverage warning: {e}")


def get_order_params(reduce_only: bool = False) -> dict:
    params = {
        "positionSide": "BOTH",
    }
    if reduce_only:
        params["reduceOnly"] = True
    return params


# =========================================================
# 5) STRATEGY - ICHIMOKU SMART
# =========================================================
def build_reason_text(reasons: List[str], limit: int = 4) -> str:
    if not reasons:
        return "No detailed reason."
    return " | ".join(reasons[:limit])


def get_market_snapshot(symbol: str) -> Optional[dict]:
    bars_4h = fetch_ohlcv_safe(symbol, TF_4H, 180)
    bars_1h = fetch_ohlcv_safe(symbol, TF_1H, 180)
    bars_15m = fetch_ohlcv_safe(symbol, TF_15M, 180)

    if not bars_4h or not bars_1h or not bars_15m:
        return None

    ichi_4h = ichimoku_snapshot(bars_4h)
    ichi_1h = ichimoku_snapshot(bars_1h)
    ichi_15m = ichimoku_snapshot(bars_15m)

    closes_15m = [b[4] for b in bars_15m]
    atr_15m = atr_from_ohlcv(bars_15m, ATR_PERIOD)
    rsi_15m = rsi(closes_15m, RSI_PERIOD)
    ema20_15m = ema(closes_15m, EMA_FAST_PERIOD)

    if not ichi_4h or not ichi_1h or not ichi_15m or atr_15m is None or rsi_15m is None or ema20_15m is None:
        return None

    best_side, best_score, other_score = decide_best_side(
        bars_4h,
        bars_1h,
        bars_15m,
        ichi_4h,
        ichi_1h,
        ichi_15m,
        atr_15m,
        rsi_15m,
    )

    close_15m = bars_15m[-1][4]

    base = {
        "symbol": symbol,
        "bars_4h": bars_4h,
        "bars_1h": bars_1h,
        "bars_15m": bars_15m,
        "close_15m": close_15m,
        "atr_15m": atr_15m,
        "rsi_15m": rsi_15m,
        "ema20_15m": ema20_15m,
        "ichi_4h": ichi_4h,
        "ichi_1h": ichi_1h,
        "ichi_15m": ichi_15m,
    }

    if not best_side:
        long_s = best_score["score"]
        short_s = other_score["score"]
        return {
            **base,
            "signal": "NO_TRADE",
            "score": max(long_s, short_s),
            "side": None,
            "reason": f"No clean side | LongScore {long_s}/100 | ShortScore {short_s}/100",
            "reasons": [],
        }

    score = best_score["score"]
    reasons = best_score["reasons"]
    reason_text = build_reason_text(reasons)

    if score >= ENTRY_SCORE:
        return {
            **base,
            "signal": best_side,
            "side": best_side,
            "score": score,
            "reason": f"Ichimoku Smart Score {score}/100 | {reason_text}",
            "reasons": reasons,
        }

    if score >= WATCH_SCORE:
        return {
            **base,
            "signal": "WATCH",
            "side": best_side,
            "score": score,
            "reason": f"{best_side} setup قريب لكن ليس قوي كفاية | Score {score}/100 | {reason_text}",
            "reasons": reasons,
        }

    return {
        **base,
        "signal": "NO_TRADE",
        "side": best_side,
        "score": score,
        "reason": f"Weak setup | Score {score}/100",
        "reasons": reasons,
    }


def calculate_trade_plan(
    symbol: str,
    snapshot: dict,
    side: str,
    balance: float,
    risk_multiplier: float = 1.0,
) -> Optional[dict]:
    entry = snapshot["close_15m"]
    atr_15m = snapshot["atr_15m"]
    bars_15m = snapshot["bars_15m"]
    ichi_15m = snapshot["ichi_15m"]

    if entry <= 0 or atr_15m <= 0:
        return None

    if side == "LONG":
        swing_stop = min([b[3] for b in bars_15m[-6:]])
        cloud_stop = ichi_15m["cloud_bottom"]
        atr_stop = entry - atr_15m * 1.4
        stop_loss = min(swing_stop, atr_stop, cloud_stop)
        risk_per_unit = entry - stop_loss
        tp1 = entry + risk_per_unit * TP1_R
        tp2 = entry + risk_per_unit * TP2_R
    else:
        swing_stop = max([b[2] for b in bars_15m[-6:]])
        cloud_stop = ichi_15m["cloud_top"]
        atr_stop = entry + atr_15m * 1.4
        stop_loss = max(swing_stop, atr_stop, cloud_stop)
        risk_per_unit = stop_loss - entry
        tp1 = entry - risk_per_unit * TP1_R
        tp2 = entry - risk_per_unit * TP2_R

    if risk_per_unit <= 0:
        return None

    rr_to_tp2 = abs(tp2 - entry) / risk_per_unit
    if rr_to_tp2 < MIN_RR:
        return None

    effective_risk = risk_per_trade * risk_multiplier
    risk_amount = balance * effective_risk
    raw_amount = risk_amount / risk_per_unit
    amount = normalize_amount(symbol, raw_amount)

    if amount <= 0:
        return None

    return {
        "entry": normalize_price(symbol, entry),
        "stop_loss": normalize_price(symbol, stop_loss),
        "tp1": normalize_price(symbol, tp1),
        "tp2": normalize_price(symbol, tp2),
        "amount": amount,
        "risk_amount": risk_amount,
        "risk_per_unit": risk_per_unit,
        "effective_risk": effective_risk,
        "score": snapshot.get("score", 0),
    }


def get_exit_score(symbol: str, side: str) -> Optional[dict]:
    snapshot = get_market_snapshot(symbol)
    if not snapshot:
        return None

    opposite = "SHORT" if side == "LONG" else "LONG"
    bars_4h = snapshot["bars_4h"]
    bars_1h = snapshot["bars_1h"]
    bars_15m = snapshot["bars_15m"]
    ichi_4h = snapshot["ichi_4h"]
    ichi_1h = snapshot["ichi_1h"]
    ichi_15m = snapshot["ichi_15m"]
    atr_15m = snapshot["atr_15m"]
    rsi_15m = snapshot["rsi_15m"]

    current_score = score_setup(
        side, bars_4h, bars_1h, bars_15m, ichi_4h, ichi_1h, ichi_15m, atr_15m, rsi_15m
    )
    opposite_score = score_setup(
        opposite, bars_4h, bars_1h, bars_15m, ichi_4h, ichi_1h, ichi_15m, atr_15m, rsi_15m
    )

    return {
        "current_score": current_score["score"],
        "opposite_score": opposite_score["score"],
        "reason": build_reason_text(current_score["reasons"], limit=3),
        "close": snapshot["close_15m"],
        "atr_15m": atr_15m,
    }


# =========================================================
# 6) ORDERS / POSITION MANAGEMENT
# =========================================================
def open_position(symbol: str, side: str, plan: dict, snapshot: dict) -> bool:
    order_side = "buy" if side == "LONG" else "sell"
    try:
        leverage = AGGRESSIVE_LEVERAGE if BOT_MODE == "AGGRESSIVE" else NORMAL_LEVERAGE
        set_leverage_and_margin(symbol, leverage)

        order = exchange.create_market_order(
            symbol,
            order_side,
            plan["amount"],
            params=get_order_params(reduce_only=False),
        )
        logger.info(f"{symbol}: opened {side} -> {order.get('id')}")

        trade_state[symbol] = {
            "symbol": symbol,
            "side": side,
            "entry": plan["entry"],
            "stop_loss": plan["stop_loss"],
            "tp1": plan["tp1"],
            "tp2": plan["tp2"],
            "tp1_taken": False,
            "tp2_taken": False,
            "trailing_active": False,
            "trailing_stop": None,
            "opened_at": now_ts(),
            "entry_score": snapshot.get("score", 0),
            "entry_reason": snapshot.get("reason", ""),
        }
        return True
    except Exception as e:
        logger.error(f"{symbol}: open position error: {e}")
        return False


def close_position(symbol: str, position: dict, portion: float = 1.0) -> bool:
    try:
        contracts = safe_float(position.get("contracts", 0))
        if contracts <= 0:
            return False

        amount = normalize_amount(symbol, contracts * portion)
        if amount <= 0:
            return False

        side = str(position.get("side", "")).lower()
        close_side = "sell" if side == "long" else "buy"

        exchange.create_market_order(
            symbol,
            close_side,
            amount,
            params=get_order_params(reduce_only=True),
        )
        return True
    except Exception as e:
        logger.error(f"{symbol}: close position error: {e}")
        return False


def record_closed_trade(symbol: str, entry_price: float, exit_price: float, side: str) -> None:
    reset_daily_stats_if_needed()

    pnl_pct = 0.0
    if entry_price > 0:
        if side == "LONG":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100

    daily_stats["closed_trades"] += 1
    daily_stats["realized_pnl"] += pnl_pct
    if pnl_pct >= 0:
        daily_stats["wins"] += 1
    else:
        daily_stats["losses"] += 1

    all_stats["closed_trades"] += 1
    all_stats["realized_pnl"] += pnl_pct
    if pnl_pct >= 0:
        all_stats["wins"] += 1
        all_stats["best_win"] = max(safe_float(all_stats.get("best_win", 0.0)), pnl_pct)
    else:
        all_stats["losses"] += 1
        all_stats["worst_loss"] = min(safe_float(all_stats.get("worst_loss", 0.0)), pnl_pct)

    update_symbol_stats(symbol, pnl_pct)
    save_stats_to_disk()


async def notify_close(context: ContextTypes.DEFAULT_TYPE, symbol: str, side: str, reason: str, entry: float, exit_price: float) -> None:
    pnl_pct = 0.0
    if entry > 0:
        if side == "LONG":
            pnl_pct = ((exit_price - entry) / entry) * 100
        else:
            pnl_pct = ((entry - exit_price) / entry) * 100

    await notify(
        context,
        (
            f"📌 Position Closed\n"
            f"Symbol: {symbol}\n"
            f"Side: {side}\n"
            f"Reason: {reason}\n"
            f"Entry: {format_num(entry, 6)}\n"
            f"Exit: {format_num(exit_price, 6)}\n"
            f"PnL%: {format_num(pnl_pct, 2)}%"
        )
    )


async def manage_open_positions(context: ContextTypes.DEFAULT_TYPE):
    positions = fetch_positions()
    if not positions:
        return

    for pos in positions:
        contracts = safe_float(pos.get("contracts", 0))
        if contracts == 0:
            continue

        symbol = pos["symbol"]
        entry_price = safe_float(pos.get("entryPrice", 0))
        mark_price = safe_float(pos.get("markPrice", 0))
        side_raw = str(pos.get("side", "")).lower()

        if entry_price <= 0 or mark_price <= 0:
            continue

        side = "LONG" if side_raw == "long" else "SHORT"

        state = trade_state.get(symbol)
        if not state:
            fallback_sl = entry_price * (0.985 if side == "LONG" else 1.015)
            fallback_tp1 = entry_price * (1.009 if side == "LONG" else 0.991)
            fallback_tp2 = entry_price * (1.018 if side == "LONG" else 0.982)
            trade_state[symbol] = {
                "symbol": symbol,
                "side": side,
                "entry": entry_price,
                "stop_loss": fallback_sl,
                "tp1": fallback_tp1,
                "tp2": fallback_tp2,
                "tp1_taken": False,
                "tp2_taken": False,
                "trailing_active": False,
                "trailing_stop": None,
                "opened_at": now_ts(),
                "entry_score": 0,
                "entry_reason": "Recovered open position",
            }
            state = trade_state[symbol]

        bars_15m = fetch_ohlcv_safe(symbol, TF_15M, 180)
        if not bars_15m:
            continue
        atr_15m = atr_from_ohlcv(bars_15m, ATR_PERIOD)
        if atr_15m is None:
            continue

        exit_info = get_exit_score(symbol, side)
        current_score = exit_info["current_score"] if exit_info else 100
        opposite_score = exit_info["opposite_score"] if exit_info else 0

        if side == "LONG":
            if mark_price <= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, "Stop Loss", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (
                not state["tp1_taken"]
                and current_score <= EMERGENCY_EXIT_SCORE
                and opposite_score >= WATCH_SCORE
            ):
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, f"Smart Emergency Exit | score {current_score}", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price >= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        f"✅ TP1 LONG\n{symbol}\nClosed 50%\nSL moved to breakeven\nSmartScore now: {current_score}/100"
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price >= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        f"🚀 TP2 LONG\n{symbol}\nTrailing stop activated\nSmartScore now: {current_score}/100"
                    )
                    continue

            if state["tp1_taken"] and current_score <= EARLY_EXIT_SCORE and opposite_score > current_score:
                refreshed = get_symbol_position(symbol) or pos
                if close_position(symbol, refreshed, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, f"Smart Weakness Exit | score {current_score}", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if state["trailing_active"]:
                new_trailing = mark_price - atr_15m * TRAILING_ATR_MULTIPLIER
                if state["trailing_stop"] is None:
                    state["trailing_stop"] = new_trailing
                else:
                    state["trailing_stop"] = max(state["trailing_stop"], new_trailing)

                if mark_price <= state["trailing_stop"]:
                    refreshed = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed, 1.0):
                        record_closed_trade(symbol, entry_price, mark_price, side)
                        await notify_close(context, symbol, side, "Trailing Stop", entry_price, mark_price)
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue

        else:
            if mark_price >= state["stop_loss"]:
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, "Stop Loss", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (
                not state["tp1_taken"]
                and current_score <= EMERGENCY_EXIT_SCORE
                and opposite_score >= WATCH_SCORE
            ):
                if close_position(symbol, pos, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, f"Smart Emergency Exit | score {current_score}", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if (not state["tp1_taken"]) and mark_price <= state["tp1"]:
                if close_position(symbol, pos, 0.5):
                    state["tp1_taken"] = True
                    state["stop_loss"] = state["entry"]
                    await notify(
                        context,
                        f"✅ TP1 SHORT\n{symbol}\nClosed 50%\nSL moved to breakeven\nSmartScore now: {current_score}/100"
                    )
                    continue

            refreshed = get_symbol_position(symbol) or pos
            if (not state["tp2_taken"]) and mark_price <= state["tp2"]:
                if close_position(symbol, refreshed, 0.6):
                    state["tp2_taken"] = True
                    state["trailing_active"] = True
                    state["trailing_stop"] = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                    await notify(
                        context,
                        f"🚀 TP2 SHORT\n{symbol}\nTrailing stop activated\nSmartScore now: {current_score}/100"
                    )
                    continue

            if state["tp1_taken"] and current_score <= EARLY_EXIT_SCORE and opposite_score > current_score:
                refreshed = get_symbol_position(symbol) or pos
                if close_position(symbol, refreshed, 1.0):
                    record_closed_trade(symbol, entry_price, mark_price, side)
                    await notify_close(context, symbol, side, f"Smart Weakness Exit | score {current_score}", entry_price, mark_price)
                    trade_state.pop(symbol, None)
                    set_cooldown(symbol)
                continue

            if state["trailing_active"]:
                new_trailing = mark_price + atr_15m * TRAILING_ATR_MULTIPLIER
                if state["trailing_stop"] is None:
                    state["trailing_stop"] = new_trailing
                else:
                    state["trailing_stop"] = min(state["trailing_stop"], new_trailing)

                if mark_price >= state["trailing_stop"]:
                    refreshed = get_symbol_position(symbol) or pos
                    if close_position(symbol, refreshed, 1.0):
                        record_closed_trade(symbol, entry_price, mark_price, side)
                        await notify_close(context, symbol, side, "Trailing Stop", entry_price, mark_price)
                        trade_state.pop(symbol, None)
                        set_cooldown(symbol)
                    continue


# =========================================================
# 7) SCAN JOB
# =========================================================
async def trading_job(context: ContextTypes.DEFAULT_TYPE):
    global last_scan_summary, last_signal_summary, bot_paused

    reset_daily_stats_if_needed()

    if bot_paused:
        last_scan_summary = "Bot is paused."
        return

    await manage_open_positions(context)

    if count_open_positions() >= MAX_OPEN_POSITIONS:
        last_scan_summary = "Max open positions reached."
        logger.info(last_scan_summary)
        return

    scan_lines = []

    for symbol in SYMBOLS:
        try:
            if is_in_cooldown(symbol):
                scan_lines.append(f"{symbol}: COOLDOWN")
                continue

            if get_symbol_position(symbol):
                scan_lines.append(f"{symbol}: ALREADY_OPEN")
                continue

            snapshot = get_market_snapshot(symbol)
            if not snapshot:
                scan_lines.append(f"{symbol}: NO_DATA")
                continue

            signal = snapshot["signal"]
            reason = snapshot["reason"]
            score = snapshot.get("score", 0)

            scan_lines.append(f"{symbol}: {signal} | Score {score}/100 | {reason}")
            last_signal_summary = f"{symbol}: {signal} | Score {score}/100 | {reason}"

            if signal not in ("LONG", "SHORT"):
                continue

            entry_side = signal

            balance = fetch_balance_usdt()
            if balance <= 5:
                scan_lines.append(f"{symbol}: LOW_BALANCE")
                continue

            plan = calculate_trade_plan(symbol, snapshot, entry_side, balance, risk_multiplier=1.0)
            if not plan:
                scan_lines.append(f"{symbol}: PLAN_REJECTED")
                continue

            if open_position(symbol, entry_side, plan, snapshot):
                await notify(
                    context,
                    (
                        f"🚀 Ichimoku Smart Trade Opened\n"
                        f"Symbol: {symbol}\n"
                        f"Side: {entry_side}\n"
                        f"SmartScore: {score}/100\n"
                        f"Reason: {reason}\n"
                        f"Entry: {format_num(plan['entry'], 6)}\n"
                        f"SL: {format_num(plan['stop_loss'], 6)}\n"
                        f"TP1: {format_num(plan['tp1'], 6)}\n"
                        f"TP2: {format_num(plan['tp2'], 6)}\n"
                        f"Amount: {plan['amount']}\n"
                        f"Risk: {format_num(plan['effective_risk'] * 100, 2)}%\n"
f"Mode: {BOT_MODE}\n"
f"Leverage: {AGGRESSIVE_LEVERAGE if BOT_MODE == 'AGGRESSIVE' else NORMAL_LEVERAGE}x"
                    )
                )
                scan_lines.append(f"{symbol}: OPENED {entry_side} | Score {score}/100")
                if count_open_positions() >= MAX_OPEN_POSITIONS:
                    break
            else:
                scan_lines.append(f"{symbol}: OPEN_FAILED")

        except Exception as e:
            logger.error(f"{symbol}: trading_job error: {e}")
            scan_lines.append(f"{symbol}: ERROR")

    last_scan_summary = "\n".join(scan_lines[-12:]) if scan_lines else "No scan results."
    logger.info(f"Scan summary:\n{last_scan_summary}")


# =========================================================
# 8) TELEGRAM DASHBOARD
# =========================================================
def dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 الرصيد", callback_data="dash_balance"),
            InlineKeyboardButton("📡 الرادار", callback_data="dash_radar"),
        ],
        [
            InlineKeyboardButton("📂 الصفقات", callback_data="dash_positions"),
            InlineKeyboardButton("📈 اليوم", callback_data="dash_stats"),
        ],
        [
            InlineKeyboardButton("📊 الكل", callback_data="dash_allstats"),
            InlineKeyboardButton("🏆 الأفضل", callback_data="dash_best"),
        ],
        [
            InlineKeyboardButton("🔎 فحص يدوي", callback_data="dash_scan"),
            InlineKeyboardButton("⚙️ المخاطرة", callback_data="dash_risk_menu"),
        ],
        [
            InlineKeyboardButton("⏸ إيقاف", callback_data="dash_pause"),
            InlineKeyboardButton("▶️ تشغيل", callback_data="dash_resume"),
        ],
        [
            InlineKeyboardButton("🛑 إغلاق الكل", callback_data="dash_close_all"),
            InlineKeyboardButton("✅ إغلاق الرابحة", callback_data="dash_close_winners"),
        ],
        [
            InlineKeyboardButton("❌ إغلاق الخاسرة", callback_data="dash_close_losers"),
        ],
        [
    InlineKeyboardButton("🔥 Aggressive", callback_data="mode_aggressive"),
    InlineKeyboardButton("🧠 Normal", callback_data="mode_normal"),
],
    ])


def risk_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0.3%", callback_data="risk_0.003"),
            InlineKeyboardButton("0.5%", callback_data="risk_0.005"),
        ],
        [
            InlineKeyboardButton("0.6%", callback_data="risk_0.006"),
            InlineKeyboardButton("0.75%", callback_data="risk_0.0075"),
        ],
        [
            InlineKeyboardButton("1.0%", callback_data="risk_0.01"),
            InlineKeyboardButton("⬅️ رجوع", callback_data="dash_home"),
        ],
    ])


async def show_positions(message_target, positions: List[dict]):
    lines = []
    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue
        symbol = p["symbol"]
        state = trade_state.get(symbol, {})
        extra = ""
        if state:
            extra = f" | Score: {state.get('entry_score', '-')}"
        lines.append(
            f"{symbol} | {p.get('side')} | "
            f"Entry: {format_num(safe_float(p.get('entryPrice', 0)), 6)} | "
            f"Mark: {format_num(safe_float(p.get('markPrice', 0)), 6)} | "
            f"UPnL: {format_num(safe_float(p.get('unrealizedPnl', 0)), 4)}"
            f"{extra}"
        )
    if not lines:
        await message_target.reply_text("لا توجد صفقات مفتوحة.")
    else:
        await message_target.reply_text("📂 الصفقات المفتوحة:\n" + "\n".join(lines[:20]))


async def close_by_pnl(update_or_message, mode: str):
    positions = fetch_positions()
    count = 0

    for p in positions:
        contracts = safe_float(p.get("contracts", 0))
        if contracts == 0:
            continue

        upnl = safe_float(p.get("unrealizedPnl", 0))
        symbol = p["symbol"]

        should_close = (
            (mode == "all") or
            (mode == "winners" and upnl > 0) or
            (mode == "losers" and upnl < 0)
        )

        if not should_close:
            continue

        if close_position(symbol, p, 1.0):
            trade_state.pop(symbol, None)
            set_cooldown(symbol)
            count += 1

    if mode == "all":
        msg = f"🛑 تم طلب إغلاق {count} صفقة."
    elif mode == "winners":
        msg = f"✅ تم طلب إغلاق {count} صفقة رابحة."
    else:
        msg = f"❌ تم طلب إغلاق {count} صفقة خاسرة."

    await update_or_message.reply_text(msg)


# =========================================================
# 9) COMMANDS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        context.bot_data["chat_id"] = str(update.effective_chat.id)
    await update.message.reply_text(
        "🤖 Ichimoku Smart Bot جاهز.\nاستخدم لوحة التحكم:",
        reply_markup=dashboard_kb()
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused = "نعم" if bot_paused else "لا"
    bal = fetch_balance_usdt()
    await update.message.reply_text(
        f"📊 الحالة\n"
        f"الاستراتيجية: Ichimoku Smart Score\n"
        f"متوقف: {paused}\n"
        f"الرصيد: {bal:.2f} USDT\n"
        f"الصفقات المفتوحة: {count_open_positions()}/{MAX_OPEN_POSITIONS}\n"
        f"المخاطرة الحالية: {risk_per_trade * 100:.2f}%\n"
        f"نقطة الدخول: {ENTRY_SCORE}/100\n"
        f"آخر إشارة: {last_signal_summary}\n\n"
        f"آخر فحص:\n{last_scan_summary}",
        reply_markup=dashboard_kb()
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = fetch_balance_usdt()
    await update.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_positions(update.message, fetch_positions())


async def cmd_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📡 آخر فحص:\n{last_scan_summary}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = True
    await update.message.reply_text("⏸ تم إيقاف البوت.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    bot_paused = False
    await update.message.reply_text("▶️ تم تشغيل البوت.")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 جاري الفحص اليدوي...")
    await trading_job(context)
    await update.message.reply_text(f"تم.\n\n{last_scan_summary}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_today_stats_text())


async def cmd_allstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_all_stats_text())


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_best_symbols_text())


async def cmd_close_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await close_by_pnl(update.message, "all")


# =========================================================
# 10) CALLBACKS
# =========================================================
async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_paused, risk_per_trade
global BOT_MODE


    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "dash_home":
        await query.message.reply_text("🎛 لوحة التحكم:", reply_markup=dashboard_kb())

    elif data == "dash_balance":
        bal = fetch_balance_usdt()
        await query.message.reply_text(f"💰 الرصيد المتاح: {bal:.2f} USDT")
elif data == "mode_normal":
    BOT_MODE = "NORMAL"
    await query.message.reply_text("🧠 Mode: NORMAL (3x)")

elif data == "mode_aggressive":
    BOT_MODE = "AGGRESSIVE"
    await query.message.reply_text("🔥 Mode: AGGRESSIVE (5x)")
    elif data == "dash_radar":
        await query.message.reply_text(f"📡 آخر فحص:\n{last_scan_summary}")

    elif data == "dash_positions":
        await show_positions(query.message, fetch_positions())

    elif data == "dash_stats":
        await query.message.reply_text(build_today_stats_text())

    elif data == "dash_allstats":
        await query.message.reply_text(build_all_stats_text())

    elif data == "dash_best":
        await query.message.reply_text(build_best_symbols_text())

    elif data == "dash_scan":
        await query.message.reply_text("🔎 جاري الفحص اليدوي...")
        await trading_job(context)
        await query.message.reply_text(f"تم.\n\n{last_scan_summary}")

    elif data == "dash_pause":
        bot_paused = True
        await query.message.reply_text("⏸ تم إيقاف البوت.")

    elif data == "dash_resume":
        bot_paused = False
        await query.message.reply_text("▶️ تم تشغيل البوت.")

    elif data == "dash_risk_menu":
        await query.message.reply_text(
            f"⚙️ اختر المخاطرة الحالية\nالحالية: {risk_per_trade * 100:.2f}%",
            reply_markup=risk_kb()
        )

    elif data.startswith("risk_"):
        try:
            risk_per_trade = float(data.split("_", 1)[1])
            await query.message.reply_text(
                f"✅ تم تغيير المخاطرة إلى {risk_per_trade * 100:.2f}%"
            )
        except Exception:
            await query.message.reply_text("❌ فشل تغيير المخاطرة.")

    elif data == "dash_close_all":
        await close_by_pnl(query.message, "all")

    elif data == "dash_close_winners":
        await close_by_pnl(query.message, "winners")

    elif data == "dash_close_losers":
        await close_by_pnl(query.message, "losers")


# =========================================================
# 11) HEALTH
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ICHIMOKU_SMART_BOT_LIVE")


# =========================================================
# 12) MAIN
# =========================================================
def main():
    load_stats_from_disk()

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(),
        daemon=True,
    ).start()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(TOKEN).build()

    if app.job_queue is None:
        raise RuntimeError('JobQueue unavailable. Install "python-telegram-bot[job-queue]".')

    if ENV_CHAT_ID:
        app.bot_data["chat_id"] = str(ENV_CHAT_ID)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("radar", cmd_radar))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("allstats", cmd_allstats))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("closeall", cmd_close_all))
    app.add_handler(CallbackQueryHandler(dashboard_handler))

    app.job_queue.run_repeating(trading_job, interval=SCAN_INTERVAL_SECONDS, first=10)
    app.job_queue.run_repeating(manage_open_positions, interval=POSITION_CHECK_INTERVAL_SECONDS, first=15)

    logger.info("Starting Ichimoku Smart Bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
