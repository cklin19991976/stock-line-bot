import yfinance as yf
import requests
import time
import os

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
    "AAPL": {"upper": 300, "lower": 299},
    "SPY": {"upper": 697, "lower": 614},
    "QQQ": {"upper": 637, "lower": 540},
    "TSM": {"upper": 390, "lower": 300},
    "ASML": {"upper": 1547, "lower": 1250},
    "UCO": {"upper": 40, "lower": 28},
    "GOOG": {"upper": 350, "lower": 276},
    "MSFT": {"upper": 555, "lower": 347},
    "NVDA": {"upper": 212, "lower": 160},
    "CL=F": {"upper": 110, "lower": 80},
    "^TNX": {"upper": 4.55, "lower": 3.95},
    "2330.TW": {"upper": 2025, "lower": 1750},
    "0050.TW": {"upper": 81.8, "lower": 70},
    "1215.TW": {"upper": 163, "lower": 140},
    "00662.TW": {"upper": 105, "lower": 90},
}

CHECK_INTERVAL = 60
COOLDOWN = 1800  # seconds
last_heartbeat = 0

# Track last alert state
last_state = {}   # "above", "below", "normal"
last_alert_time = {}

def send_line(msg):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "to": USER_ID,
            "messages": [{"type": "text", "text": msg}]
        }
    )
    print("LINE:", r.status_code, r.text)

def send_heartbeat():
    msg = "🟢 Stock bot alive and running"
    send_line(msg)

def check_stock(symbol, config):
    try:
        data = yf.Ticker(symbol).history(period="1d")

        if data.empty:
            print(f"No data for {symbol}")
            return

        price = data["Close"].iloc[-1]
        print(f"{symbol} price: {price}")

        upper = config["upper"]
        lower = config["lower"]

        now = time.time()
        prev_state = last_state.get(symbol, "normal")
        last_time = last_alert_time.get(symbol, 0)

        # Determine current state
        if price > upper:
            current_state = "above"
        elif price < lower:
            current_state = "below"
        else:
            current_state = "normal"

        # Only alert when state changes + cooldown passed
        if current_state != prev_state and (now - last_time > COOLDOWN):

            if current_state == "above":
                msg = f"🚀 {symbol} ABOVE {upper}\nNow: {round(price,2)}"

            elif current_state == "below":
                msg = f"🔻 {symbol} BELOW {lower}\nNow: {round(price,2)}"

            else:
                msg = f"↔️ {symbol} back to normal range\nNow: {round(price,2)}"

            send_line(msg)

            last_state[symbol] = current_state
            last_alert_time[symbol] = now

    except Exception as e:
        print(f"Error {symbol}:", e)


def main():
    global last_heartbeat

    print("Stock bot started...")

    while True:
        now = time.time()

        for symbol, config in SYMBOLS.items():
            check_stock(symbol, config)

        # Heartbeat check
        if now - last_heartbeat > (COOLDOWN*8):
            print("Sending heartbeat...")
            send_heartbeat()
            last_heartbeat = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
