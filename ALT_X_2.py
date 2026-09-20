import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
import io
import zipfile
import time
import json
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
# ============================================================
# IST
# ============================================================
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)
def get_ist_now():
    return datetime.now(IST)
# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(
    page_title="ATL x2 Scanner",
    layout="wide"
)
# ============================================================
# CUSTOM CSS
# ============================================================
st.markdown("""
    <style>
        .block-container {
            padding-top: 1rem !important;
            padding-bottom: 1rem !important;
        }
        h1 {
            font-size: 1.8rem !important;
            margin-bottom: 0rem !important;
            white-space: nowrap !important;
        }
        h2 {
            font-size: 1.1rem !important;
            padding-top: 0.2rem !important;
            margin-bottom: 0.1rem !important;
        }
        h3 {
            font-size: 1.0rem !important;
            padding-top: 0.1rem !important;
            margin-bottom: 0.1rem !important;
        }
        /* Prevent graying during refresh */
        .stApp {
            transition: none !important;
        }
        [data-testid="stAppViewContainer"],
        [data-testid="stHeader"] {
            opacity: 1 !important;
            transition: none !important;
        }
        /* Dataframe */
        div[data-testid="stDataFrame"] {
            font-weight: 600 !important;
        }
    </style>
""", unsafe_allow_html=True)
# ============================================================
# PERSISTENT STORAGE
# ============================================================
DATA_DIR = "data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
TRIGGER_ALERT_FILE = os.path.join(DATA_DIR, "trigger_alert_state.json")
LAST_LTP_FILE = os.path.join(DATA_DIR, "last_ltp_state.json")
ALERT_LOG_FILE = os.path.join(DATA_DIR, "alert_log.csv")
# ============================================================
# OPTIONAL EXTERNAL PERSISTENCE (GitHub Gist)
#
# Local disk under DATA_DIR is NOT reliable on Streamlit Cloud — the
# container (and everything on its filesystem) gets wiped on restarts,
# redeploys, or after a period of inactivity. That means the Upstox
# token and, worse, the Telegram alert-dedup state can silently reset
# mid-day, causing duplicate alerts.
#
# If you add these two secrets in .streamlit/secrets.toml (or the
# Streamlit Cloud secrets UI), the token and alert-dedup state are
# additionally backed up to a private GitHub Gist, which survives
# app restarts:
#
#   GITHUB_GIST_TOKEN = "ghp_xxx..."   # PAT with the "gist" scope
#   GITHUB_GIST_ID    = "abcdef123..."  # id of an existing (empty) gist
#
# To create the gist: go to https://gist.github.com/, add any one
# file (e.g. "placeholder.txt" with any content), save it as a
# SECRET gist, then copy the id from its URL
# (https://gist.github.com/<username>/<THIS PART>).
#
# Without these secrets set, everything falls back to local-disk-only
# behavior exactly as before — nothing breaks if you skip this.
# ============================================================
GIST_TOKEN = st.secrets.get("GITHUB_GIST_TOKEN", "")
GIST_ID = st.secrets.get("GITHUB_GIST_ID", "")
USE_GIST_PERSISTENCE = bool(GIST_TOKEN and GIST_ID)
def _gist_headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
def _gist_read_file(filename):
    """Returns the raw text content of one file inside the configured
    Gist, or None if not configured / not found / on any error."""
    if not USE_GIST_PERSISTENCE:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            timeout=10
        )
        if resp.status_code != 200:
            return None
        file_info = resp.json().get("files", {}).get(filename)
        return file_info.get("content") if file_info else None
    except Exception:
        return None
def _gist_write_file(filename, content_str):
    """Writes (creates/overwrites) one file inside the configured Gist.
    Returns True on success, False otherwise (including if not
    configured) — callers should treat this as best-effort."""
    if not USE_GIST_PERSISTENCE:
        return False
    try:
        payload = {"files": {filename: {"content": content_str}}}
        resp = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers(),
            json=payload,
            timeout=10
        )
        return resp.status_code == 200
    except Exception:
        return False
# ============================================================
# TOKEN — local disk first (fast), Gist as durable backup/fallback
# ============================================================
def load_token():
    today_str = get_ist_now().strftime("%Y-%m-%d")
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return data.get("token", "")
        except:
            pass
    # Local copy missing/stale (likely a fresh container after a restart)
    # — try the Gist backup before giving up.
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("token.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    # Warm the local cache too, so we don't hit the Gist
                    # API again this session.
                    try:
                        with open(TOKEN_FILE, "w") as f:
                            json.dump(data, f)
                    except:
                        pass
                    return data.get("token", "")
            except Exception:
                pass
    return ""
def save_token(token):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "token": token
    }
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
    if USE_GIST_PERSISTENCE:
        _gist_write_file("token.json", json.dumps(data))
