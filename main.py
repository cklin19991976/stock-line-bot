import yfinance as yf
import requests
import time
import os
import feedparser
from urllib.parse import quote

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
    "AAPL": {"upper": 300, "lower": 99},
    "SPY": {"upper": 697, "lower": 614},
    "QQQ": {"upper": 637, "lower": 540},
    "TSM": {"upper": 310, "lower": 300},
    "ASML": {"upper": 1547, "lower": 1250},
    "UCO": {"upper": 40, "lower": 28},
    "GOOG": {"upper": 350, "lower": 276},
    "MSFT": {"upper": 555, "lower": 347},
    "NVDA": {"upper": 212, "lower": 160},
    "CL=F": {"upper": 110, "lower": 80},
    "^TNX": {"upper": 4.55, "lower": 3.95},
    "2330.TW": {"upper": 2025, "lower": 1750},
    "0050.TW": {"upper": 81.8, "lower": 70},
    "1215.TW": {"upper": 160, "lower": 140},
    "00662.TW": {"upper": 105, "lower": 90},
}

# Better news search names
COMPANY_NAMES = {
    "AAPL": "Apple",
    "TSLA": "Tesla",
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "GOOG": "Google",
    "TSM": "TSMC"
}

# Sector / market ETF mapping
SECTOR_ETF = {
    "AAPL": "QQQ",     # tech / mega-cap
    "MSFT": "QQQ",     # tech / mega-cap
    "GOOG": "QQQ",     # tech / mega-cap
    "NVDA": "SOXX",    # semiconductors
    "TSM": "SOXX",    # semiconductors
    "TSLA": "XLY",     # consumer discretionary / EV sentiment
    "2330.TW": "^TWII" # Taiwan market index
}

CHECK_INTERVAL = 60        # seconds
COOLDOWN = 1800             # seconds
HEARTBEAT_INTERVAL = 14400  # seconds
MEANINGFUL_MOVE_PCT = 0  # only explain reason if stock up > 2%

# Runtime state
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
            },
            timeout=20
        )

        print("LINE STATUS:", r.status_code)
        print("LINE RESPONSE:", r.text)

    except Exception as e:
        print("LINE send error:", e)


def send_heartbeat():
    """
    Periodically send a system alive message
    """
    msg = "🟢 StockBot alive running"
    send_line(msg)


def get_stock_reason(symbol, max_items=2):
    """
    Try Yahoo Finance news first.
    If empty, fallback to Google News RSS.
    Returns headline + source + link.
    """
    # ---------- 1) Try yfinance news ----------
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news

        reasons = []

        if news:
            for item in news[:max_items]:
                title = item.get("title", "").strip()
                publisher = item.get("publisher", "").strip()
                link = item.get("link", "").strip()

                if title:
                    parts = [f"- {title}"]

                    if publisher:
                        parts.append(f"  Source: {publisher}")

                    if link:
                        parts.append(f"  Link: {link}")

                    reasons.append("\n".join(parts))

        if reasons:
            return "\n\n".join(reasons)

    except Exception as e:
        print(f"Yahoo news fetch error for {symbol}: {e}")

    # ---------- 2) Fallback: Google News RSS ----------
    try:
        company = COMPANY_NAMES.get(symbol, symbol)
        query = quote(f"{company} stock")
        rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

        feed = feedparser.parse(rss_url)

        reasons = []

        for entry in feed.entries[:max_items]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()

            source = ""
            if " - " in title:
                title_parts = title.rsplit(" - ", 1)
                title = title_parts[0].strip()
                source = title_parts[1].strip()

            if title:
                parts = [f"- {title}"]

                if source:
                    parts.append(f"  Source: {source}")

                if link:
                    parts.append(f"  Link: {link}")

                reasons.append("\n".join(parts))

        if reasons:
            return "\n\n".join(reasons)

    except Exception as e:
        print(f"Google RSS fetch error for {symbol}: {e}")

    return "No recent news found."


