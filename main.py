import yfinance as yf
import requests
import time
import os
import feedparser
import re
import calendar
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

LINE_TOKEN = os.getenv("LINE_TOKEN")
USER_ID = os.getenv("USER_ID")

# ===== CONFIG =====
SYMBOLS = {
 #   "AAPL": {"upper": 240, "lower": 200},
    "SPY": {"upper": 800, "lower": 614},
    "QQQ": {"upper": 800, "lower": 540},
    "TSM": {"upper": 500, "lower": 350},
    "ASML": {"upper": 1850, "lower": 1250},
 #   "UCO": {"upper": 44.5, "lower": 28},
    "GOOG": {"upper": 400, "lower": 300},
    "MSFT": {"upper": 500, "lower": 370},
    "META": {"upper": 700, "lower": 484},
    "ISRG": {"upper": 540, "lower": 370},
    "NVDA": {"upper": 250, "lower": 190},
 #   "T": {"upper": 29, "lower": 23.4},
 #   "CL=F": {"upper": 120, "lower": 80},
    "^TNX": {"upper": 4.55, "lower": 3.95},
    "2330.TW": {"upper": 2500, "lower": 2100},
    "0050.TW": {"upper": 120, "lower": 97},
    "1215.TW": {"upper": 160, "lower": 105},
    "4772.TWO": {"upper": 340, "lower": 290},
    "2912.TW": {"upper": 260, "lower": 200},
    "00662.TW": {"upper": 135, "lower": 100},
}

# ===== TAIWAN MONTHLY REVENUE CONFIG =====
# Stock ID → display name mapping for Taiwan listed companies
# Add or remove stocks here. Use the numeric ID without ".TW"/".TWO"
TW_REVENUE_STOCKS = {
    "2330": "TSMC",
    "2912": "President Chain Store",
    "1215": "Uni-President",
    "4772": "Superpmi",   # 4772.TWO
}

# Taiwan listed companies report monthly revenue between the 7th and 10th
# of the following month (TWSE rule). We alert once per month.
# Key: stock_id (str), Value: last reported month string "YYYY-MM"
revenue_last_reported = {}

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
    "AAPL": "QQQ",
    "MSFT": "QQQ",
    "GOOG": "QQQ",
    "NVDA": "SOXX",
    "TSM": "SOXX",
    "TSLA": "XLY",
    "2330.TW": "^TWII"
}

CHECK_INTERVAL = 60
COOLDOWN = 1800
HEARTBEAT_INTERVAL = 86400

# Revenue check runs once a day (in seconds)
REVENUE_CHECK_INTERVAL = 86400

MEANINGFUL_UP_MOVE_PCT = 0.1
MEANINGFUL_DOWN_MOVE_PCT = 1.5

last_state = {}
last_alert_time = {}
last_heartbeat = 0
last_revenue_check = 0


# ─────────────────────────────────────────────
#  LINE MESSAGING
# ─────────────────────────────────────────────

def send_line(msg):
    """Send a text message to LINE."""
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
    msg = "🟢 StockBot making money!"
    send_line(msg)


# ─────────────────────────────────────────────
#  TAIWAN MONTHLY REVENUE FEATURE
# ─────────────────────────────────────────────

def _is_revenue_release_window():
    """
    TWSE rule: monthly revenue is released between the 7th and 10th
    of the following month (Taiwan time, UTC+8).
    Returns True if today falls in that window.
    """
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    return 1 <= tw_now.day <= 10


def _revenue_report_month(tw_now=None):
    """
    Return the month whose revenue is being reported.
    e.g. if today is 2026-06-09, the report is for 2026-05 → "2026-05"
    """
    if tw_now is None:
        tw_now = datetime.now(timezone(timedelta(hours=8)))
    # Revenue released in month M is for month M-1
    first_of_current = tw_now.replace(day=1)
    last_month = first_of_current - timedelta(days=1)
    return last_month.strftime("%Y-%m")


