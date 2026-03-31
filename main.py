import yfinance as yf
import requests
import time
import os
import feedparser
import re
import calendar
from urllib.parse import quote
from datetime import datetime, timezone

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
    "AAPL": {"upper": 240, "lower": 200},
    "SPY": {"upper": 697, "lower": 610},
    "QQQ": {"upper": 637, "lower": 540},
    "TSM": {"upper": 390, "lower": 340},
    "ASML": {"upper": 1547, "lower": 1250},
    "UCO": {"upper": 44.5, "lower": 28},
    "GOOG": {"upper": 350, "lower": 276},
    "MSFT": {"upper": 555, "lower": 347},
    "NVDA": {"upper": 212, "lower": 160},
    "CL=F": {"upper": 110, "lower": 80},
    "^TNX": {"upper": 4.55, "lower": 3.95},
    "2330.TW": {"upper": 2025, "lower": 1751},
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

CHECK_INTERVAL = 60
COOLDOWN = 1800
HEARTBEAT_INTERVAL = 43200

MEANINGFUL_UP_MOVE_PCT = 0.1
MEANINGFUL_DOWN_MOVE_PCT = 1.5

last_state = {}
last_alert_time = {}
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
    msg = "🟢 StockBot alive runningxx"
    send_line(msg)


def get_stock_reason(symbol, max_items=2, direction=None):
    """
    Smarter news fetch:
    1) Try Yahoo Finance news first
    2) Fallback to Google News RSS
    3) Score headlines by relevance
    4) Prefer recent + directional + stock-relevant headlines

    direction = "above" or "below" or None
    """
    POSITIVE_KEYWORDS = [
        "beat", "beats", "surge", "jump", "rise", "rises", "rally", "gain", "gains",
        "upgrade", "upgrades", "buy rating", "outperform", "strong demand", "record",
        "forecast raised", "guidance raised", "ai demand", "partnership", "approval"
    ]

    NEGATIVE_KEYWORDS = [
        "miss", "misses", "drop", "drops", "fall", "falls", "selloff", "decline",
        "downgrade", "downgrades", "cut target", "guidance cut", "weak demand",
        "delay", "lawsuit", "probe", "investigation", "recall", "warning"
    ]

    GENERAL_RELEVANT = [
        "earnings", "revenue", "profit", "margin", "guidance", "forecast",
        "analyst", "rating", "price target", "delivery", "demand", "sales",
        "chip", "ai", "iphone", "ev", "semiconductor"
    ]

    company = COMPANY_NAMES.get(symbol, symbol)
    symbol_root = symbol.replace(".TW", "").upper()

    def score_headline(title, source="", published_dt=None):
        score = 0
        text = f"{title} {source}".lower()

        # Mention company or ticker
        if company.lower() in text:
            score += 4
        if symbol_root.lower() in text:
            score += 3

        # General finance relevance
        for kw in GENERAL_RELEVANT:
            if kw in text:
                score += 2

        # Directional relevance
        if direction == "above":
            for kw in POSITIVE_KEYWORDS:
                if kw in text:
                    score += 3
        elif direction == "below":
            for kw in NEGATIVE_KEYWORDS:
                if kw in text:
                    score += 3
        else:
            for kw in POSITIVE_KEYWORDS + NEGATIVE_KEYWORDS:
                if kw in text:
                    score += 2

        # Penalize weak headlines
        WEAK_PATTERNS = [
            "opens new store",
            "what analysts think",
            "watch these stocks",
            "market wrap",
            "top stocks to watch",
            "morning briefing",
            "newsletter"
        ]
        for weak in WEAK_PATTERNS:
            if weak in text:
                score -= 3

        # Recency bonus
        if published_dt:
            try:
                now = datetime.now(timezone.utc)
                age_hours = (now - published_dt).total_seconds() / 3600

                if age_hours <= 24:
                    score += 4
                elif age_hours <= 48:
                    score += 2
                elif age_hours <= 72:
                    score += 1
                else:
                    score -= 2
            except:
                pass

        return score

    candidates = []

    # ---------- Yahoo Finance ----------
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news

        if news:
            for item in news:
                title = item.get("title", "").strip()
                publisher = item.get("publisher", "").strip()
                link = item.get("link", "").strip()

                published_dt = None
                ts = item.get("providerPublishTime") or item.get("pubDate")
                if ts:
                    try:
                        if isinstance(ts, (int, float)):
                            published_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    except:
                        pass

                if title:
                    score = score_headline(title, publisher, published_dt)
                    candidates.append({
                        "title": title,
                        "source": publisher,
                        "link": link,
                        "score": score,
                        "published_dt": published_dt
                    })

    except Exception as e:
        print(f"Yahoo news fetch error for {symbol}: {e}")

    # ---------- Google News RSS ----------
    try:
        query = quote(f"{company} stock")
        rss_url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

        feed = feedparser.parse(rss_url)

        for entry in feed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()

            source = ""
            if " - " in title:
                title_parts = title.rsplit(" - ", 1)
                title = title_parts[0].strip()
                source = title_parts[1].strip()

            published_dt = None
            try:
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    ts = calendar.timegm(entry.published_parsed)
                    published_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            except:
                pass

            if title:
                score = score_headline(title, source, published_dt)
                candidates.append({
                    "title": title,
                    "source": source,
                    "link": link,
                    "score": score,
                    "published_dt": published_dt
                })

    except Exception as e:
        print(f"Google RSS fetch error for {symbol}: {e}")

    # ---------- Filter ----------
    filtered = [x for x in candidates if x["score"] >= 4]
    if not filtered:
        filtered = [x for x in candidates if x["score"] >= 2]

    if not filtered:
        return "No recent high-confidence news found."

    # ---------- Sort ----------
    def sort_key(x):
        published_ts = x["published_dt"].timestamp() if x["published_dt"] else 0
        return (x["score"], published_ts)

    filtered = sorted(filtered, key=sort_key, reverse=True)

    # ---------- Deduplicate ----------
    final_items = []
    seen = set()

    for item in filtered:
        key = re.sub(r'[^a-z0-9 ]', '', item["title"].lower()).strip()
        if key not in seen:
            seen.add(key)
            final_items.append(item)

        if len(final_items) >= max_items:
            break

    # ---------- Format ----------
    reasons = []
    for item in final_items:
        parts = [f"- {item['title']}"]

        if item["source"]:
            parts.append(f"  Source: {item['source']}")

        if item["link"]:
            parts.append(f"  Link: {item['link']}")

        reasons.append("\n".join(parts))

    if reasons:
        return "\n\n".join(reasons)

    return "No recent high-confidence news found."


