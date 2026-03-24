# =========================
# Railway Stock Alert Bot
# Yahoo Finance → LINE
# =========================

# -------- requirements.txt --------
# yfinance
# requests
# pandas


# -------- main.py --------
import yfinance as yf
import requests
import time
import os

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
    "AAPL": 200,
    "TSLA": 250,
    "NVDA": 900
}
CHECK_INTERVAL = 60  # seconds
COOLDOWN = 300  # seconds per symbol

last_alert_time = {}


def send_line(msg):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "to": USER_ID,
        "messages": [{"type": "text", "text": msg}]
    }

    r = requests.post(url, headers=headers, json=data)

    print("STATUS:", r.status_code)
    print("RESPONSE:", r.text)	


def check_stock(symbol, threshold):
    try:
        ticker = yf.Ticker(symbol)
        price = ticker.history(period="1d", interval="1m")["Close"].iloc[-1]

        now = time.time()
        last_time = last_alert_time.get(symbol, 0)

        if price > threshold and (now - last_time > COOLDOWN):
            msg = f"🚨 {symbol} crossed {threshold}\nCurrent: {round(price,2)}"
            print(msg)
            send_line(msg)
            last_alert_time[symbol] = now

    except Exception as e:
        print(f"Error checking {symbol}:", e)


def main():
    print("Stock bot started...")
    while True:
        for symbol, threshold in SYMBOLS.items():
            check_stock(symbol, threshold)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()


# -------- Procfile (for Railway) --------
# worker: python main.py


# -------- README.md --------
"""
# Railway Stock Alert Bot

## Setup

1. Create a LINE Messaging API channel
2. Get:
   - LINE_TOKEN
   - USER_ID

3. Deploy to Railway

## Environment Variables

LINE_TOKEN=your_token
USER_ID=your_user_id

## Run

Railway will automatically run using Procfile

## Customize

Edit SYMBOLS in main.py:

SYMBOLS = {
    "AAPL": 200,
    "TSLA": 250
}

## Notes

- Checks every 60 seconds
- Cooldown prevents spam alerts
- Uses Yahoo Finance via yfinance
"""
