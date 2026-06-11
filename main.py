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
    "^TNX": {"upper": 5.0, "lower": 3.95},
    "2330.TW": {"upper": 2500, "lower": 2100},
    "0050.TW": {"upper": 120, "lower": 97},
    "1215.TW": {"upper": 160, "lower": 105},
    "4772.TWO": {"upper": 360, "lower": 240},
    "2912.TW": {"upper": 260, "lower": 200},
    "00662.TW": {"upper": 135, "lower": 100},
}

# ===== TAIWAN MONTHLY REVENUE CONFIG =====
TW_REVENUE_STOCKS = {
    "2330": "TSMC",
    "2912": "President Chain Store",
    "1215": "Uni-President",
    "4772": "Superpmi",   # 4772.TWO
}

revenue_last_reported = {}

# ===== UPDATED: QUARTERLY EARNINGS CONFIG =====
EARNINGS_CHECK_INTERVAL = 14400  # STEP 1: Reduced from 12 hours to 4 hrs to eliminate alert lag
last_earnings_check = 0
earnings_last_reported = {}  # Tracks reported quarters: {"AAPL": "2026-Q1"}

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
REVENUE_CHECK_INTERVAL = 14400   # Check revenue every 4 hrs between 1st and 10th in each month

MEANINGFUL_UP_MOVE_PCT = 0.1
MEANINGFUL_DOWN_MOVE_PCT = 1.5

last_state = {}
last_alert_time = {}
last_heartbeat = 0
last_revenue_check = 0

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


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
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    return 1 <= tw_now.day <= 10


def _revenue_report_month(tw_now=None):
    if tw_now is None:
        tw_now = datetime.now(timezone(timedelta(hours=8)))
    first_of_current = tw_now.replace(day=1)
    last_month = first_of_current - timedelta(days=1)
    return last_month.strftime("%Y-%m")


def fetch_tw_monthly_revenue(stock_id: str) -> dict | None:
    """Fetch the latest monthly revenue safely for Taiwan-listed/OTC stocks."""
    try:
        is_otc = stock_id in [k for k in TW_REVENUE_STOCKS if
                               f"{k}.TWO" in SYMBOLS or
                               not any(f"{k}.TW" in s for s in SYMBOLS)]

        url = (
            "https://www.tpex.org.tw/openapi/v1/mopsfin_t187" if is_otc else 
            "https://opendata.twse.com.tw/v1/financialStatements/monthly_revenue"
        ) + f"?response=json&id={stock_id}"

        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list) or not data:
            return None

        latest = data[0]
        period_str = str(latest.get("期別", "")).strip()
        
        if len(period_str) >= 5:
            minguo_year = int(period_str[:-2])
            month = int(period_str[-2:])
            ce_year = minguo_year + 1911
            year_month = f"{ce_year}-{month:02d}"
        else:
            return None

        return {
            "stock_id": stock_id,
            "year_month": year_month,
            "revenue": int(latest.get("當月營收", 0) or 0),
            "revenue_mom_pct": float(latest.get("上月比較增減(%)", 0) or 0),
            "revenue_yoy_pct": float(latest.get("去年同月增減(%)", 0) or 0),
        }
    except Exception as e:
        print(f"[Revenue] fetch error for {stock_id}: {e}")
        return None


def _format_revenue(value: int) -> str:
    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    elif value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    else:
        return f"{value:,}"


def _revenue_trend_emoji(pct: float) -> str:
    if pct >= 10: return "🚀"
    elif pct >= 5: return "📈"
    elif pct >= 0: return "➡️"
    elif pct >= -5: return "📉"
    else: return "🔻"


