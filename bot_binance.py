# =========================================================
# IMPORTANT: Replace ONLY these parts in your existing file
# This patch adds SHORT support to the paper trading bot.
# =========================================================

# =========================================================
# 1) REPLACE check_long_signal() WITH THIS NEW FUNCTION
# =========================================================
def check_signal(symbol: str) -> dict:
    df = fetch_df(symbol)
    if df is None or len(df) < EMA_PERIOD + 5:
        return {"signal": False, "reason": "NO_DATA"}

    prev = df.iloc[-2]
    cur = df.iloc[-1]

    close = float(cur["close"])
    ema200 = float(cur["ema200"])
    rsi_now = float(cur["rsi"])
    rsi_prev = float(prev["rsi"])
    sar_now = float(cur["sar"])
    sar_prev = float(prev["sar"])
    atr = float(cur["atr"])

    if pd.isna(ema200) or pd.isna(rsi_now) or pd.isna(sar_now) or pd.isna(atr):
        return {"signal": False, "reason": "INDICATORS_NOT_READY"}

    # =====================================================
    # LONG SETUP
    # =====================================================
    if close > ema200:
        rsi_recovery = rsi_prev < 30 and rsi_now > 30
        recent_under_30 = (
            df["rsi"].iloc[-5:].min() < 30
            and rsi_now > float(df["rsi"].iloc[-2])
        )

        sar_flip = sar_prev > float(prev["close"]) and sar_now < close
        distance_from_ema = abs(close - ema200) / ema200

        if (
            (rsi_recovery or recent_under_30)
            and sar_flip
            and distance_from_ema <= MAX_DISTANCE_FROM_EMA
            and rsi_now <= MAX_RSI_CONFIRM
        ):
            stop_loss = close - (atr * ATR_SL_MULTIPLIER)

            return {
                "signal": True,
                "side": "LONG",
                "entry": close,
                "stop_loss": stop_loss,
                "sar": sar_now,
                "atr": atr,
                "rsi": rsi_now,
                "ema200": ema200,
                "reason": "EMA200 trend + RSI recovery + SAR flip",
            }

    # =====================================================
    # SHORT SETUP
    # =====================================================
    if close < ema200:
        rsi_recovery = rsi_prev > 70 and rsi_now < 70
        recent_above_70 = (
            df["rsi"].iloc[-5:].max() > 70
            and rsi_now < float(df["rsi"].iloc[-2])
        )

        sar_flip = sar_prev < float(prev["close"]) and sar_now > close
        distance_from_ema = abs(close - ema200) / ema200

        if (
            (rsi_recovery or recent_above_70)
            and sar_flip
            and distance_from_ema <= MAX_DISTANCE_FROM_EMA
            and rsi_now >= 45
        ):
            stop_loss = close + (atr * ATR_SL_MULTIPLIER)

            return {
                "signal": True,
                "side": "SHORT",
                "entry": close,
                "stop_loss": stop_loss,
                "sar": sar_now,
                "atr": atr,
                "rsi": rsi_now,
                "ema200": ema200,
                "reason": "Bear trend + RSI pullback + SAR flip",
            }

    return {
        "signal": False,
        "reason": f"No valid setup | RSI {rsi_now:.2f}"
    }


# =========================================================
# 2) REPLACE calc_amount()
# =========================================================
def calc_amount(entry: float, stop_loss: float) -> float:
    risk_usdt = float(state["balance"]) * current_risk_pct()
    risk_per_unit = abs(entry - stop_loss)

    if risk_per_unit <= 0:
        return 0.0

    return risk_usdt / risk_per_unit


# =========================================================
# 3) REPLACE open_paper_position()
# =========================================================
def open_paper_position(symbol: str, signal: dict) -> bool:
    positions = state["positions"]

    if symbol in positions:
        return False

    entry = float(signal["entry"])
    stop_loss = float(signal["stop_loss"])
    amount = calc_amount(entry, stop_loss)

    if amount <= 0:
        return False

    side = signal["side"]

    position = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "amount": amount,
        "stop_loss": stop_loss,
        "initial_stop": stop_loss,
        "trailing_stop": stop_loss,
        "opened_at": now_iso(),
        "rsi": signal["rsi"],
        "ema200": signal["ema200"],
        "atr": signal["atr"],
        "leverage": current_leverage(),
        "risk_pct": current_risk_pct(),
    }

    positions[symbol] = position
    save_state()
    journal("OPEN", position)
    return True


