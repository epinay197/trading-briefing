#!/usr/bin/env python3
"""
Daily Trading Briefing Generator
─────────────────────────────────
Run at ~7:55 AM ET for 8:00 AM session prep (NQ / ES, 8AM–12PM ET).

Usage:
  python daily_briefing.py            # generate & open briefing
  python daily_briefing.py --schedule # register Windows scheduled task at 7:55 AM ET

Environment variables (set via Claude Code settings or shell profile):
  MENTHORQ_EMAIL      MenthorQ login email
  MENTHORQ_PASSWORD   MenthorQ login password
  ANTHROPIC_API_KEY   Anthropic API key for AI-generated narrative
"""

# ── Bootstrap dependencies ────────────────────────────────────────────────────
import subprocess, sys

def _pip(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs, "-q", "--quiet"])

try:
    import requests
except ImportError:
    _pip("requests"); import requests

# tzdata required on Windows for zoneinfo
try:
    import tzdata  # noqa: F401
except ImportError:
    _pip("tzdata")
    import tzdata  # noqa: F401

try:
    import anthropic as _ant
    HAS_ANTHROPIC = True
except ImportError:
    try:
        _pip("anthropic"); import anthropic as _ant; HAS_ANTHROPIC = True
    except Exception:
        HAS_ANTHROPIC = False

# ── Standard library ──────────────────────────────────────────────────────────
import json, os, webbrowser, threading, html, textwrap
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
IS_CI = bool(os.environ.get("CI"))          # True inside GitHub Actions