def build_revenue_message(result: dict, display_name: str) -> str:
    stock_id = result["stock_id"]
    year_month = result["year_month"]
    rev_str = _format_revenue(result["revenue"])
    mom = result["revenue_mom_pct"]
    yoy = result["revenue_yoy_pct"]

    mom_emoji = _revenue_trend_emoji(mom)
    yoy_emoji = _revenue_trend_emoji(yoy)

    return (
        f"📊 Monthly Revenue Update\n{'─' * 28}\n"
        f"🏢 {display_name} ({stock_id})\n📅 Period: {year_month}\n"
        f"💰 Revenue: NTD {rev_str}\n{mom_emoji} MoM: {mom:+.1f}%\n"
        f"{yoy_emoji} YoY: {yoy:+.1f}%\n{'─' * 28}"
    )


def check_tw_monthly_revenue():
    if not _is_revenue_release_window():
        print("[Revenue] Not in release window, skipping.")
        return

    report_month = _revenue_report_month()
    messages = []

    for stock_id, display_name in TW_REVENUE_STOCKS.items():
        if revenue_last_reported.get(stock_id) == report_month:
            continue

        result = fetch_tw_monthly_revenue(stock_id)
        if result is None or result["year_month"] != report_month:
            continue

        messages.append(build_revenue_message(result, display_name))
        revenue_last_reported[stock_id] = report_month

    for msg in messages:
        send_line(msg)
        time.sleep(1)


# ─────────────────────────────────────────────
#  QUARTERLY EARNINGS & FORECAST REASONING
# ─────────────────────────────────────────────

def fetch_quarterly_earnings_and_forecast(symbol: str) -> dict | None:
    """Fetches income statement filings and pairs them against target consensus profiles."""
    try:
        ticker = yf.Ticker(symbol)
        income_stmt = ticker.quarterly_income_stmt
        if income_stmt.empty:
            return None
            
        latest_date = income_stmt.columns[0]
        quarter_num = (latest_date.month - 1) // 3 + 1
        quarter_tag = f"{latest_date.year}-Q{quarter_num}"
        
        net_income = income_stmt.loc['Net Income'].iloc[0] if 'Net Income' in income_stmt.index else None
        revenue = income_stmt.loc['Total Revenue'].iloc[0] if 'Total Revenue' in income_stmt.index else None
        
        if revenue is None:
            return None

        eps_actual, eps_estimate = None, None
        cal = ticker.calendar
        if cal and isinstance(cal, dict):
            eps_actual = cal.get('Earnings Actual')
            eps_estimate = cal.get('Earnings Estimate')
            if isinstance(eps_actual, list) and eps_actual: eps_actual = eps_actual[0]
            if isinstance(eps_estimate, list) and eps_estimate: eps_estimate = eps_estimate[0]

        info = ticker.info or {}
        if not eps_estimate:
            eps_estimate = info.get('earningsEstimateNextQuarter')
            
        return {
            "symbol": symbol,
            "quarter_tag": quarter_tag,
            "report_date": latest_date.strftime("%Y-%m-%d"),
            "reported_revenue": revenue,
            "reported_net_income": net_income,
            "eps_actual": eps_actual,
            "eps_consensus": eps_estimate,
            "currency": info.get('currency', 'USD')
        }
    except Exception as e:
        print(f"[Earnings] Fundamental read failed for {symbol}: {e}")
        return None


