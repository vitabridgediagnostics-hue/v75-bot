""" V75 Signal Bot — Deriv API -> Telegram ---------------------------------------- Pulls live Volatility 75 Index (R_75) candles directly from Deriv's public WebSocket API (no MT5, no desktop needed) and pushes BUY/SELL alerts with suggested SL/TP to a Telegram chat whenever a signal fires. This bot NEVER places trades. It only reads public market data and sends you a message. You place orders yourself, manually, in the Deriv app. SIGNAL LOGIC (same framework as the MT5 version, ported to Python): Trend : EMA(fast) vs EMA(slow) Momentum : RSI turning back out of overbought/oversold in trend direction Confirmation: MACD line vs signal line agreeing with trend Volatility : ATR must clear a minimum threshold (skips dead stretches) SL / TP : ATR multiples (default 1.5x / 3x -> ~1:2 risk:reward) This is a technical framework only, not a guarantee of any outcome. Treat every alert as one input, not an instruction to trade. SETUP ----- 1. pip install websockets requests 2. Create a Telegram bot: message @BotFather on Telegram -> /newbot -> copy the token 3. Get your chat_id: message your new bot once, then visit https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates in a browser and read the "chat":{"id": ...} value from the JSON. 4. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below. 5. Run: python v75_telegram_bot.py (For 24/7 operation without a laptop, deploy this on a free/cheap cloud host — Railway, Render, PythonAnywhere, etc. — all can be set up entirely from a phone browser. Ask me if you want step-by-step help with a specific one.) """

import asyncio
import json
import time
from collections import deque

import requests
import websockets

# ==================== CONFIG ====================
DERIV_WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=1089"  # public app_id, no login needed
SYMBOL         = "R_75"     # Volatility 75 Index on Deriv's API
GRANULARITY    = 900        # seconds per candle: 900 = 15 min (see docstring below for other values)
HISTORY_COUNT  = 200        # candles to keep in memory

TELEGRAM_BOT_TOKEN = "8833754016:AAH7A0U0HSNrv0U6fRTdBnWgE_ZbSsJxyz0"
TELEGRAM_CHAT_ID   = "6073070307"

EMA_FAST, EMA_SLOW   = 20, 50
RSI_PERIOD           = 14
RSI_OB, RSI_OS        = 70.0, 30.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD           = 14
MIN_ATR               = 50.0   # minimum ATR (in index points) to allow a signal — calibrate after watching logged values
SL_ATR_MULT, TP_ATR_MULT = 1.5, 3.0
# ==================================================


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print("Telegram send failed:", e)


def ema_series(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(closes, period):
    if len(closes) <= period:
        return [50.0] * len(closes)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    rsi = [50.0] * (period + 1)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        rsi.append(100 - (100 / (1 + rs)))
    while len(rsi) < len(closes):
        rsi.append(rsi[-1])
    return rsi


def macd_series(closes, fast, slow, signal):
    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = ema_series(macd_line, signal)
    return macd_line, signal_line


def atr_series(highs, lows, closes, period):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = [trs[0]]
    for i in range(1, len(trs)):
        if i < period:
            atr.append(sum(trs[:i + 1]) / (i + 1))
        else:
            atr.append((atr[-1] * (period - 1) + trs[i]) / period)
    return atr


def evaluate_signal(candles):
    """candles: list of dicts with open/high/low/close/epoch, oldest first."""
    if len(candles) < max(EMA_SLOW, MACD_SLOW, ATR_PERIOD, RSI_PERIOD) + 5:
        return None

    closes = [c["close"] for c in candles]
    highs  = [c["high"] for c in candles]
    lows   = [c["low"] for c in candles]

    ema_fast = ema_series(closes, EMA_FAST)
    ema_slow = ema_series(closes, EMA_SLOW)
    rsi      = rsi_series(closes, RSI_PERIOD)
    macd_line, signal_line = macd_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    atr      = atr_series(highs, lows, closes, ATR_PERIOD)

    i = len(closes) - 1  # last CLOSED candle
    if atr[i] < MIN_ATR:
        return None

    trend_up   = ema_fast[i] > ema_slow[i]
    trend_down = ema_fast[i] < ema_slow[i]
    macd_bull  = macd_line[i] > signal_line[i]
    macd_bear  = macd_line[i] < signal_line[i]
    rsi_bull_turn = rsi[i - 1] < RSI_OS and rsi[i] >= RSI_OS
    rsi_bear_turn = rsi[i - 1] > RSI_OB and rsi[i] <= RSI_OB

    entry = closes[i]
    if trend_up and macd_bull and rsi_bull_turn:
        sl = entry - atr[i] * SL_ATR_MULT
        tp = entry + atr[i] * TP_ATR_MULT
        return "BUY", entry, sl, tp, atr[i]
    if trend_down and macd_bear and rsi_bear_turn:
        sl = entry + atr[i] * SL_ATR_MULT
        tp = entry - atr[i] * TP_ATR_MULT
        return "SELL", entry, sl, tp, atr[i]
    return None


async def run_bot():
    candles = deque(maxlen=HISTORY_COUNT)
    last_evaluated_epoch = None

    async with websockets.connect(DERIV_WS_URL) as ws:
        request = {
            "ticks_history": SYMBOL,
            "adjust_start_time": 1,
            "count": HISTORY_COUNT,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": GRANULARITY,
            "subscribe": 1,
        }
        await ws.send(json.dumps(request))
        print(f"Subscribed to {SYMBOL} candles, granularity {GRANULARITY}s")
        send_telegram(f"V75 signal bot started. Watching {SYMBOL} on {GRANULARITY}s candles.")

        async for raw in ws:
            data = json.loads(raw)

            if data.get("msg_type") == "candles":
                for c in data["candles"]:
                    candles.append({
                        "epoch": c["epoch"], "open": float(c["open"]),
                        "high": float(c["high"]), "low": float(c["low"]),
                        "close": float(c["close"]),
                    })
                print(f"Loaded {len(candles)} historical candles.")

            elif data.get("msg_type") == "ohlc":
                o = data["ohlc"]
                epoch = int(o["open_time"])
                candle = {
                    "epoch": epoch, "open": float(o["open"]),
                    "high": float(o["high"]), "low": float(o["low"]),
                    "close": float(o["close"]),
                }
                if candles and candles[-1]["epoch"] == epoch:
                    candles[-1] = candle  # still-forming candle, update in place
                else:
                    candles.append(candle)  # new candle opened -> previous one just closed

                    if last_evaluated_epoch != candles[-2]["epoch"] if len(candles) > 1 else False:
                        result = evaluate_signal(list(candles)[:-1])  # evaluate the just-closed candle
                        if result:
                            direction, entry, sl, tp, atr_val = result
                            msg = (
                                f"{SYMBOL} {direction} signal\n"
                                f"Entry ~{entry:.2f}\n"
                                f"SL: {sl:.2f}\n"
                                f"TP: {tp:.2f}\n"
                                f"ATR: {atr_val:.1f}\n"
                                f"Time: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
                            )
                            print(msg)
                            send_telegram(msg)
                        last_evaluated_epoch = candles[-2]["epoch"]

            elif data.get("error"):
                print("Deriv API error:", data["error"])


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(run_bot())
        except Exception as e:
            print("Bot crashed, restarting in 10s:", e)
            time.sleep(10)