def fetch_tw_monthly_revenue(stock_id: str) -> dict | None:
    """
    Fetch the latest monthly revenue for a Taiwan-listed stock via
    the TWSE open-data API.

    Returns a dict:
        {
            "stock_id": "2330",
            "year_month": "2026-05",        # Minguo era converted to CE
            "revenue": 260_000_000_000,     # NTD (int)
            "revenue_mom_pct": 3.5,         # month-over-month %
            "revenue_yoy_pct": 12.1,        # year-over-year %
        }
    or None on failure.

    API reference:
      https://opendata.twse.com.tw/v1/financialStatements/monthly_revenue
      (TWSE listed stocks, free, no auth required)

    For OTC (TWO) stocks the equivalent TPEX endpoint is used.
    """
    try:
        # Determine if OTC or listed
        is_otc = stock_id in [k for k in TW_REVENUE_STOCKS if
                               f"{k}.TWO" in SYMBOLS or
                               not any(f"{k}.TW" in s for s in SYMBOLS)]

        # ---------- TWSE listed ----------
        if not is_otc:
            url = (
                "https://opendata.twse.com.tw/v1/financialStatements/monthly_revenue"
                f"?response=json&id={stock_id}"
            )
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                return None

            # API returns a list sorted newest-first
            latest = data[0]

            # The "期別" field is "YYYMMM" in Minguo era, e.g. "11505" = 民國115年5月
            period_str = str(latest.get("期別", ""))
            if len(period_str) == 5:
                minguo_year = int(period_str[:3])
                month = int(period_str[3:])
                ce_year = minguo_year + 1911
                year_month = f"{ce_year}-{month:02d}"
            else:
                year_month = "unknown"

            revenue = int(latest.get("當月營收", 0))
            mom_pct = float(latest.get("上月比較增減(%)", 0) or 0)
            yoy_pct = float(latest.get("去年同月增減(%)", 0) or 0)

            return {
                "stock_id": stock_id,
                "year_month": year_month,
                "revenue": revenue,
                "revenue_mom_pct": mom_pct,
                "revenue_yoy_pct": yoy_pct,
            }

        # ---------- TPEX OTC ----------
        else:
            # TPEX open data for OTC monthly revenue
            url = (
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187"
                f"?response=json&id={stock_id}"
            )
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                return None

            latest = data[0]

            period_str = str(latest.get("期別", ""))
            if len(period_str) == 5:
                minguo_year = int(period_str[:3])
                month = int(period_str[3:])
                ce_year = minguo_year + 1911
                year_month = f"{ce_year}-{month:02d}"
            else:
                year_month = "unknown"

            revenue = int(latest.get("當月營收", 0))
            mom_pct = float(latest.get("上月比較增減(%)", 0) or 0)
            yoy_pct = float(latest.get("去年同月增減(%)", 0) or 0)

            return {
                "stock_id": stock_id,
                "year_month": year_month,
                "revenue": revenue,
                "revenue_mom_pct": mom_pct,
                "revenue_yoy_pct": yoy_pct,
            }

    except Exception as e:
        print(f"[Revenue] fetch error for {stock_id}: {e}")
        return None


def _format_revenue(value: int) -> str:
    """Human-readable NTD revenue string, e.g. 260.0B, 3.5B, 450M."""
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    elif value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    else:
        return f"{value:,}"


def _revenue_trend_emoji(pct: float) -> str:
    if pct >= 10:
        return "🚀"
    elif pct >= 5:
        return "📈"
    elif pct >= 0:
        return "➡️"
    elif pct >= -5:
        return "📉"
    else:
        return "🔻"


def build_revenue_message(result: dict, display_name: str) -> str:
    """
    Format a LINE-ready revenue update message for one stock.
    """
    stock_id = result["stock_id"]
    year_month = result["year_month"]
    rev_str = _format_revenue(result["revenue"])
    mom = result["revenue_mom_pct"]
    yoy = result["revenue_yoy_pct"]

    mom_emoji = _revenue_trend_emoji(mom)
    yoy_emoji = _revenue_trend_emoji(yoy)

    msg = (
        f"📊 Monthly Revenue Update\n"
        f"{'─' * 28}\n"
        f"🏢 {display_name} ({stock_id})\n"
        f"📅 Period: {year_month}\n"
        f"💰 Revenue: NTD {rev_str}\n"
        f"{mom_emoji} MoM: {mom:+.1f}%\n"
        f"{yoy_emoji} YoY: {yoy:+.1f}%\n"
        f"{'─' * 28}"
    )
    return msg