def detect_event_context(symbol):
    """
    Detect likely event / earnings context from news headlines.
    """
    try:
        raw = get_stock_reason(symbol, max_items=4, direction=None).lower()

        event_signals = []

        if any(k in raw for k in ["earnings", "revenue", "profit", "guidance", "forecast"]):
            event_signals.append("📅 Event context:\nPossible earnings / guidance-related move.")

        if any(k in raw for k in ["analyst", "rating", "price target", "upgrade", "downgrade"]):
            event_signals.append("📅 Event context:\nPossible analyst rating / target-related move.")

        if any(k in raw for k in ["approval", "partnership", "launch", "delivery", "demand"]):
            event_signals.append("📅 Event context:\nPossible product / demand / business event-related move.")

        return "\n\n".join(event_signals)

    except Exception as e:
        print(f"Event context error for {symbol}: {e}")
        return ""


def explain_stock_move(symbol, price, pct_change, direction):
    """
    More accurate explanation using:
    1) company news
    2) sector ETF move
    3) technical breakout / breakdown
    4) event-day detection
    """
    reasons = []

    # ---------- A) Company news ----------
    news_reason = get_stock_reason(symbol, max_items=2, direction=direction)

    if "No recent high-confidence news found" not in news_reason and "Unable to fetch" not in news_reason:
        reasons.append("📰 Company / recent news:\n" + news_reason)

    # ---------- B) Event context ----------
    event_context = detect_event_context(symbol)
    if event_context:
        reasons.append(event_context)

    # ---------- C) Sector / market move ----------
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

                relative = pct_change - sector_pct
                if abs(relative) >= 2.0:
                    if relative > 0:
                        reasons.append(
                            f"⚖️ Relative move:\n"
                            f"{symbol} outperformed {sector_symbol} by {relative:+.2f}% today"
                        )
                    else:
                        reasons.append(
                            f"⚖️ Relative move:\n"
                            f"{symbol} underperformed {sector_symbol} by {abs(relative):.2f}% today"
                        )

    except Exception as e:
        print(f"Sector check error for {symbol}: {e}")

    # ---------- D) Technical breakout / breakdown ----------
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

    # ---------- Fallback ----------
    if not reasons:
        if direction == "above":
            reasons.append(
                "⚠️ No strong single catalyst found.\n"
                "Move may be driven by broad market momentum, sector rotation, or technical buying."
            )
        else:
            reasons.append(
                "⚠️ No strong single catalyst found.\n"
                "Move may be driven by broad market weakness, sector selling, or technical breakdown."
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
                if pct_change >= MEANINGFUL_UP_MOVE_PCT:
                    reason = explain_stock_move(symbol, price, pct_change, "above")
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
                if pct_change <= -MEANINGFUL_DOWN_MOVE_PCT:
                    reason = explain_stock_move(symbol, price, pct_change, "below")
                    msg = (
                        f"🔻 {symbol} BELOW {lower}\n"
                        f"Now: {round(price,2)} ({pct_text})\n\n"
                        f"Possible reason:\n{reason}"
                    )
                else:
                    msg = (
                        f"🔻 {symbol} BELOW {lower}\n"
                        f"Now: {round(price,2)} ({pct_text})\n"
                        f"Drop is modest; no strong catalyst detected."
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