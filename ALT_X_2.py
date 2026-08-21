import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
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
def fetch_future_open_v3(instrument_keys, headers):
    url = "https://api.upstox.com/v3/market-quote/ohlc"
    rows = []
    raw_sample = None
    for keys in chunk_list(instrument_keys):
        params = {"instrument_key": ",".join(keys), "interval": "1d"}
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            st.warning(f"OHLC request error: {e}")
            continue
        if response.status_code != 200:
            st.warning(f"OHLC Error {response.status_code}: {response.text[:300]}")
            continue
        try:
            payload = response.json()
            data = payload.get("data", {})
        except Exception as e:
            st.warning(f"Invalid OHLC response: {e}")
            continue
        if raw_sample is None:
            raw_sample = dict(list(data.items())[:2])
        for response_key, item in data.items():
            if not isinstance(item, dict):
                continue
            live = item.get("live_ohlc") or item.get("ohlc") or {}
            prev = item.get("prev_ohlc") or {}
            true_key = item.get("instrument_token") or response_key
            open_price = (
                live.get("open")
                if live.get("open") not in (None, 0)
                else prev.get("close") if prev.get("close") not in (None, 0)
                else item.get("last_price")
            )
            rows.append({
                "instrument_key": true_key,
                "future_open": open_price,
                "future_ltp": item.get("last_price"),
            })
    return pd.DataFrame(rows), raw_sample
def nearest_option(options_df, underlying_key, expiry, option_type, future_open):
    chain = options_df[
        (options_df["underlying_key"] == underlying_key) &
        (options_df["expiry_date"] == expiry) &
        (options_df["instrument_type"] == option_type)
    ].copy()
    if chain.empty or pd.isna(future_open):
        return None
    chain["strike_diff"] = (chain["strike_price"] - future_open).abs()
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
#   change them here if the rule ever changes.)
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
    batched call across all instruments — same v3 OHLC endpoint used by
    fetch_future_open_v3, just extracting high/low/last_price instead of
    just open.
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
    ATL x2 scanner: alerts a symbol the moment its live LTP is actually
    AT OR ABOVE its Entry price (LTP >= Entry) — checked fresh every
    refresh off the current quote, not off the historical Status. This
    matters because Status ("Open" in particular) reflects a contract
    that triggered on ANY day within the whole look-back window and
    stays "Open" even if price has since fallen back below Entry — that
    is correct for the table (matches the reference backtest's "keep
    every entered trade listed" behaviour) but would be wrong for
    alerting: a contract sitting at "Open" with today's LTP now well
    below Entry has NOT just crossed anything and must not alert. Using
    row["LTP"] >= row["Entry"] directly means only symbols where price
    is genuinely at/above Entry right now are ever considered.
    Still de-duplicated per calendar day via the persisted alert-state
    file (tagged "ATL:<symbol>", reset automatically each trading day)
    so a symbol that stays above Entry for hours only sends ONE Telegram
    message that day, not one every refresh.
    """
    if not telegram_enabled:
        return
    if df.empty:
        return
    alerted = load_trigger_alert_state()
    newly_triggered = []
    for _, row in df.iterrows():
        symbol = row.get("Symbol")
        if not symbol:
            continue
        ltp, entry = row.get("LTP"), row.get("Entry")
        if pd.isna(ltp) or pd.isna(entry) or ltp < entry:
            continue  # LTP hasn't actually crossed Entry (yet, or anymore)
        alert_id = f"ATL:{symbol}"
        if alert_id not in alerted:
            newly_triggered.append((alert_id, row))
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
def build_atl_scanner(access_token, expiry_choice, start_date):
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
    fut_quotes, _ = fetch_future_open_v3(futures["instrument_key"].tolist(), headers)
    if fut_quotes.empty:
        st.error("No futures open data received")
        return pd.DataFrame(), pd.DataFrame()
    futures = futures.merge(fut_quotes, on="instrument_key", how="left")
    futures = futures.dropna(subset=["future_open"])
    if futures.empty:
        st.error("All futures were dropped after the Open-price fetch.")
        return pd.DataFrame(), pd.DataFrame()
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
    result = selected[[
        "Symbol", "LTP", "ATL", "ATL Date", "Entry", "Away %", "TGT", "SL",
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
    "TGT": "{:.2f}",
    "SL": "{:.2f}",
}
# "Status" is internal-only — used to filter to genuinely-triggered
# contracts (build_atl_scanner) and to decide alert eligibility
# (check_and_alert_atl) — and deliberately excluded from display.
DISPLAY_COLS_ATL = ["Symbol", "LTP", "ATL", "ATL Date", "Entry", "Away %", "TGT", "SL", "Lot", "Cap"]
CE_ATL_TINTS = {
    "Entry": {"background-color": "#E3F2FD", "color": "#0D47A1", "font-weight": "600"},
    "TGT": {"background-color": "#E8F5E9", "color": "#1B5E20", "font-weight": "600"},
    "SL": {"background-color": "#FFEBEE", "color": "#B71C1C", "font-weight": "600"},
}
PE_ATL_TINTS = {
    "Entry": {"background-color": "#EDE7F6", "color": "#4527A0", "font-weight": "600"},
    "TGT": {"background-color": "#E0F2F1", "color": "#00695C", "font-weight": "600"},
    "SL": {"background-color": "#FFF3E0", "color": "#E65100", "font-weight": "600"},
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
            st.dataframe(ce_style, width="stretch", hide_index=True, height=table_height(ce_table))
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
            st.dataframe(pe_style, width="stretch", hide_index=True, height=table_height(pe_table))
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
            st.success("Alert state cleared — already-triggered options will alert again.")
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
            access_token, expiry_type, atl_start_date
        )
        if not ce_table.empty or not pe_table.empty:
            if telegram_enabled:
                combined = pd.concat([ce_table, pe_table], ignore_index=True)
                check_and_alert_atl(combined, telegram_enabled, telegram_bot_token, telegram_chat_id)
            show_atl_side_by_side(ce_table, pe_table)
        else:
            st.info("No triggered entries yet — waiting for market data or a breakout above Entry.")
    show_atl()
