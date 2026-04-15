import os
import asyncio
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, List

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

PORT = int(os.environ.get("PORT", 8080))

# ===== Futures mode assumptions =====
# BingX USDT perpetual / Isolated / One-way
MARGIN_MODE = "isolated"

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
    "DOT/USDT:USDT",
    "TRX/USDT:USDT",
    "BCH/USDT:USDT",
    "ATOM/USDT:USDT",
    "NEAR/USDT:USDT",
]

TREND_TF_4H = "4h"
TREND_TF_1H = "1h"
ENTRY_TF = "15m"

MAX_OPEN_POSITIONS = 3
COOLDOWN_MINUTES = 45

# Risk
RISK_PER_TRADE = 0.006  # 0.6%
LEVERAGE = 3

# Indicators
EMA_FILTER_PERIOD = 50
ATR_PERIOD = 14

# SAR settings
SAR_AF = 0.02
SAR_MAX_AF = 0.2

# SL/TP
TP1_R = 1.0
TP2_R = 2.0
TRAILING_ATR_MULTIPLIER = 1.2

# Scan
SCAN_INTERVAL_SECONDS = 60
POSITION_CHECK_INTERVAL_SECONDS = 20

logging.basicConfig(
    level=logging.INFO,