# ============================================================
# TELEGRAM TRIGGER-ALERT STATE
#
# Persisted to disk (and, if configured, to the Gist backup — see the
# "OPTIONAL EXTERNAL PERSISTENCE" section above) so alert
# de-duplication survives restarts. Without a durable backup, a
# Streamlit Cloud restart mid-day wipes this file and can cause
# duplicate Telegram alerts for options that already fired earlier.
# Resets automatically each new trading day. Each entry is
# "ATL:<symbol>".
# ============================================================
def load_trigger_alert_state():
    today_str = get_ist_now().strftime("%Y-%m-%d")
    if os.path.exists(TRIGGER_ALERT_FILE):
        try:
            with open(TRIGGER_ALERT_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return set(data.get("keys", []))
        except:
            pass
    # Local copy missing/stale — likely a fresh container after a
    # restart. Try the Gist backup before falling back to empty.
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("trigger_alert_state.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    keys = set(data.get("keys", []))
                    try:
                        with open(TRIGGER_ALERT_FILE, "w") as f:
                            json.dump({"date": today_str, "keys": list(keys)}, f)
                    except:
                        pass
                    return keys
            except Exception:
                pass
    return set()
def save_trigger_alert_state(keys):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "keys": list(keys)
    }
    try:
        with open(TRIGGER_ALERT_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
    if USE_GIST_PERSISTENCE:
        _gist_write_file("trigger_alert_state.json", json.dumps(data))
# ============================================================
# LAST-SEEN LTP STATE — the baseline check_and_alert_atl uses to detect a
# genuine Entry CROSSOVER (previous LTP below Entry, current LTP at/above
# it) instead of just "LTP happens to be >= Entry right now". Without this,
# enabling Telegram alerts, restarting the app, or clicking "Reset Alerts"
# while a contract is already sitting well past Entry (e.g. Away % 127%,
# 192%) fires an immediate, stale-looking alert for a move that may have
# happened hours earlier. Persisted (+ Gist-backed, same as the alert-dedup
# state) and reset each new trading day. Each entry is "<symbol>": <ltp>.
# ============================================================
def load_last_ltp_state():
    today_str = get_ist_now().strftime("%Y-%m-%d")
    if os.path.exists(LAST_LTP_FILE):
        try:
            with open(LAST_LTP_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return dict(data.get("ltp", {}))
        except:
            pass
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("last_ltp_state.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    ltp_map = dict(data.get("ltp", {}))
                    try:
                        with open(LAST_LTP_FILE, "w") as f:
                            json.dump({"date": today_str, "ltp": ltp_map}, f)
                    except:
                        pass
                    return ltp_map
            except Exception:
                pass
    return {}
def save_last_ltp_state(ltp_map):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "ltp": ltp_map
    }
    try:
        with open(LAST_LTP_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
    if USE_GIST_PERSISTENCE:
        _gist_write_file("last_ltp_state.json", json.dumps(data))
# ============================================================
# ALERT LOG (CSV) — every fired ATL x2 alert gets one row here: when it
# crossed, at what LTP, and what the Entry/TGT/SL levels were at that
# moment. Lets you go back later and check whether price actually
# reached TGT before SL, instead of trusting the fixed TGT/SL
# percentages blind. Also mirrored to the Gist backup (if configured)
# so the day's alert history isn't lost on a restart — see the
# "OPTIONAL EXTERNAL PERSISTENCE" section above.
# ============================================================
ALERT_LOG_HEADER = "timestamp_ist,tab,symbol,ltp,trigger,tgt,sl\n"
def log_alert_event(tab, symbol, ltp, trigger, tgt=None, sl=None):
    ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
    tgt_str = f"{tgt:.2f}" if tgt is not None else ""
    sl_str = f"{sl:.2f}" if sl is not None else ""
    line = f"{ts},{tab},{symbol},{ltp:.2f},{trigger:.2f},{tgt_str},{sl_str}\n"
    try:
        is_new = not os.path.exists(ALERT_LOG_FILE)
        with open(ALERT_LOG_FILE, "a", newline="") as f:
            if is_new:
                f.write(ALERT_LOG_HEADER)
            f.write(line)
    except Exception:
        pass
    if USE_GIST_PERSISTENCE:
        try:
            existing = _gist_read_file("alert_log.csv")
            if not existing:
                existing = ALERT_LOG_HEADER
            elif not existing.startswith("timestamp_ist"):
                existing = ALERT_LOG_HEADER + existing
            _gist_write_file("alert_log.csv", existing + line)
        except Exception:
            pass
def send_telegram_alert(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        return False, "Missing bot token or chat ID"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return False, f"Exception: {e}"
# ============================================================
# LIVE INSTRUMENT FILE (fetched directly from Upstox)
# ============================================================
LIVE_INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
def normalize_expiry(series):
    return pd.to_datetime(
        pd.to_numeric(series, errors="coerce"),
        unit="ms",
        errors="coerce"
    ).dt.date
@st.cache_data(ttl=3600, show_spinner="Loading live instrument file...")
def load_live_fo_instruments():
    instruments = pd.read_json(LIVE_INSTRUMENT_URL, compression="gzip")
    instruments["expiry_date"] = normalize_expiry(instruments["expiry"])
    futures = instruments[
        (instruments["segment"] == "NSE_FO") &
        (instruments["instrument_type"] == "FUT") &
        (instruments["underlying_type"] == "EQUITY")
    ].copy()
    options = instruments[
        (instruments["segment"] == "NSE_FO") &
        (instruments["instrument_type"].isin(["CE", "PE"])) &
        (instruments["underlying_type"] == "EQUITY")
    ].copy()
    return futures, options
def get_expiry_for_choice(df, choice):
    today = date.today()
    valid = sorted(df[df["expiry_date"] >= today]["expiry_date"].unique())
    if not valid:
        return None
    if choice == "Current Month":
        return valid[0]
    current_month = today.month
    for exp in valid:
        if exp.month != current_month:
            return exp
    return valid[-1] if len(valid) > 1 else valid[0]
def chunk_list(items, size=300):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]
def nearest_option(options_df, underlying_key, expiry, option_type, ref_price):
    """
    ref_price is the reference price the "nearest strike" is measured
    against — sourced entirely from the uploaded NSE F&O Bhavcopy (see
    parse_bhavcopy_underlying_prices below), so the selected CE/PE
    strike is whatever the exchange's own end-of-day underlying price
    says is nearest, not a live-computed open.
    """
    chain = options_df[
        (options_df["underlying_key"] == underlying_key) &
        (options_df["expiry_date"] == expiry) &
        (options_df["instrument_type"] == option_type)
    ].copy()
    if chain.empty or pd.isna(ref_price):
        return None
    chain["strike_diff"] = (chain["strike_price"] - ref_price).abs()
    return chain.sort_values("strike_diff").iloc[0]
def table_height(df, row_px=35, header_px=38, max_px=900):
    return min(header_px + row_px * max(len(df), 1) + 3, max_px)
def style_away_percent(value):
    try:
        value = float(value)
        if value >= 100:
            return "background-color: darkgreen; color: white; font-weight: bold;"
        elif value >= 90:
            return "background-color: lightgreen; color: black; font-weight: bold;"
    except Exception:
        pass
    return ""
def apply_column_tints(styler, tints):
    for col, css in tints.items():
        styler = styler.set_properties(subset=[col], **css)
    return styler
# ============================================================
# UPSTOX CHART DEEP-LINK
#
# Clicking a strike in the tables opens that option's chart on Upstox Pro
# Web in a new tab. Upstox's chart URL identifies a contract by segment
# ("exchange") + "chartToken", and the chartToken is the instrument's
# exchange_token — which is exactly the part after the "|" in the
# instrument_key we already have (e.g. "NSE_FO|54321" -> exchange=NSE_FO,
# chartToken=54321). No extra lookup needed.
#
# If Upstox ever changes the chart URL format, edit ONLY
# UPSTOX_CHART_URL_TEMPLATE below — everything else keys off it.
# ============================================================
UPSTOX_CHART_URL_TEMPLATE = "https://pro.upstox.com/trading-charts?exchange={exchange}&chartToken={token}"
def upstox_chart_url(instrument_key, symbol):
    """
    Chart URL for one option contract. The human-readable symbol is
    appended as a trailing "&sym=..." param purely so the table's
    LinkColumn can display it (via a display_text regex) while the
    whole cell stays a clickable link — Upstox ignores the extra param.
    """
    try:
        exchange, token = str(instrument_key).split("|", 1)
    except ValueError:
        return None
    base = UPSTOX_CHART_URL_TEMPLATE.format(exchange=exchange, token=token)
    return f"{base}&sym={symbol}"
# ============================================================
# ATL SCANNER — ported from atl_fetcher.py + strategy.py + the daily
# backtest script (simulate_trade below is unchanged from the backtest).
#
#   ATL (All-Time-Low) = lowest daily LOW over a user-set look-back
#   window (sidebar "ATL Look-back (days)"). Same caveat as the
#   original atl_fetcher.py: this is the lowest low within whatever
#   window you fetch, not literally since listing — exactly how your
#   script already behaved with its manual start/end date prompts.
#
#   Entry = ATL x ENTRY_MULT (2.0)
#   TGT   = Entry x EXIT_MULT (2.0)   [ = ATL x 4.0 ]
#   SL    = Entry x SL_MULT (0.5)     e.g. Entry=98.60 -> SL=49.30
#   (all three multipliers live right here, at the top of this section —
#   change them here if the rule ever changes.) TGT/SL/Lot/Cap are still
#   computed internally (used for the Telegram alert message and the
#   alert log) but are no longer shown in the on-screen table — see
#   DISPLAY_COLS_ATL below.
#
#   Status (Open / TGT Hit / SL Hit / Not Triggered) is resolved in two
#   layers, purely internally — it is used to decide WHICH contracts
#   qualify to be shown (only genuinely triggered ones) and to detect a
#   fresh trigger for the Telegram alert, but is not itself rendered as
#   a table column:
#     1) a HISTORICAL pass over the look-back window's completed daily
#        candles using simulate_trade() unchanged from the backtest
#        script (same same-day tie-break: if a single day's range spans
#        both TGT and SL, assume SL hit first);
#     2) a LIVE overlay using today's still-forming daily candle
#        (fetched once per refresh, batched across every instrument)
#        to catch a fresh trigger/TGT/SL happening today, without
#        re-fetching/re-simulating the whole history every refresh.
#   Only contracts whose entry has actually triggered (historically or
#   today) are shown — a "genuine signals only" table, not a watchlist
#   of every ATM strike.
# ============================================================
ENTRY_MULT = 2.0   # Entry = ATL * ENTRY_MULT
EXIT_MULT = 2.0    # TGT   = Entry * EXIT_MULT
SL_MULT = 0.5      # SL    = Entry * SL_MULT
MIN_ATL = 3.0      # Options whose ATL is below this (in rupees) are dropped from the table entirely
ATL_HIST_UNIT = "days"
ATL_HIST_INTERVAL = "1"
def fetch_atl_history(instrument_key, headers, from_date, to_date, max_retries=2):
    """
    Daily candles for `instrument_key` between from_date and to_date (date
    objects), sorted oldest -> newest. Same URL shape as atl_fetcher.py /
    the backtest script. Returns None if nothing came back after retries.
    """
    encoded_key = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v3/historical-candle/"
        f"{encoded_key}/{ATL_HIST_UNIT}/{ATL_HIST_INTERVAL}/"
        f"{to_date.isoformat()}/{from_date.isoformat()}"
    )
    attempt = 0
    while True:
        try:
            response = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            return None
        if response.status_code == 429:
            if attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                attempt += 1
                continue
            return None
        if response.status_code != 200:
            return None
        candles = (response.json().get("data") or {}).get("candles") or []
        if not candles:
            return None
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
        )
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df if not df.empty else None
def simulate_trade(candles, entry, exit_target, sl):
    """
    Unchanged from the backtest script. candles: DataFrame sorted oldest ->
    newest with high/low/timestamp columns. Returns a dict describing the
    outcome: NOT_TRIGGERED / TARGET / SL / OPEN.
    """
    entry_date = None
    for _, row in candles.iterrows():
        if entry_date is None:
            if row["high"] >= entry:
                entry_date = row["timestamp"]
            else:
                continue  # still waiting for entry, nothing else to check yet
        hit_sl = row["low"] <= sl
        hit_target = row["high"] >= exit_target
        if hit_sl and hit_target:
            # Can't tell intraday order from daily candles -> assume SL first
            return {
                "result": "SL", "entry_date": entry_date,
                "exit_date": row["timestamp"], "exit_price": sl,
            }
        if hit_sl:
            return {
                "result": "SL", "entry_date": entry_date,
                "exit_date": row["timestamp"], "exit_price": sl,
            }
        if hit_target:
            return {
                "result": "TARGET", "entry_date": entry_date,
                "exit_date": row["timestamp"], "exit_price": exit_target,
            }
    if entry_date is None:
        return {"result": "NOT_TRIGGERED", "entry_date": None, "exit_date": None, "exit_price": None}
    last_close = candles.iloc[-1]["close"]
    return {
        "result": "OPEN", "entry_date": entry_date,
        "exit_date": candles.iloc[-1]["timestamp"], "exit_price": last_close,
    }
def _fetch_single_atl_data(instrument_key, headers, from_date, to_date, max_retries=2):
    candles = fetch_atl_history(instrument_key, headers, from_date, to_date, max_retries)
    if candles is None or candles.empty:
        return instrument_key, None, "No historical daily candles in look-back window"
    atl_idx = candles["low"].idxmin()
    atl = candles.loc[atl_idx, "low"]
    atl_timestamp = candles.loc[atl_idx, "timestamp"]
    if atl in (None, 0) or pd.isna(atl):
        return instrument_key, None, "Invalid ATL (zero/NaN low)"
    entry = atl * ENTRY_MULT
    tgt = entry * EXIT_MULT
    sl = entry * SL_MULT
    sim = simulate_trade(candles, entry, tgt, sl)
    info = {
        "atl": atl,
        # Plain "YYYY-MM-DD" string, not a Timestamp — otherwise it
        # displays with a time-of-day and +05:30 offset (e.g.
        # "2026-08-10 00:00:00+05:30") instead of just "2026-08-10".
        "atl_date": atl_timestamp.strftime("%Y-%m-%d") if pd.notna(atl_timestamp) else None,
        "entry": entry,
        "tgt": tgt,
        "sl": sl,
        "hist_status": sim["result"],
    }
    return instrument_key, info, None
@st.cache_data(ttl=1800, show_spinner="Scanning ATL look-back history...")
def fetch_atl_map(instrument_keys, headers_tuple, from_date_iso, to_date_iso):
    headers = dict(headers_tuple)
    from_date = datetime.strptime(from_date_iso, "%Y-%m-%d").date()
    to_date = datetime.strptime(to_date_iso, "%Y-%m-%d").date()
    result = {}
    sample_errors = []
    instrument_keys = list(instrument_keys)
    # Conservative batching (modest concurrency, small batches, short
    # pause between them) to avoid tripping Upstox's rate limiter.
    # Daily-candle history is cached for 30 minutes (ttl above) since it
    # barely changes intraday, so this heavy pass runs far less often
    # than the live overlay below.
    batch_size = 10
    pause_between_batches = 0.6
    with ThreadPoolExecutor(max_workers=5) as executor:
        for i in range(0, len(instrument_keys), batch_size):
            batch = instrument_keys[i:i + batch_size]
            futures = {
                executor.submit(_fetch_single_atl_data, key, headers, from_date, to_date): key
                for key in batch
            }
            for future in as_completed(futures):
                key, info, error = future.result()
                result[key] = info
                if error and len(sample_errors) < 5:
                    sample_errors.append(f"{key} -> {error}")
            if i + batch_size < len(instrument_keys):
                time.sleep(pause_between_batches)
    return result, sample_errors
def fetch_today_live_ohlc(instrument_keys, headers):
    """
    Today's still-forming daily candle (running high/low) plus LTP, for
    the live overlay on top of the cached historical ATL pass. One
    batched call across all instruments via Upstox's v3 OHLC endpoint,
    extracting high/low/last_price.
    """
    url = "https://api.upstox.com/v3/market-quote/ohlc"
    rows = []
    for keys in chunk_list(instrument_keys):
        params = {"instrument_key": ",".join(keys), "interval": "1d"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception:
            continue
        if response.status_code != 200:
            continue
        try:
            data = response.json().get("data", {})
        except Exception:
            continue
        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue
            live = item.get("live_ohlc") or item.get("ohlc") or {}
            true_key = item.get("instrument_token") or response_key
            rows.append({
                "instrument_key": true_key,
                "today_high": live.get("high"),
                "today_low": live.get("low"),
                "today_ltp": item.get("last_price"),
            })
    return pd.DataFrame(rows)
def _resolve_atl_status(row):
    """
    Combines the cached historical result (simulate_trade over the whole
    look-back window) with today's live overlay to get a single current
    status: "Not Triggered" / "Open" / "TGT Hit" / "SL Hit". Whether this
    is the very first day Entry was ever crossed (vs. having already
    triggered on some earlier day within the look-back window) makes no
    difference here — that distinction used to gate the Telegram alert,
    but doing so meant most contracts (having likely crossed 2x-ATL at
    some point over the whole look-back window already) could never
    alert at all. See check_and_alert_atl for how alerting is decided
    now — it dedupes per calendar day instead, off this Status value.
    """
    hist = row["_hist_status"]
    entry, tgt, sl = row["Entry"], row["TGT"], row["SL"]
    today_high, today_low = row.get("today_high"), row.get("today_low")
    if hist == "TARGET":
        return "TGT Hit"
    if hist == "SL":
        return "SL Hit"
    if hist == "OPEN":
        # Entry already triggered on a past day within the look-back
        # window; today just decides whether TGT/SL finally hits.
        hit_tgt = pd.notna(today_high) and today_high >= tgt
        hit_sl = pd.notna(today_low) and today_low <= sl
        if hit_sl:
            return "SL Hit"  # same tie-break as simulate_trade
        if hit_tgt:
            return "TGT Hit"
        return "Open"
    # hist == "NOT_TRIGGERED": entry not yet hit as of yesterday's close.
    hit_entry_today = pd.notna(today_high) and today_high >= entry
    if not hit_entry_today:
        return "Not Triggered"
    hit_tgt = pd.notna(today_high) and today_high >= tgt
    hit_sl = pd.notna(today_low) and today_low <= sl
    if hit_sl:
        return "SL Hit"
    if hit_tgt:
        return "TGT Hit"
    return "Open"
def check_and_alert_atl(df, telegram_enabled, bot_token, chat_id):
    """
    ATL x2 scanner: alerts a symbol only on a genuine FRESH CROSSOVER of
    its Entry level — the previously-seen LTP was below Entry and the
    current LTP is at/above it. This is checked fresh every refresh off
    the current quote, not off the historical Status column, because
    Status ("Open" in particular) reflects a contract that triggered on
    ANY day within the whole look-back window and stays "Open" even if
    price has since fallen back below Entry.

    Requiring an actual prev-below / now-above edge (via
    load_last_ltp_state / save_last_ltp_state) — rather than simply
    "LTP >= Entry right now" — avoids a stale-looking alert firing the
    moment Telegram gets enabled, the app restarts, or "Reset Alerts" is
    clicked while price is already sitting well past Entry (e.g. an
    Away % of 127% or 192%, far beyond the 100% mark that means "just
    crossed"). On the first observation of a symbol in a session/day
    there is no prior LTP to compare against, so that refresh only seeds
    the baseline and never fires an alert by itself — the earliest a
    symbol can alert is the refresh after one where it was seen below
    Entry.

    Still de-duplicated per calendar day via the persisted alert-state
    file (tagged "ATL:<symbol>", reset automatically each trading day)
    on top of the crossover check, so a symbol that re-alerts logic
    doesn't fire twice for the same crossing across retries.
    """
    if not telegram_enabled:
        return
    if df.empty:
        return
    alerted = load_trigger_alert_state()
    last_ltp = load_last_ltp_state()
    newly_triggered = []
    ltp_state_changed = False
    for _, row in df.iterrows():
        symbol = row.get("Symbol")
        if not symbol:
            continue
        ltp, entry = row.get("LTP"), row.get("Entry")
        if pd.isna(ltp) or pd.isna(entry):
            continue
        prev_ltp = last_ltp.get(symbol)
        if prev_ltp != ltp:
            last_ltp[symbol] = ltp
            ltp_state_changed = True
        if prev_ltp is None or prev_ltp >= entry:
            continue  # no prior below-Entry baseline to cross FROM this refresh
        if ltp < entry:
            continue  # hasn't crossed yet
        alert_id = f"ATL:{symbol}"
        if alert_id not in alerted:
            newly_triggered.append((alert_id, row))
    if ltp_state_changed:
        save_last_ltp_state(last_ltp)
    if not newly_triggered:
        return
    sent_count = 0
    fail_count = 0
    for alert_id, row in newly_triggered:
        message = (
            "🚀 <b>ATL x2 — Entry Triggered</b>\n\n"
            f"<b>{row['Symbol']}</b>\n"
            f"LTP: {row['LTP']:.2f}\n"
            f"Entry: {row['Entry']:.2f}\n"
            f"TGT: {row['TGT']:.2f}  |  SL: {row['SL']:.2f}\n"
            f"Away %: {row['Away %']:.2f}%\n"
            f"Lot: {row['Lot']}  |  Cap: {row['Cap']}"
        )
        success, error = send_telegram_alert(bot_token, chat_id, message)
        if success:
            alerted.add(alert_id)
            save_trigger_alert_state(alerted)
            log_alert_event("ATL", row['Symbol'], row['LTP'], row['Entry'], tgt=row['TGT'], sl=row['SL'])
            sent_count += 1
        else:
            fail_count += 1
    if sent_count:
        st.toast(f"Telegram alert sent for {sent_count} ATL entry trigger(s).", icon="🚀")
    if fail_count:
        st.toast(f"{fail_count} ATL alert(s) failed — will retry next refresh.", icon="⚠️")
# ============================================================
# BHAVCOPY-BASED STRIKE-SELECTION REFERENCE PRICE
#
# nearest_option() needs one reference price per underlying to pick the
# nearest CE/PE strike. This is now sourced ENTIRELY from an uploaded
# NSE F&O Bhavcopy — there is no live Upstox futures-open fetch and no
# monthly-open fallback any more. Without a Bhavcopy uploaded, the
# scanner refuses to run (build_atl_scanner errors out and returns
# empty tables) rather than falling back to a live-computed price.
#
# Upload the daily UDiFF "BhavCopy_NSE_FO_0_0_0_<date>_F_0000.csv"
# report in the sidebar (also accepted as .csv.gz, or the .zip NSE
# distributes it in). Its per-row "UndrlygPric" column (NSE's own
# published underlying closing price for that contract) is grouped by
# symbol into one price per underlying. Older Bhavcopy layouts that
# don't publish UndrlygPric fall back to the FUT row's own closing
# price (INSTRUMENT/ClsPric) instead — that's the only fallback left.
#
# This is looked up by underlying SYMBOL (e.g. "RELIANCE"), not by
# Upstox instrument_key, since that's what a Bhavcopy row identifies.
# Any underlying not found in the uploaded file is simply dropped from
# the scan (reported in an expander in build_atl_scanner) rather than
# substituted with a live price.
# ============================================================
def parse_bhavcopy_underlying_prices(uploaded_file):
    """
    Returns (price_map, error): price_map is {symbol: price} built from
    the uploaded NSE F&O Bhavcopy file. error is None on success, or a
    short message to show the user if the file couldn't be parsed.
    Returns ({}, None) if uploaded_file is None (nothing uploaded yet).
    """
    if uploaded_file is None:
        return {}, None
    name = uploaded_file.name.lower()
    try:
        raw_bytes = uploaded_file.getvalue()
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    return {}, "No CSV found inside the uploaded zip."
                with zf.open(csv_names[0]) as f:
                    df = pd.read_csv(f)
        elif name.endswith(".gz"):
            df = pd.read_csv(io.BytesIO(raw_bytes), compression="gzip")
        else:
            df = pd.read_csv(io.BytesIO(raw_bytes))
    except Exception as e:
        return {}, f"Could not read Bhavcopy file: {e}"
    df.columns = [str(c).strip() for c in df.columns]
    symbol_col = next((c for c in ["TckrSymb", "SYMBOL", "Symbol"] if c in df.columns), None)
    if symbol_col is None:
        return {}, "Bhavcopy is missing a symbol column (expected TckrSymb / SYMBOL)."
    price_col = next((c for c in ["UndrlygPric", "UNDRLYPRC", "UnderlyingPrice"] if c in df.columns), None)
    if price_col is not None:
        work_df = df
    else:
        # Older layouts don't publish UndrlygPric — fall back to the
        # futures row's own closing price for that underlying.
        instr_col = next((c for c in ["FinInstrmTp", "INSTRUMENT"] if c in df.columns), None)
        close_col = next((c for c in ["ClsPric", "CLOSE"] if c in df.columns), None)
        if instr_col is None or close_col is None:
            return {}, "Bhavcopy has neither UndrlygPric nor a recognizable FUT close column."
        work_df = df[df[instr_col].astype(str).str.contains("F", case=False, na=False)]
        price_col = close_col
    work_df = work_df.copy()
    work_df[price_col] = pd.to_numeric(work_df[price_col], errors="coerce")
    price_map = work_df.dropna(subset=[price_col]).groupby(symbol_col)[price_col].first().to_dict()
    if not price_map:
        return {}, "Bhavcopy parsed but no usable prices were found in it."
    return price_map, None
def render_bhavcopy_uploader(container):
    """
    Renders the Bhavcopy uploader + status message in the given
    Streamlit container (st.sidebar in the normal view, or st itself in
    client view where the sidebar is hidden via CSS), and returns the
    parsed {symbol: price} map. Bhavcopy is now the ONLY source of the
    strike-selection reference price — there is no live-price fallback
    — so this upload is required before the scanner will run.
    """
    container.markdown("**Bhavcopy (Strike Reference) — required**")
    bhavcopy_file = container.file_uploader(
        "Upload NSE F&O Bhavcopy",
        type=["csv", "gz", "zip"],
        help=(
            "REQUIRED. Strike selection (which CE/PE strike counts as "
            "'nearest') is read entirely from this file's 'UndrlygPric' "
            "column per symbol (falling back to the FUT row's close "
            "price on older layouts) — there is no live-price fallback "
            "any more. The scanner won't run without one uploaded."
        ),
        key="bhavcopy_uploader",
    )
    price_map, error = parse_bhavcopy_underlying_prices(bhavcopy_file)
    if bhavcopy_file is not None:
        if error:
            container.error(f"Bhavcopy: {error}")
        else:
            container.success(f"Bhavcopy loaded — {len(price_map)} underlying symbols.")
    else:
        container.warning("No Bhavcopy uploaded yet — required for strike selection.")
    return price_map
def build_atl_scanner(access_token, expiry_choice, start_date, bhavcopy_price_map=None):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    futures, options = load_live_fo_instruments()
    expiry = get_expiry_for_choice(futures, expiry_choice)
    if expiry is None:
        st.error("No futures expiry found")
        return pd.DataFrame(), pd.DataFrame()
    futures = futures[futures["expiry_date"] == expiry].copy()
    options = options[options["expiry_date"] == expiry].copy()
    bhavcopy_price_map = bhavcopy_price_map or {}
    if not bhavcopy_price_map:
        st.error("Upload an NSE F&O Bhavcopy in the sidebar first — strike selection now reads its reference price from the Bhavcopy only.")
        return pd.DataFrame(), pd.DataFrame()
    # Strike selection reference price comes ONLY from the uploaded
    # Bhavcopy's per-symbol price (see parse_bhavcopy_underlying_prices
    # above) — no live futures-open fetch, no monthly-open fallback. Any
    # underlying not present in the uploaded file is simply dropped.
    futures["future_open"] = futures["underlying_symbol"].map(bhavcopy_price_map)
    dropped = futures[futures["future_open"].isna()]["underlying_symbol"].unique().tolist()
    futures = futures.dropna(subset=["future_open"])
    if futures.empty:
        st.error("None of this expiry's underlyings were found in the uploaded Bhavcopy.")
        return pd.DataFrame(), pd.DataFrame()
    if dropped:
        with st.expander(f"⚠️ {len(dropped)} underlying(s) skipped — not found in the uploaded Bhavcopy"):
            st.write(", ".join(sorted(dropped)))
    selected_rows = []
    for _, fut in futures.iterrows():
        ce = nearest_option(options, fut["underlying_key"], expiry, "CE", fut["future_open"])
        pe = nearest_option(options, fut["underlying_key"], expiry, "PE", fut["future_open"])
        for opt in [ce, pe]:
            if opt is None:
                continue
            selected_rows.append({
                "underlying_symbol": fut["underlying_symbol"],
                "strike": opt["strike_price"],
                "option_type": opt["instrument_type"],
                "option_key": opt["instrument_key"],
                "Lot": opt["lot_size"],
            })
    selected = pd.DataFrame(selected_rows)
    if selected.empty:
        st.error("No CE/PE options found")
        return pd.DataFrame(), pd.DataFrame()
    selected["Symbol"] = (
        selected["underlying_symbol"].astype(str) + " "
        + selected["strike"].astype(int).astype(str) + " "
        + selected["option_type"].astype(str)
    )
    today = get_ist_now().date()
    from_date = start_date
    to_date = today - timedelta(days=1)  # historical endpoint won't have today yet
    atl_map, atl_errors = fetch_atl_map(
        tuple(sorted(selected["option_key"].unique())),
        tuple(headers.items()),
        from_date.isoformat(),
        to_date.isoformat(),
    )
    def _atl_field(key, field, default=None):
        info = atl_map.get(key)
        return info.get(field, default) if info else default
    selected["ATL"] = pd.to_numeric(selected["option_key"].apply(lambda k: _atl_field(k, "atl")), errors="coerce")
    selected["ATL Date"] = selected["option_key"].apply(lambda k: _atl_field(k, "atl_date"))
    selected["Entry"] = pd.to_numeric(selected["option_key"].apply(lambda k: _atl_field(k, "entry")), errors="coerce")
    selected["TGT"] = pd.to_numeric(selected["option_key"].apply(lambda k: _atl_field(k, "tgt")), errors="coerce")
    selected["SL"] = pd.to_numeric(selected["option_key"].apply(lambda k: _atl_field(k, "sl")), errors="coerce")
    selected["_hist_status"] = selected["option_key"].apply(lambda k: _atl_field(k, "hist_status", "NOT_TRIGGERED"))
    missing_count = selected["ATL"].isna().sum()
    total_count = len(selected)
    if missing_count > 0:
        with st.expander(
            f"⚠️ ATL not available for {missing_count}/{total_count} options (hidden from table below)",
            expanded=(missing_count == total_count)
        ):
            st.write(f"Needs at least one completed daily candle between {from_date} and {to_date}. Can also happen on rate-limited requests — those retry within 30 minutes (ATL cache TTL).")
            if atl_errors:
                for err in atl_errors:
                    st.code(err)
    live_ohlc = fetch_today_live_ohlc(selected["option_key"].tolist(), headers)
    selected = selected.merge(live_ohlc, left_on="option_key", right_on="instrument_key", how="left")
    selected["Status"] = selected.apply(_resolve_atl_status, axis=1)
    selected["LTP"] = pd.to_numeric(selected["today_ltp"], errors="coerce")
    selected["Away %"] = np.where(
        selected["Entry"] > 0,
        (selected["LTP"] / selected["Entry"]) * 100,
        np.nan
    )
    selected["Away %"] = selected["Away %"].clip(lower=0)
    selected["Cap"] = selected["LTP"] * selected["Lot"]
    # Clickable link (opens this strike's chart on Upstox Pro Web) — shown
    # in place of the plain Symbol column, see DISPLAY_COLS_ATL /
    # ATL_COLUMN_CONFIG below.
    selected["Chart"] = selected.apply(
        lambda r: upstox_chart_url(r["option_key"], r["Symbol"]), axis=1
    )
    # TGT, SL, Lot, Cap are kept in `result` even though they are no
    # longer shown in the table (DISPLAY_COLS_ATL below) — they're still
    # needed internally by check_and_alert_atl for the Telegram message
    # and the alert log.
    result = selected[[
        "Symbol", "Chart", "LTP", "ATL", "ATL Date", "Entry", "Away %", "TGT", "SL",
        "Status", "Lot", "Cap"
    ]].copy()
    for col in ["LTP", "ATL", "Entry", "Away %", "TGT", "SL"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)
    result["Lot"] = pd.to_numeric(result["Lot"], errors="coerce").fillna(0).astype(int)
    result["Cap"] = pd.to_numeric(result["Cap"], errors="coerce").round(0).fillna(0).astype(int)
    # Only show contracts whose entry has actually triggered — historically
    # within the look-back window, or live today — not a watchlist of every
    # ATM strike. Status itself stays internal (used here for the filter and
    # for the Telegram alert) and is not rendered as a table column.
    result = result[result["Status"] != "Not Triggered"].reset_index(drop=True)
    # Drop options whose ATL is a "penny" price below MIN_ATL rupees — these
    # are typically deep OTM/illiquid strikes where the ATL x2/x4 levels are
    # not meaningful trade levels.
    result = result[result["ATL"] >= MIN_ATL].reset_index(drop=True)
    ce_table = result[result["Symbol"].str.endswith("CE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)
    pe_table = result[result["Symbol"].str.endswith("PE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)
    return ce_table, pe_table
DECIMAL_COLS_ATL = {
    "LTP": "{:.2f}",
    "ATL": "{:.2f}",
    "Entry": "{:.2f}",
    "Away %": "{:.2f}%",
}
# "Status" is internal-only — used to filter to genuinely-triggered
# contracts (build_atl_scanner) and to decide alert eligibility
# (check_and_alert_atl) — and deliberately excluded from display.
# TGT, SL, Lot and Cap are likewise kept out of the displayed table (they
# still exist on the underlying DataFrame for the Telegram alert / log).
#
# "Chart" is the clickable strike: it holds the Upstox chart URL, and the
# LinkColumn below relabels it "Symbol" and displays just the contract
# name (regex grabs everything after "sym=" at the end of the URL) — so
# it looks like the normal Symbol column but opens the chart on click.
DISPLAY_COLS_ATL = ["Chart", "LTP", "ATL", "ATL Date", "Entry", "Away %"]
ATL_COLUMN_CONFIG = {
    "Chart": st.column_config.LinkColumn(
        "Symbol",
        display_text=r"sym=(.*)$",
        help="Click a strike to open its chart on Upstox Pro Web.",
    ),
}
CE_ATL_TINTS = {
    "Entry": {"background-color": "#E3F2FD", "color": "#0D47A1", "font-weight": "600"},
}
PE_ATL_TINTS = {
    "Entry": {"background-color": "#EDE7F6", "color": "#4527A0", "font-weight": "600"},
}
def show_atl_side_by_side(ce_table, pe_table):
    last_updated = get_ist_now().strftime("%H:%M:%S")
    st.caption(f"Last Updated: {last_updated} IST")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Calls (CE)**")
        if ce_table.empty:
            st.info("No CE data available.")
        else:
            ce_style = (
                ce_table[DISPLAY_COLS_ATL].style
                .map(style_away_percent, subset=["Away %"])
                .pipe(apply_column_tints, CE_ATL_TINTS)
                .format(DECIMAL_COLS_ATL, na_rep="-")
            )
            st.dataframe(
                ce_style, width="stretch", hide_index=True,
                height=table_height(ce_table), column_config=ATL_COLUMN_CONFIG
            )
    with col2:
        st.markdown("**Puts (PE)**")
        if pe_table.empty:
            st.info("No PE data available.")
        else:
            pe_style = (
                pe_table[DISPLAY_COLS_ATL].style
                .map(style_away_percent, subset=["Away %"])
                .pipe(apply_column_tints, PE_ATL_TINTS)
                .format(DECIMAL_COLS_ATL, na_rep="-")
            )
            st.dataframe(
                pe_style, width="stretch", hide_index=True,
                height=table_height(pe_table), column_config=ATL_COLUMN_CONFIG
            )
# ============================================================
# CONFIGURATION (sidebar)
# ============================================================
is_client_view = "UPSTOX_ACCESS_TOKEN" in st.secrets and st.secrets["UPSTOX_ACCESS_TOKEN"].strip() != ""
if is_client_view:
    access_token = st.secrets["UPSTOX_ACCESS_TOKEN"]
    st.markdown("""
        <style>
            [data-testid="stSidebar"] { display: none; }
        </style>
    """, unsafe_allow_html=True)
    auto_refresh = True
    refresh_interval = 15
    expiry_type = "Current Month"
    atl_start_date = get_ist_now().date() - timedelta(days=365)
    telegram_bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    telegram_enabled = bool(telegram_bot_token and telegram_chat_id)
    # The sidebar (and its config) is hidden in client view, but the
    # Bhavcopy upload is required and has no live-price fallback, so it
    # gets rendered in the main body instead, above the title.
    bhavcopy_price_map = render_bhavcopy_uploader(st)
    st.markdown("---")
else:
    with st.sidebar:
        st.header("Configuration")
        if USE_GIST_PERSISTENCE:
            st.caption("🔒 Persistence: local + Gist backup (survives restarts)")
        else:
            st.caption("⚠️ Persistence: local disk only — resets on app restart/redeploy. See GITHUB_GIST_TOKEN / GITHUB_GIST_ID in the code comments to enable a durable backup.")
        saved_token = load_token()
        access_token = st.text_input("Upstox Access Token", value=saved_token, type="password")
        if access_token and access_token != saved_token:
            save_token(access_token)
        st.markdown("---")
        st.header("Expiry Settings")
        expiry_type = st.radio(
            "Select Expiry Month",
            options=["Current Month", "Next Month"],
            index=0,
            help="Which monthly expiry's ATM options the scanner tracks."
        )
        st.markdown("---")
        st.header("Bhavcopy (Strike Reference)")
        bhavcopy_file = st.file_uploader(
            "Upload NSE F&O Bhavcopy",
            type=["csv", "gz", "zip"],
            help=(
                "Used as the strike-selection reference price (which CE/PE "
                "strike counts as 'nearest') instead of Upstox's live "
                "monthly futures open. Reads the Bhavcopy's 'UndrlygPric' "
                "column per symbol when present, falling back to the FUT "
                "row's close price on older layouts. Any symbol not found "
                "in the uploaded file falls back to the live monthly-open "
                "method automatically, so this is optional."
            )
        )
        bhavcopy_price_map, bhavcopy_error = parse_bhavcopy_underlying_prices(bhavcopy_file)
        if bhavcopy_file is not None:
            if bhavcopy_error:
                st.error(f"Bhavcopy: {bhavcopy_error}")
            else:
                st.success(f"Bhavcopy loaded — {len(bhavcopy_price_map)} underlying symbols.")
        st.markdown("---")
        st.header("ATL x2 Scanner Settings")
        atl_start_date = st.date_input(
            "ATL Look-back From",
            value=get_ist_now().date() - timedelta(days=365),
            min_value=get_ist_now().date() - timedelta(days=3650),
            max_value=get_ist_now().date() - timedelta(days=1),
            help=(
                "All-Time-Low (ATL) is the lowest daily LOW from this date "
                "up to yesterday's close — not literally since listing, "
                "same as the manual start/end date range in "
                "atl_fetcher.py. Entry = ATL x 2.0, TGT = Entry x 2.0, "
                "SL = Entry x 0.5."
            )
        )
        st.markdown("---")
        st.header("Telegram Alerts")
        telegram_enabled = st.checkbox(
            "Enable Trigger Alerts",
            value=st.session_state.get("telegram_enabled", False),
            key="telegram_enabled",
            help="Sends a Telegram message the moment an option's ATL x2 Entry level is crossed for the first time today."
        )
        telegram_bot_token = st.text_input(
            "Bot Token",
            type="password",
            value=st.session_state.get("telegram_bot_token", ""),
            key="telegram_bot_token",
            help="Create a bot via @BotFather on Telegram to get this token."
        )
        telegram_chat_id = st.text_input(
            "Chat ID",
            value=st.session_state.get("telegram_chat_id", ""),
            key="telegram_chat_id",
            help="Your personal or group chat ID. Message @userinfobot to find yours."
        )
        tg_col1, tg_col2 = st.columns(2)
        test_telegram_clicked = tg_col1.button("Send Test", width="stretch")
        reset_alert_state_clicked = tg_col2.button("Reset Alerts", width="stretch")
        if reset_alert_state_clicked:
            save_trigger_alert_state(set())
            save_last_ltp_state({})
            st.success("Alert state cleared — options will alert again on their next fresh Entry crossover (not instantly, even if already above Entry).")
        if test_telegram_clicked:
            success, error = send_telegram_alert(
                telegram_bot_token,
                telegram_chat_id,
                "✅ Test alert from ATL x2 Scanner — Telegram is wired up correctly."
            )
            if success:
                st.success("Test message sent — check Telegram.")
            else:
                st.error(f"Test message failed: {error}")
        st.markdown("---")
        st.header("Auto Refresh")
        auto_refresh = st.checkbox("Enable Auto-Refresh", value=False)
        refresh_interval = st.slider("Refresh Interval (seconds)", min_value=5, max_value=60, value=15)
# ============================================================
# MAIN PAGE — single live scanner:
#   ATL x2: Entry = ATL x2.0, TGT = Entry x2.0, SL = Entry x0.5
#           (ported from atl_fetcher.py / strategy.py).
# ============================================================
st.title("ATL x2 Scanner")
run_every = refresh_interval if auto_refresh else None
if not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar first.")
else:
    st.header("All-Time-Low x2 Breakout (Live)")
    st.caption(f"Entry = ATL x{ENTRY_MULT:g}  |  TGT = Entry x{EXIT_MULT:g}  |  SL = Entry x{SL_MULT:g}  |  Look-back from {atl_start_date}")
    @st.fragment(run_every=run_every)
    def show_atl():
        ce_table, pe_table = build_atl_scanner(
            access_token, expiry_type, atl_start_date, bhavcopy_price_map
        )
        if not ce_table.empty or not pe_table.empty:
            if telegram_enabled:
                combined = pd.concat([ce_table, pe_table], ignore_index=True)
                check_and_alert_atl(combined, telegram_enabled, telegram_bot_token, telegram_chat_id)
            show_atl_side_by_side(ce_table, pe_table)
        else:
            st.info("No triggered entries yet — waiting for market data or a breakout above Entry.")
    show_atl()