# =========================================================
# 4) REPLACE close_paper_position()
# =========================================================
def close_paper_position(symbol: str, exit_price: float, reason: str) -> Optional[dict]:
    pos = state["positions"].get(symbol)
    if not pos:
        return None

    entry = float(pos["entry"])
    amount = float(pos["amount"])
    side = pos["side"]

    if side == "LONG":
        pnl = (exit_price - entry) * amount
        pnl_pct = ((exit_price - entry) / entry) * 100
    else:
        pnl = (entry - exit_price) * amount
        pnl_pct = ((entry - exit_price) / entry) * 100

    state["balance"] = float(state["balance"]) + pnl
    state["closed_trades"] += 1
    state["realized_pnl"] += pnl

    if pnl >= 0:
        state["wins"] += 1
    else:
        state["losses"] += 1

    state["positions"].pop(symbol, None)
    save_state()

    payload = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "amount": amount,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "balance": state["balance"],
    }

    journal("CLOSE", payload)
    return payload


# =========================================================
# 5) REPLACE update_open_positions()
# =========================================================
def update_open_positions() -> List[dict]:
    updates = []

    for symbol, pos in list(state["positions"].items()):
        df = fetch_df(symbol)
        if df is None or len(df) < 5:
            continue

        cur = df.iloc[-1]
        close = float(cur["close"])
        sar = float(cur["sar"])

        side = pos["side"]
        current_sl = float(pos["trailing_stop"])

        # LONG trailing
        if side == "LONG":
            if sar > current_sl and sar < close:
                pos["trailing_stop"] = sar
                updates.append({
                    "type": "SL_UPDATE",
                    "symbol": symbol,
                    "new_sl": sar,
                    "price": close,
                })

            if close <= float(pos["trailing_stop"]):
                closed = close_paper_position(symbol, close, "Trailing Stop")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

            if sar > close:
                closed = close_paper_position(symbol, close, "SAR Exit")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

        # SHORT trailing
        else:
            if sar < current_sl and sar > close:
                pos["trailing_stop"] = sar
                updates.append({
                    "type": "SL_UPDATE",
                    "symbol": symbol,
                    "new_sl": sar,
                    "price": close,
                })

            if close >= float(pos["trailing_stop"]):
                closed = close_paper_position(symbol, close, "Trailing Stop")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

            if sar < close:
                closed = close_paper_position(symbol, close, "SAR Exit")
                if closed:
                    updates.append({"type": "CLOSE", **closed})
                continue

    save_state()
    return updates


# =========================================================
# 6) REPLACE positions_text()
# =========================================================
def positions_text() -> str:
    if not state["positions"]:
        return "📁 لا توجد صفقات مفتوحة."

    lines = ["📁 الصفقات المفتوحة:"]

    for symbol, pos in state["positions"].items():
        try:
            price = get_price(symbol)

            if pos["side"] == "LONG":
                pnl = (price - float(pos["entry"])) * float(pos["amount"])
                pnl_pct = ((price - float(pos["entry"])) / float(pos["entry"])) * 100
            else:
                pnl = (float(pos["entry"]) - price) * float(pos["amount"])
                pnl_pct = ((float(pos["entry"]) - price) / float(pos["entry"])) * 100

        except Exception:
            price, pnl, pnl_pct = 0, 0, 0

        lines.append(
            f"\n{symbol}\n"
            f"Side: {pos['side']}\n"
            f"Entry: {pos['entry']:.6f}\n"
            f"Now: {price:.6f}\n"
            f"SL: {pos['trailing_stop']:.6f}\n"
            f"PnL: {pnl:.2f} USDT ({pnl_pct:.2f}%)"
        )

    return "\n".join(lines)


# =========================================================
# 7) REPLACE scan_now()
# =========================================================
async def scan_now(context: ContextTypes.DEFAULT_TYPE) -> str:
    global last_scan_summary

    if bot_paused:
        last_scan_summary = "Bot paused."
        return last_scan_summary

    lines = []

    for symbol in SYMBOLS:
        if symbol in state["positions"]:
            lines.append(f"{symbol}: ALREADY_OPEN")
            continue

        signal = check_signal(symbol)

        if signal["signal"]:
            opened = open_paper_position(symbol, signal)

            if opened:
                msg = (
                    f"🚀 صفقة وهمية جديدة\n"
                    f"{symbol}\n"
                    f"Side: {signal['side']}\n"
                    f"Entry: {signal['entry']:.6f}\n"
                    f"SL: {signal['stop_loss']:.6f}\n"
                    f"RSI: {signal['rsi']:.2f}\n"
                    f"Reason: {signal['reason']}"
                )

                await send_to_user(context, msg)
                lines.append(f"{symbol}: OPENED {signal['side']}")
            else:
                lines.append(f"{symbol}: SIGNAL BUT NOT OPENED")

        else:
            lines.append(f"{symbol}: NO_TRADE | {signal['reason']}")

    last_scan_summary = "\n".join(lines[-12:])
    return last_scan_summary