def explain_stock_move(symbol, price, pct_change):
    """
    More accurate explanation using:
    1) company news
    2) sector ETF move
    3) technical breakout
    """
    reasons = []

    # ---------- A) Company news ----------
    news_reason = get_stock_reason(symbol, max_items=2)

    if "No recent news found" not in news_reason and "Unable to fetch" not in news_reason:
        reasons.append("📰 Company / recent news:\n" + news_reason)

    # ---------- B) Sector / market move ----------
    try:
        sector_symbol = SECTOR_ETF.get(symbol)

        if sector_symbol:
            sector_data = yf.Ticker(sector_symbol).history(period="2d")

            if not sector_data.empty and len(sector_data) >= 2:
                sector_price = sector_data["Close"].iloc[-1]
                sector_prev = sector_data["Close"].iloc[-2]
                sector_pct = ((sector_price - sector_prev) / sector_prev) * 100

                if abs(sector_pct) >= 1.0:
                    reasons.append(
                        f"📊 Sector / market context:\n"
                        f"{sector_symbol} moved {sector_pct:+.2f}% today"
                    )

                # Compare stock move vs sector move
                relative = pct_change - sector_pct
                if abs(relative) >= 2.0:
                    reasons.append(
                        f"⚖️ Relative move:\n"
                        f"{symbol} outperformed {sector_symbol} by {relative:+.2f}% today"
                    )

    except Exception as e:
        print(f"Sector check error for {symbol}: {e}")

    # ---------- C) Technical breakout ----------
    try:
        hist = yf.Ticker(symbol).history(period="1mo")

        if not hist.empty and len(hist) >= 20:
            recent_20d_high = hist["Close"].tail(20).max()
            recent_20d_low = hist["Close"].tail(20).min()

            if price >= recent_20d_high:
                reasons.append("📈 Technical signal:\nPrice is at / above 20-day high (breakout momentum).")
            elif price <= recent_20d_low:
                reasons.append("📉 Technical signal:\nPrice is at / below 20-day low (breakdown pressure).")
    except Exception as e:
        print(f"Technical check error for {symbol}: {e}")

    # ---------- D) Fallback ----------
    if not reasons:
        reasons.append(
            "⚠️ No strong single catalyst found.\n"
            "Move may be driven by broad market momentum, sector rotation, or technical buying."
        )

    return "\n\n".join(reasons)


def check_stock(symbol, config):
    """
    Check stock price against upper/lower thresholds
    """
    try:
        data = yf.Ticker(symbol).history(period="2d")

        if data.empty or len(data) < 1:
            print(f"No data for {symbol}")
            return

        price = data["Close"].iloc[-1]
        print(f"{symbol} price: {price}")

        upper = config["upper"]
        lower = config["lower"]

        # Daily % change
        if len(data) >= 2:
            prev_close = data["Close"].iloc[-2]
            pct_change = ((price - prev_close) / prev_close) * 100
        else:
            pct_change = 0

        pct_text = f"{pct_change:+.2f}% today"

        now = time.time()
        prev_state = last_state.get(symbol, "normal")
        last_time = last_alert_time.get(symbol, 0)

        # Current state
        if price > upper:
            current_state = "above"
        elif price < lower:
            current_state = "below"
        else:
            current_state = "normal"

        # Only alert on state change + cooldown
        if current_state != prev_state and (now - last_time > COOLDOWN):

            if current_state == "above":
                # Only explain reason if move is meaningful
                if pct_change >= MEANINGFUL_MOVE_PCT:
                    reason = explain_stock_move(symbol, price, pct_change)
                    msg = (
                        f"🚀 {symbol} ABOVE {upper}\n"
                        f"Now: {round(price,2)} ({pct_text})\n\n"
                        f"Possible reason:\n{reason}"
                    )
                else:
                    msg = (
                        f"🚀 {symbol} ABOVE {upper}\n"
                        f"Now: {round(price,2)} ({pct_text})\n"
                        f"Move is modest; no strong catalyst detected."
                    )

            elif current_state == "below":
                msg = (
                    f"🔻 {symbol} BELOW {lower}\n"
                    f"Now: {round(price,2)} ({pct_text})"
                )

            else:
                msg = (
                    f"↔️ {symbol} back to normal range\n"
                    f"Now: {round(price,2)} ({pct_text})"
                )

            send_line(msg)

            last_state[symbol] = current_state
            last_alert_time[symbol] = now

    except Exception as e:
        print(f"Error checking {symbol}: {e}")


def main():
    global last_heartbeat

    print("Stock bot started...")

    # Optional startup notification
    # send_line("🚀 Stock bot restarted successfully")

    while True:
        now = time.time()

        # 1) Check stocks
        for symbol, config in SYMBOLS.items():
            check_stock(symbol, config)

        # 2) Heartbeat
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            print("Sending heartbeat...")
            send_heartbeat()
            last_heartbeat = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()