def explain_earnings_performance(symbol: str, result: dict) -> str:
    """Generates qualitative reasoning explaining top/bottom line variance and sector impact."""
    analysis_points = []
    rev_actual = result.get("reported_revenue")
    eps_actual = result.get("eps_actual")
    eps_est = result.get("eps_consensus")
    
    if eps_actual is not None and eps_est is not None:
        delta = eps_actual - eps_est
        pct_delta = (delta / abs(eps_est)) * 100 if eps_est != 0 else 0
        if delta > 0:
            analysis_points.append(f"📈 Financial Health:\nBottom-line EPS outpaced analyst consensus targets by {pct_delta:.1f}%.")
        else:
            analysis_points.append(f"📉 Financial Health:\nBottom-line EPS missed consensus targets by {abs(pct_delta):.1f}%. Check for compression margins.")
            
    try:
        raw_news = get_stock_reason(symbol, max_items=2)
        if "No recent high-confidence news found" not in raw_news:
            analysis_points.append(f"🔮 Guidance / Forward Outlook Commentary:\n{raw_news}")
    except:
        pass

    try:
        sector_symbol = SECTOR_ETF.get(symbol)
        if sector_symbol:
            sector_hist = yf.Ticker(sector_symbol).history(period="2d")
            if len(sector_hist) >= 2:
                sec_pct = ((sector_hist["Close"].iloc[-1] - sector_hist["Close"].iloc[-2]) / sector_hist["Close"].iloc[-2]) * 100
                analysis_points.append(f"📊 Market Backdrop:\nProxy tracker {sector_symbol} printed {sec_pct:+.2f}% during this earnings rollout cycle.")
    except:
        pass

    return "\n\n".join(analysis_points) if analysis_points else "⚠️ No abnormal variances flagged."


def check_and_alert_earnings():
    """Monitors both US and TW tickers for freshly minted corporate performance alerts."""
    # STEP 2: Time-gate check to protect API limits.
    # Earnings are not released on weekends. We filter checks out between Friday night and Sunday night UTC.
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < 20) or (now_utc.weekday() == 4 and now_utc.hour > 22):
        print("[Earnings] Market weekend pause window active. Skipping API scrape pass.")
        return

    print("[Earnings] Initiating global structural earnings scan...")
    for symbol in SYMBOLS.keys():
        if symbol.startswith("^") or symbol in ["SPY", "QQQ"]:
            continue
            
        result = fetch_quarterly_earnings_and_forecast(symbol)
        if not result:
            continue
            
        quarter_tag = result["quarter_tag"]
        if earnings_last_reported.get(symbol) == quarter_tag:
            continue
            
        curr = result["currency"]
        scale = 1_000_000_000 if curr == "TWD" or result["reported_revenue"] >= 1_000_000_000 else 1_000_000
        suffix = "B" if scale == 1_000_000_000 else "M"
        
        rev_human = f"{result['reported_revenue'] / scale:.2f}{suffix}"
        ni_human = f"{result['reported_net_income'] / scale:.2f}{suffix}" if result['reported_net_income'] else "N/A"
        
        eps_perf_str = "N/A"
        if result["eps_actual"] is not None and result["eps_consensus"] is not None:
            diff = result["eps_actual"] - result["eps_consensus"]
            eps_perf_str = f"{result['eps_actual']:.2f} vs Est. {result['eps_consensus']:.2f} ({'🚀 Beat' if diff >= 0 else '🔻 Miss'} of {diff:+.2f})"
        elif result["eps_actual"] is not None:
            eps_perf_str = f"{result['eps_actual']:.2f}"

        reasoning_summary = explain_earnings_performance(symbol, result)

        msg = (
            f"📢 Quarterly Earnings Release\n{'─' * 28}\n"
            f"🏢 Ticker: {symbol}\n📅 Period: {quarter_tag} (Ended {result['report_date']})\n"
            f"💰 Revenue: {curr} {rev_human}\n📈 Net Income: {curr} {ni_human}\n"
            f"📊 EPS Benchmark Matrix:\n   {eps_perf_str}\n\n"
            f"🧐 Bot Analysis & Context:\n{reasoning_summary}\n{'─' * 28}"
        )
        
        send_line(msg)
        earnings_last_reported[symbol] = quarter_tag
        time.sleep(1)


# ─────────────────────────────────────────────
#  NEWS & REASON HELPERS (unchanged)
# ─────────────────────────────────────────────