# In CI the workflow sets TRADING_DIR=./docs so output lands in docs/
BASE_DIR = Path(os.environ.get("TRADING_DIR", Path.home() / "trading"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")
NOW = datetime.now(ET)
DATE_STR     = NOW.strftime("%Y-%m-%d")
DATE_DISPLAY = NOW.strftime("%A, %B %d, %Y")
GEN_TIME     = NOW.strftime("%I:%M %p ET")

# ntfy.sh push notification topic (set as GitHub secret or env var)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
# GitHub Pages root URL e.g. https://username.github.io/repo-name
PAGES_URL  = os.environ.get("PAGES_URL", "").rstrip("/")

# ── Market Calendar ───────────────────────────────────────────────────────────
def _nth_weekday(year, month, weekday, n):
    """Return the date of the nth weekday (0=Mon…6=Sun) in a given month."""
    d = date(year, month, 1)
    delta = (weekday - d.weekday()) % 7
    d += timedelta(days=delta)
    return d + timedelta(weeks=n - 1)

def _last_weekday(year, month, weekday):
    """Return the last occurrence of weekday in a given month."""
    d = date(year, month + 1, 1) - timedelta(days=1)
    delta = (d.weekday() - weekday) % 7
    return d - timedelta(days=delta)

def _observed(d):
    """Shift a holiday to Monday if it falls on Sunday, or Friday if Saturday."""
    if d.weekday() == 6:   # Sunday -> Monday
        return d + timedelta(days=1)
    if d.weekday() == 5:   # Saturday -> Friday
        return d - timedelta(days=1)
    return d

def nyse_holidays(year: int) -> set:
    """
    Return the set of NYSE market holidays for the given year.
    Covers: New Year's Day, MLK Day, Presidents' Day, Good Friday,
            Memorial Day, Juneteenth, Independence Day, Labor Day,
            Thanksgiving, Christmas Day.
    """
    # Easter calculation (Anonymous Gregorian algorithm)
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    easter = date(year, month, day + 1)
    good_friday = easter - timedelta(days=2)

    holidays = {
        _observed(date(year, 1,  1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                     # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),                     # Presidents' Day (3rd Mon Feb)
        good_friday,                                      # Good Friday
        _last_weekday(year, 5, 0),                       # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),                    # Juneteenth
        _observed(date(year, 7,  4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                     # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),                    # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),                   # Christmas Day
    }
    # New Year's Day observed in Jan of NEXT year (if Dec 31 is Friday)
    dec31 = date(year, 12, 31)
    if dec31.weekday() == 4:  # Friday -> NYE not a holiday but NY observed next Mon
        holidays.add(date(year + 1, 1, 1))
    return holidays

def get_market_status(dt: datetime) -> dict:
    """
    Return market open/closed status and context for a given ET datetime.

    Returns dict with keys:
      is_trading_day  bool   True if NYSE is open today
      session_open    bool   True if regular session (9:30–16:00 ET) is live now
      futures_open    bool   True if CME Globex is live (Sun 18:00 – Fri 17:00 ET)
      reason          str    Human-readable status
      next_open       str    Next session open description
    """
    today    = dt.date()
    weekday  = today.weekday()      # 0=Mon … 6=Sun
    t        = dt.time()
    holidays = nyse_holidays(today.year)

    # ── Futures (CME Globex): Sun 18:00 ET – Fri 17:00 ET, daily break 17:00-18:00
    futures_open = False
    if weekday == 5:                                              # Saturday — closed
        futures_open = False
    elif weekday == 6:                                            # Sunday — opens 18:00
        from datetime import time as dtime
        futures_open = t >= dtime(18, 0)
    else:                                                         # Mon-Fri
        from datetime import time as dtime
        futures_open = not (t >= dtime(17, 0) and t < dtime(18, 0))  # closed 17-18

    # ── NYSE regular session: Mon-Fri 09:30–16:00, no holidays
    from datetime import time as dtime
    is_trading_day = (weekday < 5) and (today not in holidays)
    session_open   = (
        is_trading_day
        and t >= dtime(9, 30)
        and t < dtime(16, 0)
    )

    # ── Briefing window: fire if it's a trading day, regardless of current time
    #    (script runs at 7:55 AM — pre-market is valid)

    # ── Build reason string ───────────────────────────────────────────────────
    day_name = today.strftime("%A")
    if weekday == 5:
        reason = "Weekend — NYSE and CME closed"
        next_open = "Sunday 6:00 PM ET (futures) / Monday 9:30 AM ET (NYSE)"
    elif weekday == 6:
        if futures_open:
            reason = "Sunday evening — CME Globex open, NYSE opens Monday"
            next_open = "Monday 9:30 AM ET"
        else:
            reason = "Sunday — CME opens 6:00 PM ET, NYSE opens Monday"
            next_open = "Sunday 6:00 PM ET (futures)"
    elif today in holidays:
        # Find next trading day
        nxt = today + timedelta(days=1)
        while nxt.weekday() >= 5 or nxt in nyse_holidays(nxt.year):
            nxt += timedelta(days=1)
        reason = f"{day_name} — NYSE Holiday"
        next_open = f"{nxt.strftime('%A %B %d')} 9:30 AM ET"
    else:
        if t < dtime(9, 30):
            reason = f"{day_name} — Pre-market (NYSE opens 9:30 AM ET)"
        elif session_open:
            reason = f"{day_name} — NYSE Regular Session LIVE"
        else:
            reason = f"{day_name} — NYSE After-hours"
        next_open = "Now" if session_open else "9:30 AM ET tomorrow"

    return {
        "is_trading_day": is_trading_day,
        "session_open":   session_open,
        "futures_open":   futures_open,
        "reason":         reason,
        "next_open":      next_open,
        "is_weekend":     weekday >= 5,
        "is_holiday":     today in holidays,
    }

MENTHORQ_EMAIL    = os.environ.get("MENTHORQ_EMAIL", "")
MENTHORQ_PASSWORD = os.environ.get("MENTHORQ_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

OUTPUT_FILE = BASE_DIR / f"briefing_{DATE_STR}.html"
LATEST_FILE = BASE_DIR / "briefing_latest.html"

# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_get(url, *, timeout=10, headers=None, **kwargs):
    h = {"User-Agent": "Mozilla/5.0 TradingBriefing/2.0 (automated research tool)"}
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, headers=h, timeout=timeout, **kwargs)
        r.raise_for_status()
        return r
    except Exception:
        return None

def pct_color(v):
    if v is None: return "#6b7280"
    return "#4ade80" if v >= 0 else "#f87171"

def arrow(v):
    if v is None: return "—"
    return "▲" if v >= 0 else "▼"

def fmt_price(v, decimals=2):
    if v is None: return "—"
    return f"{v:,.{decimals}f}"

def fmt_pct(v):
    if v is None: return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"

# ── Data fetchers (all run in parallel threads) ───────────────────────────────

def fetch_fear_greed():
    r = safe_get("https://api.alternative.me/fng/?limit=2")
    if not r:
        return {}
    try:
        d = r.json()["data"]
        return {
            "value":       int(d[0]["value"]),
            "label":       d[0]["value_classification"],
            "prev_value":  int(d[1]["value"]),
            "prev_label":  d[1]["value_classification"],
        }
    except Exception:
        return {}


def fetch_futures():
    """Yahoo Finance intraday snapshot for key instruments."""
    instruments = [
        ("NQ=F",      "NQ Futures",   2),
        ("ES=F",      "ES Futures",   2),
        ("YM=F",      "YM Futures",   0),
        ("^VIX",      "VIX",          2),
        ("CL=F",      "WTI Crude",    2),
        ("GC=F",      "Gold",         1),
        ("DX-Y.NYB",  "DXY",          3),
        ("^TNX",      "10Y Yield",    3),
        ("EURUSD=X",  "EUR/USD",      4),
    ]
    results = []
    def _fetch_one(sym, name, dec):
        r = safe_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval=5m&range=1d"
        )
        if not r:
            results.append({"name": name, "sym": sym, "price": None, "dec": dec})
            return
        try:
            meta  = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
            chg   = round(price - prev, 4) if (price and prev) else 0
            pct   = round(chg / prev * 100, 2) if prev else 0
            results.append({
                "name":  name, "sym": sym, "dec": dec,
                "price": price, "prev": prev,
                "chg":   chg,   "pct":  pct,
                "high":  meta.get("regularMarketDayHigh"),
                "low":   meta.get("regularMarketDayLow"),
            })
        except Exception:
            results.append({"name": name, "sym": sym, "price": None, "dec": dec})

    threads = [threading.Thread(target=_fetch_one, args=a) for a in instruments]
    for t in threads: t.start()
    for t in threads: t.join()
    order = {s: i for i, (s, _, _) in enumerate(instruments)}
    results.sort(key=lambda x: order.get(x["sym"], 99))
    return results


def fetch_stocktwits_symbol(sym):
    r = safe_get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json")
    if not r:
        return None
    try:
        data = r.json()
        msgs = data.get("messages", [])
        bull  = sum(1 for m in msgs if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bear  = sum(1 for m in msgs if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total = max(len(msgs), 1)
        top   = sorted(msgs, key=lambda x: x.get("likes", {}).get("total", 0), reverse=True)[:3]
        return {
            "symbol":    sym,
            "watchlist": data.get("symbol", {}).get("watchlist_count", 0),
            "bull":      bull,
            "bear":      bear,
            "bull_pct":  round(bull / total * 100),
            "bear_pct":  round(bear / total * 100),
            "top": [
                {
                    "user":  m.get("user", {}).get("username", "anon"),
                    "body":  m.get("body", "")[:130],
                    "sent":  m.get("entities", {}).get("sentiment", {}).get("basic", ""),
                    "likes": m.get("likes", {}).get("total", 0),
                }
                for m in top
            ],
        }
    except Exception:
        return None


def fetch_stocktwits_trending():
    r = safe_get("https://api.stocktwits.com/api/2/trending/symbols.json")
    if not r:
        return []
    try:
        return [
            {
                "symbol":    s["symbol"],
                "title":     s.get("title", ""),
                "watchlist": s.get("watchlist_count", 0),
            }
            for s in r.json().get("symbols", [])[:12]
        ]
    except Exception:
        return []


def fetch_reddit_wsb():
    r = safe_get(
        "https://www.reddit.com/r/wallstreetbets/hot.json?limit=20",
        headers={"User-Agent": "TradingBriefing/2.0 research-only"},
    )
    if not r:
        return []
    try:
        posts = r.json()["data"]["children"]
        out = []
        for p in posts:
            d = p["data"]
            if d.get("stickied"):
                continue
            if d.get("ups", 0) < 50:
                continue
            out.append({
                "title":    d["title"][:110],
                "ups":      d["ups"],
                "comments": d["num_comments"],
                "flair":    d.get("link_flair_text") or "",
                "url":      f"https://reddit.com{d['permalink']}",
            })
        return out[:8]
    except Exception:
        return []


def fetch_menthorq():
    """
    Authenticate to MenthorQ and pull key chart images via the admin-ajax API.

    Flow:
      1. Login via wp-login.php → WordPress session cookies
      2. Load CTA dashboard page → extract QDataParams.nonce
      3. POST admin-ajax.php?action=get_command for each key slug
      4. Download chart images from signed S3 URLs → base64 embed

    Key slugs discovered via browser inspection:
      CTA:  cta_table, cta_index, cta_spx, cta_nasdaq
      Vol:  netgex, key_levels, vol_barometer, skew, vol_control, netgex_0dte
    """
    if not MENTHORQ_EMAIL or not MENTHORQ_PASSWORD:
        return {"status": "no_credentials"}

    import re, base64

    sess = requests.Session()
    ua  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"
    hdr = {"User-Agent": ua}

    # ── 1. Login ──────────────────────────────────────────────────────────────
    try:
        sess.get("https://menthorq.com/login/", headers=hdr, timeout=10)
        login = sess.post(
            "https://menthorq.com/wp-login.php",
            data={
                "log":         MENTHORQ_EMAIL,
                "pwd":         MENTHORQ_PASSWORD,
                "wp-submit":   "Log In",
                "redirect_to": "https://menthorq.com/account/",
                "rememberme":  "forever",
            },
            headers={**hdr, "Referer": "https://menthorq.com/login/"},
            allow_redirects=True,
            timeout=20,
        )
        if "account" not in login.url and "menthorq.com" not in login.url:
            return {"status": "login_failed", "url": login.url}
    except Exception as e:
        return {"status": "login_error", "error": str(e)}

    # ── 2. Extract nonce from CTA dashboard page ──────────────────────────────
    try:
        dash = sess.get(
            f"https://menthorq.com/account/?action=data&type=dashboard&commands=cta&date={DATE_STR}",
            headers=hdr, timeout=20
        )
        nonce_match = re.search(r'"nonce"\s*:\s*"([^"]+)"', dash.text)
        if not nonce_match:
            return {"status": "nonce_not_found"}
        nonce = nonce_match.group(1)
    except Exception as e:
        return {"status": "nonce_error", "error": str(e)}

    # ── 3. Fetch key command charts ───────────────────────────────────────────
    AJAX_URL = "https://menthorq.com/wp-admin/admin-ajax.php"

    # Slugs → display label for briefing sections
    SLUGS = {
        # CTA section
        "cta_table":    "CTA Main Table",
        "cta_spx":      "CTA SPX",
        "cta_nasdaq":   "CTA Nasdaq",
        # Vol / Gamma section
        "netgex":       "Net GEX (SPX)",
        "netgex_0dte":  "Net GEX 0DTE",
        "key_levels":   "Key Levels",
        "vol_barometer":"Vol Barometer",
        "skew":         "Skew",
        "vol_control":  "Vol Control",
    }

    charts = {}
    for slug, label in SLUGS.items():
        try:
            resp = sess.post(
                AJAX_URL,
                data={
                    "action":      "get_command",
                    "security":    nonce,
                    "command_slug": slug,
                    "date":        DATE_STR,
                    "is_intraday": "false",
                },
                headers={**hdr, "Referer": f"https://menthorq.com/account/?action=data&type=dashboard&commands=cta&date={DATE_STR}"},
                timeout=15,
            )
            j = resp.json()
            if not j.get("success"):
                charts[slug] = {"label": label, "status": "api_error", "msg": j.get("data", {}).get("message", "")}
                continue

            resource   = j["data"].get("resource", {})
            image_url  = resource.get("image_url", "")
            text_data  = resource.get("text_data") or ""
            table_data = resource.get("table_data") or []
            data_date  = resource.get("date", DATE_STR)

            # Download image and encode as base64
            img_b64 = ""
            if image_url:
                img_resp = sess.get(image_url, timeout=15)
                if img_resp.status_code == 200:
                    img_b64 = base64.b64encode(img_resp.content).decode()

            charts[slug] = {
                "label":      label,
                "status":     "ok",
                "date":       data_date,
                "img_b64":    img_b64,
                "text_data":  text_data,
                "table_data": table_data,
            }
        except Exception as e:
            charts[slug] = {"label": label, "status": "error", "error": str(e)}

    ok_count = sum(1 for v in charts.values() if v.get("status") == "ok" and v.get("img_b64"))
    return {
        "status":    "ok" if ok_count > 0 else "partial",
        "charts":    charts,
        "ok_count":  ok_count,
        "date":      DATE_STR,
    }


def generate_ai_narrative(payload: dict) -> dict:
    """Claude generates concise, actionable analysis from raw data."""
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return {}
    try:
        client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            messages=[{
                "role": "user",
                "content": textwrap.dedent(f"""
                    You are a senior institutional trading analyst.
                    Prepare a concise pre-session briefing for a US index day trader
                    (instruments: NQ, ES; session: 8AM–12PM ET; date: {DATE_DISPLAY}).

                    RAW DATA:
                    {json.dumps(payload, indent=2, default=str)[:6000]}

                    Return ONLY valid JSON (no markdown fences) with these string keys:
                    - macro_summary      : 4 bullet points on key macro/geo themes (use \\n• prefix each)
                    - overnight_analysis : 3 sentences on overnight NQ/ES narrative
                    - gamma_regime       : 2 sentences on gamma regime + intraday vol implication
                    - cta_flow           : 2 sentences on CTA/systematic flow
                    - sentiment_read     : 2 sentences interpreting retail sentiment vs institutional bias
                    - session_bias       : One bold directional bias + 2 key watch levels
                    - risk_events        : Specific catalysts to watch today (bullets)
                    - key_levels_nq      : JSON object with keys: r1, r2, support1, support2, pivot (all numbers)
                    - key_levels_es      : JSON object with keys: r1, r2, support1, support2, pivot (all numbers)
                """).strip()
            }]
        )
        return json.loads(msg.content[0].text)
    except Exception as e:
        return {"_error": str(e)}


# ── HTML Builder ──────────────────────────────────────────────────────────────

CSS = """
:root {
  --bg:       #0d1117;
  --panel:    #161b22;
  --border:   #30363d;
  --text:     #e6edf3;
  --muted:    #8b949e;
  --green:    #4ade80;
  --red:      #f87171;
  --yellow:   #fbbf24;
  --blue:     #60a5fa;
  --purple:   #c084fc;
  --accent:   #1f6feb;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'Segoe UI', system-ui, sans-serif;
  font-size: 13px; line-height: 1.5;
}
.header {
  background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
  border-bottom: 1px solid var(--border);
  padding: 18px 32px;
  display: flex; align-items: center; justify-content: space-between;
  position: sticky; top: 0; z-index: 100;
}
.header-left h1 { font-size: 20px; font-weight: 700; color: var(--blue); letter-spacing: 0.5px; }
.header-left .subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
.header-right { text-align: right; }
.session-countdown {
  font-size: 13px; font-weight: 600;
  padding: 6px 14px; border-radius: 6px;
  border: 1px solid var(--accent); color: var(--blue);
}
.tag-gen { font-size: 11px; color: var(--muted); margin-top: 4px; }
.container { max-width: 1400px; margin: 0 auto; padding: 20px 24px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
}
.panel-title {
  font-size: 11px; font-weight: 700; letter-spacing: 1px;
  color: var(--muted); text-transform: uppercase;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 12px;
}
.panel-title .dot {
  display: inline-block; width: 6px; height: 6px;
  border-radius: 50%; margin-right: 8px; vertical-align: middle;
}
table { width: 100%; border-collapse: collapse; }
th {
  font-size: 10px; font-weight: 600; letter-spacing: 0.8px;
  color: var(--muted); text-transform: uppercase;
  padding: 4px 8px; text-align: left;
  border-bottom: 1px solid var(--border);
}
td { padding: 5px 8px; border-bottom: 1px solid #21262d; font-size: 12px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,0.03); }
.up   { color: var(--green); font-weight: 600; }
.down { color: var(--red);   font-weight: 600; }
.neutral { color: var(--muted); }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
}
.badge-green  { background: rgba(74,222,128,0.15); color: var(--green); border: 1px solid rgba(74,222,128,0.3); }
.badge-red    { background: rgba(248,113,113,0.15); color: var(--red);   border: 1px solid rgba(248,113,113,0.3); }
.badge-yellow { background: rgba(251,191,36,0.15);  color: var(--yellow); border: 1px solid rgba(251,191,36,0.3); }
.badge-blue   { background: rgba(96,165,250,0.15);  color: var(--blue);  border: 1px solid rgba(96,165,250,0.3); }
.gauge-wrap { display: flex; align-items: center; gap: 14px; margin: 8px 0; }
.gauge-num  { font-size: 40px; font-weight: 800; }
.gauge-label { font-size: 12px; color: var(--muted); }
.gauge-delta { font-size: 11px; margin-top: 2px; }
.sentiment-bar-wrap { margin: 6px 0; }
.sbar { height: 8px; border-radius: 4px; overflow: hidden;
        display: flex; background: #21262d; }
.sbar-bull    { background: var(--green); }
.sbar-neutral { background: #6b7280; }
.sbar-bear    { background: var(--red); }
.sbar-labels  { display: flex; justify-content: space-between;
                font-size: 11px; color: var(--muted); margin-top: 3px; }
.bullet { display: block; padding: 3px 0; }
.bullet::before { content: "• "; color: var(--blue); }
.levels-chip {
  display: inline-block;
  padding: 3px 10px; border-radius: 4px;
  font-size: 12px; font-weight: 600; font-family: monospace;
  margin: 2px;
}
.chip-r  { background: rgba(248,113,113,0.12); color: var(--red);   border: 1px solid rgba(248,113,113,0.25); }
.chip-s  { background: rgba(74,222,128,0.12);  color: var(--green); border: 1px solid rgba(74,222,128,0.25); }
.chip-p  { background: rgba(251,191,36,0.12);  color: var(--yellow); border: 1px solid rgba(251,191,36,0.25); }
.bias-box {
  border-left: 4px solid var(--red);
  background: rgba(248,113,113,0.06);
  padding: 12px 16px; border-radius: 0 8px 8px 0;
  font-size: 13px; line-height: 1.7;
}
.bias-box.bullish { border-left-color: var(--green); background: rgba(74,222,128,0.06); }
.bias-box.neutral-b { border-left-color: var(--yellow); background: rgba(251,191,36,0.06); }
.st-row { border-bottom: 1px solid #21262d; padding: 7px 0; }
.st-row:last-child { border-bottom: none; }
.st-user  { color: var(--blue); font-size: 11px; font-weight: 600; }
.st-body  { color: var(--text); font-size: 12px; margin-top: 2px; }
.st-meta  { color: var(--muted); font-size: 10px; margin-top: 2px; }
.wsb-row { padding: 7px 0; border-bottom: 1px solid #21262d; }
.wsb-row:last-child { border-bottom: none; }
.wsb-title { color: var(--text); font-size: 12px; }
.wsb-meta  { color: var(--muted); font-size: 11px; margin-top: 3px; }
.wsb-flair {
  display: inline-block; background: rgba(192,132,252,0.15);
  color: var(--purple); border: 1px solid rgba(192,132,252,0.25);
  border-radius: 3px; padding: 1px 6px; font-size: 10px; margin-right: 6px;
}
.trend-chip {
  display: inline-block; margin: 3px;
  padding: 3px 10px; border-radius: 14px;
  font-size: 11px; font-weight: 600;
  background: rgba(96,165,250,0.1); color: var(--blue);
  border: 1px solid rgba(96,165,250,0.2);
}
.regime-pill {
  font-size: 13px; font-weight: 700;
  padding: 6px 16px; border-radius: 20px;
  display: inline-block; margin-bottom: 10px;
}
.regime-neg  { background: rgba(248,113,113,0.15); color: var(--red);   border: 1px solid rgba(248,113,113,0.3); }
.regime-pos  { background: rgba(74,222,128,0.15);  color: var(--green); border: 1px solid rgba(74,222,128,0.3); }
.menthorq-placeholder {
  color: var(--muted); font-size: 12px; font-style: italic;
  padding: 10px 0; text-align: center;
}
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.section-divider {
  height: 1px; background: var(--border);
  margin: 20px 0;
}
@media (max-width: 900px) {
  .grid-2, .grid-3 { grid-template-columns: 1fr; }
}
"""

def _fg_color(v):
    if v is None: return "#6b7280"
    if v >= 75: return "#4ade80"
    if v >= 55: return "#86efac"
    if v >= 45: return "#fbbf24"
    if v >= 25: return "#fb923c"
    return "#f87171"

def _fg_class(v):
    if v is None: return "badge-yellow"
    if v >= 55: return "badge-green"
    if v >= 45: return "badge-yellow"
    return "badge-red"

def _build_futures_table(futures):
    rows = ""
    for f in futures:
        if f.get("price") is None:
            rows += f"""<tr><td>{f['name']}</td><td colspan="5" class="neutral">—</td></tr>"""
            continue
        dec  = f.get("dec", 2)
        p    = f["price"]
        chg  = f.get("chg", 0)
        pct  = f.get("pct", 0)
        hi   = f.get("high")
        lo   = f.get("low")
        cls  = "up" if pct >= 0 else "down"
        rows += (
            f'<tr>'
            f'<td style="font-weight:600">{f["name"]}</td>'
            f'<td style="font-family:monospace">{fmt_price(p, dec)}</td>'
            f'<td class="{cls}">{arrow(pct)} {fmt_price(abs(chg), dec)}</td>'
            f'<td class="{cls}">{fmt_pct(pct)}</td>'
            f'<td style="font-family:monospace;color:#8b949e">{fmt_price(hi, dec)}</td>'
            f'<td style="font-family:monospace;color:#8b949e">{fmt_price(lo, dec)}</td>'
            f'</tr>'
        )
    return f"""
    <table>
      <thead><tr>
        <th>Instrument</th><th>Last</th><th>Chg</th><th>%</th>
        <th>Session Hi</th><th>Session Lo</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""

def _build_sentiment_panel(st_data):
    if not st_data:
        return '<div class="neutral">No data</div>'
    out = ""
    for sym, d in st_data.items():
        if not d:
            continue
        bull_pct = d.get("bull_pct", 0)
        bear_pct = d.get("bear_pct", 0)
        neut_pct = max(0, 100 - bull_pct - bear_pct)
        out += f"""
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
            <span style="font-weight:700;font-size:13px">${sym}</span>
            <span class="neutral" style="font-size:11px">👁 {d.get('watchlist',0):,} watching</span>
          </div>
          <div class="sbar">
            <div class="sbar-bull"    style="width:{bull_pct}%"></div>
            <div class="sbar-neutral" style="width:{neut_pct}%"></div>
            <div class="sbar-bear"    style="width:{bear_pct}%"></div>
          </div>
          <div class="sbar-labels">
            <span style="color:#4ade80">▲ {bull_pct}% Bull</span>
            <span style="color:#f87171">▼ {bear_pct}% Bear</span>
          </div>
        </div>"""
    return out

def _build_st_messages(st_data):
    out = ""
    for sym, d in st_data.items():
        if not d or not d.get("top"):
            continue
        out += f'<div style="font-weight:700;font-size:12px;color:#60a5fa;margin:8px 0 4px">${sym}</div>'
        for m in d["top"]:
            sent_badge = ""
            if m["sent"] == "Bullish":
                sent_badge = '<span class="badge badge-green">BULL</span>'
            elif m["sent"] == "Bearish":
                sent_badge = '<span class="badge badge-red">BEAR</span>'
            out += f"""
            <div class="st-row">
              <div class="st-user">@{html.escape(m['user'])} {sent_badge}</div>
              <div class="st-body">{html.escape(m['body'])}</div>
              <div class="st-meta">♥ {m['likes']}</div>
            </div>"""
    return out or '<div class="neutral">No messages fetched</div>'

def _build_trending_chips(trending):
    if not trending:
        return '<div class="neutral">No data</div>'
    chips = ""
    for t in trending:
        sym  = html.escape(t.get("symbol", ""))
        wl   = t.get("watchlist", 0)
        chips += f'<span class="trend-chip" title="{wl:,} watching">{sym}</span>'
    return chips

def _build_wsb_rows(posts):
    if not posts:
        return '<div class="neutral">No posts fetched</div>'
    out = ""
    for p in posts:
        flair_html = f'<span class="wsb-flair">{html.escape(p["flair"])}</span>' if p.get("flair") else ""
        out += f"""
        <div class="wsb-row">
          <div class="wsb-title">
            <a href="{p['url']}" target="_blank">{html.escape(p['title'])}</a>
          </div>
          <div class="wsb-meta">
            {flair_html}
            <span>▲ {p['ups']:,}</span> &nbsp;
            <span>💬 {p['comments']:,}</span>
          </div>
        </div>"""
    return out

def _key_levels_html(kl_nq, kl_es):
    def _row(label, nq_val, es_val, cls):
        nq_s = f'<span class="levels-chip chip-{cls}">{fmt_price(nq_val, 0)}</span>' if nq_val else "—"
        es_s = f'<span class="levels-chip chip-{cls}">{fmt_price(es_val, 0)}</span>' if es_val else "—"
        return f"<tr><td>{label}</td><td>{nq_s}</td><td>{es_s}</td></tr>"

    return f"""
    <table>
      <thead><tr><th>Level</th><th>NQ</th><th>ES</th></tr></thead>
      <tbody>
        {_row("Resistance 2", kl_nq.get("r2"),       kl_es.get("r2"),       "r")}
        {_row("Resistance 1", kl_nq.get("r1"),       kl_es.get("r1"),       "r")}
        {_row("Pivot",        kl_nq.get("pivot"),    kl_es.get("pivot"),    "p")}
        {_row("Support 1",    kl_nq.get("support1"), kl_es.get("support1"), "s")}
        {_row("Support 2",    kl_nq.get("support2"), kl_es.get("support2"), "s")}
      </tbody>
    </table>"""

def _menthorq_section(mq):
    status = mq.get("status", "no_credentials")
    if status == "no_credentials":
        return """<div class="menthorq-placeholder">
          Set <code>MENTHORQ_PASSWORD</code> env var to enable live gamma / CTA charts.
        </div>"""
    if status in ("login_failed", "login_error", "nonce_not_found", "nonce_error"):
        err = mq.get("error") or mq.get("url") or status
        return f'<div class="menthorq-placeholder">MenthorQ auth failed: {html.escape(str(err))}</div>'

    charts = mq.get("charts", {})
    if not charts:
        return '<div class="menthorq-placeholder">No chart data returned.</div>'

    # Group: CTA vs Vol/Gamma
    cta_slugs = ["cta_table", "cta_spx", "cta_nasdaq"]
    vol_slugs = ["netgex", "netgex_0dte", "key_levels", "vol_barometer", "skew", "vol_control"]

    def _chart_html(slug):
        c = charts.get(slug)
        if not c:
            return ""
        label = c.get("label", slug)
        if c.get("status") != "ok" or not c.get("img_b64"):
            err = c.get("msg") or c.get("error") or "no data"
            return f'<div style="margin:6px 0"><span style="color:#8b949e;font-size:11px">{html.escape(label)}: {html.escape(str(err))}</span></div>'
        img_tag = f'<img src="data:image/png;base64,{c["img_b64"]}" style="width:100%;border-radius:6px;margin-top:4px" alt="{html.escape(label)}">'
        return f'<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:#8b949e;margin-bottom:3px">{html.escape(label)}</div>{img_tag}</div>'

    cta_html = "".join(_chart_html(s) for s in cta_slugs)
    vol_html = "".join(_chart_html(s) for s in vol_slugs)
    ok = mq.get("ok_count", 0)

    return f"""
    <div style="font-size:11px;color:#4ade80;margin-bottom:10px">[OK] {ok} charts loaded from MenthorQ</div>
    <div style="font-size:11px;font-weight:700;color:#60a5fa;margin:8px 0 6px">CTA Positioning</div>
    {cta_html}
    <div style="font-size:11px;font-weight:700;color:#60a5fa;margin:8px 0 6px">Gamma / Vol Models</div>
    {vol_html}
    """

def _narrative_block(key, narrative, fallback=""):
    val = narrative.get(key, fallback)
    if not val:
        return f'<span class="neutral">{fallback or "—"}</span>'
    lines = [l.strip() for l in val.split("\n") if l.strip()]
    return "".join(
        f'<span class="bullet">{html.escape(l.lstrip("•").strip())}</span>'
        if l.startswith("•") else f"<p style='margin:4px 0'>{html.escape(l)}</p>"
        for l in lines
    )

def _bias_class(text):
    t = (text or "").lower()
    if any(w in t for w in ["bearish", "sell", "short", "downside"]):
        return ""
    if any(w in t for w in ["bullish", "buy", "long", "upside"]):
        return "bullish"
    return "neutral-b"

def build_html(futures, fg, st_symbols, st_trending, wsb, mq, narrative, mkt=None):
    # ── Session countdown ─────────────────────────────────────────────────────
    session_open = NOW.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_to_open = int((session_open - NOW).total_seconds() / 60)
    if mins_to_open < 0:
        countdown_text = "Session LIVE"
    elif mins_to_open < 60:
        countdown_text = f"Open in {mins_to_open}m"
    else:
        h, m = divmod(mins_to_open, 60)
        countdown_text = f"Open in {h}h {m}m"

    # ── Fear & Greed ──────────────────────────────────────────────────────────
    fg_val   = fg.get("value")
    fg_label = fg.get("label", "N/A")
    fg_prev  = fg.get("prev_value", "—")
    fg_color = _fg_color(fg_val)
    fg_delta = ""
    if fg_val and fg.get("prev_value"):
        d = fg_val - fg["prev_value"]
        fg_delta = f'{"▲" if d >= 0 else "▼"} {abs(d)} pts vs yesterday ({fg.get("prev_label","")})'

    # ── VIX from futures list ─────────────────────────────────────────────────
    vix_f    = next((f for f in futures if f["sym"] == "^VIX"), {})
    vix_val  = vix_f.get("price")
    vix_pct  = vix_f.get("pct", 0)
    vix_cls  = "up" if vix_pct >= 0 else "down"

    # ── Gamma regime badge ────────────────────────────────────────────────────
    regime_label = "NEGATIVE GAMMA — Vol Amplifying"
    regime_class = "regime-neg"
    mq_ok = mq.get("status") == "ok" and mq.get("ok_count", 0) > 0
    if mq_ok:
        regime_label = "Live GEX — see MenthorQ charts"
        regime_class = "regime-pos"

    # ── Key levels: use narrative if available, else static defaults ─────────
    kl_nq = narrative.get("key_levels_nq") or {
        "r2": 24858, "r1": 24634, "pivot": 24400, "support1": 23971, "support2": 23800
    }
    kl_es = narrative.get("key_levels_es") or {
        "r2": None, "r1": None, "pivot": None, "support1": None, "support2": None
    }

    # ── Bias ─────────────────────────────────────────────────────────────────
    bias_text = narrative.get("session_bias", "BEARISH / Sell Rallies — Iran energy shock, negative gamma, CTA deleveraging active. Watch 24,400 NQ as pivot; break below targets 23,971.")
    bias_cls  = _bias_class(bias_text)

    # ── Market status banner ──────────────────────────────────────────────────
    mkt = mkt or {}
    mkt_session  = mkt.get("session_open", False)
    mkt_futures  = mkt.get("futures_open", True)
    mkt_reason   = mkt.get("reason", "")
    if mkt_session:
        mkt_banner = f'<div style="background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.3);border-radius:6px;padding:6px 14px;font-size:12px;color:#4ade80">NYSE Regular Session LIVE &nbsp;|&nbsp; {html.escape(mkt_reason)}</div>'
    elif mkt_futures:
        mkt_banner = f'<div style="background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:6px;padding:6px 14px;font-size:12px;color:#fbbf24">CME Futures Open &nbsp;|&nbsp; {html.escape(mkt_reason)}</div>'
    else:
        mkt_banner = f'<div style="background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);border-radius:6px;padding:6px 14px;font-size:12px;color:#f87171">Markets Closed &nbsp;|&nbsp; {html.escape(mkt_reason)}</div>'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Briefing — {DATE_DISPLAY}</title>
<style>{CSS}</style>
</head>
<body>

<!-- ── HEADER ────────────────────────────────────────────────────────────── -->
<div class="header">
  <div class="header-left">
    <h1>⚡ DAILY TRADING BRIEFING</h1>
    <div class="subtitle">NQ · ES · US Index Futures &nbsp;|&nbsp; 8:00 AM – 12:00 PM ET</div>
  </div>
  <div class="header-right">
    <div class="session-countdown">{countdown_text}</div>
    <div class="tag-gen">{DATE_DISPLAY} &nbsp;|&nbsp; Generated {GEN_TIME}</div>
  </div>
</div>

<div class="container">

<!-- ── Market Status Banner ──────────────────────────────────────────────── -->
<div style="margin-bottom:12px">{mkt_banner}</div>

<!-- ── ROW 1: Market Snapshot + Fear/Greed + VIX ─────────────────────────── -->
<div class="grid-3" style="margin-bottom:16px">

  <div class="panel" style="grid-column: span 2">
    <div class="panel-title"><span class="dot" style="background:#60a5fa"></span>Market Snapshot</div>
    {_build_futures_table(futures)}
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#fbbf24"></span>Sentiment Gauges</div>
    <div class="gauge-wrap">
      <div>
        <div class="gauge-num" style="color:{fg_color}">{fg_val if fg_val else '—'}</div>
        <div class="gauge-label">Fear & Greed</div>
        <div class="gauge-delta" style="color:{fg_color}">{fg_label}</div>
        <div class="gauge-delta neutral">{fg_delta}</div>
      </div>
    </div>
    <div style="margin-top:14px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">VIX LEVEL</div>
      <div style="font-size:28px;font-weight:800;color:{pct_color(vix_pct)}">{fmt_price(vix_val)}</div>
      <div class="{vix_cls}" style="font-size:12px">{arrow(vix_pct)} {fmt_pct(vix_pct)}</div>
      <div style="font-size:11px;color:#8b949e;margin-top:6px">
        {'⚠️ Elevated — approaching 30 threshold' if vix_val and vix_val >= 25 else 'Below 25 — contained volatility'}
      </div>
    </div>
    <div style="margin-top:16px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Gamma Regime</div>
      <span class="regime-pill {regime_class}">{regime_label}</span>
    </div>
  </div>

</div>

<!-- ── ROW 2: Macro + Overnight ──────────────────────────────────────────── -->
<div class="grid-2" style="margin-bottom:16px">

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#c084fc"></span>Macro & Geopolitical</div>
    {_narrative_block("macro_summary", narrative,
        "• Iran conflict: WTI +5%, Brent $110+, Strait of Hormuz closure risk\n"
        "• Fed held 3.5–3.75%; stagflation risk rising with energy-driven CPI\n"
        "• Money market funds at ATH $7.86T — institutional flight to safety\n"
        "• Japan 5.26% wage growth → BOJ normalization; China 15th Five-Year Plan adopted"
    )}
    <div style="margin-top:12px">
      <div class="panel-title" style="margin-bottom:8px">Risk Events Today</div>
      {_narrative_block("risk_events", narrative,
          "• Iran headline risk — any ceasefire/escalation = violent move\n"
          "• Oil price action: $110 hold vs break key for equity open\n"
          "• Bond yields: elevated — watch 10Y for equity pressure signal\n"
          "• VIX 30 close: would signal potential capitulation flush"
      )}
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#4ade80"></span>Overnight Price Action — NQ / ES</div>
    {_narrative_block("overnight_analysis", narrative,
        "Friday: NQM26 -2.05%, ESM26 -1.80% — S&P 500 7-month low, Nasdaq 6.75-month low. "
        "Selloff driven by WTI crude surging 5%+ on Iran/geopolitical escalation; global bond yields soared. "
        "Asian session likely to continue risk-off tone; London open will set directional bias for NY session."
    )}
    <div class="section-divider"></div>
    <div class="panel-title" style="margin-bottom:8px">CTA / Systematic Flow</div>
    {_narrative_block("cta_flow", narrative,
        "Multiple CTA trigger levels breached mid-March (Goldman, BofA confirmed). "
        "BofA estimates ~$62B additional selling if markets flat; potential $60B net short if markets fall. "
        "CTAs de-risked from 88th → 75th percentile equity exposure — room for more unwind."
    )}
  </div>

</div>

<!-- ── ROW 3: Gamma + Key Levels + Session Bias ───────────────────────────── -->
<div class="grid-3" style="margin-bottom:16px">

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#f87171"></span>Gamma / Options Regime (SPX)</div>
    {_menthorq_section(mq)}
    {_narrative_block("gamma_regime", narrative,
        "SPX is operating in negative gamma — dealers are net short gamma and must sell ES futures on declines, amplifying down-moves. "
        "SPX Volatility Trigger ~6,900 is overhead resistance; reclaim needed for regime shift. VIX testing but not closing above 30."
    )}
    <div style="margin-top:12px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:6px">MenthorQ Live Data</div>
      <div style="font-size:11px;color:#8b949e">CTA Dashboard</div>
      <div class="menthorq-placeholder" style="text-align:left;padding:4px 0">
        {'[OK] ' + str(mq.get("ok_count",0)) + ' charts loaded' if mq.get('status') == 'ok' else '[LOCKED] Set MENTHORQ_PASSWORD to enable'}
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#fbbf24"></span>Key Levels — NQ / ES</div>
    {_key_levels_html(kl_nq, kl_es)}
    <div style="margin-top:10px;font-size:11px;color:#8b949e">
      <span class="levels-chip chip-r">R</span> Resistance &nbsp;
      <span class="levels-chip chip-p">P</span> Pivot &nbsp;
      <span class="levels-chip chip-s">S</span> Support
    </div>
    <div class="section-divider"></div>
    <div style="font-size:11px;color:#8b949e;margin-bottom:6px">Sentiment Read</div>
    {_narrative_block("sentiment_read", narrative,
        "Retail StockTwits sentiment predominantly bearish on ES/SPY, contrarian signals absent. "
        "WSB chatter dominated by put positioning and oil plays — aligned with institutional flow. "
        "No meaningful dip-buying conviction visible in retail community."
    )}
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#4ade80"></span>Session Bias</div>
    <div class="bias-box {bias_cls}">
      {html.escape(bias_text)}
    </div>
    <div style="margin-top:14px;font-size:11px;color:#8b949e">Tactical Framework</div>
    <div style="margin-top:6px;font-size:12px;line-height:1.8">
      <span class="bullet">Sell VWAP reclaim failures on open (8–9:30AM)</span>
      <span class="bullet">Iran ceasefire headline = immediate short cover</span>
      <span class="bullet">VIX close &gt; 30 = flush/capitulation signal</span>
      <span class="bullet">NQ 50-day MA reclaim = structural regime shift</span>
      <span class="bullet">Do not fade moves without gamma support</span>
    </div>
  </div>

</div>

<!-- ── ROW 4: Retail Sentiment ────────────────────────────────────────────── -->
<div class="grid-2" style="margin-bottom:16px">

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#60a5fa"></span>StockTwits — Bull/Bear Sentiment</div>
    {_build_sentiment_panel(st_symbols)}
    <div class="section-divider"></div>
    <div class="panel-title" style="margin-bottom:8px">Trending on StockTwits Now</div>
    <div>{_build_trending_chips(st_trending)}</div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#c084fc"></span>WallStreetBets — Hot Posts</div>
    {_build_wsb_rows(wsb)}
  </div>

</div>

<!-- ── ROW 5: Top StockTwits Messages ─────────────────────────────────────── -->
<div class="panel" style="margin-bottom:16px">
  <div class="panel-title"><span class="dot" style="background:#60a5fa"></span>StockTwits — Top Messages (Most Liked)</div>
  <div class="grid-3">
    <div>{_build_st_messages({k: v for k, v in list(st_symbols.items())[:1]})}</div>
    <div>{_build_st_messages({k: v for k, v in list(st_symbols.items())[1:3]})}</div>
    <div>{_build_st_messages({k: v for k, v in list(st_symbols.items())[3:]})}</div>
  </div>
</div>

<!-- ── FOOTER ─────────────────────────────────────────────────────────────── -->
<div style="text-align:center;color:#30363d;font-size:11px;padding:20px 0 32px">
  Generated {GEN_TIME} &nbsp;|&nbsp;
  Sources: Yahoo Finance · Alternative.me · StockTwits · Reddit WSB · MenthorQ · Anthropic Claude &nbsp;|&nbsp;
  Not financial advice.
</div>

</div><!-- /container -->
</body>
</html>"""
    return page


# ── Notification ──────────────────────────────────────────────────────────────
def notify_windows(title, message):
    """Windows 10/11 toast notification via PowerShell."""
    ps = textwrap.dedent(f"""
        $ErrorActionPreference = 'Stop'
        Add-Type -AssemblyName System.Windows.Forms
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.BalloonTipIcon  = 'Info'
        $n.BalloonTipTitle = '{title}'
        $n.BalloonTipText  = '{message}'
        $n.Visible = $true
        $n.ShowBalloonTip(8000)
        Start-Sleep -Seconds 2
        $n.Dispose()
    """).strip()
    try:
        subprocess.Popen(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        pass  # notification is non-critical


# ── ntfy.sh Push Notification (cloud / headless) ──────────────────────────────
def notify_ntfy(title, message, url=""):
    """Fire a push notification via ntfy.sh (free, no account needed).
    Install the ntfy app and subscribe to your NTFY_TOPIC to receive alerts."""
    if not NTFY_TOPIC:
        return
    try:
        headers = {
            "Title":    title,
            "Priority": "high",
            "Tags":     "chart_increasing",
        }
        if url:
            headers["Click"]   = url
            headers["Actions"] = f"view, Open Briefing, {url}, clear=true"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=8,
        )
    except Exception:
        pass


# ── GitHub Pages index redirect ────────────────────────────────────────────────
def create_index_page(briefing_filename):
    """Write docs/index.html that auto-redirects to the latest briefing."""
    idx = BASE_DIR / "index.html"
    idx.write_text(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="0; url={briefing_filename}">
<title>Daily Trading Briefing</title>
<style>body{{background:#0d1117;color:#e6edf3;font-family:system-ui;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}</style>
</head>
<body>
<p>Redirecting to <a href="{briefing_filename}" style="color:#60a5fa">today's briefing</a>…</p>
</body>
</html>""", encoding="utf-8")


# ── Scheduled Task Registration ───────────────────────────────────────────────
def register_scheduled_task():
    """Register a Windows Task Scheduler entry to run at 7:55 AM daily."""
    python_exe = sys.executable
    script     = str(Path(__file__).resolve())
    task_xml   = textwrap.dedent(f"""<?xml version="1.0" encoding="UTF-16"?>
    <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
      <Triggers>
        <CalendarTrigger>
          <StartBoundary>2026-01-01T07:55:00</StartBoundary>
          <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
        </CalendarTrigger>
      </Triggers>
      <Actions Context="Author">
        <Exec>
          <Command>{python_exe}</Command>
          <Arguments>"{script}"</Arguments>
        </Exec>
      </Actions>
      <Settings>
        <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
        <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
        <Enabled>true</Enabled>
      </Settings>
    </Task>""").strip()

    xml_path = BASE_DIR / "briefing_task.xml"
    xml_path.write_text(task_xml, encoding="utf-16")
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", "DailyTradingBriefing",
         "/XML", str(xml_path), "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("[OK] Scheduled task registered: DailyTradingBriefing @ 7:55 AM daily")
    else:
        print(f"[ERR] Task registration failed: {result.stderr}")
    xml_path.unlink(missing_ok=True)


# ── Market-closed page ────────────────────────────────────────────────────────
def build_closed_html(status: dict) -> str:
    """Minimal page shown when the market is closed / it's a holiday or weekend."""
    today    = NOW.date()
    year     = today.year
    holidays = nyse_holidays(year)

    # Build the holiday list for the rest of the year
    holiday_rows = ""
    known = {
        "New Year's Day":    _observed(date(year, 1, 1)),
        "MLK Day":           _nth_weekday(year, 1, 0, 3),
        "Presidents' Day":   _nth_weekday(year, 2, 0, 3),
        "Memorial Day":      _last_weekday(year, 5, 0),
        "Juneteenth":        _observed(date(year, 6, 19)),
        "Independence Day":  _observed(date(year, 7, 4)),
        "Labor Day":         _nth_weekday(year, 9, 0, 1),
        "Thanksgiving":      _nth_weekday(year, 11, 3, 4),
        "Christmas Day":     _observed(date(year, 12, 25)),
    }
    # Good Friday
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4)
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month2, day2 = divmod(h + l - 7 * m + 114, 31)
    easter = date(year, month2, day2 + 1)
    known["Good Friday"] = easter - timedelta(days=2)

    for name, d in sorted(known.items(), key=lambda x: x[1]):
        if d >= today:
            passed  = " (today)" if d == today else ""
            row_cls = "color:#f87171;font-weight:700" if d == today else "color:#e6edf3"
            holiday_rows += (
                f'<tr><td style="{row_cls}">{name}{passed}</td>'
                f'<td style="color:#8b949e;font-family:monospace">{d.strftime("%a %b %d, %Y")}</td></tr>'
            )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Closed — {DATE_DISPLAY}</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:'Segoe UI',system-ui,sans-serif;
          display:flex; flex-direction:column; align-items:center;
          justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
           padding:40px 48px; max-width:640px; width:90%; text-align:center; }}
  h1 {{ font-size:24px; color:#60a5fa; margin:0 0 8px; }}
  .status {{ font-size:15px; color:#fbbf24; margin:12px 0 24px; }}
  .next  {{ font-size:13px; color:#8b949e; margin-bottom:28px; }}
  table  {{ width:100%; border-collapse:collapse; text-align:left; margin-top:16px; }}
  th     {{ font-size:10px; letter-spacing:.8px; text-transform:uppercase;
            color:#8b949e; padding:4px 8px; border-bottom:1px solid #30363d; }}
  td     {{ padding:5px 8px; border-bottom:1px solid #21262d; font-size:13px; }}
  tr:last-child td {{ border-bottom:none; }}
  .footer {{ margin-top:24px; font-size:11px; color:#30363d; }}
</style>
</head>
<body>
<div class="card">
  <h1>Market Closed</h1>
  <div class="status">{html.escape(status['reason'])}</div>
  <div class="next">Next session open: <strong style="color:#4ade80">{html.escape(status['next_open'])}</strong></div>
  <table>
    <thead><tr><th>NYSE Holiday</th><th>Date</th></tr></thead>
    <tbody>{holiday_rows}</tbody>
  </table>
  <div class="footer">Generated {GEN_TIME} &nbsp;|&nbsp; {DATE_DISPLAY}</div>
</div>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if "--schedule" in sys.argv:
        register_scheduled_task()
        return

    # ── Market status gate ────────────────────────────────────────────────────
    mkt = get_market_status(NOW)
    force = "--force" in sys.argv  # bypass gate for testing

    print(f"[briefing] {DATE_DISPLAY}  |  {mkt['reason']}")

    if not mkt["is_trading_day"] and not force:
        print(f"  [--] Market closed — generating closed notice ({mkt['reason']})")
        closed_html = build_closed_html(mkt)
        OUTPUT_FILE.write_text(closed_html, encoding="utf-8")
        LATEST_FILE.write_text(closed_html, encoding="utf-8")
        if IS_CI:
            create_index_page(LATEST_FILE.name)
        else:
            webbrowser.open(LATEST_FILE.as_uri())
            notify_windows("Market Closed", f"{mkt['reason']} | Next: {mkt['next_open']}")
        print(f"  [OK] Closed notice saved")
        return

    if not mkt["is_trading_day"]:
        print("  [!] Market closed but --force passed — generating full briefing anyway")

    print(f"  [+] Trading day confirmed — fetching data...")

    # ── Parallel data fetch ───────────────────────────────────────────────────
    results = {}

    def _run(key, fn, *args):
        results[key] = fn(*args)

    threads = [
        threading.Thread(target=_run, args=("futures",          fetch_futures)),
        threading.Thread(target=_run, args=("fg",               fetch_fear_greed)),
        threading.Thread(target=_run, args=("st_spy",           fetch_stocktwits_symbol, "SPY")),
        threading.Thread(target=_run, args=("st_qqq",           fetch_stocktwits_symbol, "QQQ")),
        threading.Thread(target=_run, args=("st_spx",           fetch_stocktwits_symbol, "SPX")),
        threading.Thread(target=_run, args=("st_nq",            fetch_stocktwits_symbol, "NQ")),
        threading.Thread(target=_run, args=("st_es",            fetch_stocktwits_symbol, "ES")),
        threading.Thread(target=_run, args=("st_trending",      fetch_stocktwits_trending)),
        threading.Thread(target=_run, args=("wsb",              fetch_reddit_wsb)),
        threading.Thread(target=_run, args=("menthorq",         fetch_menthorq)),
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    print("  [+] Data fetched")

    st_symbols = {
        k: results.get(k) for k in ("st_spy", "st_qqq", "st_spx", "st_nq", "st_es")
        if results.get(k)
    }
    # Rename keys for display
    st_symbols = {
        k.replace("st_", "").upper(): v for k, v in st_symbols.items()
    }

    # ── AI narrative (non-blocking — if API key present) ─────────────────────
    narrative = {}
    if ANTHROPIC_API_KEY:
        print("  [...] Generating AI narrative...")
        narrative = generate_ai_narrative({
            "futures":   results.get("futures", []),
            "fear_greed":results.get("fg", {}),
            "sentiment": {k: {"bull_pct": v.get("bull_pct"), "bear_pct": v.get("bear_pct")}
                          for k, v in st_symbols.items() if v},
        })
        print("  [+] Narrative ready")
    else:
        print("  [i] No ANTHROPIC_API_KEY — using static narrative defaults")

    # ── Build HTML ────────────────────────────────────────────────────────────
    page = build_html(
        futures     = results.get("futures", []),
        fg          = results.get("fg", {}),
        st_symbols  = st_symbols,
        st_trending = results.get("st_trending", []),
        wsb         = results.get("wsb", []),
        mq          = results.get("menthorq", {"status": "no_credentials"}),
        narrative   = narrative,
        mkt         = mkt,
    )

    OUTPUT_FILE.write_text(page, encoding="utf-8")
    LATEST_FILE.write_text(page, encoding="utf-8")
    print(f"  [+] Saved -> {OUTPUT_FILE}")

    if IS_CI:
        # ── Cloud mode: update index redirect + push notification ─────────────
        briefing_filename = OUTPUT_FILE.name
        create_index_page(briefing_filename)
        briefing_url = f"{PAGES_URL}/{briefing_filename}" if PAGES_URL else ""
        notify_ntfy(
            "Trading Briefing Ready",
            f"{DATE_DISPLAY} | NQ/ES/SPX session prep complete",
            url=briefing_url,
        )
        print(f"  [+] ntfy notification sent")
        if briefing_url:
            print(f"  [+] Briefing URL: {briefing_url}")
    else:
        # ── Desktop mode: open browser + Windows toast ────────────────────────
        webbrowser.open(LATEST_FILE.as_uri())
        print("  [+] Opened in browser")
        notify_windows(
            "Trading Briefing Ready",
            f"{DATE_DISPLAY} | NQ / ES / SPX session prep complete"
        )
        print("  [+] Notification sent")

    print(f"\n[OK] Briefing complete -- {GEN_TIME}")


if __name__ == "__main__":
    main()
