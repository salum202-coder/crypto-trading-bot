from pathlib import Path
import py_compile

src = Path("/mnt/data/bot_binance_smart_risk_100.py")
code = src.read_text(encoding="utf-8")

# Basic config upgrades
for a,b in [
    ("ENTRY_SCORE = 82", "ENTRY_SCORE = 88"),
    ("WATCH_SCORE = 70", "WATCH_SCORE = 78"),
    ("TP1_R = 0.9", "TP1_R = 1.0"),
    ("TP2_R = 1.8", "TP2_R = 2.2"),
    ("EARLY_EXIT_SCORE = 45", "EARLY_EXIT_SCORE = 52"),
    ("EMERGENCY_EXIT_SCORE = 35", "EMERGENCY_EXIT_SCORE = 42"),
]:
    code = code.replace(a,b)

code = code.replace(
'''MIN_RR = 1.35

# Entry filters''',
'''MIN_RR = 1.35

# Professional quality filters
STRICT_TREND_FILTER = True
MIN_ENTRY_VOLUME_RATIO = 1.05
MAX_CLOUD_THICKNESS_4H = 0.045
MAX_CLOUD_THICKNESS_1H = 0.035
REQUIRE_15M_PULLBACK_ZONE = True

# Entry filters'''
)

# exact RSI block from current file
start = code.find('    if side == "LONG":\n        if 42 <= rsi_15m <= 68:')
end = code.find('    score = max(0, min(100, score))', start)
if start == -1 or end == -1:
    raise RuntimeError("RSI section not found")
old_section = code[start:end]
new_section = '''    if side == "LONG":
        if 42 <= rsi_15m <= 68:
            score += 5
            reasons.append("RSI مناسب للونق")
        elif rsi_15m > 75:
            score -= 18
            reasons.append("RSI مرتفع جدًا، احتمال مطاردة")
        elif rsi_15m < 38:
            score -= 8
            reasons.append("RSI ضعيف للونق")
    else:
        if 32 <= rsi_15m <= 58:
            score += 5
            reasons.append("RSI مناسب للشورت")
        elif rsi_15m < 25:
            score -= 18
            reasons.append("RSI منخفض جدًا، احتمال مطاردة")
        elif rsi_15m > 62:
            score -= 8
            reasons.append("RSI ضعيف للشورت")

    # Professional filters / penalties
    if STRICT_TREND_FILTER:
        if trend_4h != want:
            score -= 35
            reasons.append("فلتر احترافي: 4H ليس ترند صريح")
        if trend_1h != want:
            score -= 28
            reasons.append("فلتر احترافي: 1H ليس مؤكدًا")
        if trend_15m not in (want,):
            score -= 18
            reasons.append("فلتر احترافي: 15m غير جاهز للدخول")

    if ichi_4h.get("cloud_thickness", 999) > MAX_CLOUD_THICKNESS_4H:
        score -= 10
        reasons.append("سحابة 4H سميكة، احتمال مقاومة/تذبذب")

    if ichi_1h.get("cloud_thickness", 999) > MAX_CLOUD_THICKNESS_1H:
        score -= 8
        reasons.append("سحابة 1H سميكة، جودة الاتجاه أقل")

    if vol_ratio < MIN_ENTRY_VOLUME_RATIO:
        score -= 10
        reasons.append("الحجم أقل من فلتر الدخول الاحترافي")

    if REQUIRE_15M_PULLBACK_ZONE:
        if distance_kijun > MAX_DISTANCE_FROM_KIJUN_15M:
            score -= 15
            reasons.append("ليس Pullback نظيف قرب Kijun")
        if side == "LONG" and close_15m < ichi_15m["kijun"]:
            score -= 10
            reasons.append("اللونق تحت Kijun على 15m")
        if side == "SHORT" and close_15m > ichi_15m["kijun"]:
            score -= 10
            reasons.append("الشورت فوق Kijun على 15m")

'''
code = code[:start] + new_section + code[end:]