def get_stock_reason(symbol, max_items=2, direction=None):
    POSITIVE_KEYWORDS = ["beat", "beats", "surge", "jump", "rise", "rises", "rally", "gain", "upgrade", "strong demand", "forecast raised"]
    NEGATIVE_KEYWORDS = ["miss", "misses", "drop", "drops", "fall", "falls", "selloff", "decline", "downgrade", "guidance cut", "weak demand"]
    GENERAL_RELEVANT = ["earnings", "revenue", "profit", "margin", "guidance", "forecast", "analyst", "price target", "ai", "semiconductor"]

    company = COMPANY_NAMES.get(symbol, symbol)
    symbol_root = symbol.replace(".TW", "").upper()

    def score_headline(title, source="", published_dt=None):
        score = 0
        text = f"{title} {source}".lower()
        if company.lower() in text: score += 4
        if symbol_root.lower() in text: score += 3
        for kw in GENERAL_RELEVANT:
            if kw in text: score += 2
        if direction == "above":
            for kw in POSITIVE_KEYWORDS:
                if kw in text: score += 3
        elif direction == "below":
            for kw in NEGATIVE_KEYWORDS:
                if kw in text: score += 3
        if published_dt:
            try:
                age_hours = (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600
                if age_hours <= 24: score += 4
                elif age_hours <= 48: score += 2
            except: pass
        return score

    candidates = []
    try:
        news = yf.Ticker(symbol).news
        if news:
            for item in news:
                title, publisher, link = item.get("title", "").strip(), item.get("publisher", "").strip(), item.get("link", "").strip()
                ts = item.get("providerPublishTime") or item.get("pubDate")
                published_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else None
                if title:
                    candidates.append({"title": title, "source": publisher, "link": link, "score": score_headline(title, publisher, published_dt), "published_dt": published_dt})
    except: pass

    try:
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={quote(f'{company} stock')}&hl=en-US&gl=US&ceid=US:en")
        for entry in feed.entries:
            title, link = getattr(entry, "title", "").strip(), getattr(entry, "link", "").strip()
            source = title.rsplit(" - ", 1)[1].strip() if " - " in title else ""
            title = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
            published_dt = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc) if hasattr(entry, "published_parsed") else None
            if title:
                candidates.append({"title": title, "source": source, "link": link, "score": score_headline(title, source, published_dt), "published_dt": published_dt})
    except: pass

    filtered = [x for x in candidates if x["score"] >= 4] or [x for x in candidates if x["score"] >= 2]
    if not filtered: return "No recent high-confidence news found."

    filtered = sorted(filtered, key=lambda x: (x["score"], x["published_dt"].timestamp() if x["published_dt"] else 0), reverse=True)
    final_items, seen = [], set()
    for item in filtered:
        key = re.sub(r'[^a-z0-9 ]', '', item["title"].lower()).strip()
        if key not in seen:
            seen.add(key)
            final_items.append(item)
        if len(final_items) >= max_items: break

    return "\n\n".join([f"- {i['title']}\n  Source: {i['source']}\n  Link: {i['link']}" for i in final_items])


def detect_event_context(symbol):
    try:
        raw = get_stock_reason(symbol, max_items=4, direction=None).lower()
        event_signals = []
        if any(k in raw for k in ["earnings", "revenue", "profit", "guidance", "forecast"]):
            event_signals.append("📅 Event context:\nPossible earnings / guidance-related move.")
        if any(k in raw for k in ["analyst", "rating", "price target", "upgrade", "downgrade"]):
            event_signals.append("📅 Event context:\nPossible analyst rating / target-related move.")
        return "\n\n".join(event_signals)
    except: return ""


