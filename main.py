"""
V75 Signal Bot — Deriv API -> Telegram
----------------------------------------
Pulls live Volatility 75 Index (R_75) candles directly from Deriv's public
WebSocket API (no MT5, no desktop needed) and pushes BUY/SELL alerts with
suggested SL/TP to a Telegram chat whenever a signal fires.

This bot NEVER places trades. It only reads public market data and sends
you a message. You place orders yourself, manually, in the Deriv app.
"""

import asyncio
import json
import time
from collections import deque

import requests
import websockets

# ==================== CONFIG ====================
DERIV_WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=34tOo2SXmXySNxwSYcmhn"
SYMBOL         = "R_75"
GRANULARITY    = 900
HISTORY_COUNT  = 200

TELEGRAM_BOT_TOKEN = "8833754016:AAH7A0U0HSNrv0U6fRTdBnWgE_ZbSsJxyz0"
TELEGRAM_CHAT_ID   = "6073070307"

EMA_FAST, EMA_SLOW   = 20, 50
RSI_PERIOD           = 14
RSI_OB, RSI_OS        = 70.0, 30.0
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD           = 14
MIN_ATR               = 50.0
SL_ATR_MULT, TP_ATR_MULT = 1.5, 3.0

# Headers that make our connection look like a normal browser request,
# so Deriv's protection (Cloudflare) doesn't reject it with HTTP 520.
CONNECT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://deriv.com",
}
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

    i = len(closes) - 1
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


async def connect_ws():
    """Try the modern websockets API first, fall back for older versions."""
    try:
        return await websockets.connect(DERIV_WS_URL, additional_headers=CONNECT_HEADERS)
    except TypeError:
        return await websockets.connect(DERIV_WS_URL, extra_headers=CONNECT_HEADERS)


async def run_bot():
    candles = deque(maxlen=HISTORY_COUNT)
    last_evaluated_epoch = None

    ws = await connect_ws()
    try:
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
                    candles[-1] = candle
                else:
                    candles.append(candle)
                    if len(candles) > 1 and last_evaluated_epoch != candles[-2]["epoch"]:
                        result = evaluate_signal(list(candles)[:-1])
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
    finally:
        await ws.close()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(run_bot())
        except Exception as e:
            print("Bot crashed, restarting in 10s:", e)
            time.sleep(10)
