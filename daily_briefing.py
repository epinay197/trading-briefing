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
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs, "-q", "--quiet"], creationflags=0x08000000)

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
# Narrative model. Overridable so a bad default can be swapped without a code edit.
NARRATIVE_MODEL = os.environ.get("BRIEFING_MODEL", "claude-opus-5")


def _market_phase(now_et=None):
    """Where in the session we are — the briefing must read differently at
    07:55 (nothing has traded) than at 11:30 (the range is set)."""
    from zoneinfo import ZoneInfo
    n = now_et or datetime.now(ZoneInfo("America/New_York"))
    hm = n.hour * 60 + n.minute
    if n.weekday() >= 5:
        return ("closed", "Market closed. Frame the NEXT session; nothing here is actionable today.")
    if hm < 570:
        return ("pre-open", "PRE-OPEN. Nothing has traded in RTH. Overnight acceptance is PROVISIONAL — "
                            "Globex volume is not a grade. The 09:30-09:45 candle is the judge. "
                            "Frame scenarios and triggers; do not declare a trend.")
    if hm < 600:
        return ("opening", "OPENING DRIVE (09:30-10:00). The tiebreaker candle is printing. "
                           "Call what the first range means, flag gap-and-trap risk, stay provisional.")
    if hm < 690:
        return ("morning", "PRIME WINDOW (10:00-11:30). The range is set and this is the only high-quality "
                           "window. Be directional and specific about entry location.")
    if hm < 810:
        return ("midday", "MIDDAY (11:30-13:30). Low-information chop. Say plainly that initiating here is "
                          "negative-edge; frame the afternoon instead.")
    if hm < 900:
        return ("afternoon", "AFTERNOON (13:30-15:00). Second-leg window. Only valid if the morning made a "
                             "clean high or low; if it chopped, say so.")
    if hm < 960:
        return ("power-hour", "POWER HOUR (15:00-16:00). Pin gravity and closing imbalance dominate. "
                              "Focus on the nearest wall as a magnet and on flattening.")
    return ("post-close", "POST-CLOSE. Grade what happened against the levels and set up the next session.")

# Session: "london" (3:30 AM ET), "us" (7:55 AM ET), or "nyopen" (9:15 AM ET)
_sidx    = sys.argv.index("--session") + 1 if "--session" in sys.argv else -1
SESSION  = sys.argv[_sidx] if 0 < _sidx < len(sys.argv) else "us"

_sfx_map    = {"london": "_london", "us": "", "nyopen": "_nyopen"}
_sfx        = _sfx_map.get(SESSION, "")
OUTPUT_FILE = BASE_DIR / f"briefing_{DATE_STR}{_sfx}.html"
LATEST_FILE = BASE_DIR / f"briefing_latest{_sfx}.html"

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
            # WordPress admin-ajax returns a bare `-1` body (HTTP 403) when the
            # nonce is rejected or the account lacks permission for the action —
            # so `j` is an int, not a dict, and the old `j.get("data", {})` raised
            # AttributeError. The outer except turned that into the useless
            # "'int' object has no attribute 'get'" seen against every slug.
            j = resp.json()
            if not isinstance(j, dict):
                charts[slug] = {
                    "label": label, "status": "api_error",
                    "msg": (f"HTTP {resp.status_code}, body {j!r} — WordPress rejected "
                            f"the admin-ajax call (nonce invalid or account not "
                            f"entitled to '{slug}')")}
                continue
            data = j.get("data")
            if not isinstance(data, dict):
                charts[slug] = {"label": label, "status": "api_error",
                                "msg": f"non-dict data payload ({type(data).__name__}) "
                                       f"— usually an auth/entitlement failure"}
                continue
            if not j.get("success"):
                charts[slug] = {"label": label, "status": "api_error",
                                "msg": data.get("message", "")}
                continue

            # `resource` is also int-typed on some entitlement failures — same
            # AttributeError one level deeper, so guard it the same way.
            resource = data.get("resource")
            if not isinstance(resource, dict):
                charts[slug] = {"label": label, "status": "api_error",
                                "msg": f"non-dict resource ({type(resource).__name__}) "
                                       f"— slug likely not entitled on this account"}
                continue
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


NOKEPA_DIR = Path(r"C:\Users\Anwender\Code\nokepa")


