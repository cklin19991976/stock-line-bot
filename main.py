import yfinance as yf
import requests
import time
import os

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
    "AAPL": {"upper": 200, "lower": 180},
    "TSLA": {"upper": 250, "lower": 220},
    "NVDA": {"upper": 950, "lower": 850},
    # "2330.TW": {"upper": 1100, "lower": 950},
}

CHECK_INTERVAL = 60        # check every 60 seconds
COOLDOWN = 1800             # prevent repeated alerts within 30 mins
HEARTBEAT_INTERVAL = 14400  # send alive message every 4 hour

# Track state
last_state = {}        # "above", "below", "normal"
last_alert_time = {}   # per-symbol cooldown
last_heartbeat = 0


def send_line(msg):
    """
    Send a text message to LINE
    """
    try:
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

        print("LINE STATUS:", r.status_code)
        print("LINE RESPONSE:", r.text)

    except Exception as e:
        print("LINE send error:", e)


def send_heartbeat():
    """
    Periodically send a system alive message
    """
    msg = "🟢 Stock bot runningx"
    send_line(msg)


def get_stock_reason(symbol, max_items=3):
    """
    Fetch recent news headlines for a stock and turn them into a short explanation.
    """
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news

        if not news:
            return "No recent news found. Move may be driven by general market momentum or technical buying."

        reasons = []

        for item in news[:max_items]:
            title = item.get("title", "")
            publisher = item.get("publisher", "")
            if title:
                if publisher:
                    reasons.append(f"- {title} ({publisher})")
                else:
                    reasons.append(f"- {title}")

        if not reasons:
            return "No clear recent headline found. Move may be technical or market-driven."

        return "\n".join(reasons)

    except Exception as e:
        print(f"News fetch error for {symbol}: {e}")
        return "Unable to fetch reason right now."


def check_stock(symbol, config):
    """
    Check stock price against upper/lower thresholds
    """
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
                reason = get_stock_reason(symbol)

                msg = (
                    f"🚀 {symbol} ABOVE {upper}\n"
                    f"Now: {round(price,2)}\n\n"
                    f"Possible reason:\n{reason}"
                )

            elif current_state == "below":
                msg = (
                    f"🔻 {symbol} BELOW {lower}\n"
                    f"Now: {round(price,2)}"
                )

            else:
                msg = (
                    f"↔️ {symbol} back to normal range\n"
                    f"Now: {round(price,2)}"
                )

            send_line(msg)

            last_state[symbol] = current_state
            last_alert_time[symbol] = now

    except Exception as e:
        print(f"Error checking {symbol}: {e}")


def main():
    global last_heartbeat

    print("Stock bot started...")

    while True:
        now = time.time()

        # 1) Check stocks
        for symbol, config in SYMBOLS.items():
            check_stock(symbol, config)

        # 2) Send heartbeat
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            print("Sending heartbeat...")
            send_heartbeat()
            last_heartbeat = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()