def explain_stock_move(symbol, price, pct_change, direction):
    reasons = []
    news_reason = get_stock_reason(symbol, max_items=2, direction=direction)
    if "No recent high-confidence news found" not in news_reason:
        reasons.append("📰 Company / recent news:\n" + news_reason)

    event_context = detect_event_context(symbol)
    if event_context: reasons.append(event_context)

    try:
        sector_symbol = SECTOR_ETF.get(symbol)
        if sector_symbol:
            sector_data = yf.Ticker(sector_symbol).history(period="2d")
            if len(sector_data) >= 2:
                sector_pct = ((sector_data["Close"].iloc[-1] - sector_data["Close"].iloc[-2]) / sector_data["Close"].iloc[-2]) * 100
                if abs(sector_pct) >= 1.0:
                    reasons.append(f"📊 Sector / market context:\n{sector_symbol} moved {sector_pct:+.2f}% today")
                relative = pct_change - sector_pct
                if abs(relative) >= 2.0:
                    reasons.append(f"⚖️ Relative move:\n{symbol} {'outperformed' if relative > 0 else 'underperformed'} {sector_symbol} by {abs(relative):.2f}%")
    except: pass

    try:
        hist = yf.Ticker(symbol).history(period="1mo")
        if len(hist) >= 20:
            if price >= hist["Close"].tail(20).max(): reasons.append("📈 Technical signal:\nPrice is at / above 20-day high.")
            elif price <= hist["Close"].tail(20).min(): reasons.append("📉 Technical signal:\nPrice is at / below 20-day low.")
    except: pass

    if not reasons:
        reasons.append(f"⚠️ No strong single catalyst found. Move may be broad market {'momentum' if direction=='above' else 'weakness'}.")
    return "\n\n".join(reasons)


# ─────────────────────────────────────────────
#  STOCK PRICE ALERT 
# ─────────────────────────────────────────────

def check_stock(symbol, config):
    try:
        data = yf.Ticker(symbol).history(period="2d")
        if data.empty or len(data) < 1: return

        price = data["Close"].iloc[-1]
        upper, lower = config["upper"], config["lower"]
        pct_change = ((price - data["Close"].iloc[-2]) / data["Close"].iloc[-2]) * 100 if len(data) >= 2 else 0
        pct_text = f"{pct_change:+.2f}% today"

        now = time.time()
        prev_state = last_state.get(symbol, "normal")
        last_time = last_alert_time.get(symbol, 0)

        current_state = "above" if price > upper else ("below" if price < lower else "normal")

        if current_state != prev_state and (now - last_time > COOLDOWN):
            msg = None
            if current_state == "above":
                if pct_change >= MEANINGFUL_UP_MOVE_PCT:
                    msg = f"🚀 {symbol} ABOVE {upper}\nNow: {round(price,2)} ({pct_text})\n\nPossible reason:\n{explain_stock_move(symbol, price, pct_change, 'above')}"
                else:
                    msg = f"🚀 {symbol} ABOVE {upper}\nNow: {round(price,2)} ({pct_text})\nMove is modest; no strong catalyst detected."
            elif current_state == "below":
                if pct_change <= -MEANINGFUL_DOWN_MOVE_PCT:
                    msg = f"🔻 {symbol} BELOW {lower}\nNow: {round(price,2)} ({pct_text})\n\nPossible reason:\n{explain_stock_move(symbol, price, pct_change, 'below')}"
                else:
                    msg = f"🔻 {symbol} BELOW {lower}\nNow: {round(price,2)} ({pct_text})\nDrop is modest; no strong catalyst detected."

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
    global last_heartbeat, last_revenue_check, last_earnings_check
    print("Stock bot started...")

    while True:
        now = time.time()

        # 1) Price Checks (Every 60s)
        for symbol, config in SYMBOLS.items():
            check_stock(symbol, config)

        # 2) Taiwan Monthly Revenue Check (every 4 hrs in the revenue window 1st to 10th)
        if now - last_revenue_check > REVENUE_CHECK_INTERVAL:
            print("Running Taiwan monthly revenue check...")
            check_tw_monthly_revenue()
            last_revenue_check = now

        # 3) UPDATED: Quarterly Earnings (Checks every 4 hrs, filters out weekends internally)
        if now - last_earnings_check > EARNINGS_CHECK_INTERVAL:
            # check_and_alert_earnings()
            last_earnings_check = now

        # 4) Heartbeat (Every 24 hours) Stop sending heartbeat here
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            print("Sending heartbeat...")
            send_heartbeat()
            last_heartbeat = now

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
