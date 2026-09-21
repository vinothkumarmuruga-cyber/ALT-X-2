import streamlit as st
import pandas as pd
import numpy as np
import requests
import os
import io
import time
import zipfile
import json
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
# Local disk is NOT reliable on Streamlit Cloud (wiped on restart /
# redeploy / inactivity). Add these secrets to back up the token and
# alert state to a private Gist:
#
#   GITHUB_GIST_TOKEN = "ghp_xxx..."   # PAT with the "gist" scope
#   GITHUB_GIST_ID    = "abcdef123..."  # id of an existing (secret) gist
#
# Without them, everything falls back to local-disk-only.
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
# TOKEN
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
    if USE_GIST_PERSISTENCE:
        raw = _gist_read_file("token.json")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today_str:
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
# TELEGRAM ALERT DE-DUP STATE (per trading day)
# Each entry is "PDL:<contract>".
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
# LAST-SEEN LTP STATE — baseline for detecting a genuine Trigger
# CROSSOVER (previous LTP below Trigger, current LTP at/above it).
# Each entry is "<contract>": <ltp>. Reset each trading day. Gist writes
# are throttled (see check_and_alert) since this covers every contract.
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
def save_last_ltp_state(ltp_map, gist=True):
    data = {
        "date": get_ist_now().strftime("%Y-%m-%d"),
        "ltp": ltp_map
    }
    try:
        with open(LAST_LTP_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass
    if gist and USE_GIST_PERSISTENCE:
        _gist_write_file("last_ltp_state.json", json.dumps(data))
# ============================================================
# ALERT LOG (CSV) — one row per fired alert.
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
    """Nearest strike to ref_price (Bhavcopy underlying price)."""
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
def style_change_percent(value):
    try:
        value = float(value)
        if value >= 100:
            return "background-color: darkgreen; color: white; font-weight: bold;"
        elif value >= 90:
            return "background-color: lightgreen; color: black; font-weight: bold;"
    except Exception:
        pass
    return ""
# ============================================================
# PDL x2 SCANNER  (PDL = Previous Day Low)
#
#   PDL     = the option contract's own LOW from the uploaded NSE F&O
#             Bhavcopy (UDiFF "LwPric"; older layouts "LOW"), matched per
#             contract on (symbol, expiry, strike, CE/PE).
#   Trigger = PDL x ENTRY_MULT (2.0)
#   Change% = LTP / Trigger x 100   (>= 100 means LTP is at/above Trigger)
#
#   Every scanned contract is shown (sorted by Change %), except:
#     - no PDL in the Bhavcopy
#     - PDL below MIN_LOW
#     - contracts whose Trigger was ALREADY reached on the PDL day itself
#       (that day's HIGH >= Trigger) — removed silently
#
#   Telegram alert = LTP crosses Trigger (previous refresh below, this
#   refresh at/above). Once per contract per day.
# ============================================================
ENTRY_MULT = 2.0   # Trigger = PDL * ENTRY_MULT
MIN_LOW = 3.0      # PDL below this (rupees) is dropped. Set to 0 to keep penny options.
def _parse_expiry_series(s):
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
      low_map:   {(symbol, expiry_date, strike, "CE"/"PE"): (LOW, HIGH)}
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
    low_col = next((c for c in ["LwPric", "LOW", "LowPric", "Low"] if c in df.columns), None)
    high_col = next((c for c in ["HghPric", "HIGH", "HighPric", "High"] if c in df.columns), None)
    opt_col = next((c for c in ["OptnTp", "OPTION_TYP"] if c in df.columns), None)
    strike_col = next((c for c in ["StrkPric", "STRIKE_PR"] if c in df.columns), None)
    exp_col = next((c for c in ["XpryDt", "EXPIRY_DT", "FininstrmActlXpryDt"] if c in df.columns), None)
    if None in (low_col, high_col, opt_col, strike_col, exp_col):
        return {}, {}, None, (
            "Bhavcopy is missing option Low/High columns "
            "(need LwPric/LOW + HghPric/HIGH + OptnTp + StrkPric + XpryDt)."
        )
    opts = df[df[opt_col].astype(str).str.strip().str.upper().isin(["CE", "PE"])].copy()
    opts["_low"] = pd.to_numeric(opts[low_col], errors="coerce")
    opts["_high"] = pd.to_numeric(opts[high_col], errors="coerce")
    opts["_strike"] = pd.to_numeric(opts[strike_col], errors="coerce").round(2)
    opts["_exp"] = _parse_expiry_series(opts[exp_col])
    opts["_sym"] = opts[symbol_col].astype(str).str.strip()
    opts["_ot"] = opts[opt_col].astype(str).str.strip().str.upper()
    opts = opts[opts["_low"] > 0].dropna(subset=["_strike", "_exp"])
    if opts.empty:
        return {}, {}, None, "Bhavcopy parsed but no option Low prices were found in it."
    low_map = {
        (sym, exp, float(strike), ot): (float(low), float(high) if pd.notna(high) else float("nan"))
        for sym, exp, strike, ot, low, high in zip(
            opts["_sym"], opts["_exp"], opts["_strike"], opts["_ot"], opts["_low"], opts["_high"]
        )
    }
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
    """Bhavcopy uploader + status. Returns (price_map, low_map, bhav_date)."""
    container.markdown("**Bhavcopy (Strike Ref + Low) — required**")
    bhavcopy_file = container.file_uploader(
        "Upload NSE F&O Bhavcopy",
        type=["csv", "gz", "zip"],
        help=(
            "REQUIRED. (1) 'UndrlygPric' per symbol picks the nearest CE/PE "
            "strike (falls back to the FUT close on older layouts). "
            "(2) Each option's own 'LwPric' (older layouts: 'LOW') is the "
            "base for Trigger = Low x 2."
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
def fetch_live_ltp(instrument_keys, headers):
    """LTP for all instruments via Upstox's v3 OHLC endpoint, batched."""
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
            true_key = item.get("instrument_token") or response_key
            rows.append({
                "instrument_key": true_key,
                "today_ltp": item.get("last_price"),
            })
    return pd.DataFrame(rows, columns=["instrument_key", "today_ltp"])
def check_and_alert(df, telegram_enabled, bot_token, chat_id):
    """
    Alerts a contract only on a genuine FRESH CROSSOVER of its Trigger —
    previously-seen LTP below Trigger, current LTP at/above it.

    `df` is EVERY scanned contract. The first observation of a contract in
    a day only seeds the baseline and never fires by itself, so enabling
    Telegram / restarting / "Reset Alerts" while price is already above
    Trigger does not send a stale alert. De-duplicated per day.
    """
    if not telegram_enabled or df.empty:
        return
    alerted = load_trigger_alert_state()
    last_ltp = load_last_ltp_state()
    newly_triggered = []
    ltp_state_changed = False
    for _, row in df.iterrows():
        contract = row.get("Contract")
        if not contract:
            continue
        ltp, trigger = row.get("LTP"), row.get("Trigger")
        if pd.isna(ltp) or pd.isna(trigger):
            continue
        prev_ltp = last_ltp.get(contract)
        if prev_ltp != ltp:
            last_ltp[contract] = float(ltp)
            ltp_state_changed = True
        if prev_ltp is None or prev_ltp >= trigger:
            continue  # no prior below-Trigger baseline to cross FROM
        if ltp < trigger:
            continue  # hasn't crossed yet
        alert_id = f"PDL:{contract}"
        if alert_id not in alerted:
            newly_triggered.append((alert_id, row))
    if ltp_state_changed:
        # Local write every refresh; Gist at most once a minute (or right
        # when an alert is about to go out).
        now_ts = time.time()
        gist_due = bool(newly_triggered) or (now_ts - st.session_state.get("_ltp_gist_ts", 0) >= 60)
        if gist_due:
            st.session_state["_ltp_gist_ts"] = now_ts
        save_last_ltp_state(last_ltp, gist=gist_due)
    if not newly_triggered:
        return
    sent_count = 0
    fail_count = 0
    for alert_id, row in newly_triggered:
        message = (
            "🚀 <b>PDL x2 — Trigger Crossed</b>\n\n"
            f"<b>{row['Contract']}</b>\n"
            f"LTP: {row['LTP']:.2f}\n"
            f"PDL: {row['PDL']:.2f}\n"
            f"Trigger: {row['Trigger']:.2f}\n"
            f"Change %: {row['Change %']:.2f}%\n"
            f"Lot: {row['Lot']}  |  Cap: {row['Cap']}"
        )
        success, error = send_telegram_alert(bot_token, chat_id, message)
        if success:
            alerted.add(alert_id)
            save_trigger_alert_state(alerted)
            log_alert_event("PDL", row['Contract'], row['LTP'], row['Trigger'])
            sent_count += 1
        else:
            fail_count += 1
    if sent_count:
        st.toast(f"Telegram alert sent for {sent_count} trigger(s).", icon="🚀")
    if fail_count:
        st.toast(f"{fail_count} alert(s) failed — will retry next refresh.", icon="⚠️")
def build_scanner(access_token, expiry_choice, bhavcopy_price_map, bhavcopy_low_map):
    """Returns (ce_table, pe_table, all_df). Tables hold every scanned
    contract sorted by Change % (desc); all_df is used for alerts."""
    empty = pd.DataFrame()
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    futures, options = load_live_fo_instruments()
    expiry = get_expiry_for_choice(futures, expiry_choice)
    if expiry is None:
        st.error("No futures expiry found")
        return empty, empty, empty
    futures = futures[futures["expiry_date"] == expiry].copy()
    options = options[options["expiry_date"] == expiry].copy()
    if not bhavcopy_price_map or not bhavcopy_low_map:
        st.error("Upload an NSE F&O Bhavcopy in the sidebar first — strike selection and the Low x2 Trigger both come from it.")
        return empty, empty, empty
    futures["future_open"] = futures["underlying_symbol"].map(bhavcopy_price_map)
    dropped = futures[futures["future_open"].isna()]["underlying_symbol"].unique().tolist()
    futures = futures.dropna(subset=["future_open"])
    if futures.empty:
        st.error("None of this expiry's underlyings were found in the uploaded Bhavcopy.")
        return empty, empty, empty
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
        return empty, empty, empty
    selected["Contract"] = [
        f"{u} {float(s):g} {t}"
        for u, s, t in zip(selected["underlying_symbol"], selected["strike"], selected["option_type"])
    ]
    # ---- PDL per contract: (symbol, expiry, strike, CE/PE) ----
    low_keys = [
        (str(sym).strip(), expiry, float(round(float(strike), 2)), str(ot).strip().upper())
        for sym, strike, ot in zip(
            selected["underlying_symbol"], selected["strike"], selected["option_type"]
        )
    ]
    _lh = [bhavcopy_low_map.get(k, (None, None)) for k in low_keys]
    selected["PDL"] = pd.to_numeric(
        pd.Series([x[0] for x in _lh], index=selected.index), errors="coerce"
    )
    selected["PDL High"] = pd.to_numeric(
        pd.Series([x[1] for x in _lh], index=selected.index), errors="coerce"
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
            st.write(", ".join(selected.loc[selected["PDL"].isna(), "Contract"].tolist()[:50]))
    # Drop missing + penny lows BEFORE hitting the live quote API.
    selected = selected[selected["PDL"] >= MIN_LOW].reset_index(drop=True)
    if selected.empty:
        return empty, empty, empty
    selected["Trigger"] = selected["PDL"] * ENTRY_MULT
    # Trigger already reached on the PDL day itself (that day's HIGH >=
    # Trigger) -> remove the strike (silently).
    same_day = selected["PDL High"] >= selected["Trigger"]
    selected = selected[~same_day].reset_index(drop=True)
    if selected.empty:
        return empty, empty, empty
    live = fetch_live_ltp(selected["option_key"].tolist(), headers)
    selected = selected.merge(live, left_on="option_key", right_on="instrument_key", how="left")
    selected["LTP"] = pd.to_numeric(selected["today_ltp"], errors="coerce")
    if selected["LTP"].isna().all():
        st.warning("No live quotes returned — check the Upstox access token / market hours.")
    selected["Change %"] = np.where(
        selected["Trigger"] > 0,
        (selected["LTP"] / selected["Trigger"]) * 100,
        np.nan
    )
    selected["Change %"] = selected["Change %"].clip(lower=0)
    selected["Cap"] = selected["LTP"] * selected["Lot"]
    result = pd.DataFrame({
        "Contract": selected["Contract"],
        "Symbol": selected["underlying_symbol"],
        "Strike": pd.to_numeric(selected["strike"], errors="coerce"),
        "Type": selected["option_type"],
        "PDL": selected["PDL"],
        "Trigger": selected["Trigger"],
        "LTP": selected["LTP"],
        "Change %": selected["Change %"],
        "Lot": pd.to_numeric(selected["Lot"], errors="coerce").fillna(0).astype(int),
        "Cap": pd.to_numeric(selected["Cap"], errors="coerce").round(0).fillna(0).astype(int),
    })
    for col in ["PDL", "Trigger", "LTP", "Change %"]:
        result[col] = pd.to_numeric(result[col], errors="coerce").round(2)
    ce_table = result[result["Type"] == "CE"].sort_values("Change %", ascending=False, na_position="last").reset_index(drop=True)
    pe_table = result[result["Type"] == "PE"].sort_values("Change %", ascending=False, na_position="last").reset_index(drop=True)
    return ce_table, pe_table, result
DECIMAL_COLS = {
    "Strike": "{:.2f}",
    "Trigger": "{:.2f}",
    "LTP": "{:.2f}",
    "Change %": "{:.2f}%",
}
DISPLAY_COLS = ["Symbol", "Strike", "Trigger", "LTP", "Change %"]
def show_side_by_side(ce_table, pe_table):
    last_updated = get_ist_now().strftime("%H:%M:%S")
    st.caption(f"Last Updated: {last_updated} IST")
    col1, col2 = st.columns(2)
    for col, title, table in ((col1, "Calls (CE)", ce_table), (col2, "Puts (PE)", pe_table)):
        with col:
            st.markdown(f"**{title}**")
            if table.empty:
                st.info(f"No {title[-3:-1]} data available.")
            else:
                styled = (
                    table[DISPLAY_COLS].style
                    .map(style_change_percent, subset=["Change %"])
                    .format(DECIMAL_COLS, na_rep="-")
                )
                st.dataframe(styled, width="stretch", hide_index=True, height=table_height(table))
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
            help="Sends a Telegram message the moment an option's LTP crosses its PDL x2 Trigger."
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
            st.success("Alert state cleared — options will alert again on their next fresh Trigger crossover (not instantly, even if already above Trigger).")
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
# MAIN PAGE
# ============================================================
st.title("PDL x2 Scanner")
run_every = refresh_interval if auto_refresh else None
if not access_token:
    st.warning("Enter your Upstox Access Token in the sidebar first.")
else:
    st.header("PDL x2 Breakout (Live)")
    st.caption(
        f"Trigger = PDL x{ENTRY_MULT:g}  |  Change % = LTP / Trigger"
        + (f"  |  Bhavcopy date: {bhavcopy_date}" if bhavcopy_date else "")
    )
    @st.fragment(run_every=run_every)
    def show_scanner():
        ce_table, pe_table, all_df = build_scanner(
            access_token, expiry_type, bhavcopy_price_map, bhavcopy_low_map
        )
        if telegram_enabled and not all_df.empty:
            check_and_alert(all_df, telegram_enabled, telegram_bot_token, telegram_chat_id)
        if not ce_table.empty or not pe_table.empty:
            show_side_by_side(ce_table, pe_table)
        else:
            st.info("No data — waiting for market data or Bhavcopy.")
    show_scanner()
