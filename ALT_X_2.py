import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
import io
import zipfile
import json
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone
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
    page_title="PDL x2 Scanner",
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
# "PDL:<symbol>".
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
# FIRST-HIT STATE — once a triggered contract's TGT or SL is hit, whichever
# comes FIRST is frozen for the rest of the trading day (the other one is
# ignored even if price later reaches it). Keyed by instrument_key,
# persisted to disk (+ Gist backup if configured) so a restart doesn't
# un-freeze it, and reset automatically each new trading day.
# Each entry is "<instrument_key>": "TGT Hit" | "SL Hit".
# ============================================================
FIRST_HIT_FILE = os.path.join(DATA_DIR, "first_hit_state.json")
def load_first_hit_state():
    today_str = get_ist_now().strftime("%Y-%m-%d")
    if os.path.exists(FIRST_HIT_FILE):
        try:
            with open(FIRST_HIT_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    return dict(data.get("hits", {}))
        except:
            pass
    hits = {}
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("first_hit_state.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
                    hits = dict(data.get("hits", {}))
            except Exception:
                pass
    # Warm the local cache (even when empty) so the Gist isn't hit on
    # every refresh.
    try:
        with open(FIRST_HIT_FILE, "w") as f:
            json.dump({"date": today_str, "hits": hits}, f)
    except:
        pass
    return hits
def save_first_hit_state(hits):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "hits": hits
    }
    try:
        with open(FIRST_HIT_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
    if USE_GIST_PERSISTENCE:
        _gist_write_file("first_hit_state.json", json.dumps(data))
# ============================================================
# ALERT LOG (CSV) — every fired PDL x2 alert gets one row here: when it
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
# PDL x2 SCANNER  (PDL = Previous Day Low)
#
#   PDL = the option contract's own LOW price from the uploaded NSE F&O
#   Bhavcopy (UDiFF "LwPric" column; older layouts "LOW"), matched per
#   contract on (symbol, expiry, strike, CE/PE).
#
#   Entry = PDL x ENTRY_MULT (2.0)
#   TGT   = Entry x EXIT_MULT (2.0)   [ = PDL x 4.0 ]
#   SL    = Entry x SL_MULT (0.5)     [ = PDL ]
#   (all three multipliers live right here.)
#
#   Status (shown in the table):
#     Not Triggered -> hidden (today's high hasn't reached Entry)
#     Open          -> Entry reached, neither TGT nor SL hit yet
#     TGT Hit / SL Hit -> whichever of TGT / SL was hit FIRST after Entry
#                     was reached. It is FROZEN for the day: once set it
#                     never changes, even if the other level is touched
#                     later. Order is read from today's 1-minute candles
#                     (only fetched when today's high/low says a hit is
#                     possible); a minute that spans both TGT and SL is
#                     counted as SL first. Frozen hits are persisted
#                     (see FIRST-HIT STATE above).
# ============================================================
ENTRY_MULT = 2.0   # Entry = PDL * ENTRY_MULT
EXIT_MULT = 2.0    # TGT   = Entry * EXIT_MULT
SL_MULT = 0.5      # SL    = Entry * SL_MULT
MIN_LOW = 3.0      # Options whose PDL is below this (in rupees) are dropped entirely
def _parse_expiry_series(s):
    """Bhavcopy expiry strings -> datetime.date. Handles UDiFF
    (YYYY-MM-DD) and the older DD-Mon-YYYY layout."""
    s = s.astype(str).str.strip()
    out = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    missing = out.isna()
    if missing.any():
        out.loc[missing] = pd.to_datetime(s[missing], format="%d-%b-%Y", errors="coerce")
    missing = out.isna()
    if missing.any():
        out.loc[missing] = pd.to_datetime(s[missing], errors="coerce", dayfirst=True)
    return out.dt.date
@st.cache_data(show_spinner=False)
def _parse_bhavcopy_bytes(name, raw_bytes):
    """
    Returns (price_map, low_map, bhav_date, error).
      price_map: {symbol: underlying price}  -> strike selection
      low_map:   {(symbol, expiry_date, strike, "CE"/"PE"): LOW price}
                 -> Entry = LOW x 2
      bhav_date: trade date string found in the file (or None)
    """
    name = name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    return {}, {}, None, "No CSV found inside the uploaded zip."
                with zf.open(csv_names[0]) as f:
                    df = pd.read_csv(f)
        elif name.endswith(".gz"):
            df = pd.read_csv(io.BytesIO(raw_bytes), compression="gzip")
        else:
            df = pd.read_csv(io.BytesIO(raw_bytes))
    except Exception as e:
        return {}, {}, None, f"Could not read Bhavcopy file: {e}"
    df.columns = [str(c).strip() for c in df.columns]
    symbol_col = next((c for c in ["TckrSymb", "SYMBOL", "Symbol"] if c in df.columns), None)
    if symbol_col is None:
        return {}, {}, None, "Bhavcopy is missing a symbol column (expected TckrSymb / SYMBOL)."
    # ---------- strike-selection reference price (unchanged) ----------
    price_col = next((c for c in ["UndrlygPric", "UNDRLYPRC", "UnderlyingPrice"] if c in df.columns), None)
    if price_col is not None:
        work_df = df
    else:
        instr_col = next((c for c in ["FinInstrmTp", "INSTRUMENT"] if c in df.columns), None)
        close_col = next((c for c in ["ClsPric", "CLOSE"] if c in df.columns), None)
        if instr_col is None or close_col is None:
            return {}, {}, None, "Bhavcopy has neither UndrlygPric nor a recognizable FUT close column."
        work_df = df[df[instr_col].astype(str).str.contains("F", case=False, na=False)]
        price_col = close_col
    work_df = work_df.copy()
    work_df[price_col] = pd.to_numeric(work_df[price_col], errors="coerce")
    price_map = work_df.dropna(subset=[price_col]).groupby(symbol_col)[price_col].first().to_dict()
    if not price_map:
        return {}, {}, None, "Bhavcopy parsed but no usable prices were found in it."
    # ---------- per-option LOW price ----------
    low_col = next((c for c in ["LwPric", "LOW", "LowPric", "Low"] if c in df.columns), None)
    opt_col = next((c for c in ["OptnTp", "OPTION_TYP"] if c in df.columns), None)
    strike_col = next((c for c in ["StrkPric", "STRIKE_PR"] if c in df.columns), None)
    exp_col = next((c for c in ["XpryDt", "EXPIRY_DT", "FininstrmActlXpryDt"] if c in df.columns), None)
    if None in (low_col, opt_col, strike_col, exp_col):
        return {}, {}, None, (
            "Bhavcopy is missing option Low columns "
            "(need LwPric/LOW + OptnTp + StrkPric + XpryDt)."
        )
    opts = df[df[opt_col].astype(str).str.strip().str.upper().isin(["CE", "PE"])].copy()
    opts["_low"] = pd.to_numeric(opts[low_col], errors="coerce")
    opts["_strike"] = pd.to_numeric(opts[strike_col], errors="coerce").round(2)
    opts["_exp"] = _parse_expiry_series(opts[exp_col])
    opts["_sym"] = opts[symbol_col].astype(str).str.strip()
    opts["_ot"] = opts[opt_col].astype(str).str.strip().str.upper()
    opts = opts[opts["_low"] > 0].dropna(subset=["_strike", "_exp"])
    if opts.empty:
        return {}, {}, None, "Bhavcopy parsed but no option Low prices were found in it."
    low_map = {
        (sym, exp, float(strike), ot): float(low)
        for sym, exp, strike, ot, low in zip(
            opts["_sym"], opts["_exp"], opts["_strike"], opts["_ot"], opts["_low"]
        )
    }
    # ---------- trade date (display only) ----------
    bhav_date = None
    date_col = next((c for c in ["TradDt", "BizDt", "TIMESTAMP"] if c in df.columns), None)
    if date_col is not None:
        parsed = pd.to_datetime(df[date_col].dropna().astype(str).str.strip(), errors="coerce").dropna()
        if not parsed.empty:
            bhav_date = parsed.iloc[0].strftime("%Y-%m-%d")
    return price_map, low_map, bhav_date, None
def parse_bhavcopy(uploaded_file):
    if uploaded_file is None:
        return {}, {}, None, None
    return _parse_bhavcopy_bytes(uploaded_file.name, uploaded_file.getvalue())
def render_bhavcopy_uploader(container):
    """
    Renders the Bhavcopy uploader + status message in the given
    Streamlit container and returns (price_map, low_map, bhav_date).
    The Bhavcopy is REQUIRED: it supplies both the strike-selection
    reference price and each option's Low price (Entry = Low x 2).
    """
    container.markdown("**Bhavcopy (Strike Ref + Low) — required**")
    bhavcopy_file = container.file_uploader(
        "Upload NSE F&O Bhavcopy",
        type=["csv", "gz", "zip"],
        help=(
            "REQUIRED. (1) 'UndrlygPric' per symbol picks the nearest CE/PE "
            "strike (falls back to the FUT close on older layouts). "
            "(2) Each option's own 'LwPric' (older layouts: 'LOW') is the "
            "base for Entry = Low x 2."
        ),
        key="bhavcopy_uploader",
    )
    price_map, low_map, bhav_date, error = parse_bhavcopy(bhavcopy_file)
    if bhavcopy_file is not None:
        if error:
            container.error(f"Bhavcopy: {error}")
        else:
            container.success(
                f"Bhavcopy loaded — {len(price_map)} underlyings, {len(low_map)} option lows"
                + (f" ({bhav_date})." if bhav_date else ".")
            )
    else:
        container.warning("No Bhavcopy uploaded yet — required.")
    return price_map, low_map, bhav_date
def fetch_today_live_ohlc(instrument_keys, headers):
    """
    Today's still-forming daily candle (running high/low) plus LTP.
    One batched call across all instruments via Upstox's v3 OHLC
    endpoint, extracting high/low/last_price.
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
    return pd.DataFrame(rows, columns=["instrument_key", "today_high", "today_low", "today_ltp"])
@st.cache_data(ttl=60, show_spinner=False)
def fetch_intraday_candles(instrument_key, headers_tuple):
    """Today's 1-minute candles for one instrument, oldest -> newest.
    Returns None on any failure / empty response. Cached 60s so contracts
    that are waiting on a hit aren't re-fetched every refresh."""
    headers = dict(headers_tuple)
    url = (
        "https://api.upstox.com/v3/historical-candle/intraday/"
        f"{quote(instrument_key, safe='')}/minutes/1"
    )
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        candles = (response.json().get("data") or {}).get("candles") or []
    except Exception:
        return None
    if not candles:
        return None
    df = pd.DataFrame(
        candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
    )
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.dropna(subset=["high", "low"]).sort_values("timestamp").reset_index(drop=True)
    return df if not df.empty else None
def first_hit_from_candles(candles, entry, tgt, sl):
    """After Entry is first reached, which of TGT / SL comes first?
    Returns "TGT Hit", "SL Hit" or None (neither yet). SL is only
    checked from the candle AFTER the entry candle (order inside the
    entry minute is unknown, so a low in that minute can't be trusted as
    post-entry); a later minute spanning both TGT and SL counts as SL."""
    entered = False
    for row in candles.itertuples():
        if not entered:
            if row.high >= entry:
                entered = True
                if row.high >= tgt:
                    return "TGT Hit"
            continue
        if row.low <= sl:
            return "SL Hit"
        if row.high >= tgt:
            return "TGT Hit"
    return None
def resolve_statuses(selected, headers):
    """Status for every row of `selected`: Not Triggered / Open /
    TGT Hit / SL Hit. TGT Hit / SL Hit are FROZEN (first hit only) via
    the persisted first-hit state, keyed by instrument_key."""
    headers_tuple = tuple(headers.items())
    hits = load_first_hit_state()
    hits_changed = False
    statuses = []
    for _, row in selected.iterrows():
        key = row["option_key"]
        if key in hits:
            statuses.append(hits[key])
            continue
        entry, tgt, sl = row["Entry"], row["TGT"], row["SL"]
        hi, lo = row.get("today_high"), row.get("today_low")
        if pd.isna(entry) or pd.isna(hi) or hi < entry:
            statuses.append("Not Triggered")
            continue
        status = "Open"
        hit_tgt = hi >= tgt
        hit_sl = pd.notna(lo) and lo <= sl
        if hit_tgt or hit_sl:
            candles = fetch_intraday_candles(key, headers_tuple)
            if candles is not None:
                res = first_hit_from_candles(candles, entry, tgt, sl)
                if res:
                    status = res
            elif hit_tgt and not hit_sl:
                status = "TGT Hit"   # unambiguous even without candles
            # else: order unknown and candles unavailable -> stay Open, retry next refresh
        if status in ("TGT Hit", "SL Hit"):
            hits[key] = status
            hits_changed = True
        statuses.append(status)
    if hits_changed:
        save_first_hit_state(hits)
    return statuses
def check_and_alert(df, telegram_enabled, bot_token, chat_id):
    """
    PDL x2 scanner: alerts a symbol only on a genuine FRESH CROSSOVER
    of its Entry level — the previously-seen LTP was below Entry and the
    current LTP is at/above it. Checked fresh every refresh off the
    current quote, not off the Status column.

    Requiring an actual prev-below / now-above edge (via
    load_last_ltp_state / save_last_ltp_state) — rather than simply
    "LTP >= Entry right now" — avoids a stale-looking alert firing the
    moment Telegram gets enabled, the app restarts, or "Reset Alerts" is
    clicked while price is already sitting well past Entry. On the first
    observation of a symbol in a session/day there is no prior LTP to
    compare against, so that refresh only seeds the baseline and never
    fires an alert by itself.

    Still de-duplicated per calendar day via the persisted alert-state
    file (tagged "PDL:<symbol>", reset automatically each trading day)
    on top of the crossover check.
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
        alert_id = f"PDL:{symbol}"
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
            "🚀 <b>PDL x2 — Entry Triggered</b>\n\n"
            f"<b>{row['Symbol']}</b>\n"
            f"LTP: {row['LTP']:.2f}\n"
            f"PDL: {row['PDL']:.2f}\n"
            f"Entry: {row['Entry']:.2f}\n"
            f"TGT: {row['TGT']:.2f}  |  SL: {row['SL']:.2f}\n"
            f"Away %: {row['Away %']:.2f}%\n"
            f"Lot: {row['Lot']}  |  Cap: {row['Cap']}"
        )
        success, error = send_telegram_alert(bot_token, chat_id, message)
        if success:
            alerted.add(alert_id)
            save_trigger_alert_state(alerted)
            log_alert_event("PDL", row['Symbol'], row['LTP'], row['Entry'], tgt=row['TGT'], sl=row['SL'])
            sent_count += 1
        else:
            fail_count += 1
    if sent_count:
        st.toast(f"Telegram alert sent for {sent_count} entry trigger(s).", icon="🚀")
    if fail_count:
        st.toast(f"{fail_count} alert(s) failed — will retry next refresh.", icon="⚠️")
def build_scanner(access_token, expiry_choice, bhavcopy_price_map, bhavcopy_low_map):
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
    if not bhavcopy_price_map or not bhavcopy_low_map:
        st.error("Upload an NSE F&O Bhavcopy in the sidebar first — strike selection and the Low x2 Entry both come from it.")
        return pd.DataFrame(), pd.DataFrame()
    # Strike selection reference price: uploaded Bhavcopy only.
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
    # ---- PDL per contract: (symbol, expiry, strike, CE/PE) ----
    low_keys = [
        (str(sym).strip(), expiry, float(round(float(strike), 2)), str(ot).strip().upper())
        for sym, strike, ot in zip(
            selected["underlying_symbol"], selected["strike"], selected["option_type"]
        )
    ]
    selected["PDL"] = pd.to_numeric(
        pd.Series([bhavcopy_low_map.get(k) for k in low_keys], index=selected.index),
        errors="coerce"
    )
    missing_count = int(selected["PDL"].isna().sum())
    total_count = len(selected)
    if missing_count > 0:
        with st.expander(
            f"⚠️ PDL (Bhavcopy Low) not found for {missing_count}/{total_count} options (hidden from table below)",
            expanded=(missing_count == total_count)
        ):
            st.write(
                f"No matching row (symbol + expiry {expiry} + strike + CE/PE) with a Low > 0 "
                "in the uploaded Bhavcopy — contract may not have traded, or the file is for a different expiry."
            )
            st.write(", ".join(selected.loc[selected["PDL"].isna(), "Symbol"].tolist()[:50]))
    # Drop missing + penny lows BEFORE hitting the live quote API.
    selected = selected[selected["PDL"] >= MIN_LOW].reset_index(drop=True)
    if selected.empty:
        return pd.DataFrame(), pd.DataFrame()
    selected["Entry"] = selected["PDL"] * ENTRY_MULT
    selected["TGT"] = selected["Entry"] * EXIT_MULT
    selected["SL"] = selected["Entry"] * SL_MULT
    live_ohlc = fetch_today_live_ohlc(selected["option_key"].tolist(), headers)
    selected = selected.merge(live_ohlc, left_on="option_key", right_on="instrument_key", how="left")
    selected["Status"] = resolve_statuses(selected, headers)
    selected["LTP"] = pd.to_numeric(selected["today_ltp"], errors="coerce")
    selected["Away %"] = np.where(
        selected["Entry"] > 0,
        (selected["LTP"] / selected["Entry"]) * 100,
        np.nan
    )
    selected["Away %"] = selected["Away %"].clip(lower=0)
    selected["Cap"] = selected["LTP"] * selected["Lot"]
    # Lot and Cap stay in `result` for the Telegram message + alert log
    # even though they are not shown in the table (DISPLAY_COLS below).
    result = selected[[
        "Symbol", "LTP", "PDL", "Entry", "Away %", "TGT", "SL",
        "Status", "Lot", "Cap"
    ]].copy()
    for col in ["LTP", "PDL", "Entry", "Away %", "TGT", "SL"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)
    result["Lot"] = pd.to_numeric(result["Lot"], errors="coerce").fillna(0).astype(int)
    result["Cap"] = pd.to_numeric(result["Cap"], errors="coerce").round(0).fillna(0).astype(int)
    # Only contracts whose Entry has actually been reached today.
    result = result[result["Status"] != "Not Triggered"].reset_index(drop=True)
    ce_table = result[result["Symbol"].str.endswith("CE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)
    pe_table = result[result["Symbol"].str.endswith("PE")].sort_values("Away %", ascending=False, na_position="last").reset_index(drop=True)
    return ce_table, pe_table
DECIMAL_COLS = {
    "LTP": "{:.2f}",
    "PDL": "{:.2f}",
    "Entry": "{:.2f}",
    "Away %": "{:.2f}%",
    "TGT": "{:.2f}",
    "SL": "{:.2f}",
}
# Lot and Cap are kept out of the display (still on the DataFrame for
# the Telegram alert / log).
DISPLAY_COLS = ["Symbol", "LTP", "PDL", "Entry", "Away %", "TGT", "SL", "Status"]
def style_status(value):
    if value == "TGT Hit":
        return "background-color: darkgreen; color: white; font-weight: bold;"
    if value == "SL Hit":
        return "background-color: #B71C1C; color: white; font-weight: bold;"
    return ""
CE_TINTS = {
    "Entry": {"background-color": "#E3F2FD", "color": "#0D47A1", "font-weight": "600"},
}
PE_TINTS = {
    "Entry": {"background-color": "#EDE7F6", "color": "#4527A0", "font-weight": "600"},
}
def show_side_by_side(ce_table, pe_table):
    last_updated = get_ist_now().strftime("%H:%M:%S")
    st.caption(f"Last Updated: {last_updated} IST")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Calls (CE)**")
        if ce_table.empty:
            st.info("No CE data available.")
        else:
            ce_style = (
                ce_table[DISPLAY_COLS].style
                .map(style_away_percent, subset=["Away %"])
                .map(style_status, subset=["Status"])
                .pipe(apply_column_tints, CE_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
            )
            st.dataframe(ce_style, width="stretch", hide_index=True, height=table_height(ce_table))
    with col2:
        st.markdown("**Puts (PE)**")
        if pe_table.empty:
            st.info("No PE data available.")
        else:
            pe_style = (
                pe_table[DISPLAY_COLS].style
                .map(style_away_percent, subset=["Away %"])
                .map(style_status, subset=["Status"])
                .pipe(apply_column_tints, PE_TINTS)
                .format(DECIMAL_COLS, na_rep="-")
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
    telegram_bot_token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    telegram_enabled = bool(telegram_bot_token and telegram_chat_id)
    # Sidebar is hidden in client view, but the Bhavcopy upload is
    # required, so it is rendered in the main body above the title.
    bhavcopy_price_map, bhavcopy_low_map, bhavcopy_date = render_bhavcopy_uploader(st)
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
        st.header("Bhavcopy")
        bhavcopy_price_map, bhavcopy_low_map, bhavcopy_date = render_bhavcopy_uploader(st.sidebar)
        st.markdown("---")
        st.header("Telegram Alerts")
        telegram_enabled = st.checkbox(
            "Enable Trigger Alerts",
            value=st.session_state.get("telegram_enabled", False),
            key="telegram_enabled",
            help="Sends a Telegram message the moment an option's PDL x2 Entry level is crossed for the first time today."
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
                "✅ Test alert from PDL x2 Scanner — Telegram is wired up correctly."
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
#   PDL x2: Entry = Bhavcopy option LOW x2.0,
#                TGT = Entry x2.0, SL = Entry x0.5
# ============================================================
st.title("PDL x2 Scanner")
run_every = refresh_interval if auto_refresh else None
if not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar first.")
else:
    st.header("PDL x2 Breakout (Live)")
    st.caption(
        f"Entry = PDL x{ENTRY_MULT:g}  |  TGT = Entry x{EXIT_MULT:g}  |  SL = Entry x{SL_MULT:g}"
        + (f"  |  Bhavcopy date: {bhavcopy_date}" if bhavcopy_date else "")
    )
    @st.fragment(run_every=run_every)
    def show_scanner():
        ce_table, pe_table = build_scanner(
            access_token, expiry_type, bhavcopy_price_map, bhavcopy_low_map
        )
        if not ce_table.empty or not pe_table.empty:
            if telegram_enabled:
                combined = pd.concat([ce_table, pe_table], ignore_index=True)
                check_and_alert(combined, telegram_enabled, telegram_bot_token, telegram_chat_id)
            show_side_by_side(ce_table, pe_table)
        else:
            st.info("No triggered entries yet — waiting for market data or a breakout above Entry.")
    show_scanner()