# Insert pro entry function before stats section
stats_marker = "# =========================================================\n# 3.2) STATS PERSISTENCE"
pro_func = '''
def professional_entry_allowed(side: str, snapshot: dict, score_data: dict) -> Tuple[bool, str]:
    """
    Hard gate before opening a trade.
    This is stricter than scoring: even high score gets rejected if market quality is weak.
    """
    want = "BULL" if side == "LONG" else "BEAR"
    ichi_4h = snapshot["ichi_4h"]
    ichi_1h = snapshot["ichi_1h"]
    ichi_15m = snapshot["ichi_15m"]
    close_15m = snapshot["close_15m"]
    atr_15m = snapshot["atr_15m"]
    atr_pct = atr_15m / close_15m if close_15m > 0 else 999
    vol_ratio = safe_float(score_data.get("volume_ratio", 0.0), 0.0)

    trend_4h = score_data.get("trend_4h")
    trend_1h = score_data.get("trend_1h")
    trend_15m = score_data.get("trend_15m")
    distance_kijun = safe_float(score_data.get("distance_kijun", 999), 999)

    if trend_4h != want:
        return False, f"Rejected: 4H trend not strict {want}"
    if trend_1h != want:
        return False, f"Rejected: 1H trend not strict {want}"
    if trend_15m != want:
        return False, f"Rejected: 15m trend not strict {want}"

    if ichi_4h["price_vs_cloud"] == "INSIDE" or ichi_1h["price_vs_cloud"] == "INSIDE":
        return False, "Rejected: price inside Ichimoku cloud"

    if ichi_4h.get("cloud_thickness", 999) > MAX_CLOUD_THICKNESS_4H:
        return False, "Rejected: 4H cloud too thick"
    if ichi_1h.get("cloud_thickness", 999) > MAX_CLOUD_THICKNESS_1H:
        return False, "Rejected: 1H cloud too thick"

    if vol_ratio < MIN_ENTRY_VOLUME_RATIO:
        return False, f"Rejected: weak volume {vol_ratio:.2f}x"

    if not (MIN_ATR_PERCENT_15M <= atr_pct <= MAX_ATR_PERCENT_15M):
        return False, f"Rejected: ATR not suitable {atr_pct:.4f}"

    if distance_kijun > MAX_DISTANCE_FROM_KIJUN_15M:
        return False, "Rejected: entry too far from Kijun"

    if side == "LONG":
        if close_15m < ichi_15m["kijun"]:
            return False, "Rejected: LONG below 15m Kijun"
        if snapshot["rsi_15m"] > 70:
            return False, "Rejected: LONG RSI too high"
    else:
        if close_15m > ichi_15m["kijun"]:
            return False, "Rejected: SHORT above 15m Kijun"
        if snapshot["rsi_15m"] < 30:
            return False, "Rejected: SHORT RSI too low"

    return True, "Professional filters passed"


'''
if stats_marker not in code:
    raise RuntimeError("stats marker not found")
code = code.replace(stats_marker, pro_func + stats_marker)

# Replace entry block in get_market_snapshot
old_entry = '''    if score >= ENTRY_SCORE:
        return {
            **base,
            "signal": best_side,
            "side": best_side,
            "score": score,
            "reason": f"Ichimoku Smart Score {score}/100 | {reason_text}",
            "reasons": reasons,
        }

    if score >= WATCH_SCORE:'''
new_entry = '''    if score >= ENTRY_SCORE:
        temp_snapshot = {
            **base,
            "signal": best_side,
            "side": best_side,
            "score": score,
            "reason": f"Ichimoku Pro Score {score}/100 | {reason_text}",
            "reasons": reasons,
            "score_data": best_score,
        }
        allowed, filter_reason = professional_entry_allowed(best_side, temp_snapshot, best_score)
        if allowed:
            return temp_snapshot
        return {
            **base,
            "signal": "WATCH",
            "side": best_side,
            "score": score,
            "reason": f"{best_side} rejected by Pro Filter | Score {score}/100 | {filter_reason}",
            "reasons": reasons,
            "score_data": best_score,
        }

    if score >= WATCH_SCORE:'''
if old_entry not in code:
    raise RuntimeError("entry block not found")
code = code.replace(old_entry, new_entry)

# Add score_data to watch and weak returns
code = code.replace(
'''"reason": f"{best_side} setup قريب لكن ليس قوي كفاية | Score {score}/100 | {reason_text}",
            "reasons": reasons,
        }''',
'''"reason": f"{best_side} setup قريب لكن ليس قوي كفاية | Score {score}/100 | {reason_text}",
            "reasons": reasons,
            "score_data": best_score,
        }'''
)
code = code.replace(
'''"reason": f"Weak setup | Score {score}/100",
        "reasons": reasons,
    }''',
'''"reason": f"Weak setup | Score {score}/100",
        "reasons": reasons,
        "score_data": best_score,
    }'''
)

# UI text version
code = code.replace('f"الاستراتيجية: Ichimoku Smart Score\\n"', 'f"الاستراتيجية: Ichimoku Pro Filter v2\\n"')
code = code.replace('f"🤖 Binance Ichimoku Smart Bot جاهز.\\n"', 'f"🤖 Binance Ichimoku Pro Bot جاهز.\\n"')
code = code.replace('logger.info(f"Starting Binance Ichimoku Smart Bot... Trading Mode: {TRADING_MODE}")', 'logger.info(f"Starting Binance Ichimoku Pro Bot v2... Trading Mode: {TRADING_MODE}")')

out = Path("/mnt/data/bot_binance_pro_v2.py")
out.write_text(code, encoding="utf-8")
py_compile.compile(str(out), doraise=True)
print(out.as_posix())