def fetch_local_gamma() -> dict:
    """Gamma / GEX / DEX from the user's OWN engines — not from a vendor scrape.

    2026-07-31: this page used to source gamma by screen-scraping
    menthorq.com/wp-admin, which is dead legacy infrastructure (MenthorQ moved to
    dashboard.menthorq.io) and returned HTTP 403 '-1' on every slug. Meanwhile
    NOKEPA already computes net GEX, gamma flip, walls and net DEX locally, and
    ICT_mq_levels_fetch.py already pulls real MenthorQ levels via the Chrome
    DevTools session into data/mq_levels.json. Both are authoritative here.

    Order of preference per asset:
      1. the running NOKEPA server on :8780 (live intraday)
      2. data/gex_cache.json (persisted snapshot, when the server is session-gated off)
    MenthorQ levels always come from data/mq_levels.json.
    """
    out = {"assets": {}, "mq": {}, "source": None, "mq_fetched_at": None}

    def _api(path, timeout=45):
        r = requests.get(f"http://127.0.0.1:8780{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    live = False
    try:
        requests.get("http://127.0.0.1:8780/api/health", timeout=20).raise_for_status()
        live = True
    except Exception as e:
        out["health_err"] = f"{type(e).__name__}: {str(e)[:120]}"
    out["source"] = "nokepa-live" if live else "nokepa-cache"

    for a in ("SPX", "NDX", "SPY", "QQQ"):
        rec = {}
        if live:
            try:
                d = _api(f"/api/dealer/{a}", 120) or {}
                rec.update({k: d.get(k) for k in
                            ("net_gex", "net_dex", "gex_by_dte", "pc_ratio",
                             "skew_0dte_pct", "spot")})
            except Exception as e:
                rec["dealer_err"] = type(e).__name__
                live_ok = False
            try:
                g = _api(f"/api/gex_live/{a}", 20) or {}
                rec.update({"flip": g.get("gamma_flip") or g.get("gamma_flip_est"),
                            "call_wall": g.get("pgex_wall"), "put_wall": g.get("ngex_wall"),
                            "market_char": g.get("market_char")})
            except Exception:
                pass
        # Levels: always backfill from the persisted snapshot for anything the
        # live call didn't supply, so a slow /api/gex_live can't blank the row.
        if True:
            try:
                cache = json.loads((NOKEPA_DIR / "data" / "gex_cache.json").read_text(encoding="utf-8"))
                e = cache.get(a)
                if isinstance(e, list) and len(e) == 2:
                    e = e[1]
                if isinstance(e, dict):
                    for k, v in (("flip", e.get("gamma_flip") or e.get("gamma_flip_est")),
                                 ("call_wall", e.get("pgex_wall")),
                                 ("put_wall", e.get("ngex_wall")),
                                 ("market_char", e.get("market_char"))):
                        if rec.get(k) is None and v is not None:
                            rec[k] = v
            except Exception as ex:
                rec["cache_err"] = type(ex).__name__
        out["assets"][a] = rec

    try:
        mq = json.loads((NOKEPA_DIR / "data" / "mq_levels.json").read_text(encoding="utf-8"))
        out["mq"] = mq.get("levels", {})
        out["mq_fetched_at"] = datetime.fromtimestamp(mq["fetched_at"]).strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        out["mq_err"] = f"{type(e).__name__}: {e}"

    return out


def generate_ai_narrative(payload: dict) -> dict:
    """Claude generates concise, actionable analysis from raw data.

    2026-07-31: this function had NEVER succeeded. The prompt was built as an
    f-string containing an inline dict literal, so Python parsed the ``:`` after
    ``"london"`` as a format specifier and every call raised
    ``ValueError: Invalid format specifier``. The bare ``except`` swallowed it
    into {"_error": ...}, the caller printed "Narrative ready" regardless, and
    the page silently fell back to hardcoded static prose. The session-specific
    strings are now built OUTSIDE the f-string.
    """
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return {}

    intro = {
        "london": f"Prepare a concise London-open briefing for a US index day trader (instruments: NQ, ES, /MNQ, /MES; London session: 3AM-8AM ET, NY session follows 9:30AM ET; date: {DATE_DISPLAY}). Focus on overnight futures movement, London open momentum, and key levels for the 3:30-8:00 AM ET window.",
        "us": f"Prepare a concise pre-market briefing for a US index day trader (instruments: NQ, ES; pre-market: 7:55 AM ET; regular session: 9:30AM-4:00PM ET; date: {DATE_DISPLAY}).",
        "nyopen": f"Prepare a concise post-bell briefing for a US index day trader (instruments: NQ, ES; NY bell just rung 9:15 AM ET; session: 9:30AM-4:00PM ET; date: {DATE_DISPLAY}). Focus on opening momentum, market structure setup, and confirmed bias for the main session.",
    }.get(SESSION, "")

    overnight_spec = {
        "london": "3 sentences on overnight NQ/ES narrative and London open momentum",
        "us": "3 sentences on overnight NQ/ES narrative",
        "nyopen": "3 sentences on opening 15-min action and NY session directional bias confirmation",
    }.get(SESSION, "3 sentences on overnight NQ/ES narrative")

    _cover = ("Cover the whole index complex, not just the futures: state the bias, "
              "then give the decisive level for ES/SPX, NQ/NDX and the ETF pair "
              "(SPY/QQQ), and say explicitly what invalidates the bias.")
    bias_spec = {
        "london": "One bold directional bias for London + early NY. " + _cover,
        "us": "One bold directional bias. " + _cover,
        "nyopen": "Confirmed directional bias post-bell for the session main move. " + _cover,
    }.get(SESSION, "One bold directional bias. " + _cover)

    raw = json.dumps(payload, indent=2, default=str)[:6000]
    from zoneinfo import ZoneInfo as _Z
    _n = datetime.now(_Z("America/New_York"))
    _now_et = _n.strftime("%H:%M")
    _phase, _phase_note = _market_phase(_n)

    prompt = textwrap.dedent(f"""
        You are a senior institutional trading analyst.
        {intro}

        RAW DATA:
        {raw}

        Ground every level you quote in the RAW DATA above. Never carry over levels
        from memory or from a previous session.

        LEVEL DERIVATION - this is the important part. The RAW DATA carries
        `dealer_gamma` (per-asset net GEX, gamma flip, call wall, put wall from
        our own options engine) and `menthorq_levels` (call_resistance,
        put_support, hvl, gamma_wall_0dte, and min_1d/max_1d - the implied
        1-day range). Build key levels from THOSE, not from the session high/low:
          - pivot      : the HVL / gamma flip, i.e. where dealer hedging is neutral
          - resistance : call wall / call_resistance_0dte / max_1d, in that order
          - support    : put wall / put_support_0dte / min_1d, in that order
        Only fall back to session structure for a symbol with no positioning data.
        A level that merely restates the day's high or low is not a level - if
        that is all you have for an asset, say so in session_bias.

        TIMING — you are shipping at {_now_et} ET. {_phase_note}
        Write for that moment. Do not describe the open as upcoming if it has
        happened, and do not call a trend before the 09:30-09:45 candle closes.

        SCENARIOS — give exactly 3, probabilities as integers summing to 100,
        ordered most-likely first. Each needs a concrete numeric trigger, the
        path it takes if it plays, and what invalidates it. Name them for the
        behaviour, not "bullish/bearish/neutral".

        TWO-ENGINE RULE — a break of a major wall needs confirmation from
        something outside price. State the cross-asset requirement explicitly
        (a VIX threshold, a 10Y threshold), e.g. "first touch is fade-favoured
        UNLESS 10Y slips under X while VIX holds under Y". If only one engine
        fires, that is rotation, not permission.

        LOCATION DISCIPLINE — say where the entries ARE and, just as important,
        where they are NOT. Mid-range is not a location. Chasing after an
        extended move has bad location math even when the direction is right.

        Field guidance:
        - macro_summary      : 4 bullet points on key macro themes, each line prefixed with a bullet character
        - overnight_analysis : {overnight_spec}
        - gamma_regime       : 2 sentences on gamma regime + intraday vol implication
        - cta_flow           : 2 sentences on CTA / systematic flow
        - sentiment_read     : 2 sentences interpreting retail sentiment vs institutional bias
        - session_bias       : object. Write it as INSTRUCTIONS a trader executes,
          not as commentary. Imperative voice. No hedging, no restating context.
            headline       : max 14 words. The stance and the one thing that decides it.
                             e.g. "Long above 7505, short below it - the walls cap both sides."
            decisive_level : ONLY the numbers, no sentence. Lead with the futures the
                             user actually trades. e.g. "ES 7505  |  NQ 28285  |  SPX 7480 cash"
            above          : the LONG plan in max 30 words, in this shape ->
                             ENTRY (where you buy) / TARGET (first, then stretch) / STOP.
                             e.g. "Buy pullbacks into 7505-7500. First target 7525,
                             stretch 7571. Stop below 7495. Sell the first tag, do not chase."
            below          : the SHORT plan, same 30-word shape, same ENTRY/TARGET/STOP.
            invalidation   : max 25 words. The kill switch, price AND cross-asset.
                             e.g. "Two 15-min closes under ES 7425, or 10Y above 4.75% - flat, no longs."
          Never pack all six instruments into one sentence. Quote at most the two
          futures plus one cash reference per field; the levels table holds the rest.
        - scenarios          : the 3 weighted scenarios described above
        - location_discipline: 2 sentences on where to enter and where not to
        - one_liner          : ONE panic-proof sentence a trader can hold in their
          head all session. The decisive level, both directions, the kill switch.
        - risk_events        : specific catalysts to watch today, as bullet lines
        - tactical_framework : 4-5 short actionable rules for today, as bullet lines
        - key_levels_nq / key_levels_es / key_levels_spx / key_levels_ndx /
          key_levels_spy / key_levels_qqq : numeric r1, r2, pivot, support1, support2
          for each. Futures (NQ/ES) and cash (SPX/NDX/SPY/QQQ) must be internally
          consistent - convert using the basis implied by the RAW DATA, do not
          quote a cash level that contradicts its future.
    """).strip()

    LEVELS = {
        "type": "object",
        "properties": {k: {"type": "number"} for k in
                       ("r1", "r2", "pivot", "support1", "support2")},
        "required": ["r1", "r2", "pivot", "support1", "support2"],
        "additionalProperties": False,
    }
    TEXT_FIELDS = ["macro_summary", "overnight_analysis", "gamma_regime", "cta_flow",
                   "sentiment_read", "risk_events", "tactical_framework",
                   "location_discipline", "one_liner"]
    BIAS_OBJ = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "decisive_level": {"type": "string"},
            "above": {"type": "string"},
            "below": {"type": "string"},
            "invalidation": {"type": "string"},
        },
        "required": ["headline", "decisive_level", "above", "below", "invalidation"],
        "additionalProperties": False,
    }
    SCENARIO = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "probability": {"type": "integer"},
            "trigger": {"type": "string"},
            "path": {"type": "string"},
            "invalidation": {"type": "string"},
        },
        "required": ["name", "probability", "trigger", "path", "invalidation"],
        "additionalProperties": False,
    }
    LEVEL_SYMS = ["nq", "es", "spx", "ndx", "spy", "qqq"]
    LEVEL_KEYS = [f"key_levels_{x}" for x in LEVEL_SYMS]
    SCHEMA = {
        "type": "object",
        "properties": {**{f: {"type": "string"} for f in TEXT_FIELDS},
                       **{k: LEVELS for k in LEVEL_KEYS},
                       "session_bias": BIAS_OBJ,
                       "scenarios": {"type": "array", "items": SCENARIO}},
        "required": TEXT_FIELDS + LEVEL_KEYS + ["session_bias", "scenarios"],
        "additionalProperties": False,
    }

    try:
        client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
        # Stream. Once the payload carried dealer_gamma + menthorq_levels and the
        # schema grew to six level sets, the non-streaming call started returning
        # APITimeoutError. Streaming holds the connection open and
        # get_final_message() still gives us the assembled response.
        with client.messages.stream(
            model=NARRATIVE_MODEL,
            max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA},
                           "effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            return {"_error": "model refused the request"}
        text = next((b.text for b in msg.content if b.type == "text"), "")
        if not text:
            return {"_error": f"no text block (stop_reason={msg.stop_reason})"}
        return json.loads(text)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}



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
.container { max-width: 1680px; margin: 0 auto; padding: 20px 24px; }
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
.bias-head { font-size:19px; font-weight:700; line-height:1.4; margin-bottom:14px; color:var(--text); }
.bias-decisive { font-size:12px; color:var(--muted); letter-spacing:.5px; text-transform:uppercase;
  font-weight:600; padding:10px 14px; background:rgba(255,255,255,0.03); border-radius:8px;
  border:1px solid var(--border); margin-bottom:14px; }