def check_tw_monthly_revenue():
    """
    Check and notify Taiwan monthly revenue for all configured stocks.

    Logic:
    - Only runs during the TWSE release window (7th–10th of the month).
    - Sends a notification only once per stock per reporting month.
    - Sends a combined summary if multiple stocks update simultaneously.
    """
    if not _is_revenue_release_window():
        print("[Revenue] Not in release window, skipping.")
        return

    report_month = _revenue_report_month()
    print(f"[Revenue] Checking for month: {report_month}")

    messages = []

    for stock_id, display_name in TW_REVENUE_STOCKS.items():
        # Skip if we already notified for this reporting month
        if revenue_last_reported.get(stock_id) == report_month:
            print(f"[Revenue] {stock_id} already reported for {report_month}, skipping.")
            continue

        result = fetch_tw_monthly_revenue(stock_id)

        if result is None:
            print(f"[Revenue] No data returned for {stock_id}.")
            continue

        # Only send if the data matches the expected reporting month
        if result["year_month"] != report_month:
            print(
                f"[Revenue] {stock_id} data month ({result['year_month']}) "
                f"does not match expected ({report_month}), skipping."
            )
            continue

        msg = build_revenue_message(result, display_name)
        messages.append(msg)
        revenue_last_reported[stock_id] = report_month
        print(f"[Revenue] Queued notification for {stock_id}.")

    if messages:
        # Send each as a separate LINE message so they're easy to read
        for msg in messages:
            send_line(msg)
            time.sleep(1)   # brief pause to avoid rate-limiting
    else:
        print("[Revenue] No new revenue data to notify.")


# ─────────────────────────────────────────────
#  NEWS & REASON HELPERS (unchanged)
# ─────────────────────────────────────────────

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

        if company.lower() in text:
            score += 4
        if symbol_root.lower() in text:
            score += 3

        for kw in GENERAL_RELEVANT:
            if kw in text:
                score += 2

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

        WEAK_PATTERNS = [
            "opens new store", "what analysts think", "watch these stocks",
            "market wrap", "top stocks to watch", "morning briefing", "newsletter"
        ]
        for weak in WEAK_PATTERNS:
            if weak in text:
                score -= 3

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
    """Detect likely event / earnings context from news headlines."""
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


# ─────────────────────────────────────────────
#  STOCK PRICE ALERT (unchanged)
# ─────────────────────────────────────────────

def check_stock(symbol, config):
    """Check stock price against upper/lower thresholds."""
    try:
        data = yf.Ticker(symbol).history(period="2d")

        if data.empty or len(data) < 1:
            print(f"No data for {symbol}")
            return

        price = data["Close"].iloc[-1]
        print(f"{symbol} price: {price}")

        upper = config["upper"]
        lower = config["lower"]

        if len(data) >= 2:
            prev_close = data["Close"].iloc[-2]
            pct_change = ((price - prev_close) / prev_close) * 100
        else:
            pct_change = 0

        pct_text = f"{pct_change:+.2f}% today"

        now = time.time()
        prev_state = last_state.get(symbol, "normal")
        last_time = last_alert_time.get(symbol, 0)

        if price > upper:
            current_state = "above"
        elif price < lower:
            current_state = "below"
        else:
            current_state = "normal"

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
                msg = None

            if msg:
                send_line(msg)
                last_alert_time[symbol] = now

            last_state[symbol] = current_state

    except Exception as e:
        print(f"Error checking {symbol}: {e}")


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    global last_heartbeat, last_revenue_check

    print("Stock bot started...")

    while True:
        now = time.time()

        # 1) Check stock prices
        for symbol, config in SYMBOLS.items():
            check_stock(symbol, config)

        # 2) Check Taiwan monthly revenue (once per day during release window)
        if now - last_revenue_check > REVENUE_CHECK_INTERVAL:
            print("Running Taiwan monthly revenue check...")
            check_tw_monthly_revenue()
            last_revenue_check = now

        # 3) Heartbeat
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            print("Sending heartbeat...")
            send_heartbeat()
            last_heartbeat = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