.bias-decisive b { color:var(--yellow); font-size:15px; letter-spacing:0; text-transform:none;
  margin-left:10px; font-weight:700; }
.bias-split { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px; }
.bias-side { border-radius:8px; padding:14px 16px; font-size:14px; line-height:1.65; }
.bias-side.up { background:rgba(74,222,128,0.07); border:1px solid rgba(74,222,128,0.28); }
.bias-side.dn { background:rgba(248,113,113,0.07); border:1px solid rgba(248,113,113,0.28); }
.bias-side-h { font-size:12px; font-weight:800; letter-spacing:.7px; text-transform:uppercase;
  margin-bottom:8px; }
.bias-side.up .bias-side-h { color:var(--green); }
.bias-side.dn .bias-side-h { color:var(--red); }
.bias-killbar { background:rgba(248,113,113,0.10); border:1px solid rgba(248,113,113,0.35);
  border-left:3px solid var(--red); border-radius:8px; padding:12px 16px; font-size:13.5px;
  line-height:1.6; color:var(--text); }
.bias-killbar b { color:var(--red); font-size:11px; letter-spacing:.7px; text-transform:uppercase;
  display:block; margin-bottom:4px; }
@media (max-width: 900px) { .bias-split { grid-template-columns:1fr; } }
.oneliner { font-size:15px; background:rgba(96,165,250,0.08); border:1px solid rgba(96,165,250,0.35);
  border-left:3px solid var(--blue); border-radius:8px; padding:12px 16px; margin-bottom:16px;
  font-size:13.5px; line-height:1.6; color:var(--text); }
.oneliner b { color:var(--blue); font-size:10.5px; letter-spacing:.6px; text-transform:uppercase;
  display:block; margin-bottom:4px; }
.scn { border:1px solid var(--border); border-radius:8px; padding:12px 14px; background:rgba(255,255,255,0.02); }
.scn-h { display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom:8px; }
.scn-n { font-size:12.5px; font-weight:700; color:var(--text); }
.scn-p { font-size:15px; font-weight:800; color:var(--blue); white-space:nowrap; }
.scn-bar { height:3px; background:var(--border); border-radius:2px; margin-bottom:10px; overflow:hidden; }
.scn-bar i { display:block; height:100%; background:var(--blue); }
.scn dl { margin:0; font-size:11.5px; line-height:1.55; }
.scn dt { color:var(--muted); font-size:10px; letter-spacing:.5px; text-transform:uppercase; margin-top:6px; }
.scn dd { margin:1px 0 0; color:var(--text); }
.regime-neutral { background: rgba(251,191,36,0.15); color: var(--yellow); border: 1px solid rgba(251,191,36,0.3); }
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

def _key_levels_html(levels: dict):
    """Rows = instrument, columns = level. Transposed from the old NQ/ES-only
    layout so the full complex (futures + cash + ETFs) fits without 7 columns."""
    order = [("NQ", "NQ"), ("ES", "ES"), ("SPX", "SPX"), ("NDX", "NDX"),
             ("SPY", "SPY"), ("QQQ", "QQQ")]
    cols = [("support2", "s"), ("support1", "s"), ("pivot", "p"),
            ("r1", "r"), ("r2", "r")]

    def chip(v, cls, dp):
        return (f'<span class="levels-chip chip-{cls}">{fmt_price(v, dp)}</span>'
                if v not in (None, "") else "&mdash;")

    rows = ""
    for key, label in order:
        kl = levels.get(key) or {}
        if not any(kl.get(c) for c, _ in cols):
            continue
        dp = 2 if key in ("SPY", "QQQ") else 0
        rows += f"<tr><td>{label}</td>" + "".join(
            f"<td>{chip(kl.get(c), cls, dp)}</td>" for c, cls in cols) + "</tr>"
    if not rows:
        return '<div class="menthorq-placeholder">No key levels available.</div>'

    return f"""
    <table>
      <thead><tr><th>Instrument</th><th>Support 2</th><th>Support 1</th>
      <th>Pivot</th><th>Resistance 1</th><th>Resistance 2</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _bias_card(bias, cls):
    """Full-width trade plan: headline, the decisive number, the two sides side
    by side, then the kill switch. Replaces a stacked label/value table that
    wrapped into an unreadable column."""
    if not isinstance(bias, dict) or not bias.get("headline"):
        return ('<div class="bias-box neutral-b">BIAS UNAVAILABLE — no live narrative '
                'generated this run. Do not trade from this page today.</div>')
    e = lambda k: html.escape(str(bias.get(k) or ""))
    dec = (f'<div class="bias-decisive">Decisive level<b>{e("decisive_level")}</b></div>'
           if bias.get("decisive_level") else "")
    kill = (f'<div class="bias-killbar"><b>&#9940; Invalidation &mdash; kill switch</b>'
            f'{e("invalidation")}</div>' if bias.get("invalidation") else "")
    return (f'<div class="bias-box {cls}" style="padding:18px 20px">'
            f'<div class="bias-head">{e("headline")}</div>{dec}'
            f'<div class="bias-split">'
            f'<div class="bias-side up"><div class="bias-side-h">&#9650; Above &mdash; long plan</div>{e("above")}</div>'
            f'<div class="bias-side dn"><div class="bias-side-h">&#9660; Below &mdash; short plan</div>{e("below")}</div>'
            f'</div>{kill}</div>')


def _scenarios_html(scns):
    if not isinstance(scns, list) or not scns:
        return ""
    cards = ""
    for sc in scns[:3]:
        if not isinstance(sc, dict):
            continue
        pct = sc.get("probability") or 0
        try:
            pct = max(0, min(100, int(pct)))
        except Exception:
            pct = 0
        cards += (f'<div class="scn"><div class="scn-h">'
                  f'<span class="scn-n">{html.escape(str(sc.get("name","")))}</span>'
                  f'<span class="scn-p">{pct}%</span></div>'
                  f'<div class="scn-bar"><i style="width:{pct}%"></i></div><dl>'
                  f'<dt>Trigger</dt><dd>{html.escape(str(sc.get("trigger","")))}</dd>'
                  f'<dt>Path</dt><dd>{html.escape(str(sc.get("path","")))}</dd>'
                  f'<dt>Invalidation</dt><dd>{html.escape(str(sc.get("invalidation","")))}</dd>'
                  f'</dl></div>')
    return f'<div class="grid-3" style="gap:14px">{cards}</div>'


def _playbook_html(gamma):
    """The standing session playbook, with the one live decision resolved.

    Shipped on every briefing because the rules are worth nothing if they have
    to be recalled from memory at 09:30.
    """
    g = gamma or {}
    ng = ((g.get("assets") or {}).get("SPX") or {}).get("net_gex")
    if isinstance(ng, (int, float)):
        if ng < 0:
            verdict = ("NEGATIVE GAMMA &rarr; dealers AMPLIFY. Breaks work. "
                       "Trade <b>through</b> levels, trail the stop. Do not fade.")
            vcls, vnum = "regime-neg", f"SPX net GEX {ng/1e6:+.0f}M"
        else:
            verdict = ("POSITIVE GAMMA &rarr; dealers DAMPEN. Fades work. "
                       "Trade <b>between</b> levels, take profit at the wall. Do not chase.")
            vcls, vnum = "regime-pos", f"SPX net GEX {ng/1e6:+.0f}M"
    else:
        verdict = ("Gamma sign unavailable &mdash; treat as neutral and size down.")
        vcls, vnum = "regime-neutral", "no reading"

    clock = [("9:30&ndash;9:45", "Nothing. Let the range print."),
             ("9:45&ndash;10:00", "Note which side of the pivot you are on. That is the bias."),
             ("10:00&ndash;11:30", "<b>The only real trading window.</b> Long above pivot toward the call wall; short below toward the put wall. Stop on the far side of the level you entered from."),
             ("11:30&ndash;13:30", "Flat. Lunch has no edge."),
             ("13:30&ndash;15:00", "Second leg only if the morning made a clean high or low. If it chopped, stay out."),
             ("15:00&ndash;15:45", "Pin window. Nearest wall pulls price in. The only tested edge here (PF 1.79)."),
             ("15:45&ndash;16:00", "Flat.")]
    rows = "".join(f'<tr><td style="white-space:nowrap">{t}</td><td>{w}</td></tr>' for t, w in clock)

    return f"""
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:6px">The two layers</div>
        <div style="font-size:12px;line-height:1.7">
          <span class="bullet"><b>Map (fixed all day):</b> MenthorQ EOD levels, built from
          settled open interest at yesterday's close. Open interest cannot change during the
          session &mdash; these strikes do not move. They tell you <i>where</i> price reacts.</span>
          <span class="bullet"><b>Weather (moves constantly):</b> live gamma on that same open
          interest. Gamma reprices with spot, time and IV, so intensity swings hard even though
          no strike moved. It tells you <i>how</i> price reacts when it gets there.</span>
          <span class="bullet"><b>Blind spot:</b> today's 0DTE volume creates positioning that
          does not reach open interest until tomorrow. Your only proxies are the gamma sign and
          the put/call volume ratio.</span>
        </div>
        <div style="font-size:11px;color:#8b949e;margin:14px 0 6px">The one number &mdash; today</div>
        <div class="regime-pill {vcls}">{vnum}</div>
        <div style="font-size:12.5px;line-height:1.7;margin-top:8px">{verdict}</div>
        <div style="font-size:11px;color:#8b949e;margin:14px 0 6px">Three rules</div>
        <div style="font-size:12px;line-height:1.8">
          <span class="bullet">Never trade against the gamma sign.</span>
          <span class="bullet">The wall is the target, not the entry. Enter at the pivot, exit at the wall.</span>
          <span class="bullet">Two losses = done for the session.</span>
        </div>
      </div>
      <div>
        <div style="font-size:11px;color:#8b949e;margin-bottom:6px">The day (ET)</div>
        <table><tbody>{rows}</tbody></table>
        <div style="font-size:10.5px;color:#8b949e;margin-top:10px;line-height:1.6">
          For scalping do not read this page &mdash; put <code>ICT_GammaEdgeLevels</code> on the
          ES/NQ chart. Same levels, refreshed every 5 minutes, plus SD-band touch probabilities.
        </div>
      </div>
    </div>"""


def _local_gamma_section(g):
    """Render gamma from NOKEPA + MenthorQ levels held locally."""
    if not g or not g.get("assets"):
        return '<div class="menthorq-placeholder">Local gamma engine unavailable.</div>'

    def money(v):
        if v is None:
            return "&mdash;"
        try:
            v = float(v)
        except Exception:
            return "&mdash;"
        cls = "up" if v > 0 else ("down" if v < 0 else "")
        s = f"{v/1e9:+.2f}B" if abs(v) >= 1e8 else f"{v/1e6:+.0f}M"
        return f'<span class="{cls}">{s}</span>'

    def num(v):
        return "&mdash;" if v in (None, "") else (f"{float(v):,.0f}" if isinstance(v, (int, float)) else str(v))

    rows = ""
    for a, r in g["assets"].items():
        m = (g.get("mq") or {}).get(a, {})
        rows += (f"<tr><td>{a}</td><td>{money(r.get('net_gex'))}</td>"
                 f"<td>{money((r.get('gex_by_dte') or {}).get('0-5'))}</td>"
                 f"<td>{money(r.get('net_dex'))}</td>"
                 f"<td>{num(r.get('flip'))}</td><td>{num(r.get('call_wall'))}</td>"
                 f"<td>{num(r.get('put_wall'))}</td>"
                 f"<td>{num(m.get('call_resistance_0dte'))}</td>"
                 f"<td>{num(m.get('put_support_0dte'))}</td>"
                 f"<td>{num(m.get('min_1d'))}&ndash;{num(m.get('max_1d'))}</td></tr>")

    src = "NOKEPA live (:8780)" if g.get("source") == "nokepa-live" else "NOKEPA cached snapshot"
    mqs = f"MenthorQ levels {g.get('mq_fetched_at') or 'n/a'}" if g.get("mq") else "MenthorQ levels unavailable"
    return f"""
    <table>
      <thead><tr><th>Asset</th><th>Net GEX</th><th>0-5d</th><th>Net DEX</th>
      <th>&gamma;flip</th><th>Call wall</th><th>Put wall</th>
      <th>MQ CR0</th><th>MQ PS0</th><th>MQ 1-day band</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="font-size:10.5px;color:#8b949e;margin-top:8px">
      Source: {src} &middot; {mqs}. Net GEX dealer-signed (negative = dealers short
      gamma, moves amplify); Net DEX dealer-signed.
    </div>"""


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

UNAVAIL_HTML = ('<span class="neutral" style="color:#fbbf24">'
                '&#9888; UNAVAILABLE &mdash; no live narrative this run '
                '(set ANTHROPIC_API_KEY). Nothing is shown rather than stale text.'
                '</span>')


def _narrative_block(key, narrative, fallback=""):
    """Render a narrative section.

    2026-07-31: previously this fell back to hardcoded static prose whenever the
    AI narrative was missing (no ANTHROPIC_API_KEY). That silently published
    months-old text as if it were today's read — on 2026-07-30 the page shipped
    an Iran-oil-shock / "BEARISH sell rallies" call, with NQ pivot levels ~3,400
    points below spot, on a day that closed +3.4% on the Nasdaq. A stale briefing
    that looks live is worse than no briefing, so a missing narrative now fails
    loud. The `fallback` arg is retained for call-site compatibility and ignored.
    """
    val = narrative.get(key)
    if not val:
        return UNAVAIL_HTML
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

def build_html(futures, fg, st_symbols, st_trending, wsb, mq, narrative, mkt=None, gamma=None):
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
    # Do NOT assert a gamma regime we have not measured. With MenthorQ down this
    # badge used to claim "NEGATIVE GAMMA" every day regardless of the tape.
    gamma = gamma or {}
    # Regime comes from OUR OWN engine's net GEX, not from a vendor scrape.
    _spx = (gamma.get("assets") or {}).get("SPX") or {}
    _ng = _spx.get("net_gex")
    if isinstance(_ng, (int, float)):
        if _ng < 0:
            regime_label = f"NEGATIVE GAMMA — vol amplifying (SPX net GEX {_ng/1e6:+.0f}M)"
            regime_class = "regime-neg"
        else:
            regime_label = f"POSITIVE GAMMA — vol dampening (SPX net GEX {_ng/1e6:+.0f}M)"
            regime_class = "regime-pos"
    else:
        regime_label = "GAMMA REGIME UNAVAILABLE — local engine unreachable"
    regime_class = "regime-neutral"
    mq_ok = mq.get("status") == "ok" and mq.get("ok_count", 0) > 0
    if mq_ok:
        regime_label = "Live GEX — see MenthorQ charts"
        regime_class = "regime-pos"

    # ── Key levels: live narrative only. NEVER static ────────────────────────
    # The old defaults (24858/24634/24400/23971/23800) were frozen from an early
    # 2026 session and kept publishing while NQ traded ~28,100 — a 3,400-point
    # error presented as today's pivot. Blank beats wrong.
    def _mq_kl(sym):
        m = (gamma.get("mq") or {}).get(sym) or {}
        if not m:
            return None
        return {"r2": m.get("call_resistance"), "r1": m.get("call_resistance_0dte"),
                "pivot": m.get("hvl"), "support1": m.get("put_support_0dte"),
                "support2": m.get("put_support")}

    # Narrative levels win; real MenthorQ levels on disk are the fallback so the
    # table still populates when the model omits a symbol.
    kl_all = {}
    for _sym in ("NQ", "ES", "SPX", "NDX", "SPY", "QQQ"):
        kl_all[_sym] = (narrative.get(f"key_levels_{_sym.lower()}")
                        or _mq_kl(_sym) or {})

    # ── Bias: live narrative only. A stale directional call is the single most
    #    dangerous thing this page can publish, so absence is stated explicitly.
    _bias = narrative.get("session_bias")
    narrative_ok = isinstance(_bias, dict) and bool(_bias.get("headline"))
    bias_cls = _bias_class(_bias.get("headline", "")) if narrative_ok else "neutral-b"
    bias_card = _bias_card(_bias, bias_cls)
    scenarios_html = _scenarios_html(narrative.get("scenarios"))
    _ol = narrative.get("one_liner")
    oneliner_html = (f'<div class="oneliner"><b>One-liner &mdash; panic-proof</b>{html.escape(str(_ol))}</div>'
                     if _ol else "")

    # ── Degraded-run banner ───────────────────────────────────────────────────
    # Make a half-dead briefing impossible to mistake for a live one.
    _missing = []
    if not narrative_ok:
        _missing.append("AI narrative (ANTHROPIC_API_KEY)")
    if not (gamma.get("assets") or {}).get("SPX", {}).get("net_gex"):
        _missing.append("local gamma engine (NOKEPA :8780 / gex_cache)")
    degraded_banner = ""
    if _missing:
        degraded_banner = (
            '<div style="margin-bottom:12px;background:rgba(251,191,36,0.12);'
            'border:1px solid rgba(251,191,36,0.45);border-radius:6px;padding:10px 14px;'
            'font-size:12.5px;color:#fbbf24;line-height:1.6">'
            '<b>&#9888; DEGRADED RUN &mdash; ANALYTICAL SECTIONS DISABLED.</b><br>'
            'Missing: ' + html.escape(", ".join(_missing)) + '.<br>'
            'The market snapshot and cross-asset quotes above are <b>live</b>. '
            'Bias, key levels, gamma regime, CTA flow and tactical framework are '
            '<b>blank by design</b> &mdash; this page no longer substitutes stale '
            'placeholder text for missing data.</div>')

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
{degraded_banner}
{oneliner_html}

<!-- ── ROW A: TRADE PLAN (top of page, full width) ────────────────────────── -->
<div class="panel" style="margin-bottom:16px">
  <div class="panel-title"><span class="dot" style="background:#4ade80"></span>Session Bias &mdash; the trade plan</div>
  {bias_card}
  <div class="grid-2" style="margin-top:16px">
    <div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:6px">Tactical framework</div>
      <div style="font-size:13px;line-height:1.8">{_narrative_block("tactical_framework", narrative)}</div>
    </div>
    <div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:6px">Location discipline &mdash; where to enter, where not to</div>
      <div style="font-size:13px;line-height:1.7">{_narrative_block("location_discipline", narrative)}</div>
    </div>
  </div>
</div>

<!-- ── ROW 3a: Scenarios ──────────────────────────────────────────────────── -->
<div class="panel" style="margin-bottom:16px">
  <div class="panel-title"><span class="dot" style="background:#c084fc"></span>Scenarios &mdash; weighted, most likely first</div>
  {scenarios_html}
</div>

<!-- ── ROW C: Key Levels + Gamma (wide, side by side) ─────────────────────── -->
<div class="grid-2" style="margin-bottom:16px">

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#fbbf24"></span>Key Levels &mdash; Index Complex</div>
    {_key_levels_html(kl_all)}
    <div style="margin-top:10px;font-size:11px;color:#8b949e">
      <span class="levels-chip chip-r">R</span> Resistance &nbsp;
      <span class="levels-chip chip-p">P</span> Pivot &nbsp;
      <span class="levels-chip chip-s">S</span> Support
      &nbsp;&middot;&nbsp; fixed for the session (built from settled open interest)
    </div>
  </div>

  <div class="panel">
    <div class="panel-title"><span class="dot" style="background:#f87171"></span>Gamma / Options Regime (SPX)</div>
    {_local_gamma_section(gamma)}
    {_narrative_block("gamma_regime", narrative,
        "SPX is operating in negative gamma — dealers are net short gamma and must sell ES futures on declines, amplifying down-moves. "
        "SPX Volatility Trigger ~6,900 is overhead resistance; reclaim needed for regime shift. VIX testing but not closing above 30."
    )}
    <div style="margin-top:12px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:6px">
        Gamma and levels sourced from local engines (NOKEPA + MenthorQ via CDP).
        The legacy menthorq.com WordPress scrape was retired 2026-07-31 &mdash;
        MenthorQ now lives at dashboard.menthorq.io.
      </div>
    </div>
  </div>

</div>

<!-- ── ROW 3b: Session Playbook ───────────────────────────────────────────── -->
<div class="panel" style="margin-bottom:16px">
  <div class="panel-title"><span class="dot" style="background:#60a5fa"></span>Session Playbook &mdash; how to trade the levels above</div>
  {_playbook_html(gamma)}
</div>

<!-- ── ROW E: Market Snapshot + Fear/Greed + VIX ──────────────────────────── -->
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
        <div class="gauge-label">Crypto Fear &amp; Greed</div>
        <div style="font-size:9px;color:#8b949e;margin-top:2px">alternative.me &mdash; crypto, not equity</div>
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

<!-- ── ROW D2: Sentiment read ─────────────────────────────────────────────── -->
<div class="panel" style="margin-bottom:16px">
  <div class="panel-title"><span class="dot" style="background:#8b949e"></span>Sentiment read</div>
  <div style="font-size:13px;line-height:1.7">{_narrative_block("sentiment_read", narrative)}</div>
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
    """Write session redirect page: london.html, index.html (US), or nyopen.html."""
    page_names = {"london": "london.html", "us": "index.html", "nyopen": "nyopen.html"}
    labels     = {"london": "London session", "us": "US pre-market", "nyopen": "NY open session"}
    page_name = page_names.get(SESSION, "index.html")
    label     = labels.get(SESSION, "today's briefing")
    idx = BASE_DIR / page_name
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
<p>Redirecting to <a href="{briefing_filename}" style="color:#60a5fa">{label}</a>…</p>
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
    ,
        creationflags=0x08000000)
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
        threading.Thread(target=_run, args=("gamma",            fetch_local_gamma)),
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
        # Feed the model the ACTUAL computed levels. Without this it only saw
        # futures prices + session hi/lo and was reverse-engineering "key levels"
        # from the day's range — NQ R2 came back as 28,726 against a session high
        # of 28,725.75. Anchoring on real gamma walls and MenthorQ bands turns an
        # interpolation of the range into positioning-derived structure.
        _g = results.get("gamma", {}) or {}
        narrative = generate_ai_narrative({
            "futures":   results.get("futures", []),
            "fear_greed":results.get("fg", {}),
            "sentiment": {k: {"bull_pct": v.get("bull_pct"), "bear_pct": v.get("bear_pct")}
                          for k, v in st_symbols.items() if v},
            "dealer_gamma": _g.get("assets", {}),
            "menthorq_levels": _g.get("mq", {}),
        })
        # Never claim success unconditionally — the old code printed "Narrative
        # ready" even when generate_ai_narrative() had returned {"_error": ...},
        # which is how a permanently-broken narrative went unnoticed.
        if narrative.get("_error"):
            print(f"  [!] NARRATIVE FAILED: {narrative['_error']}")
            narrative = {}
        elif narrative.get("session_bias"):
            print(f"  [+] Narrative ready ({NARRATIVE_MODEL})")
        else:
            print("  [!] NARRATIVE EMPTY — no session_bias returned")
            narrative = {}
    else:
        print("  [i] No ANTHROPIC_API_KEY — analytical sections will render as UNAVAILABLE")

    # ── Build HTML ────────────────────────────────────────────────────────────
    page = build_html(
        futures     = results.get("futures", []),
        fg          = results.get("fg", {}),
        st_symbols  = st_symbols,
        st_trending = results.get("st_trending", []),
        wsb         = results.get("wsb", []),
        mq          = {"status": "retired"},
        gamma       = results.get("gamma", {}),
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
        session_labels = {"london": "London Open", "us": "US Pre-Market", "nyopen": "NY Open"}
        session_label = session_labels.get(SESSION, "Trading")
        notify_ntfy(
            f"Trading Briefing Ready — {session_label}",
            f"{DATE_DISPLAY} | NQ/ES/SPX {session_label} prep complete",
            url=briefing_url,
        )
        print(f"  [+] ntfy notification sent")
        if briefing_url:
            print(f"  [+] Briefing URL: {briefing_url}")
    else:
        # ── Desktop mode: open browser + Windows toast ────────────────────────
        webbrowser.open(LATEST_FILE.as_uri())
        print("  [+] Opened in browser")
        session_labels = {"london": "London Open", "us": "US Pre-Market", "nyopen": "NY Open"}
        session_label = session_labels.get(SESSION, "Trading")
        notify_windows(
            f"Trading Briefing Ready — {session_label}",
            f"{DATE_DISPLAY} | NQ / ES / SPX {session_label} prep complete"
        )
        print("  [+] Notification sent")
        print(f"  [+] Briefing available locally: {LATEST_FILE.as_uri()}")

    print(f"\n[OK] Briefing complete -- {GEN_TIME}")


if __name__ == "__main__":
    main()
