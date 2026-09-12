import ssl
ssl._create_default_https_context = ssl.create_default_context
import certifi
import os
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from api_client import (call_parse, call_build_bid, call_poll_push,
                         call_set_thread_learning, call_get_thread_learning_status,
                         call_backfill_thread,
                         call_get_telegram_status, call_set_telegram_enabled)

import sys
import io

# Force UTF-8 output encoding for compiled EXE on Windows
if sys.stdout is not None:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr is not None:
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass
import ctypes
import time
import re
from typing import Optional
import html as html_lib
import base64
import json
import requests
import threading
import tkinter as tk
from tkinter import messagebox
from urllib.parse import quote
from email.mime.text import MIMEText
from email.utils import parseaddr
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import webbrowser
import pyperclip
from concurrent.futures import ThreadPoolExecutor
import queue as _queue
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from PIL import Image, ImageTk

# Pillow >=9.1 moved LANCZOS under Image.Resampling; Image.LANCZOS is kept
# for runtime backwards-compat but modern type stubs only declare the new
# location, hence the AttributeError fallback (never actually triggers on
# Pillow >=9.1, only kept for older installs).
try:
    _RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    _RESAMPLE_LANCZOS = Image.LANCZOS  # type: ignore[attr-defined]
import logging
from logging.handlers import RotatingFileHandler

from activation_screen import run_activation_gate
from api_client import call_parse, call_build_bid, call_record_bid, call_classify_reply
from license_manager import get_machine_id

# Driver bot (2026-09-11) — optional, self-contained module; its own
# import failing (e.g. a missing dependency) must never take down the
# whole dispatcher app, so it's wrapped exactly per its own documented
# integration contract (see the top of driver_bot.py).
try:
    import driver_bot
except Exception as _e:
    driver_bot = None
    print(f"driver_bot import failed (driver bot disabled): {_e}")

# Cache machine_id once — it never changes during a session
_MACHINE_ID: str = ""

def _get_machine_id() -> str:
    global _MACHINE_ID
    if not _MACHINE_ID:
        _MACHINE_ID = get_machine_id()
    return _MACHINE_ID

# =============================================================
# CONFIGURATION
# =============================================================
def _get_exe_dir() -> str:
    # sys.argv[0] always points to the real EXE location
    # even in Nuitka onefile (unlike __file__ which points to temp)
    path = os.path.abspath(sys.argv[0])
    if os.path.isfile(path):
        return os.path.dirname(path)
    return os.path.dirname(os.path.abspath(__file__))

_EXE_DIR = _get_exe_dir()

# =============================================================
# FILE LOGGING SYSTEM
# =============================================================

LOG_FILE = os.path.join(_EXE_DIR, "plutus_bot.log")

def _setup_file_logger() -> logging.Logger:
    logger = logging.getLogger("plutus")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    try:
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
    except Exception as e:
        print(f"[LOG] Could not create log file: {e}")
    return logger

_file_logger = _setup_file_logger()

def _flog(level: str, msg: str):
    """Write to file log. level: DEBUG, INFO, WARNING, ERROR, CRITICAL"""
    try:
        getattr(_file_logger, level.lower())(msg)
    except Exception:
        pass
# =============================================================
# STARTUP VALIDATION — check required files exist
# =============================================================

def _validate_startup_files() -> list:
    """Returns list of missing/invalid files that will cause failures."""
    issues = []
    
    # credentials.json — must exist and be valid JSON with client_id
    cred_path = os.path.join(_EXE_DIR, "credentials.json")
    if not os.path.exists(cred_path):
        issues.append(
            f"credentials.json not found.\n"
            f"Expected at: {cred_path}\n\n"
            f"Download it from Google Cloud Console:\n"
            f"console.cloud.google.com → APIs → OAuth 2.0 Credentials"
        )
    else:
        try:
            with open(cred_path, "r") as f:
                cred_data = json.load(f)
            if "installed" not in cred_data and "web" not in cred_data:
                issues.append(
                    "credentials.json is invalid — missing 'installed' or 'web' key.\n"
                    "Re-download from Google Cloud Console."
                )
        except Exception as e:
            issues.append(f"credentials.json is corrupted: {e}")

    # token.json — optional, will be created on first auth
    # license_cache.json — optional, created by activation screen
    
    return issues

LOGO_PATH       = "assets/plutus_logo.ico"
LOGO_DARK_PATH  = "assets/plutus_logo_dark.jpg"
LOGO_LIGHT_PATH = "assets/plutus_logo_light.jpg"
PREFS_FILE    = os.path.join(_EXE_DIR, "plutus_prefs.json")
# Add near other globals
MC_NUMBER = "12345678"

def _resolve_logo_path(configured_path: str, fallback_name: str) -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isabs(configured_path) and os.path.exists(configured_path):
        return configured_path
    local = os.path.join(script_dir, configured_path)
    if os.path.exists(local):
        return local
    assets = os.path.join(script_dir, "assets", fallback_name)
    if os.path.exists(assets):
        return assets
    direct = os.path.join(script_dir, fallback_name)
    if os.path.exists(direct):
        return direct
    return configured_path

# Bumped on every rebuild handed to the client — shown in the window
# title bar and the very first log line. Added 2026-09-12 after a real
# case of "I installed the latest version but the old bug is still
# there" that traced to a stale exe somewhere in the hand-off, not an
# actual code issue (confirmed via direct code search + a fresh
# launch test, twice) — this makes "which build is this, really"
# instantly checkable without any back-and-forth investigation.
BUILD_VERSION          = "2026-09-12a"

BOT_TOKEN              = "8157082619:AAHqoxicji5_awWjDmd1Ia7FGxpgp2R6Vkc"
# Driver bot (2026-09-11) — a SEPARATE Telegram bot from BOT_TOKEN above,
# so driver bid replies never mix into the dispatcher's own chat. Same
# hardcoded-constant pattern as BOT_TOKEN itself (not a GUI field).
DRIVER_BOT_TOKEN       = "8371628317:AAEEcVu0jnpKu9Zni3meYMSlcpRaanv1VhE"
TELEGRAM_UPDATE_OFFSET = 0
TELEGRAM_OFFSET_LOCK   = threading.Lock()

CHAT_IDS: list = [0000]
_CHAT_IDS_LOCK = threading.Lock()

# Cached "should we actually send to Telegram right now" flag — checked
# by every real send call site (_telegram_send_one and friends),
# refreshed on launch and via the header toggle button, never by hitting
# the server on every single message (too slow, too much load for
# something that rarely changes). Defaults True: fails OPEN on a network
# hiccup or before the first refresh completes — a dispatcher missing a
# real load notification because of a transient status-check failure
# would be worse than the rare case of one extra message sent while
# toggling off. Only an explicit, successful "disabled" response ever
# sets this False.
_TELEGRAM_ENABLED = True

# Find the EXE's real directory

CREDENTIALS_PATH = os.path.join(_EXE_DIR, "credentials.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

FRESH_WINDOW      = "2d"
STOP_EVENT        = threading.Event()
BOT_THREAD        = None
TRUCKS            = []
_DRIVER_BOT_ENABLED = False   # set by _init_driver_bot(), read in _process_email
LOAD_STORE        = {}
LOAD_STORE_LOCK   = threading.Lock()
BID_TEMPLATE_LOCK = threading.Lock()
BID_TEMPLATE      = """Rate: $
{vehicle_type}
Dims: {truck_dimensions}
MC#

Truck is {google_deadhead} miles out
{truck_equipment}

ETA to PU: {deadhead_eta_str}

ALL BIDS ARE VALID 15 MIN"""

ACTIVE_LICENSE_KEY = None

_URL_STORE: dict = {}
_URL_STORE_LOCK  = threading.Lock()
_url_counter     = 0

session = requests.Session()
_http_retry = Retry(
    total=4, backoff_factor=0.6,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
session.mount("https://", HTTPAdapter(max_retries=_http_retry))
session.mount("http://",  HTTPAdapter(max_retries=_http_retry))

# =============================================================
# US STATES — kept locally ONLY for subject-line pre-filtering
# (no parsing logic — that's all in parser_core.py on server)
# =============================================================

_US_STATES_SET = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

FREIGHT_MARKERS = [
    "BID ON ORDER", "REQUEST FOR QUOTE", "POSTED LOAD",
    "LARGE STRAIGHT", "SMALL STRAIGHT", "CARGO VAN", "SPRINTER",
    "TRACTOR", "BOX TRUCK", "STRAIGHT TRUCK", "FLATBED", "REEFER",
    "HOT SHOT", "POWER ONLY", "STEP DECK", "LOWBOY", "CUBE VAN",
    "EXPEDITED LOAD", "EXPEDITED TRUCK",
]

_SYSIDS = frozenset({
    "INBOX","UNREAD","SENT","IMPORTANT","STARRED","TRASH","SPAM","DRAFT",
    "CATEGORY_FORUMS","CATEGORY_UPDATES","CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL","CATEGORY_PERSONAL",
})

# =============================================================
# TRUCK CONFIG PARSING — local only, sent to server as JSON data
# No matching/routing logic here — all in parser_core.py on server
# =============================================================

def parse_weight_lbs(weight_text):
    if not weight_text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", weight_text.replace(" ", ""))
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None

def parse_height_from_dims(dims: str):
    if not dims:
        return None
    parts = re.split(r"\s*[xX]\s*", dims.strip())
    if len(parts) >= 3:
        m = re.search(r"\d+", parts[2])
        if m:
            try:
                return int(m.group())
            except ValueError:
                pass
    return None

def expand_states(raw: str):
    REGION_MAP = {
        "WEST COAST": {"AZ","CA","CO","ID","MT","NV","NM","OR","TX","UT","WA","WY"},
        "MIDWEST":    {"IL","IN","IA","KS","KY","MI","MN","MO","NE","ND","OH","SD","TN","WI"},
        "EAST COAST": {"CT","DE","FL","GA","ME","MD","MA","NH","NJ","NY","NC","PA","RI","SC","VT","VA"},
    }
    if not raw or not raw.strip():
        return None
    result = set()
    for token in raw.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token in REGION_MAP:
            result |= REGION_MAP[token]
            continue
        if len(token) > 2:
            matched = False
            for rname, states in REGION_MAP.items():
                if token in rname:
                    result |= states
                    matched = True
                    break
            if matched:
                continue
        if token in _US_STATES_SET:
            result.add(token)
    return result if result else None

def parse_truck_definitions(text):
    trucks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            continue
        vehicle      = parts[0]
        driver       = parts[1]
        dims         = parts[2]
        payload_text = parts[3]
        equipment    = parts[4] if len(parts) > 4 else ""
        states_raw   = parts[5] if len(parts) > 5 else ""
        zip_loc      = parts[6] if len(parts) > 6 else ""
        date         = parts[7].upper() if len(parts) > 7 else ""
        # RADIUS (2026-09-11) — optional 9th field, per-vehicle override
        # of the global Max radius setting. Blank/absent = use the
        # global default, same "trailing optional field" pattern as
        # EQUIPMENT/STATES/ZIP/DATE above.
        radius_raw   = parts[8] if len(parts) > 8 else ""
        radius_miles = parse_weight_lbs(radius_raw) if radius_raw.strip() else None
        # CHAT_ID (2026-09-11) — optional 10th field, this driver's own
        # Telegram chat ID with the (separate) driver bot. Set = the
        # driver bot sends them load cards and forwards their bids to
        # the dispatcher chat; blank = driver bot skips them entirely.
        # Uses int(), not parse_weight_lbs(), because a Telegram GROUP
        # chat ID is negative (e.g. -1001234567890) — parse_weight_lbs'
        # digits-only regex would silently strip the sign.
        chatid_raw = parts[9] if len(parts) > 9 else ""
        telegram_chat_id = None
        if chatid_raw.strip():
            try:
                telegram_chat_id = int(chatid_raw.strip())
            except ValueError:
                telegram_chat_id = None
        truck_states = expand_states(states_raw) if states_raw.strip() else None
        trucks.append({
            "vehicle":         vehicle.upper(),
            "zip":             zip_loc,
            "driver_name":     driver,
            "dimensions":      dims,
            "max_payload_lbs": parse_weight_lbs(payload_text),
            "max_height_in":   parse_height_from_dims(dims),
            "pickup_date":     date,
            "allowed_states":  truck_states,
            "equipment":       equipment,
            "radius_miles":    radius_miles,
            "telegram_chat_id": telegram_chat_id,
        })
    return trucks

def validate_truck_definitions(text):
    errors = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            errors.append(
                f"Line {i}: need VEHICLE:DRIVER:DIMS:PAYLOAD "
                f"(got {len(parts)} field{'s' if len(parts) != 1 else ''})"
            )
            continue
        if not parts[0]:
            errors.append(f"Line {i}: vehicle type is empty")
        if not parts[1]:
            errors.append(f"Line {i}: driver name is empty")
        if parse_weight_lbs(parts[3]) is None:
            errors.append(f"Line {i}: cannot parse payload '{parts[3]}' as a number")
        if len(parts) > 5 and parts[5].strip():
            if not expand_states(parts[5]):
                errors.append(
                    f"Line {i}: cannot expand '{parts[5]}' — "
                    f"use state codes (OH,PA) or region names (East Coast, Midwest, West Coast)"
                )
        if len(parts) > 7 and parts[7].strip():
            valid = False
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    datetime.strptime(parts[7].strip(), fmt)
                    valid = True
                    break
                except ValueError:
                    pass
            if not valid:
                errors.append(f"Line {i}: date '{parts[7]}' must be MM/DD/YYYY or MM/DD/YY")
        if len(parts) > 8 and parts[8].strip():
            if parse_weight_lbs(parts[8]) is None:
                errors.append(f"Line {i}: cannot parse radius '{parts[8]}' as a number")
        if len(parts) > 9 and parts[9].strip():
            try:
                int(parts[9].strip())
            except ValueError:
                errors.append(f"Line {i}: chat ID '{parts[9]}' must be a whole number "
                               f"(get it from @userinfobot on Telegram)")
    return errors

# =============================================================
# URL STORE
# =============================================================

def _store_url(url: str) -> str:
    global _url_counter
    with _URL_STORE_LOCK:
        _url_counter += 1
        key = str(_url_counter)
        if len(_URL_STORE) >= 200:
            del _URL_STORE[next(iter(_URL_STORE))]
        _URL_STORE[key] = url
    return key

def _retrieve_url(key: str) -> str:
    with _URL_STORE_LOCK:
        return _URL_STORE.get(key, "")

# =============================================================
# TELEGRAM — identical to original
# =============================================================

def _telegram_send_one(chat_id: int, payload: dict) -> bool:
    """Returns whether the send actually succeeded — 2026-09-08: the
    caller in _process_email was logging "sent to Telegram" in the GUI
    BEFORE this even ran, so a real failure here (bad chat ID, bot
    never messaged first, network issue) was invisible — the GUI showed
    success regardless, and the actual error only ever reached a bare
    print(), which the compiled exe launched by double-click (no
    attached console) has nowhere visible to send. Now returns a real
    result AND writes any failure to the durable file log (_flog) so
    it's diagnosable even without a console."""
    if not _TELEGRAM_ENABLED:
        print(f"[TELEGRAM] suppressed (toggle off) -> chat {chat_id}: "
              f"{(payload.get('text') or '')[:80]!r}")
        return False
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = dict(payload)
    body["chat_id"] = chat_id
    if "reply_markup" in body and isinstance(body["reply_markup"], str):
        try:
            body["reply_markup"] = json.loads(body["reply_markup"])
        except Exception:
            pass
    try:
        r = session.post(url, json=body, timeout=5)
        if not r.ok:
            err = f"Telegram send error (chat {chat_id}): {r.text[:200]}"
            print(err)
            _flog("error", err)
            return False
        return True
    except Exception as e:
        err = f"Telegram exception (chat {chat_id}): {e}"
        print(err)
        _flog("error", err)
        return False

def send_to_telegram(text, bid_order_id=None, mobile_thread_url=None,
                     reply_msg_id=None, open_url=None, open_url_text="OPEN GMAIL",
                     route_url=None) -> bool:
    """Returns True if the message reached at least one configured chat
    (matches the old "fire and forget" behavior when there's only one
    chat ID, which is the common case) — see _telegram_send_one's
    docstring for why this now matters to the caller."""
    payload = {"text": text}
    if bid_order_id and mobile_thread_url:
        row1 = [
            {"text": "💵 BID PC",    "callback_data": f"bid:{bid_order_id}"},
            {"text": "💵 BID PHONE", "callback_data": f"phone:{bid_order_id}"},
            {"text": "📋 DRAFT",     "callback_data": f"text:{bid_order_id}"},
        ]
        row2 = []
        if route_url:
            row2.append({"text": "🚩ROUTE🚩", "url": route_url})
        keyboard = [row1] + ([row2] if row2 else [])
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    elif reply_msg_id:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "✉️ REPLY", "callback_data": f"reply:{reply_msg_id}"}
        ]]})
    elif open_url:
        url_key = _store_url(open_url)
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": open_url_text, "callback_data": f"openurl:{url_key}"}
        ]]})
    with _CHAT_IDS_LOCK:
        ids = list(CHAT_IDS)
    if not ids:
        return False
    if len(ids) == 1:
        return _telegram_send_one(ids[0], payload)
    else:
        results = {}
        def _run(cid):
            results[cid] = _telegram_send_one(cid, payload)
        threads = [threading.Thread(target=_run, args=(cid,), daemon=True) for cid in ids]
        for t in threads: t.start()
        for t in threads: t.join(timeout=6)
        return any(results.values())

def send_to_telegram_with_buttons(text: str, buttons: list):
    MAX_URL   = 2048
    safe_rows = []
    overflow  = []
    for row in buttons:
        safe_row = []
        for btn in row:
            url = btn.get("url", "")
            if url and len(url) > MAX_URL:
                overflow.append((btn["text"], url))
                safe_row.append({"text": btn["text"] + " (copy URL below)",
                                  "callback_data": "noop"})
            else:
                safe_row.append(btn)
        if safe_row:
            safe_rows.append(safe_row)
    payload = {"text": text, "reply_markup": json.dumps({"inline_keyboard": safe_rows})}
    with _CHAT_IDS_LOCK:
        ids = list(CHAT_IDS)
    if len(ids) == 1:
        _telegram_send_one(ids[0], payload)
    else:
        threads = [threading.Thread(target=_telegram_send_one,
                                    args=(cid, payload), daemon=True) for cid in ids]
        for t in threads: t.start()
        for t in threads: t.join(timeout=6)
    for label, url in overflow:
        send_to_telegram(f"🔗 {label}:\n{url}")

def get_telegram_updates(offset: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r    = session.get(url, params={"offset": offset, "timeout": 0}, timeout=5)
        data = r.json()
        if not data.get("ok"):
            return offset, []
        updates = data.get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
        return offset, updates
    except Exception as e:
        print("getUpdates error:", e)
        return offset, []

def answer_callback_query(callback_query_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        session.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=5)
    except Exception as e:
        print("answerCallbackQuery error:", e)

# =============================================================
# GMAIL AUTH + HELPERS — identical to original
# =============================================================
def register_gmail_watch(service):
    try:
        result = service.users().watch(
            userId="me",
            body={
                "labelIds": ["INBOX"],
                "topicName": "projects/augmented-path-406416/topics/gmail-push"
            }
        ).execute()
        print(f"[WATCH] Registered. historyId={result.get('historyId')}")
        return result
    except Exception as e:
        print(f"[WATCH] Failed: {e}")
        return None
def authenticate_gmail():
    token_path = os.path.join(_EXE_DIR, "token.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_path, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print("[AUTH] Token refreshed silently.")
                return creds
            except Exception as e:
                print(f"[AUTH] Silent refresh failed ({e}), re-authenticating...")
        flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds

def build_gmail_thread_url(thread_id):
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"

def mark_as_read(service, msg_id):
    try:
        service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"mark_as_read failed: {e}")

def mark_as_unread(service, msg_id):
    try:
        service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        print(f"mark_as_unread failed: {e}")

def get_label_map(service):
    resp = service.users().labels().list(userId="me").execute()
    return {lbl["id"]: lbl["name"] for lbl in resp.get("labels", [])}

def get_custom_label_names(msg_full, label_map):
    SYSTEM = {
        "INBOX","UNREAD","SENT","IMPORTANT","STARRED","TRASH","SPAM","DRAFT",
        "CATEGORY_FORUMS","CATEGORY_UPDATES","CATEGORY_PROMOTIONS",
        "CATEGORY_SOCIAL","CATEGORY_PERSONAL",
    }
    return [label_map[lid] for lid in msg_full.get("labelIds", [])
            if lid in label_map and label_map[lid] not in SYSTEM]

def get_current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])

def poll_new_messages_via_history(service, start_history_id: str):
    new_msg_ids    = []
    page_token     = None
    new_history_id = start_history_id
    while True:
        try:
            params = {
                "userId":         "me",
                "startHistoryId": start_history_id,
                "historyTypes":   ["messageAdded"],
            }
            if page_token:
                params["pageToken"] = page_token
            resp = service.users().history().list(**params).execute()
            new_history_id = str(resp.get("historyId", new_history_id))
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg    = added.get("message", {})
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels and "UNREAD" in labels:
                        if _has_custom_labels(labels):
                            continue
                        mid = msg.get("id")
                        if mid:
                            new_msg_ids.append(mid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        except HttpError as e:
            if e.resp.status == 404:
                return None, []
            print(f"History API error {e.resp.status}: {e}")
            break
        except Exception as e:
            print(f"History poll exception: {e}")
            break
    return new_history_id, new_msg_ids

def _has_custom_labels(label_ids):
    return any(lid not in _SYSIDS and not lid.startswith("CATEGORY_")
               for lid in label_ids)

def _get_thread_info(svc, thread_id: str, label_map: dict) -> tuple:
    try:
        thread = svc.users().threads().get(
            userId="me", id=thread_id,
            format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        found   = set()
        subject = ""
        for msg in thread.get("messages", []):
            for lid in msg.get("labelIds", []):
                if lid not in _SYSIDS and not lid.startswith("CATEGORY_"):
                    found.add(lid)
            if not subject:
                for h in msg.get("payload", {}).get("headers", []):
                    if h.get("name", "").lower() == "subject":
                        v = h.get("value", "").strip()
                        if v:
                            subject = v
                            break
        return [label_map.get(lid, lid) for lid in found], subject
    except Exception as e:
        print(f"[_get_thread_info] thread={thread_id} error: {e}")
        return [], ""

def _get_thread_label_names(svc, thread_id: str, label_map: dict) -> list:
    names, _ = _get_thread_info(svc, thread_id, label_map)
    return names

def _safe_mark_read(svc, msg_id: str, thread_id: str,
                    label_map: dict, subject: str, log_func=None):
    label_names, thread_subject = _get_thread_info(svc, thread_id, label_map)
    if label_names:
        try:
            mark_as_unread(svc, msg_id)
        except Exception:
            pass
        _notify_labeled_thread(label_names, thread_subject or subject,
                               thread_id, reverted=True)
        if log_func:
            log_func(f"🔒 Protected labeled thread [{msg_id[-8:]}] labels={label_names}")
        return
    mark_as_read(svc, msg_id)

def _extract_state_codes_from_text(text: str) -> list:
    found = []
    seen  = set()
    for token in re.findall(r"\b([A-Z]{2})\b", text.upper()):
        if token in _US_STATES_SET and token not in seen:
            seen.add(token)
            found.append(token)
    return found

def _notify_labeled_thread(label_names: list, subject: str,
                            thread_id: str, svc=None, reverted: bool = False):
    states    = _extract_state_codes_from_text(subject)
    state_str = " · ".join(states) if states else "—"
    lines = [
        "",
        f"📌 Label:  {', '.join(label_names)}",
        f"📍 States: {state_str}",
    ]
    print(f"[NOTIFY_LABELED] labels={label_names} states={states} subject={subject[:80]!r}")
    thread_url = build_gmail_thread_url(thread_id)
    send_to_telegram(
        "\n".join(lines),
        open_url=thread_url,
        open_url_text="💵 REPLY BID"
    )

# =============================================================
# EMAIL BODY EXTRACTION — identical to original
# =============================================================

def extract_text_from_full_message(msg_full):
    def _walk(payload):
        if not payload:
            return
        for p in payload.get("parts", []):
            yield from _walk(p)
        yield payload

    def _decode(b64):
        return base64.urlsafe_b64decode(b64 + "==").decode("utf-8", errors="replace")

    def html_to_text(h):
        h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>", "\n", h)
        h = re.sub(r"(?i)</(p|div|tr|td|th|li|h\d)>", "\n", h)
        h = re.sub(r"<[^>]+>", " ", h)
        h = html_lib.unescape(h)
        h = re.sub(r"[ \t]+", " ", h)
        return re.sub(r"\n\s*\n+", "\n\n", h).strip()

    plain = html = None
    for part in _walk(msg_full.get("payload", {})):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain" and not plain:
            plain = _decode(data)
        elif mime == "text/html" and not html:
            html = _decode(data)

    if plain and plain.strip():
        return plain
    if html and html.strip():
        return html_to_text(html)
    return msg_full.get("snippet", "")

# =============================================================
# REPLY DRAFT — identical to original
# =============================================================

SIGNATURE = """

MC 1616501


Mike.K
Dispatch
+1(440) 797-4007

MIRONETWORK LLC
MC: 1616501
DOT: 4193490
6807 Talbot Dr
Parma, OH 44129
"""



def build_bid_reply_html(body_text, logo_cid=None):
    bid_html  = "<br>".join(html_lib.escape(body_text).splitlines())
    logo_html = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0">'
        f'<tr><td style="padding-top:18px;">'
        f'<img src="cid:{logo_cid}" width="120" style="display:block;border:0;height:auto;">'
        f'</td></tr></table>'
    ) if logo_cid else ""
    return (
        '<html><body style="font-family:Arial,sans-serif;font-size:12px;'
        'font-weight:700;color:#222;line-height:1.45;margin:0;padding:0;">'
        f'<div>{bid_html}</div>'
        '<div style="height:32px;"></div><div>MC 1616501</div>'
        '<div style="height:70px;"></div>'
        '<div>Mike.K</div><div>Dispatch</div><div>+1(440) 797-4007</div>'
        '<div style="height:26px;"></div>'
        '<div>MIRONETWORK LLC</div><div>MC: 1616501</div><div>DOT: 4193490</div>'
        '<div>6807 Talbot Dr</div><div>Parma, OH 44129</div>'
        f'{logo_html}</body></html>'
    )

def create_reply_draft(service, original_msg_full, body_text,
                       logo_path=None, empty=False):
    hdr_map    = {h["name"].lower(): h["value"]
                  for h in original_msg_full.get("payload", {}).get("headers", [])}
    to_addr    = parseaddr(hdr_map.get("from", ""))[1]
    subject    = hdr_map.get("subject", "")
    message_id = hdr_map.get("message-id", "")
    references = hdr_map.get("references", "").strip()
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    if empty:
        mime = MIMEText("", "plain", "utf-8")
    elif logo_path and os.path.exists(logo_path):
        mime     = MIMEMultipart("related")
        alt      = MIMEMultipart("alternative")
        mime.attach(alt)
        logo_cid = "companylogo"
        alt.attach(MIMEText(body_text + SIGNATURE, "plain", "utf-8"))
        alt.attach(MIMEText(build_bid_reply_html(body_text, logo_cid), "html", "utf-8"))
        with open(logo_path, "rb") as f:
            img = MIMEImage(f.read(), _subtype="png")
        img.add_header("Content-ID", f"<{logo_cid}>")
        img.add_header("Content-Disposition", "inline",
                       filename=os.path.basename(logo_path))
        mime.attach(img)
    else:
        mime = MIMEMultipart("alternative")
        mime.attach(MIMEText(body_text + SIGNATURE, "plain", "utf-8"))
        mime.attach(MIMEText(build_bid_reply_html(body_text), "html", "utf-8"))
    mime["To"]      = to_addr
    mime["Subject"] = subject
    if message_id:
        mime["In-Reply-To"] = message_id
        mime["References"]  = (f"{references} {message_id}".strip()
                               if references else message_id)
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")

    # Retry up to 3 times on connection errors
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            return service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw,
                                  "threadId": original_msg_full.get("threadId")}},
            ).execute()
        except Exception as e:
            last_err = e
            err_str = str(e)
            if any(x in err_str for x in ("10053", "10054", "ConnectionReset",
                                           "ConnectionAborted", "BrokenPipe")):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    try:
                        creds = authenticate_gmail()
                        http  = httplib2.Http(timeout=60)
                        http.disable_ssl_certificate_validation = True
                        service = build("gmail", "v1",
                                       http=AuthorizedHttp(creds, http),
                                       cache_discovery=False,
                                       static_discovery=False)
                    except Exception:
                        pass
                    continue  # ← retry the loop
            raise  # ← only raise for non-connection errors
    # Unreachable in practice (the loop above always either returns or
    # raises), but typed explicitly so static analysis knows this can
    # never try to raise None.
    raise last_err if last_err else RuntimeError("create_reply_draft failed with no captured exception")
# =============================================================
# BID BODY — calls server to render template
# =============================================================

def _build_bid_body_for_order(order_id):
    with LOAD_STORE_LOCK:
        load = LOAD_STORE.get(order_id)
    if not load:
        return None
    return call_build_bid(
        license_key=ACTIVE_LICENSE_KEY,
        machine_id=_get_machine_id(),
        load_data={k: v for k, v in load.items() if k != "original_msg_full"},
    )

# =============================================================
# BID HISTORY — fire-and-forget write on every bid-send action
# =============================================================

def _record_bid(load: dict, method: str, truck: Optional[dict] = None) -> Optional[int]:
    """
    Called right after a bid is actually copied/sent/drafted (BID PC,
    BID PHONE, or DRAFT). `truck` is the selected all_trucks entry when
    the dispatcher chose a specific driver from a multi-truck list;
    None when there was only one candidate and `load`'s own top-level
    fields (driver_name/truck_type/google_deadhead) apply instead.

    Never let a history-write failure affect the actual bid action —
    this always runs after the real send/copy/draft has already
    happened, and any error here is swallowed and logged only.

    Returns the new bid_id (or None on failure). bid_amount starts out
    unset — it's filled in automatically afterward by the periodic
    thread-learning backfill (reads it straight from the dispatcher's
    own reply in the "bid"-labeled Gmail thread), not typed in here.
    """
    driver = truck if truck else load
    try:
        result = call_record_bid(
            license_key=ACTIVE_LICENSE_KEY,
            machine_id=_get_machine_id(),
            bid_data={
                "order_id":        load.get("order", ""),
                "thread_id":       load.get("original_msg_full", {}).get("threadId", ""),
                "bid_method":      method,
                "vehicle_type":    driver.get("truck_type") or load.get("vehicle_required", ""),
                "driver_name":     driver.get("driver_name", ""),
                "pickup_loc":      load.get("pickup_loc", ""),
                "delivery_loc":    load.get("delivery_loc", ""),
                "broker_name":     load.get("broker_name", ""),
                "broker_email":    load.get("broker_email", ""),
                "deadhead_miles":  driver.get("google_deadhead") or load.get("google_deadhead"),
                "loaded_miles":    load.get("loaded_miles"),
                "total_miles":     load.get("total_miles"),
                "verified_miles":  (load.get("maps_verification") or {}).get("verified_miles"),
                "verified_source": (load.get("maps_verification") or {}).get("verified_source"),
            },
        )
        return result.get("bid_id") if result else None
    except Exception as e:
        print(f"_record_bid failed (non-fatal): {e}")
        return None


# Emoji per classify_broker_reply() outcome — anything not in this map
# (an unrecognized status, or no confident status at all) falls back to
# a plain envelope in _run_classify_and_notify() below.
_REPLY_STATUS_EMOJI = {"won": "✅", "lost": "❌", "countered": "🔄"}


def _run_classify_and_notify(license_key: str, machine_id: str,
                             thread_id: str, subject: str, body: str):
    """
    Classify a broker's reply (won/lost/countered/no_signal) AND tell
    the dispatcher a reply came in at all — real bug, reported
    2026-09-12: the reply-guard fix from the day before (2026-09-11,
    correctly stopping a reply from being re-parsed as a brand new
    posting and resent 6x) had the side effect of going from "way too
    many notifications" straight to "zero" — classify_broker_reply's
    result was always silently discarded here, so a reply that changed
    nothing about the message pipeline also told the dispatcher nothing
    at all. This is now the ONLY place a reply notification comes from.

    Runs on its own thread (started by the caller) so it never delays
    the main polling loop — an LLM call on the server can take a few
    seconds.
    """
    result = call_classify_reply(license_key, machine_id, thread_id, subject, body)
    if not result or not result.get("matched"):
        return  # no bid on file for this thread — nothing to report

    order = result.get("order", {}) or {}
    order_id     = order.get("order_id", "")
    pickup_loc   = order.get("pickup_loc", "")
    delivery_loc = order.get("delivery_loc", "")
    broker_name  = order.get("broker_name", "")
    lane = f"{pickup_loc} → {delivery_loc}" if pickup_loc and delivery_loc else ""

    body_excerpt = (body or "").strip()
    if len(body_excerpt) > 400:
        body_excerpt = body_excerpt[:400].rstrip() + "…"

    header_bits = [b for b in (f"Order #{order_id}" if order_id else "",
                                broker_name, lane) if b]
    header = "  ·  ".join(header_bits)

    if result.get("updated"):
        cls    = result.get("classification", {}) or {}
        status = cls.get("status", "")
        emoji  = _REPLY_STATUS_EMOJI.get(status, "✉️")
        text = (f"{emoji} {status.upper()} — {header}\n"
                f"“{cls.get('reason', '')}”\n\n"
                f"{body_excerpt}")
    else:
        text = f"✉️ Reply — {header}\n\n{body_excerpt}"

    mobile_url = build_gmail_thread_url(thread_id) if thread_id else None
    send_to_telegram(text, open_url=mobile_url, open_url_text="OPEN THREAD")


# The "what rate did you quote?" ForceReply prompt (and its reply-
# matching handler in handle_bid_callbacks) was removed 2026-09-09 —
# client feedback: dispatchers won't reliably type the rate back every
# single time they bid. bid_amount now gets filled in automatically
# instead, via the periodic thread-learning backfill below: it reads
# the client's own quoted rate straight out of "bid"-labeled Gmail
# threads, and confirms wins via the "RC" (Rate Confirmation) label —
# see run_thread_learning_backfill() / _periodic_thread_learning_backfill().

# =============================================================
# THREAD LEARNING — automatic periodic backfill, gated on the
# server-side toggle (thread_learning_enabled)
# =============================================================

def run_thread_learning_backfill(days_back: int = 45) -> dict:
    """
    Manually triggered from the GUI button — NOT a background scheduled
    job. Walks broker threads from the last `days_back` days, pulls
    each thread's messages, and hands them to the server via
    call_backfill_thread for rate/outcome extraction. The server
    re-checks thread_learning_enabled on every call regardless of this
    local check, so this is a convenience gate, not the real one.
    """
    status = call_get_thread_learning_status(ACTIVE_LICENSE_KEY, _get_machine_id())
    if not status or not status.get("enabled"):
        return {"skipped": True, "reason": "Thread learning is OFF — enable it first."}

    try:
        creds = authenticate_gmail()
        http  = httplib2.Http(timeout=30)
        http.disable_ssl_certificate_validation = True
        gmail_service = build("gmail", "v1",
                              http=AuthorizedHttp(creds, http),
                              cache_discovery=False,
                              static_discovery=False)

        my_email = gmail_service.users().getProfile(userId="me").execute().get("emailAddress", "")
        # Gmail's raw labelIds are opaque tokens for custom labels (e.g.
        # "Label_16"), not the display name — translate through the
        # label map so the server can match on the human name ("RC").
        label_map = get_label_map(gmail_service)

        after_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())

        # Query each label separately (own maxResults cap) rather than
        # one combined OR query. A shared query returns Gmail's newest
        # N threads across ALL matched labels together — "bid"
        # (Sylectus auto-notifications) is far higher volume than the
        # rest, and was crowding rarer-but-more-valuable labels like
        # RC out of the capped result set entirely, not just pushing
        # them later in the batch. Confirmed 2026-09-02: only 1 of 5
        # real RC-labeled threads in a 30-day window ever made it into
        # a combined-query run.
        BACKFILL_LABELS = ["bid", "Finished Loads", "BROKERS", "RC", "in route"]
        per_label_counts = {}
        thread_ids_seen = set()
        thread_ids = []
        for label in BACKFILL_LABELS:
            label_q = f'label:"{label}"' if " " in label else f"label:{label}"
            results = gmail_service.users().threads().list(
                userId="me", q=f"{label_q} after:{after_ts}", maxResults=200
            ).execute()
            found = [t["id"] for t in results.get("threads", [])]
            per_label_counts[label] = len(found)
            for tid in found:
                if tid not in thread_ids_seen:
                    thread_ids_seen.add(tid)
                    thread_ids.append(tid)
        print(f"[BACKFILL] per-label thread counts: {per_label_counts}, "
              f"{len(thread_ids)} unique threads total")

        processed, skipped, errors = 0, 0, 0
        for tid in thread_ids:
            try:
                thread = gmail_service.users().threads().get(
                    userId="me", id=tid, format="full"
                ).execute()
                messages_out = []
                for msg in thread.get("messages", []):
                    headers = {h["name"].lower(): h["value"]
                               for h in msg.get("payload", {}).get("headers", [])}
                    from_addr = parseaddr(headers.get("from", ""))[1].lower()
                    messages_out.append({
                        "message_id": msg["id"],
                        "date_ms":    int(msg.get("internalDate", "0")),
                        "is_from_me": bool(my_email) and from_addr == my_email.lower(),
                        "subject":    headers.get("subject", ""),
                        "body":       extract_text_from_full_message(msg),
                        "label_ids":  [label_map.get(lid, lid) for lid in msg.get("labelIds", [])],
                    })
                result = call_backfill_thread(
                    ACTIVE_LICENSE_KEY, _get_machine_id(), tid, None, messages_out
                )
                if result and result.get("success") and result.get("processed"):
                    processed += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"[BACKFILL] thread {tid} failed: {e}")
                errors += 1

        return {"total": len(thread_ids), "processed": processed,
                "skipped": skipped, "errors": errors,
                "per_label_counts": per_label_counts}
    except Exception as e:
        print(f"[BACKFILL] run failed: {e}")
        return {"error": str(e)}

# =============================================================
# TELEGRAM CALLBACK HANDLER — identical to original
# =============================================================

def handle_bid_callbacks(service):
    global TELEGRAM_UPDATE_OFFSET
    with TELEGRAM_OFFSET_LOCK:
        current_offset = TELEGRAM_UPDATE_OFFSET
    new_offset, updates = get_telegram_updates(current_offset)
    with TELEGRAM_OFFSET_LOCK:
        if new_offset > TELEGRAM_UPDATE_OFFSET:
            TELEGRAM_UPDATE_OFFSET = new_offset

    for upd in updates:
        # Plain (non-callback) messages — e.g. a dispatcher typing in the
        # chat — nothing to do with them since the ForceReply rate-prompt
        # flow was removed (2026-09-09); only callback_query updates
        # (button taps) matter below.
        if not upd.get("callback_query"):
            continue

        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        cqid = cq.get("id")
        answer_callback_query(cqid, "")

        # ── BID PC ────────────────────────────────────────────────────────
        if data.startswith("bid:"):
            parts    = data.split(":")
            order_id = parts[1]

            with LOAD_STORE_LOCK:
                load = LOAD_STORE.get(order_id)
            if not load:
                continue

            all_trucks = load.get("all_trucks", [])
            print(f"[DEBUG] Order {order_id}: all_trucks={len(all_trucks)} -> {[t.get('driver_name') for t in all_trucks]}")
            if len(parts) > 2:
                # Driver already selected — perform the bid
                truck_idx = int(parts[2])
                if truck_idx >= len(all_trucks):
                    continue
                selected = all_trucks[truck_idx]
                body = _build_bid_body_for_load(load, selected)
                if not body:
                    continue
                try:
                    pyperclip.copy(body)
                    thread_id = load.get("original_msg_full", {}).get("threadId", "")
                    if thread_id:
                        webbrowser.open(build_gmail_thread_url(thread_id))
                    else:
                        webbrowser.open("https://mail.google.com/mail/u/0/#all")
                    _bid_id = _record_bid(load, "pc", selected)
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    send_to_telegram(
                        f"📋 Bid for {selected['driver_name']} copied. Press Reply and paste (Ctrl+V).")
                except Exception as e:
                    print("Bid callback failed:", e)

            elif not all_trucks or len(all_trucks) == 1:
                # Only one driver — proceed directly
                body = _build_bid_body_for_order(order_id)
                if not body:
                    continue
                try:
                    pyperclip.copy(body)
                    thread_id = load.get("original_msg_full", {}).get("threadId", "")
                    if thread_id:
                        webbrowser.open(build_gmail_thread_url(thread_id))
                    else:
                        webbrowser.open("https://mail.google.com/mail/u/0/#all")
                    _bid_id = _record_bid(load, "pc")
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    send_to_telegram("📋 Bid text copied. Press Reply and paste (Ctrl+V).")
                except Exception as e:
                    print("Bid callback failed:", e)

            else:
                # Multiple drivers — show selection
                buttons = []
                for i, truck in enumerate(all_trucks):
                    name = truck.get("driver_name", f"Driver {i+1}")
                    dh   = truck.get("google_deadhead", "?")
                    buttons.append([{
                        "text":          f"🚛 {name}  —  {dh} mi out",
                        "callback_data": f"bid:{order_id}:{i}",
                    }])
                send_to_telegram_with_buttons(
                    f"👤 Select driver for Order #{order_id}:",
                    buttons
                )

        # ── BID PHONE ─────────────────────────────────────────────────────
        elif data.startswith("phone:"):
            parts    = data.split(":")
            order_id = parts[1]

            with LOAD_STORE_LOCK:
                load = LOAD_STORE.get(order_id)
            if not load:
                continue

            all_trucks = load.get("all_trucks", [])

            if len(parts) > 2:
                # Driver already selected
                truck_idx = int(parts[2])
                if truck_idx >= len(all_trucks):
                    continue
                selected = all_trucks[truck_idx]
                body = _build_bid_body_for_load(load, selected)
                if not body:
                    continue
                try:
                    send_to_telegram(body)
                    original_msg = load.get("original_msg_full", {})
                    draft    = create_reply_draft(service, original_msg, "", None, empty=True)
                    draft_id = draft.get("id", "")
                    _bid_id = _record_bid(load, "phone", selected)
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    if draft_id:
                        draft_url = f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"
                        send_to_telegram_with_buttons(
                            f"✅ Draft created for {selected['driver_name']} — Order #{order_id}\n"
                            f"Tap below → opens Gmail draft ready to send:",
                            [[{"text": "📨 Open Draft & Send", "url": draft_url}]]
                        )
                    else:
                        send_to_telegram_with_buttons(
                            f"✅ Draft created for Order #{order_id} — open your Drafts:",
                            [[{"text": "📂 Open Gmail Drafts",
                               "url": "https://mail.google.com/mail/u/0/#drafts"}]]
                        )
                except Exception as e:
                    print("Phone callback failed:", e)
                    send_to_telegram(f"❌ Failed to create draft: {e}")

            elif not all_trucks or len(all_trucks) == 1:
                # Single driver — proceed directly
                body = _build_bid_body_for_order(order_id)
                if not body:
                    continue
                try:
                    send_to_telegram(body)
                    original_msg = load.get("original_msg_full", {})
                    draft    = create_reply_draft(service, original_msg, "", None, empty=True)
                    draft_id = draft.get("id", "")
                    _bid_id = _record_bid(load, "phone")
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    if draft_id:
                        draft_url = f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"
                        send_to_telegram_with_buttons(
                            f"✅ Draft created for Order #{order_id}\n"
                            f"Tap below → opens Gmail draft ready to send:",
                            [[{"text": "📨 Open Draft & Send", "url": draft_url}]]
                        )
                    else:
                        send_to_telegram_with_buttons(
                            f"✅ Draft created for Order #{order_id} — open your Drafts:",
                            [[{"text": "📂 Open Gmail Drafts",
                               "url": "https://mail.google.com/mail/u/0/#drafts"}]]
                        )
                except Exception as e:
                    print("Phone callback failed:", e)
                    send_to_telegram(f"❌ Failed to create draft: {e}")

            else:
                # Multiple drivers — show selection
                buttons = []
                for i, truck in enumerate(all_trucks):
                    name = truck.get("driver_name", f"Driver {i+1}")
                    dh   = truck.get("google_deadhead", "?")
                    buttons.append([{
                        "text":          f"📱 {name}  —  {dh} mi out",
                        "callback_data": f"phone:{order_id}:{i}",
                    }])
                send_to_telegram_with_buttons(
                    f"👤 Select driver for Order #{order_id} (Phone):",
                    buttons
                )

        # ── DRAFT TEXT ────────────────────────────────────────────────────
        elif data.startswith("text:"):
            parts    = data.split(":")
            order_id = parts[1]

            with LOAD_STORE_LOCK:
                load = LOAD_STORE.get(order_id)
            if not load:
                continue

            all_trucks = load.get("all_trucks", [])

            if len(parts) > 2:
                truck_idx = int(parts[2])
                if truck_idx >= len(all_trucks):
                    continue
                selected = all_trucks[truck_idx]
                body = _build_bid_body_for_load(load, selected)
                if body:
                    _bid_id = _record_bid(load, "draft", selected)
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    send_to_telegram(f"📋 ORDER #{order_id} — {selected['driver_name']}:\n\n{body}")

            elif not all_trucks or len(all_trucks) == 1:
                body = _build_bid_body_for_order(order_id)
                if body:
                    _bid_id = _record_bid(load, "draft")
                    # (ForceReply rate-prompt removed 2026-09-09 — bid_amount
                    # now fills in automatically via thread learning instead)
                    send_to_telegram(f"📋 ORDER #{order_id}:\n\n{body}")

            else:
                # Multiple drivers — show selection
                buttons = []
                for i, truck in enumerate(all_trucks):
                    name = truck.get("driver_name", f"Driver {i+1}")
                    dh   = truck.get("google_deadhead", "?")
                    buttons.append([{
                        "text":          f"📋 {name}  —  {dh} mi out",
                        "callback_data": f"text:{order_id}:{i}",
                    }])
                send_to_telegram_with_buttons(
                    f"👤 Select driver for Order #{order_id} (Draft):",
                    buttons
                )

        # ── OPEN URL ──────────────────────────────────────────────────────
        elif data.startswith("openurl:"):
            key = data[len("openurl:"):]
            url = _retrieve_url(key)
            if url:
                webbrowser.open(url)

        # ── REPLY ─────────────────────────────────────────────────────────
        elif data.startswith("reply:"):
            msg_id = data.split(":", 1)[1]
            try:
                full = service.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
                webbrowser.open(build_gmail_thread_url(full["threadId"]))
                send_to_telegram("📨 Reply thread opened.")
            except HttpError as e:
                if e.resp.status == 404:
                    send_to_telegram("⚠️ Email not found (may have been deleted).")
                else:
                    print("Reply callback failed:", e)
            except Exception as e:
                print("Reply callback failed:", e)


def _build_bid_body_for_load(load: dict, truck: dict) -> Optional[str]:
    """Build bid body using a specific truck from all_trucks list."""
    return call_build_bid(
        license_key=ACTIVE_LICENSE_KEY,
        machine_id=_get_machine_id(),
        load_data={
            "order":                load.get("order"),
            "vehicle_required":     load.get("vehicle_required"),
            "pickup_loc":           load.get("pickup_loc"),
            "pickup_dt":            load.get("pickup_dt"),
            "delivery_loc":         load.get("delivery_loc"),
            "delivery_dt":          load.get("delivery_dt"),
            "google_deadhead":      truck.get("google_deadhead"),
            "deadhead_eta_minutes": truck.get("deadhead_eta_minutes"),
            "driver_name":          truck.get("driver_name"),
            "truck_type":           truck.get("truck_type"),
            "truck_dimensions":     truck.get("truck_dimensions"),
            "truck_equipment":      truck.get("truck_equipment", ""),
            "bid_template":         load.get("bid_template"),
        }
    )

# =============================================================
# MAIN POLL LOOP
# Only difference from original: _process_email calls call_parse()
# instead of process_bid_email(). Everything else is identical.
# =============================================================

def main_loop(poll_seconds, allowed_vehicles, radius,
              log_func=None, allowed_delivery_states=None):

    def _log(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('utf-8', errors='replace').decode('ascii', errors='replace'))
        if log_func:
            try:
                log_func(msg)
            except Exception:
                pass
        # Also write to file log
        level = "ERROR" if ("❌" in msg or "[ERR]" in msg) else "INFO"
        _flog(level, msg)

    def _make_service():
        creds = authenticate_gmail()
        http  = httplib2.Http(timeout=60)
        http.disable_ssl_certificate_validation = True
        # Force new connections — prevents stale socket errors (WinError 10053)
        http.force_exception_to_status_code = False
        return build("gmail", "v1",
                    http=AuthorizedHttp(creds, http),
                    cache_discovery=False,
                    static_discovery=False)

    service      = _make_service()
    label_map    = get_label_map(service)
    last_rebuild = time.time()
    register_gmail_watch(service)  # ← ADD THIS

    processed_ids  = set()
    in_flight      = set()
    in_flight_lock = threading.Lock()
    active_futures = {}
    futures_lock   = threading.Lock()
    _retry_counts  = {}

    state_info = (f"  Delivery states: {', '.join(sorted(allowed_delivery_states))}"
                  if allowed_delivery_states else "  Delivery states: ALL")
    _log(f"▶ Watching: {', '.join(allowed_vehicles)}")
    _log(state_info)
    send_to_telegram(
        f"✅ Watching: {', '.join(allowed_vehicles)}\nWindow: {FRESH_WINDOW}\n{state_info}"
    )

    def _cb_loop():
        cb_svc = _make_service()
        while not STOP_EVENT.is_set():
            try:
                handle_bid_callbacks(cb_svc)
            except Exception as e:
                print(f"[CB ERR] {e}")
                try:
                    cb_svc = _make_service()
                except Exception:
                    pass
            time.sleep(0.1)  # 100ms — faster callback response

    cb_thread = threading.Thread(target=_cb_loop, daemon=True, name="callbacks")
    cb_thread.start()

    # ── Service pool — identical to original ──────────────────────────────
    _NUM_WORKERS = 5
    _svc_q: _queue.SimpleQueue = _queue.SimpleQueue()

    def _fill_pool():
        for _ in range(_NUM_WORKERS + 2):
            try:
                _svc_q.put(_make_service())
            except Exception as ex:
                print(f"[POOL] service init error: {ex}")

    _pool_thread = threading.Thread(target=_fill_pool, daemon=True, name="svc-pool")
    _pool_thread.start()
    _log("[SYS] Warming service pool (up to 30s)…")

    _warmup_deadline = time.time() + 30
    while time.time() < _warmup_deadline and _pool_thread.is_alive():
        if STOP_EVENT.is_set():
            _log("[SYS] Stop during warmup — exiting.")
            return
        # Break early if pool already has enough services
        if _svc_q.qsize() >= _NUM_WORKERS:
            break
        time.sleep(0.5)

    if STOP_EVENT.is_set():
        return
    _log(f"[SYS] Service pool ready ({_svc_q.qsize()} services).")

    def _get_svc():
        try:
            return _svc_q.get_nowait()
        except _queue.Empty:
            return _make_service()

    def _ret_svc(svc):
        _svc_q.put(svc)

    executor = ThreadPoolExecutor(max_workers=_NUM_WORKERS, thread_name_prefix="mailbot")

    _MAX_CONN_RETRIES = 3

    def _is_conn_reset(exc: Exception) -> bool:
        if isinstance(exc, ConnectionResetError):
            return True
        if isinstance(exc, OSError):
            winerr = getattr(exc, "winerror", None)
            if winerr == 10054:
                return True
            import errno as _errno
            if exc.errno in (_errno.ECONNRESET, _errno.ECONNABORTED):
                return True
        if "10054" in str(exc) or "ConnectionReset" in type(exc).__name__:
            return True
        return False

    def _process_email(msg_id: str):
        # NOTE: do NOT import time locally — use module-level time to avoid shadow
        _T0 = time.perf_counter()
        if STOP_EVENT.is_set():
            return
        svc = _get_svc()
        try:
            # STEP 1: single full fetch — saves ~0.5-1s vs metadata+full two-shot.
            # labelIds are included in full format so the custom-label check works immediately.
            try:
                full = svc.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
                _T1 = time.perf_counter()
                print(f"[CLIENT TIMING] full fetch: {_T1-_T0:.3f}s", flush=True)
            except HttpError as e:
                if e.resp.status == 404:
                    processed_ids.add(msg_id)
                    return
                raise

            if STOP_EVENT.is_set():
                return
            

            # STEP 2: label guard — skip if this message has custom labels
            if _has_custom_labels(full.get("labelIds", [])):
                processed_ids.add(msg_id)
                return
            # In _process_email, before the thread label guard:
            _subj_check = ""
            for _h in full.get("payload", {}).get("headers", []):
                if _h.get("name", "").lower() == "subject":
                    _subj_check = _h.get("value", "").upper()
                    break

            # STEP 3: thread label guard — one extra API call, only when message is clean.
            # Protects against labeled threads where the trigger message has no custom label yet.
            # Skipped for clear freight emails to save the extra API call.
            _thread_id_full = full.get("threadId", "")
            _is_freight = any(m in _subj_check for m in FREIGHT_MARKERS)
            if not _is_freight and _thread_id_full:
                _tl2, _ts2 = _get_thread_info(svc, _thread_id_full, label_map)
                if _tl2:
                    _subj = ""
                    for _h in full.get("payload", {}).get("headers", []):
                        if _h.get("name", "").lower() == "subject":
                            _subj = _h.get("value", "")
                            break
                    _notify_labeled_thread(_tl2, _subj or _ts2, _thread_id_full)
                    processed_ids.add(msg_id)
                    return

            # STEP 4: extract body, send to server
            body          = extract_text_from_full_message(full)
            internal_date = int(full.get("internalDate", "0"))

            # ── Broker-reply outcome check — cheap on the server (a local
            # SQLite lookup runs before anything else), so this can safely
            # fire for every processed message. It only actually spends an
            # LLM call when this thread has a bid still awaiting an
            # outcome. Runs in its own thread so it never delays parsing.
            _thread_id_cls = full.get("threadId", "")
            if _thread_id_cls:
                _subj_cls = ""
                for _h in full.get("payload", {}).get("headers", []):
                    if _h.get("name", "").lower() == "subject":
                        _subj_cls = _h.get("value", "")
                        break
                threading.Thread(
                    target=_run_classify_and_notify,
                    args=(ACTIVE_LICENSE_KEY, _get_machine_id(),
                          _thread_id_cls, _subj_cls, body),
                    daemon=True,
                ).start()

            # STEP 4.5: reply guard — don't re-parse an in-thread REPLY as
            # if it were a brand new freight posting. Real bug, reported
            # 2026-09-11: a broker's reply to an existing bid thread kept
            # matching FREIGHT_MARKERS (it quotes/keeps the original
            # subject, e.g. "Re: ... LARGE STRAIGHT ..."), so it sailed
            # through every guard above and got fully re-parsed AND
            # re-notified as a fresh posting — resending the whole
            # original bid message the dispatcher had already seen, among
            # other duplicate Telegram sends from the same reply. In-Reply-To
            # is only ever set by the sender's mail client when actually
            # replying to a specific prior message — a genuine NEW posting,
            # even a follow-up one in the same thread, is never "in reply
            # to" anything. classify_reply() just above already handles
            # won/lost/countered detection for exactly this case; it
            # shouldn't ALSO go through the full match+notify pipeline.
            _in_reply_to = ""
            for _h in full.get("payload", {}).get("headers", []):
                if _h.get("name", "").lower() == "in-reply-to":
                    _in_reply_to = (_h.get("value") or "").strip()
                    break
            if _in_reply_to:
                processed_ids.add(msg_id)
                return

            trucks_payload = []
            for t in TRUCKS:
                trucks_payload.append({
                    "vehicle":         t["vehicle"],
                    "driver_name":     t["driver_name"],
                    "dimensions":      t["dimensions"],
                    "max_payload_lbs": t.get("max_payload_lbs"),
                    "zip_location":    t["zip"],
                    "equipment":       t.get("equipment", ""),
                    "allowed_states":  list(t["allowed_states"]) if t.get("allowed_states") else None,
                    "pickup_date":     t.get("pickup_date", ""),
                    "radius_miles":    t.get("radius_miles"),
                })

            _T2 = time.perf_counter()
            try:
                result = call_parse(
                    license_key      = ACTIVE_LICENSE_KEY,
                    machine_id       = _get_machine_id(),
                    email_body       = body,
                    internal_date_ms = internal_date,
                    allowed_vehicles = allowed_vehicles,
                    max_radius_miles = radius,
                    trucks           = trucks_payload,
                    bid_template     = BID_TEMPLATE,
                )
                _T3 = time.perf_counter()
                print(f"[CLIENT TIMING] fetch={_T1-_T0:.3f}s  thread={_T2-_T1:.3f}s  server={_T3-_T2:.3f}s  TOTAL={_T3-_T0:.3f}s", flush=True)
            except PermissionError as e:
                send_to_telegram(f"⛔ License revoked: {e}")
                STOP_EVENT.set()
                return

            if STOP_EVENT.is_set():
                return

            if result is None:
                _log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ Server unreachable [{msg_id[-6:]}]")
                return

            formatted = result.get("formatted")
            info      = result.get("message", "")
            order     = result.get("order_id")

            if order and result.get("load_data"):
                with LOAD_STORE_LOCK:
                    if len(LOAD_STORE) >= 500:
                        del LOAD_STORE[next(iter(LOAD_STORE))]
                    LOAD_STORE[order] = result["load_data"]
                    LOAD_STORE[order]["original_msg_full"] = full

            mobile_bid_url = build_gmail_thread_url(full.get("threadId", ""))
            ts        = datetime.now().strftime("%H:%M:%S")
            order_tag = f" #{order}" if order else ""
            gmid_tag  = f" [gmid:{msg_id[-8:]}]"

            if formatted:
                _route_url = None
                with LOAD_STORE_LOCK:
                    _ld = LOAD_STORE.get(order)
                    if _ld:
                        _route_url = _ld.get("route_url")
                _tg_ok = send_to_telegram(formatted, bid_order_id=order,
                                          mobile_thread_url=mobile_bid_url,
                                          route_url=_route_url)
                if _tg_ok:
                    _log(f"[{ts}] ✅ #{order}{gmid_tag}  →  sent to Telegram")
                elif not _TELEGRAM_ENABLED:
                    _log(f"[{ts}] ⏸ #{order}{gmid_tag}  →  Telegram is OFF, not sent")
                else:
                    _log(f"[{ts}] ❌ #{order}{gmid_tag}  →  Telegram send FAILED "
                        f"(check chat ID / bot token — see log file for details)")

                # Driver bot — independent of the dispatcher send above
                # (own bot, own chat(s), own failure mode; a dispatcher
                # send failure shouldn't also block drivers from seeing
                # the load). Per driver_bot.py's own documented
                # integration contract.
                if _DRIVER_BOT_ENABLED and driver_bot is not None:
                    with LOAD_STORE_LOCK:
                        _ld_for_drivers = dict(LOAD_STORE.get(order, {}))
                    if _ld_for_drivers:
                        _ld_for_drivers["formatted_message"] = formatted
                        threading.Thread(
                            target=driver_bot.notify_drivers,
                            args=(order, _ld_for_drivers),
                            daemon=True,
                        ).start()
            else:
                _log(f"[{ts}] ⏭  SKIPPED{order_tag}{gmid_tag}  →  {info}")

            # STEP 5: safe mark-read
            _subject_final = ""
            for _h in full.get("payload", {}).get("headers", []):
                if _h.get("name", "").lower() == "subject":
                    _subject_final = _h.get("value", "")
                    break
            _safe_mark_read(svc, msg_id,
                            full.get("threadId", ""),
                            label_map, _subject_final, _log)

        except Exception as e:
            if _is_conn_reset(e) and not STOP_EVENT.is_set():
                attempt = _retry_counts.get(msg_id, 0) + 1
                if attempt <= _MAX_CONN_RETRIES:
                    _retry_counts[msg_id] = attempt
                    wait = 2 ** attempt
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"⚠ ConnReset {msg_id[-6:]} — retry {attempt}/{_MAX_CONN_RETRIES} in {wait}s")
                    time.sleep(wait)
                    with in_flight_lock:
                        in_flight.discard(msg_id)
                    _submit(msg_id)
                    return
                _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                     f"❌ {msg_id[-6:]}: ConnReset after {_MAX_CONN_RETRIES} retries — giving up")
            else:
                _log(f"[ERR] {msg_id[-6:]}: {e}")
        finally:
            _ret_svc(svc)

    def _submit(msg_id: str):
        # Double-check inside lock to prevent race between processed_ids and in_flight
        with in_flight_lock:
            if msg_id in processed_ids or msg_id in in_flight:
                return
            in_flight.add(msg_id)
        future = executor.submit(_process_email, msg_id)
        with futures_lock:
            active_futures[future] = msg_id

    def _reap_futures():
        with futures_lock:
            done = [(f, mid) for f, mid in active_futures.items() if f.done()]
            for f, _ in done:
                del active_futures[f]
            remaining_mids = set(active_futures.values())
        for future, mid in done:
            if mid not in remaining_mids:
                with in_flight_lock:
                    in_flight.discard(mid)
                processed_ids.add(mid)
                _retry_counts.pop(mid, None)
            try:
                future.result()
            except Exception as e:
                _log(f"[ERR] worker {mid[-6:]}: {e}")

    _log("[SYS] Getting initial historyId...")
    try:
        history_id = get_current_history_id(service)
        _log(f"[SYS] historyId = {history_id}")
    except Exception as e:
        _log(f"[SYS] historyId failed: {e}. Falling back to list-only mode.")
        history_id = None

    # ── Initial scan — identical to original ──────────────────────────────
    _log("[SYS] Initial catch-up scan...")
    try:
        _veh_terms  = " OR ".join(f'"{v}"' for v in allowed_vehicles)
        _init_query = f'is:unread newer_than:{FRESH_WINDOW} ({_veh_terms})'
        _log(f"[SYS] Query: {_init_query}")
        _page_token = None
        _total      = 0
        while True:
            _resp = service.users().messages().list(
                userId="me", q=_init_query, maxResults=500, pageToken=_page_token,
            ).execute()
            for _msg in _resp.get("messages", []):
                if STOP_EVENT.is_set():
                    break
                _submit(_msg["id"])
                _total += 1
            _page_token = _resp.get("nextPageToken")
            if not _page_token or STOP_EVENT.is_set():
                break
        _log(f"[SYS] Initial scan done: {_total} queued.")

        # ── Startup cleanup — identical to original ────────────────────────
        _log("[SYS] Startup cleanup — sweeping remaining unread emails...")
        _cleanup_ids  = []
        _cleanup_page = None
        _cleanup_cnt  = 0
        while True:
            _cr = service.users().messages().list(
                userId="me", q=f"is:unread newer_than:{FRESH_WINDOW}",
                maxResults=500, pageToken=_cleanup_page,
            ).execute()
            for _cm in _cr.get("messages", []):
                _cid = _cm["id"]
                if _cid in processed_ids:
                    continue
                with in_flight_lock:
                    if _cid in in_flight:
                        continue
                try:
                    _cmeta = service.users().messages().get(
                        userId="me", id=_cid, format="metadata",
                        metadataHeaders=["Subject"],
                    ).execute()
                except Exception:
                    continue

                if _has_custom_labels(_cmeta.get("labelIds", [])):
                    processed_ids.add(_cid)
                    continue

                _cleanup_thread_id = _cmeta.get("threadId", "")
                if _cleanup_thread_id:
                    _ctl, _ = _get_thread_info(service, _cleanup_thread_id, label_map)
                    if _ctl:
                        processed_ids.add(_cid)
                        continue

                _cleanup_ids.append(_cid)
                processed_ids.add(_cid)
                _cleanup_cnt += 1

            _cleanup_page = _cr.get("nextPageToken")
            if not _cleanup_page or STOP_EVENT.is_set():
                break

        _safe_ids = []
        for _cid in _cleanup_ids:
            try:
                _recheck = service.users().messages().get(
                    userId="me", id=_cid, format="metadata",
                    metadataHeaders=["Subject"],
                ).execute()
                if not _has_custom_labels(_recheck.get("labelIds", [])):
                    _safe_ids.append(_cid)
            except Exception:
                pass

        for _i in range(0, len(_safe_ids), 1000):
            try:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": _safe_ids[_i:_i + 1000],
                          "removeLabelIds": ["UNREAD"]},
                ).execute()
            except Exception as _ce:
                _log(f"[SYS] Cleanup batch error: {_ce}")

        # Log each cleaned email with subject — identical to original
        for _cid in _safe_ids:
            try:
                _cmeta2 = service.users().messages().get(
                    userId="me", id=_cid, format="metadata",
                    metadataHeaders=["Subject"],
                ).execute()
                _subj2 = ""
                for _h in _cmeta2.get("payload", {}).get("headers", []):
                    if _h.get("name", "").lower() == "subject":
                        _subj2 = _h.get("value", "").upper()
                        break
                _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                     f"🗑 Cleaned [{_cid[-8:]}]: {_subj2[:70]}")
            except Exception:
                _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                     f"🗑 Cleaned [{_cid[-8:]}]")

        _log(f"[SYS] Startup cleanup done: {len(_safe_ids)} cleared.")
    except Exception as e:
        _log(f"[SYS] Initial scan failed: {e}")

    # ── Real-time loop — push + periodic history polling ─────────────────
    _log("[SYS] Entering real-time mode...")

    # _wake is set by push-poller thread to interrupt the 2s sleep
    _wake = threading.Event()

    # _msg_queue receives message IDs discovered by history-poller thread
    _msg_queue: _queue.SimpleQueue = _queue.SimpleQueue()

    # history_id is shared between main thread (rebuilds) and history-poller
    _hid_lock  = threading.Lock()

    def _push_poller():
        """Background: polls our server for Gmail push notifications every 0.2s.
        When one arrives, wakes the history-poller immediately via _wake."""
        while not STOP_EVENT.is_set():
            try:
                ids = call_poll_push(ACTIVE_LICENSE_KEY, _get_machine_id())
                if ids:
                    print(f"[PUSH] {time.strftime('%H:%M:%S')} waking history poller", flush=True)
                    _wake.set()
            except Exception:
                pass
            time.sleep(0.2)

    def _history_poller():
        nonlocal history_id
        _hist_svc = _make_service()
        while not STOP_EVENT.is_set():
            # Don't wait for push — poll every 2s unconditionally
            _wake.wait(timeout=2.0)
            _wake.clear()
            if STOP_EVENT.is_set():
                break
            try:
                with _hid_lock:
                    hid = history_id
                if not hid:
                    try:
                        hid = get_current_history_id(_hist_svc)
                        with _hid_lock:
                            history_id = hid
                    except Exception as e:
                        _log(f"[SYS] historyId recovery failed: {e}")
                        continue

                new_hid, new_mids = poll_new_messages_via_history(_hist_svc, hid)

                if new_hid is None:
                    try:
                        new_hid = get_current_history_id(_hist_svc)
                        with _hid_lock:
                            history_id = new_hid
                    except Exception as e:
                        _log(f"[SYS] historyId reset failed: {e}")
                    continue

                with _hid_lock:
                    history_id = new_hid

                for mid in new_mids:
                    _msg_queue.put(mid)

            except Exception as e:
                _log(f"[HIST ERR] {e}")
                try:
                    _hist_svc = _make_service()
                except Exception:
                    pass
    _push_thread = threading.Thread(target=_push_poller,   daemon=True, name="push-poller")
    _hist_thread = threading.Thread(target=_history_poller, daemon=True, name="hist-poller")
    _push_thread.start()
    _hist_thread.start()
    def _fast_gmail_poller():
        """Poll Gmail list directly every 3s as backup to push notifications."""
        _fsvc = _make_service()
        _veh_terms = " OR ".join(f'"{v}"' for v in allowed_vehicles)
        _query = f'is:unread newer_than:1h ({_veh_terms})'
        while not STOP_EVENT.is_set():
            try:
                resp = _fsvc.users().messages().list(
                    userId="me", q=_query, maxResults=10
                ).execute()
                for msg in resp.get("messages", []):
                    mid = msg["id"]
                    if mid not in processed_ids:
                        _msg_queue.put(mid)
            except Exception:
                try:
                    _fsvc = _make_service()
                except Exception:
                    pass
            time.sleep(3)   # Poll every 3 seconds

    _fast_thread = threading.Thread(target=_fast_gmail_poller, daemon=True, name="fast-poll")
    _fast_thread.start()
    while not STOP_EVENT.is_set():
        try:
            _reap_futures()

            if time.time() - last_rebuild > 1800:
                try:
                    service      = _make_service()
                    label_map    = get_label_map(service)
                    last_rebuild = time.time()
                    register_gmail_watch(service)
                    _log("[SYS] Gmail credentials refreshed.")
                except Exception as e:
                    _log(f"[SYS] Credential refresh failed: {e}")

            # Drain all queued message IDs — non-blocking
            drained = 0
            while not STOP_EVENT.is_set():
                try:
                    msg_id = _msg_queue.get_nowait()
                except _queue.Empty:
                    break
                if msg_id not in processed_ids:
                    _submit(msg_id)
                    drained += 1
                # always drain the queue even for already-processed IDs

            if drained == 0:
                time.sleep(0.05)  # tiny sleep only when queue is empty

        except Exception as e:
            _log(f"[LOOP ERR] {e}")

        if len(processed_ids) > 2000:
            stale = list(processed_ids)[:len(processed_ids) - 2000]
            for _id in stale:
                processed_ids.discard(_id)

    _log("[SYS] Shutting down worker pool…")
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    _log("[SYS] Worker pool shut down.")

# =============================================================
# THEME — identical to original
# =============================================================

_THEMES = {
    "dark": {
        "bg":     "#1c1c1e", "surf":   "#2c2c2e", "input":  "#3a3a3c",
        "border": "#48484a", "accent": "#0a84ff", "green":  "#30d158",
        "yellow": "#ffd60a", "red":    "#ff453a", "cyan":   "#5ac8fa",
        "text":   "#ffffff", "text2":  "#98989e", "text3":  "#636366",
        "log_bg": "#111114",
    },
    "light": {
        "bg":     "#f2f2f7", "surf":   "#ffffff", "input":  "#e5e5ea",
        "border": "#c7c7cc", "accent": "#007aff", "green":  "#34c759",
        "yellow": "#ff9500", "red":    "#ff3b30", "cyan":   "#32ade6",
        "text":   "#1c1c1e", "text2":  "#48484a", "text3":  "#8e8e93",
        "log_bg": "#f9f9fb",
    },
}

def _load_prefs() -> dict:
    try:
        if os.path.exists(PREFS_FILE):
            with open(PREFS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"theme": "dark"}

def _save_prefs(prefs: dict):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass

# =============================================================
# PERSISTENT CONFIG — saves trucks, chat IDs, template, settings
# =============================================================

CONFIG_FILE = os.path.join(_EXE_DIR, "plutus_config.json")

def _load_config() -> dict:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_config(data: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[CONFIG] Save failed: {e}")

_prefs         = _load_prefs()
_current_theme = _prefs.get("theme", "dark")
_C             = dict(_THEMES[_current_theme])

# =============================================================
# TITLEBAR
# =============================================================

def _apply_titlebar(root: tk.Tk, dark: bool = True) -> None:
    if sys.platform != "win32":
        return
    try:
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val  = 1 if dark else 0
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(ctypes.c_int(val)), ctypes.sizeof(ctypes.c_int(val)),
        )
    except Exception:
        pass

# =============================================================
# GUI — identical to original minus GraphHopper and Test Email
# =============================================================

def create_app():
    global BOT_THREAD, _current_theme, _C, _prefs

    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    root = tk.Tk()
    root.title(f"PLUTUS BOT — build {BUILD_VERSION}")
    root.configure(bg=_C["bg"])
    root.minsize(800, 700)
    root.resizable(True, True)

    def _center_window(r):
        sw = r.winfo_screenwidth()
        sh = r.winfo_screenheight()
        w  = min(1100, max(900, int(sw * 0.70)))
        h  = min(int(sh * 0.95), max(800, int(sh * 0.90)))
        x  = (sw - w) // 2
        y  = max(0, (sh - h) // 2)
        r.geometry(f"{w}x{h}+{x}+{y}")

    root.after(0, lambda: _center_window(root))

    _logo_images: dict = {}

    def _load_logo(mode: str):
        if mode in _logo_images:
            return _logo_images[mode]
        raw_path = LOGO_DARK_PATH if mode == "dark" else LOGO_LIGHT_PATH
        fname    = "plutus_logo_dark.png" if mode == "dark" else "plutus_logo_light.png"
        path     = _resolve_logo_path(raw_path, fname)
        try:
            if os.path.exists(path):
                img = Image.open(path)
                img.thumbnail((320, 110), _RESAMPLE_LANCZOS)
                ph  = ImageTk.PhotoImage(img)
                _logo_images[mode] = ph
                return ph
        except Exception as e:
            print(f"[LOGO] Failed to load {path}: {e}")
        return None

    try:
        ico_path = _resolve_logo_path(LOGO_PATH, "plutus_logo.ico")
        if os.path.exists(ico_path):
            root.wm_iconbitmap(ico_path)
        else:
            root.wm_iconbitmap('')
    except Exception:
        pass

    main = tk.Frame(root, bg=_C["bg"])
    main.pack(fill="both", expand=True, padx=20, pady=0)

    # ── HEADER — must be packed FIRST ─────────────────────────────────────
    hdr = tk.Frame(main, bg=_C["bg"], height=110)
    hdr.pack(fill="x", pady=(10, 6))
    hdr.pack_propagate(False)

    hdr_left_spacer = tk.Frame(hdr, bg=_C["bg"])
    hdr_left_spacer.pack(side="left", fill="both", expand=True)

    logo_lbl = tk.Label(hdr, bg=_C["bg"], text="PLUTUS BOT",
                        font=("Segoe UI", 20, "bold"), fg=_C["text"])
    logo_lbl.place(relx=0.5, rely=0.5, anchor="center")

    hdr_right = tk.Frame(hdr, bg=_C["bg"])
    hdr_right.pack(side="right", anchor="e", padx=(0, 4))

    theme_btn_var = tk.StringVar(
        value="☀  Light" if _current_theme == "dark" else "🌙  Dark")
    theme_btn = tk.Button(
        hdr_right, textvariable=theme_btn_var,
        bg=_C["input"], fg=_C["text"],
        activebackground=_C["border"], activeforeground=_C["text"],
        relief="flat", bd=0, padx=10, pady=5,
        font=("Segoe UI", 8), cursor="hand2",
    )
    theme_btn.pack(side="top", anchor="e")

    # ── AI thread-learning toggle — reads current state from the server
    # on launch, flips it via call_set_thread_learning on click. Off by
    # default server-side; this button only ever reflects/changes that
    # server-held flag, never assumes a local default.
    learning_btn_var = tk.StringVar(value="🧠  Learning: …")
    learning_btn_enabled = {"state": False}  # mutable box, closures below read/write it

    def _refresh_learning_btn():
        color = _C["accent"] if learning_btn_enabled["state"] else _C["input"]
        label = "🧠  Learning: ON" if learning_btn_enabled["state"] else "🧠  Learning: OFF"
        learning_btn_var.set(label)
        learning_btn.configure(bg=color)

    def _toggle_learning():
        new_state = not learning_btn_enabled["state"]
        learning_btn.configure(state="disabled")
        def _do():
            result = call_set_thread_learning(ACTIVE_LICENSE_KEY, _get_machine_id(), new_state)
            def _apply():
                if result and result.get("success"):
                    learning_btn_enabled["state"] = result.get("enabled", new_state)
                    _refresh_learning_btn()
                else:
                    messagebox.showerror("Learning toggle",
                                          "Couldn't reach the server to change this setting. Try again.")
                learning_btn.configure(state="normal")
            root.after(0, _apply)
        threading.Thread(target=_do, daemon=True).start()

    learning_btn = tk.Button(
        hdr_right, textvariable=learning_btn_var,
        bg=_C["input"], fg=_C["text"],
        activebackground=_C["border"], activeforeground=_C["text"],
        relief="flat", bd=0, padx=10, pady=5,
        font=("Segoe UI", 8), cursor="hand2",
        command=_toggle_learning,
    )
    learning_btn.pack(side="top", anchor="e", pady=(4, 0))

    def _load_learning_status():
        result = call_get_thread_learning_status(ACTIVE_LICENSE_KEY, _get_machine_id())
        def _apply():
            learning_btn_enabled["state"] = bool(result.get("enabled")) if result else False
            _refresh_learning_btn()
        root.after(0, _apply)
    threading.Thread(target=_load_learning_status, daemon=True).start()

    # ── Telegram on/off — status-only, no button (button removed
    # 2026-09-08 on request). The desktop still needs to KNOW and RESPECT
    # this flag — it's the same telegram_enabled column the web
    # dashboard's own Settings toggle controls, and _telegram_send_one
    # gates on _TELEGRAM_ENABLED on every real send — so this keeps
    # polling status in the background with no UI control of its own;
    # toggling it now only happens from the web dashboard, not here.
    def _load_telegram_status():
        global _TELEGRAM_ENABLED
        result = call_get_telegram_status(ACTIVE_LICENSE_KEY, _get_machine_id())
        def _apply():
            global _TELEGRAM_ENABLED
            # Fail open on a failed/empty response — a status-check
            # hiccup should never silently mute real notifications, so
            # only an explicit result flips this away from the True
            # default set at module load.
            if result and "enabled" in result:
                _TELEGRAM_ENABLED = bool(result.get("enabled"))
        root.after(0, _apply)
    threading.Thread(target=_load_telegram_status, daemon=True).start()

    # Periodic re-check, not just on launch — otherwise toggling this
    # from the web dashboard while the desktop is already running would
    # silently do nothing until the next restart. 5 minutes: frequent
    # enough that a web-side toggle takes effect promptly, infrequent
    # enough to not matter as server load for something that rarely changes.
    def _periodic_telegram_refresh():
        threading.Thread(target=_load_telegram_status, daemon=True).start()
        root.after(5 * 60 * 1000, _periodic_telegram_refresh)
    root.after(5 * 60 * 1000, _periodic_telegram_refresh)

    # ── Manual backfill trigger — only meaningful once learning is ON;
    # the server re-checks the flag itself regardless, so this is safe
    # to leave clickable even if learning is OFF (it'll just no-op).
    backfill_btn = tk.Button(
        hdr_right, text="⏳  Run backfill",
        bg=_C["input"], fg=_C["text"],
        activebackground=_C["border"], activeforeground=_C["text"],
        relief="flat", bd=0, padx=10, pady=5,
        font=("Segoe UI", 8), cursor="hand2",
        command=lambda: threading.Thread(
            target=lambda: messagebox.showinfo("Backfill", str(run_thread_learning_backfill())),
            daemon=True).start(),
    )
    backfill_btn.pack(side="top", anchor="e", pady=(4, 0))

    # ── Automatic periodic backfill (2026-09-09) — replaces the manual
    # "type the rate into Telegram" flow. Every 15 minutes while the bot
    # is actually running, silently re-scans the last few days of
    # labeled threads: an "RC" (Rate Confirmation) label is definitive
    # proof a bid was won, and a "bid"-labeled thread's own dispatcher
    # reply is where the actually-quoted rate gets read from — both go
    # straight into bid_history server-side (thread_learner.py), no
    # dispatcher action needed. Small days_back (3) keeps each run fast/
    # light instead of re-walking the full history every time; the
    # manual "⏳ Run backfill" button above still does a deep 45-day
    # pass on demand. run_thread_learning_backfill() already re-checks
    # the server-side enabled flag itself, so this safely no-ops if
    # thread learning ever gets turned off.
    def _periodic_thread_learning_backfill():
        if not STOP_EVENT.is_set():
            def _run():
                result = run_thread_learning_backfill(days_back=3)
                print(f"[BACKFILL] periodic run: {result}")
            threading.Thread(target=_run, daemon=True).start()
        root.after(15 * 60 * 1000, _periodic_thread_learning_backfill)
    root.after(15 * 60 * 1000, _periodic_thread_learning_backfill)

    gh_status_row = tk.Frame(hdr_right, bg=_C["bg"])
    gh_status_row.pack(side="top", anchor="e", pady=(4, 0))
    gh_lbl = tk.Label(gh_status_row, text="Idle", bg=_C["bg"], fg=_C["text3"],
                      font=("Segoe UI", 10))
    gh_lbl.pack(side="left")
    gh_dot = tk.Label(gh_status_row, text="●", bg=_C["bg"], fg=_C["text3"],
                      font=("Segoe UI", 10))
    gh_dot.pack(side="left", padx=(3, 0))

    # ── SCROLLABLE CONFIG — packed second, no expand ──────────────────────
    cfg_canvas_outer = tk.Frame(main, bg=_C["bg"])
    cfg_canvas_outer.pack(fill="x")

    cfg_canvas = tk.Canvas(cfg_canvas_outer, bg=_C["bg"],
                           highlightthickness=0, bd=0)
    cfg_vsb = tk.Scrollbar(cfg_canvas_outer, orient="vertical",
                            command=cfg_canvas.yview,
                            width=6, relief="flat",
                            bg=_C["surf"], troughcolor=_C["bg"],
                            activebackground=_C["border"])
    cfg_canvas.configure(yscrollcommand=cfg_vsb.set)
    cfg_vsb.pack(side="right", fill="y")
    cfg_canvas.pack(side="left", fill="x", expand=True)

    cfg_pane = tk.Frame(cfg_canvas, bg=_C["bg"])
    cfg_win  = cfg_canvas.create_window((0, 0), window=cfg_pane, anchor="nw")

    def _update_canvas_height():
        root.update_idletasks()
        content_h = cfg_pane.winfo_reqheight()
        win_h     = root.winfo_height()
        hdr_h     = hdr.winfo_height()
        # reserve 50px buttons + 220px log minimum
        available = max(100, win_h - hdr_h - 50 - 220)
        cfg_canvas.configure(height=min(content_h, available))
        cfg_canvas.configure(scrollregion=cfg_canvas.bbox("all"))

    def _on_cfg_configure(event):
        _update_canvas_height()

    def _on_cfg_width(event):
        cfg_canvas.itemconfig(cfg_win, width=event.width)

    cfg_pane.bind("<Configure>", _on_cfg_configure)
    cfg_canvas.bind("<Configure>", _on_cfg_width)

    def _on_mousewheel(event):
        cfg_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    cfg_canvas.bind("<Enter>",
                    lambda e: cfg_canvas.bind_all("<MouseWheel>", _on_mousewheel))
    cfg_canvas.bind("<Leave>",
                    lambda e: cfg_canvas.unbind_all("<MouseWheel>"))

    # ── BOTTOM PANE — packed third, expand=True takes all remaining space ─
    bot_pane = tk.Frame(main, bg=_C["bg"])
    bot_pane.pack(fill="both", expand=True)

    # ── Theme / widget registries ─────────────────────────────────────────
    _all_frames:  list = []
    _all_labels:  list = []
    _all_entries: list = []
    _all_texts:   list = []
    _all_buttons: list = []
    _sep_frames:  list = []

    def _apply_theme_to_all():
        app_bg = "#000000" if _current_theme == "dark" else _C["bg"]
        root.configure(bg=app_bg)
        main.configure(bg=app_bg)
        hdr.configure(bg=app_bg)
        hdr_left_spacer.configure(bg=app_bg)
        hdr_right.configure(bg=app_bg)
        gh_status_row.configure(bg=app_bg)
        logo_lbl.configure(bg=app_bg)
        gh_dot.configure(bg=app_bg)
        gh_lbl.configure(bg=app_bg)
        theme_btn.configure(bg=_C["input"], fg=_C["text"],
                            activebackground=_C["border"])
        _refresh_learning_btn()
        backfill_btn.configure(bg=_C["input"], fg=_C["text"],
                               activebackground=_C["border"])
        cfg_canvas_outer.configure(bg=app_bg)
        cfg_canvas.configure(bg=app_bg)
        cfg_pane.configure(bg=app_bg)
        bot_pane.configure(bg=app_bg)
        cfg_vsb.configure(bg=_C["surf"], troughcolor=app_bg,
                          activebackground=_C["border"])
        try:
            btn_row.configure(bg=app_bg)
        except Exception:
            pass
        for w, role in _all_frames:
            try: w.configure(bg=_C[role])
            except Exception: pass
        for w, role, fg_role in _all_labels:
            try: w.configure(bg=_C[role], fg=_C[fg_role])
            except Exception: pass
        for w in _all_entries:
            try:
                w.configure(bg=_C["input"], fg=_C["text"],
                            insertbackground=_C["text"],
                            highlightbackground=_C["border"],
                            highlightcolor=_C["accent"])
            except Exception: pass
        for w in _all_texts:
            try:
                w.configure(bg=_C["input"], fg=_C["text"],
                            insertbackground=_C["text"],
                            selectbackground=_C["accent"],
                            highlightbackground=_C["border"],
                            highlightcolor=_C["accent"])
            except Exception: pass
        for w in _sep_frames:
            try: w.configure(bg=_C["border"])
            except Exception: pass
        try:
            log_box.configure(bg=_C["log_bg"], fg=_C["text"],
                              selectbackground=_C["accent"])
            log_sb.configure(bg=_C["surf"], troughcolor=_C["bg"],
                             activebackground=_C["border"])
            log_outer.configure(bg=_C["surf"])
            log_hdr.configure(bg=_C["surf"])
            log_body.configure(bg=_C["surf"])
            search_row.configure(bg=_C["surf"])
            clr_lbl.configure(bg=_C["surf"], fg=_C["accent"])
            log_search_icon.configure(bg=_C["surf"], fg=_C["text3"])
            match_lbl.configure(bg=_C["surf"], fg=_C["text3"])
            search_ent.configure(bg=_C["input"], fg=_C["text"],
                                 insertbackground=_C["text"],
                                 highlightbackground=_C["border"],
                                 highlightcolor=_C["accent"])
            log_hdr_lbl.configure(bg=_C["surf"], fg=_C["text"])
            log_sep.configure(bg=_C["border"])
        except Exception: pass
        ph = _load_logo(_current_theme)
        if ph:
            logo_lbl.configure(image=ph, text="", compound="none", bg=app_bg)
            # No need to also stash `ph` on the widget to prevent GC — it's
            # already retained by the _logo_images cache dict in _load_logo(),
            # which lives for the lifetime of create_app()'s closure.
        else:
            logo_lbl.configure(image="", text="PLUTUS BOT",
                               font=("Segoe UI", 20, "bold"),
                               fg=_C["text"], bg=app_bg)
        logo_lbl.place(relx=0.5, rely=0.5, anchor="center")
        _apply_titlebar(root, dark=(_current_theme == "dark"))

    def toggle_theme():
        global _current_theme, _C
        _current_theme = "light" if _current_theme == "dark" else "dark"
        _C = dict(_THEMES[_current_theme])
        _prefs["theme"] = _current_theme
        _save_prefs(_prefs)
        theme_btn_var.set("☀  Light" if _current_theme == "dark" else "🌙  Dark")
        _apply_theme_to_all()

    theme_btn.config(command=toggle_theme)

    def set_graphhopper_status(text):
        def _up():
            gh_lbl.config(text=text)
            if any(x in text for x in ("Working", "✅", "Server")):  c = _C["green"]
            elif any(x in text for x in ("Failed", "❌")):            c = _C["red"]
            elif "Starting" in text:                                   c = _C["yellow"]
            else:                                                      c = _C["text3"]
            gh_dot.config(fg=c)
            gh_lbl.config(fg=c)
        root.after(0, _up)

    # ── TOOLTIP ───────────────────────────────────────────────────────────
    class _Tooltip:
        def __init__(self, widget, text):
            self.widget = widget
            self.text   = text
            self.tw     = None
            widget.bind("<Enter>", self._show)
            widget.bind("<Leave>", self._hide)

        def _show(self, _=None):
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.tw = tk.Toplevel(self.widget)
            self.tw.wm_overrideredirect(True)
            self.tw.wm_geometry(f"+{x}+{y}")
            fr = tk.Frame(self.tw, bg="#1a1a2e", bd=1, relief="solid")
            fr.pack()
            tk.Label(fr, text=self.text, bg="#1a1a2e", fg="#ffffff",
                     font=("Segoe UI", 10), padx=10, pady=6,
                     justify="left", wraplength=400).pack()

        def _hide(self, _=None):
            if self.tw:
                self.tw.destroy()
                self.tw = None

    # ── SECTION / FIELD HELPERS ───────────────────────────────────────────
    def _section(parent, title, expand=False):
        outer = tk.Frame(parent, bg=_C["surf"])
        outer.pack(fill="x", pady=(0, 10))
        _all_frames.append((outer, "surf"))
        lbl = tk.Label(outer, text=f"  {title}", bg=_C["surf"], fg=_C["text"],
                       font=("Segoe UI", 10, "bold"))
        lbl.pack(anchor="w", pady=(9, 0))
        _all_labels.append((lbl, "surf", "text"))
        sep = tk.Frame(outer, bg=_C["border"], height=1)
        sep.pack(fill="x", padx=8, pady=(4, 0))
        _sep_frames.append(sep)
        body = tk.Frame(outer, bg=_C["surf"])
        body.pack(fill="x", padx=12, pady=(8, 12))
        _all_frames.append((body, "surf"))
        return body

    def _field_row(parent, label, default="", hint="", tooltip=""):
        row = tk.Frame(parent, bg=_C["surf"])
        row.pack(fill="x", pady=2)
        _all_frames.append((row, "surf"))
        lbl = tk.Label(row, text=label, bg=_C["surf"], fg=_C["text"],
                       font=("Segoe UI", 10), width=18, anchor="w")
        lbl.pack(side="left")
        _all_labels.append((lbl, "surf", "text"))
        ent = tk.Entry(row, bg=_C["input"], fg=_C["text"],
                       insertbackground=_C["text"],
                       relief="flat", font=("Segoe UI", 10), highlightthickness=1,
                       highlightbackground=_C["border"], highlightcolor=_C["accent"])
        ent.insert(0, default)
        ent.pack(side="left", fill="x", expand=True, ipady=4)
        _all_entries.append(ent)
        if tooltip:
            _Tooltip(ent, tooltip)
            _Tooltip(lbl, tooltip)
        if hint:
            hint_lbl = tk.Label(parent, text=f"  {hint}",
                                bg=_C["surf"], fg=_C["text"],
                                font=("Segoe UI", 10),
                                anchor="w", justify="left", wraplength=800)
            hint_lbl.pack(fill="x", pady=(0, 2))
            _all_labels.append((hint_lbl, "surf", "text"))
        return ent

    def _theme_text(widget):
        widget.config(
            bg=_C["input"], fg=_C["text"], insertbackground=_C["text"],
            selectbackground=_C["accent"], selectforeground=_C["text"],
            relief="flat", highlightthickness=1,
            highlightbackground=_C["border"], highlightcolor=_C["accent"],
        )
        _all_texts.append(widget)

    def _btn(parent, text, command, bg, fg="#ffffff", hover=None, size="md", **pack_kw):
        pad  = {"lg": (20, 9), "md": (14, 7), "sm": (10, 5)}.get(size, (14, 7))
        font = ("Segoe UI", 10, "bold") if size == "lg" else ("Segoe UI", 9, "bold")
        b = tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=fg,
            activebackground=hover or bg, activeforeground=fg,
            font=font, relief="flat", bd=0,
            padx=pad[0], pady=pad[1], cursor="hand2",
        )
        if hover:
            b.bind("<Enter>", lambda _: b.config(bg=hover))
            b.bind("<Leave>", lambda _: b.config(bg=bg))
        b.pack(**pack_kw)
        return b

    def _mini(parent, lbl, val, w=8, tooltip=""):
        lb = tk.Label(parent, text=lbl, bg=_C["surf"], fg=_C["text"],
                      font=("Segoe UI", 10))
        lb.pack(side="left", padx=(0, 4))
        _all_labels.append((lb, "surf", "text"))
        e = tk.Entry(parent, bg=_C["input"], fg=_C["text"],
                     insertbackground=_C["text"],
                     relief="flat", font=("Segoe UI", 10), width=w,
                     highlightthickness=1,
                     highlightbackground=_C["border"], highlightcolor=_C["accent"])
        e.insert(0, val)
        e.pack(side="left", ipady=3, padx=(0, 20))
        _all_entries.append(e)
        if tooltip:
            _Tooltip(e, tooltip)
            _Tooltip(lb, tooltip)
        return e

    # ── BID TEMPLATE WINDOW ───────────────────────────────────────────────
    def _open_bid_template():
        win = tk.Toplevel(root)
        win.title("Bid Template")
        win.configure(bg=_C["bg"])
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = 650, 700
        win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        win.minsize(550, 550)

        outer = tk.Frame(win, bg=_C["bg"])
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(outer, text="📝  BID TEMPLATE",
                 bg=_C["bg"], fg=_C["text"],
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 4))

        tk.Label(outer,
                 text="This text is pre-filled when you click BID on Telegram.\n"
                      "Variables in {curly braces} are automatically replaced with load data.",
                 bg=_C["bg"], fg=_C["text"],
                 font=("Segoe UI", 10), justify="left").pack(anchor="w", pady=(0, 8))

        tbox = tk.Text(outer, font=("Consolas", 10), height=8,
                       bg=_C["input"], fg=_C["text"],
                       insertbackground=_C["text"],
                       selectbackground=_C["accent"],
                       relief="flat", highlightthickness=1,
                       highlightbackground=_C["border"],
                       highlightcolor=_C["accent"])
        tbox.insert("1.0", BID_TEMPLATE)
        tbox.pack(fill="x", pady=(0, 8))

        # ── Buttons — pack BEFORE the scrollable area so it always
        #    reserves its space at the bottom, regardless of content ──────
        btn_f = tk.Frame(outer, bg=_C["bg"], height=50)
        btn_f.pack(fill="x", pady=(8, 0), side="bottom")
        btn_f.pack_propagate(False)

        def _save_template():
            global BID_TEMPLATE
            with BID_TEMPLATE_LOCK:
                BID_TEMPLATE = tbox.get("1.0", "end").strip()
            c = _load_config()
            c["bid_template"] = BID_TEMPLATE
            _save_config(c)
            win.destroy()

        tk.Button(btn_f, text="💾  Save & Close", command=_save_template,
                  bg="#1a7f4b", fg="#ffffff",
                  activebackground="#22a05e", activeforeground="#ffffff",
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  padx=16, pady=7, cursor="hand2").pack(side="left", padx=(0, 8), pady=8)

        tk.Button(btn_f, text="Cancel", command=win.destroy,
                  bg=_C["input"], fg=_C["text2"],
                  activebackground=_C["border"], activeforeground=_C["text"],
                  font=("Segoe UI", 10), relief="flat",
                  padx=12, pady=7, cursor="hand2").pack(side="left", pady=8)

        # ── Scrollable variables reference — fills remaining space ─────────
        ref_outer = tk.Frame(outer, bg=_C["input"])
        ref_outer.pack(fill="both", expand=True, pady=(0, 8))

        ref_canvas = tk.Canvas(ref_outer, bg=_C["input"], highlightthickness=0)
        ref_vsb = tk.Scrollbar(ref_outer, orient="vertical",
                               command=ref_canvas.yview, width=8)
        ref_canvas.configure(yscrollcommand=ref_vsb.set)
        ref_vsb.pack(side="right", fill="y")
        ref_canvas.pack(side="left", fill="both", expand=True)

        ref_inner = tk.Frame(ref_canvas, bg=_C["input"])
        ref_win = ref_canvas.create_window((0, 0), window=ref_inner, anchor="nw")

        def _ref_resize(e):
            ref_canvas.itemconfig(ref_win, width=e.width)

        def _ref_scroll(e):
            ref_canvas.configure(scrollregion=ref_canvas.bbox("all"))

        ref_canvas.bind("<Configure>", _ref_resize)
        ref_inner.bind("<Configure>", _ref_scroll)

        def _ref_mousewheel(e):
            ref_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        ref_canvas.bind("<Enter>",
                        lambda e: ref_canvas.bind_all("<MouseWheel>", _ref_mousewheel))
        ref_canvas.bind("<Leave>",
                        lambda e: ref_canvas.unbind_all("<MouseWheel>"))

        tk.Label(ref_inner, text="AVAILABLE VARIABLES:",
                 bg=_C["input"], fg=_C["accent"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 4))

        for var, desc in [
            ("{truck_dimensions}",   "Truck dimensions  e.g. 264x97x103"),
            ("{google_deadhead}",    "Miles from truck to pickup"),
            ("{truck_equipment}",    "Equipment  e.g. Dock High, Air Ride"),
            ("{deadhead_eta_str}",   "Estimated travel time to pickup"),
            ("{vehicle_type}",       "Vehicle type  e.g. LARGE STRAIGHT"),
            ("{driver_name}",        "Driver's name"),
            ("{pickup_loc}",         "Pickup location"),
            ("{pickup_dt}",          "Pickup date and time"),
            ("{pickup_date_only}",   "Pickup date without time"),
            ("{delivery_loc}",       "Delivery location"),
            ("{delivery_dt}",        "Delivery date and time"),
            ("{delivery_date_only}", "Delivery date without time"),
            ("{deadhead_miles}",     "Deadhead miles (same as google_deadhead)"),
            ("{order}",              "Order number"),
            ("{broker_name}",        "Broker name"),
            ("{vehicle_required}",   "Vehicle required from email"),
        ]:
            r = tk.Frame(ref_inner, bg=_C["input"])
            r.pack(fill="x", pady=1, padx=10)
            tk.Label(r, text=var, bg=_C["input"], fg=_C["green"],
                     font=("Consolas", 10), width=22, anchor="w").pack(side="left")
            tk.Label(r, text=f"—  {desc}", bg=_C["input"], fg=_C["text"],
                     font=("Segoe UI", 10)).pack(side="left")

        win.grab_set()
        win.focus_set()
        tbox.focus_set()

    # ── CONFIGURATION SECTION ─────────────────────────────────────────────
    cfg_sec = _section(cfg_pane, "⚙  CONFIGURATION")

    v_ent = _field_row(cfg_sec, "Vehicle Types", "",
        hint="Comma-separated list of vehicle types to watch.  "
             "Example: LARGE STRAIGHT, CARGO VAN, SPRINTER",
        tooltip="Enter the vehicle types you want to monitor.\n"
                "Separate multiple types with commas.\n\n"
                "Common types:\n"
                "  LARGE STRAIGHT\n"
                "  SMALL STRAIGHT\n"
                "  CARGO VAN\n"
                "  SPRINTER")

    chatids_ent = _field_row(cfg_sec, "Telegram Chat IDs", str(CHAT_IDS[0]),
        hint="Your Telegram Chat ID — where load alerts are sent.  "
             "Get it by messaging @userinfobot on Telegram.  "
             "Multiple IDs: separate with commas.",
        tooltip="Your Telegram Chat ID — where load alerts get sent.\n\n"
                "How to get your Chat ID:\n"
                "  1. Open Telegram\n"
                "  2. Search @userinfobot\n"
                "  3. Send any message\n"
                "  4. It replies with your ID\n\n"
                "Multiple IDs: 1234567, 9876543")
    

    row2 = tk.Frame(cfg_sec, bg=_C["surf"])
    row2.pack(fill="x", pady=2)
    _all_frames.append((row2, "surf"))

    r_ent = _mini(row2, "Max Radius (mi)", "200",
        tooltip="Max deadhead distance in miles.\n"
                "Only loads within this range of your truck\n"
                "will be shown.\n\nRecommended: 150–300")
    p_ent = _mini(row2, "Poll Interval (s)", "0", w=6,
        tooltip="How often to check for emails.\n"
                "0 = instant (recommended)")

    # ── TRUCKS SECTION ────────────────────────────────────────────────────
    trk_sec = _section(cfg_pane,
        "🚛  TRUCKS  ·  VEHICLE:DRIVER:DIMS:PAYLOAD:EQUIPMENT:STATES:ZIP[:DATE[:RADIUS[:CHAT_ID]]]")

    guide_frame = tk.Frame(trk_sec, bg=_C["input"], padx=8, pady=6)
    guide_frame.pack(fill="x", pady=(0, 6))
    _all_frames.append((guide_frame, "input"))

    tk.Label(guide_frame,
             text="FORMAT — separate each field with a colon  :",
             bg=_C["input"], fg=_C["accent"],
             font=("Segoe UI", 10, "bold")).pack(anchor="w")
    tk.Label(guide_frame,
             text="VEHICLE : DRIVER : LxWxH : MAX LBS : EQUIPMENT : STATES : ZIP : DATE : RADIUS : CHAT_ID",
             bg=_C["input"], fg=_C["text"],
             font=("Consolas", 10)).pack(anchor="w", pady=(2, 2))
    tk.Label(guide_frame,
             text="Example:  LARGE STRAIGHT:John Smith:264x97x103:26000"
                  ":Dock High,Air Ride:OH,PA,NY:44129:05/29/26:150:111222333",
             bg=_C["input"], fg=_C["green"],
             font=("Consolas", 10)).pack(anchor="w")
    tk.Label(guide_frame,
             text="STATES: blank = all states  |  codes: OH,PA,NY  |  "
                  "regions: East Coast · Midwest · West Coast          "
                  "DATE: blank = any  |  format: MM/DD/YY          "
                  "RADIUS: blank = use Max radius above  |  e.g. 150",
             bg=_C["input"], fg=_C["text"],
             font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 0))
    tk.Label(guide_frame,
             text="CHAT_ID: this driver's own Telegram chat ID with the driver "
                  "bot — blank = driver bot skips them  |  get it from @userinfobot",
             bg=_C["input"], fg=_C["text"],
             font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 0))

    t_box = tk.Text(trk_sec, font=("Consolas", 12), height=15)
    _theme_text(t_box)
    t_box.insert("1.0",
        "LARGE STRAIGHT:John Smith:264x97x103:8000"
        ":Dock High,Air Ride:OH,PA,NY:44129::150"
    )
    t_box.pack(fill="x")
    _Tooltip(t_box,
             "One truck per line, fields separated by  :\n\n"
             "VEHICLE   — must match Vehicle Types above\n"
             "DRIVER    — driver's name\n"
             "DIMS      — LxWxH in inches, e.g. 264x97x103\n"
             "PAYLOAD   — max weight in lbs, e.g. 26000\n"
             "EQUIPMENT — e.g. Dock High,Air Ride,Lift Gate\n"
             "STATES    — blank=all, or OH,PA or East Coast\n"
             "ZIP       — truck's current location\n"
             "DATE      — optional, e.g. 05/29/26\n"
             "RADIUS    — optional, this truck's own max radius in "
             "miles (e.g. 150) — blank uses the Max radius setting "
             "above instead\n"
             "CHAT_ID   — optional, this driver's own Telegram chat ID "
             "— set it and the driver bot sends them load cards and "
             "lets them tap BID; blank = driver bot skips them. Get a "
             "driver's chat ID by having them message @userinfobot")

    # ── Load persisted config ─────────────────────────────────────────────
    _cfg = _load_config()
    if _cfg.get("vehicle_types"):
        v_ent.delete(0, "end"); v_ent.insert(0, _cfg["vehicle_types"])
    if _cfg.get("chat_ids"):
        chatids_ent.delete(0, "end"); chatids_ent.insert(0, _cfg["chat_ids"])
    if _cfg.get("max_radius"):
        r_ent.delete(0, "end"); r_ent.insert(0, _cfg["max_radius"])
    if _cfg.get("poll_interval"):
        p_ent.delete(0, "end"); p_ent.insert(0, _cfg["poll_interval"])
    if _cfg.get("trucks"):
        t_box.delete("1.0", "end"); t_box.insert("1.0", _cfg["trucks"])
    if _cfg.get("bid_template"):
        global BID_TEMPLATE
        BID_TEMPLATE = _cfg["bid_template"]
    
    # ── BUTTON ROW ────────────────────────────────────────────────────────
    btn_row = tk.Frame(bot_pane, bg=_C["bg"])
    btn_row.pack(fill="x", pady=(4, 6))

    start_btn = _btn(btn_row, "▶  START", lambda: None,
                     "#1a7f4b", fg="#ffffff", hover="#22a05e",
                     size="lg", side="left", padx=(0, 8))
    stop_btn  = _btn(btn_row, "⏹  STOP", lambda: None,
                     "#3a3a3c", fg=_C["text3"],
                     size="lg", side="left", padx=(0, 8))
    mark_read_btn = _btn(btn_row, "✉  Mark All Read",
         lambda: make_all_mail_read_from_gui(log),
         "#1c3d6b", fg="#ffffff", hover="#199cc4",
         size="sm", side="left", padx=(0, 6))
    bid_tpl_btn = _btn(btn_row, "📝  Bid Template",
         lambda: _open_bid_template(),
         "#1c3d6b", fg="#ffffff", hover="#199cc4",
         size="sm", side="left")

    _Tooltip(start_btn,
             "Start monitoring Gmail for new loads.\n"
             "Alerts will be sent to your Telegram.")
    _Tooltip(stop_btn, "Stop the bot.")
    _Tooltip(mark_read_btn,
             "Mark all unread emails as read.\n"
             "Use this on first launch to clear old emails.")
    _Tooltip(bid_tpl_btn,
             "Edit the bid template.\n"
             "Pre-filled when you click BID in Telegram.")

    # ── LOG SECTION — fills all remaining space in bot_pane ───────────────
    log_outer = tk.Frame(bot_pane, bg=_C["surf"])
    log_outer.pack(fill="both", expand=True)

    log_hdr = tk.Frame(log_outer, bg=_C["surf"])
    log_hdr.pack(fill="x")
    log_hdr_lbl = tk.Label(log_hdr, text="  📋  LIVE LOG",
                            bg=_C["surf"], fg=_C["text"],
                            font=("Segoe UI", 10, "bold"))
    log_hdr_lbl.pack(side="left", pady=(7, 0))
    clr_lbl = tk.Label(log_hdr, text="Clear  ", bg=_C["surf"], fg=_C["accent"],
                       font=("Segoe UI", 10), cursor="hand2")
    clr_lbl.pack(side="right", pady=(7, 0))

    search_row = tk.Frame(log_outer, bg=_C["surf"])
    search_row.pack(fill="x", padx=10, pady=(4, 0))
    log_search_icon = tk.Label(search_row, text="🔍", bg=_C["surf"],
                                fg=_C["text3"], font=("Segoe UI", 10))
    log_search_icon.pack(side="left", padx=(0, 4))
    search_var = tk.StringVar()
    search_ent = tk.Entry(
        search_row, textvariable=search_var,
        bg=_C["input"], fg=_C["text"], insertbackground=_C["text"],
        relief="flat", font=("Consolas", 10), highlightthickness=1,
        highlightbackground=_C["border"], highlightcolor=_C["accent"],
    )
    search_ent.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 6))
    match_lbl = tk.Label(search_row, text="", bg=_C["surf"], fg=_C["text3"],
                         font=("Segoe UI", 10), width=8, anchor="e")
    match_lbl.pack(side="left")

    def _nav_btn_small(txt, cmd):
        b = tk.Button(search_row, text=txt, command=cmd,
                      bg=_C["input"], fg=_C["text"],
                      activebackground=_C["border"], activeforeground=_C["text"],
                      relief="flat", bd=0, padx=6, pady=2,
                      font=("Segoe UI", 10), cursor="hand2")
        b.pack(side="left", padx=(2, 0))
        _all_buttons.append(b)
        return b

    _nav_btn_small("▲", lambda: _nav_prev())
    _nav_btn_small("▼", lambda: _nav_next())

    log_sep = tk.Frame(log_outer, bg=_C["border"], height=1)
    log_sep.pack(fill="x", padx=6, pady=(4, 0))

    log_body = tk.Frame(log_outer, bg=_C["surf"])
    log_body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
    log_sb = tk.Scrollbar(log_body, bg=_C["surf"], troughcolor=_C["bg"],
                           activebackground=_C["border"], relief="flat", width=7)
    log_sb.pack(side="right", fill="y")
    log_box = tk.Text(log_body, bg=_C["log_bg"], fg=_C["text"],
                      insertbackground=_C["text"],
                      relief="flat", font=("Consolas", 10), state="disabled",
                      highlightthickness=0, selectbackground=_C["accent"],
                      yscrollcommand=log_sb.set)
    log_box.pack(side="left", fill="both", expand=True)
    log_sb.config(command=log_box.yview)

    log_box.tag_configure("ok",             foreground=_C["green"])
    log_box.tag_configure("skip",           foreground=_C["yellow"])
    log_box.tag_configure("err",            foreground=_C["red"])
    log_box.tag_configure("sys",            foreground=_C["text2"])
    log_box.tag_configure("clean",          foreground=_C["cyan"])
    log_box.tag_configure("search_match",   background="#3a3000", foreground="#ffd60a")
    log_box.tag_configure("search_current", background="#0a84ff",  foreground="#ffffff")

    _search_matches = []
    _search_cursor  = [-1]

    def _do_search(*_):
        log_box.tag_remove("search_match",   "1.0", "end")
        log_box.tag_remove("search_current", "1.0", "end")
        _search_matches.clear()
        _search_cursor[0] = -1
        term = search_var.get()
        if not term:
            match_lbl.config(text="")
            return
        start = "1.0"
        while True:
            pos = log_box.search(term, start, stopindex="end",
                                  nocase=True, exact=True)
            if not pos:
                break
            end = f"{pos}+{len(term)}c"
            log_box.tag_add("search_match", pos, end)
            _search_matches.append((pos, end))
            start = end
        total = len(_search_matches)
        if total == 0:
            match_lbl.config(text="no match", fg=_C["red"])
        else:
            _search_cursor[0] = 0
            _highlight_current()
            match_lbl.config(fg=_C["text3"])

    def _highlight_current():
        log_box.tag_remove("search_current", "1.0", "end")
        if not _search_matches:
            match_lbl.config(text="")
            return
        idx = _search_cursor[0]
        pos, end = _search_matches[idx]
        log_box.tag_add("search_current", pos, end)
        log_box.see(pos)
        match_lbl.config(text=f"{idx+1} / {len(_search_matches)}", fg=_C["text3"])

    def _nav_next(*_):
        if not _search_matches: return
        _search_cursor[0] = (_search_cursor[0] + 1) % len(_search_matches)
        _highlight_current()

    def _nav_prev(*_):
        if not _search_matches: return
        _search_cursor[0] = (_search_cursor[0] - 1) % len(_search_matches)
        _highlight_current()

    def _refresh_search():
        if search_var.get(): _do_search()

    search_var.trace_add("write", _do_search)
    search_ent.bind("<Return>",       _nav_next)
    search_ent.bind("<Shift-Return>", _nav_prev)

    def _focus_search(event=None):
        search_ent.focus_set()
        search_ent.select_range(0, "end")
        return "break"

    def _escape_search(event=None):
        search_var.set("")
        log_box.focus_set()

    root.bind("<Control-f>", _focus_search)
    root.bind("<Control-F>", _focus_search)
    search_ent.bind("<Escape>", _escape_search)

    def log(msg):
        def _w():
            log_box.config(state="normal")
            tag = "sys"
            if   "✅" in msg:                     tag = "ok"
            elif "⏭" in msg or "SKIPPED" in msg: tag = "skip"
            elif "❌" in msg or "[ERR]" in msg:   tag = "err"
            elif "🗑" in msg:                      tag = "clean"
            log_box.insert("end", msg + "\n", tag)
            log_box.see("end")
            log_box.config(state="disabled")
            _refresh_search()
        root.after(0, _w)

    def clear_log(_=None):
        log_box.config(state="normal")
        log_box.delete("1.0", "end")
        log_box.config(state="disabled")
        search_var.set("")
        match_lbl.config(text="")

    clr_lbl.bind("<Button-1>", clear_log)

    def _set_running(running: bool):
        if running:
            start_btn.config(bg="#2a2a2c", fg=_C["text3"],
                             activebackground="#2a2a2c",
                             cursor="arrow", state="disabled")
            stop_btn.config(bg="#9b1c1c", fg="#ffffff",
                            activebackground="#b91c1c",
                            cursor="hand2", state="normal")
            stop_btn.bind("<Enter>", lambda _: stop_btn.config(bg="#b91c1c"))
            stop_btn.bind("<Leave>", lambda _: stop_btn.config(bg="#9b1c1c"))
        else:
            start_btn.config(bg="#1a7f4b", fg="#ffffff",
                             activebackground="#22a05e",
                             cursor="hand2", state="normal")
            stop_btn.config(bg="#3a3a3c", fg=_C["text3"],
                            activebackground="#3a3a3c",
                            cursor="arrow", state="disabled")
            stop_btn.unbind("<Enter>")
            stop_btn.unbind("<Leave>")

    _set_running(False)

    def start_bot():
        global BOT_THREAD, BID_TEMPLATE, MC_NUMBER
        if BOT_THREAD is not None and BOT_THREAD.is_alive():
            messagebox.showinfo("MailBot",
                                "Bot is still shutting down — please wait.")
            return
        errors = validate_truck_definitions(t_box.get("1.0", "end"))
        if errors:
            messagebox.showerror("Truck Definition Errors",
                                 "Fix before starting:\n\n" + "\n".join(errors))
            return
        if not v_ent.get().strip():
            messagebox.showerror("Missing Field",
                                 "Vehicle Types cannot be empty.\n"
                                 "Example: LARGE STRAIGHT")
            return
        if not chatids_ent.get().strip():
            messagebox.showerror("Missing Field",
                                 "Telegram Chat ID cannot be empty.\n"
                                 "Message @userinfobot on Telegram to get your ID.")
            return

       

        _save_config({
            "vehicle_types":  v_ent.get().strip(),
            "chat_ids":       chatids_ent.get().strip(),
            "max_radius":     r_ent.get().strip(),
            "poll_interval":  p_ent.get().strip(),
            "trucks":         t_box.get("1.0", "end").strip(),
            "bid_template":   BID_TEMPLATE
        })
        STOP_EVENT.clear()
        _set_running(True)
        set_graphhopper_status("Server-side ✅")
        log("▶  Starting MailBot…")

        # Pressing START means the dispatcher wants to actively run —
        # Telegram notifications should just work, not stay silently
        # suppressed because something else (the web dashboard's own
        # Settings toggle, which shares this same flag) turned it off
        # for an unrelated reason. Requested 2026-09-08, and plausibly
        # the actual root cause of an earlier "log said sent, nothing
        # arrived" report — if the flag was off, that send would have
        # been silently suppressed with no error at all. Set locally
        # first (takes effect immediately, this session), then persist
        # server-side on a background thread (so it stays on across
        # the periodic 5-minute re-check, and the web dashboard's
        # Settings page reflects reality too) — never block START on
        # the network round trip.
        global _TELEGRAM_ENABLED
        _TELEGRAM_ENABLED = True

        def _enable_telegram_serverside():
            result = call_set_telegram_enabled(ACTIVE_LICENSE_KEY, _get_machine_id(), True)
            if not (result and result.get("success")):
                log("⚠ Couldn't confirm Telegram enabled with the server — "
                    "notifications will still send this session, but the "
                    "toggle may not stay on long-term.")
        threading.Thread(target=_enable_telegram_serverside, daemon=True).start()

        # Same reasoning as Telegram just above, for thread learning
        # (2026-09-09): the client shouldn't have to remember to flip
        # this on for bid_amount/win-loss to start filling in
        # automatically — pressing START should just make it work.
        # The header "🧠 Learning" button still exists and can turn it
        # back off; this only changes the default at START time.
        def _enable_learning_serverside():
            result = call_set_thread_learning(ACTIVE_LICENSE_KEY, _get_machine_id(), True)
            if result and result.get("success"):
                root.after(0, lambda: (
                    learning_btn_enabled.__setitem__("state", result.get("enabled", True)),
                    _refresh_learning_btn(),
                ))
            else:
                log("⚠ Couldn't confirm thread learning enabled with the server.")
        threading.Thread(target=_enable_learning_serverside, daemon=True).start()

        BOT_THREAD = threading.Thread(
            target=run_bot_from_gui,
            args=(v_ent.get(), t_box.get("1.0", "end"),
                  r_ent.get(), p_ent.get(), log,
                  set_graphhopper_status,
                  "",
                  chatids_ent.get()),
            daemon=True,
        )
        BOT_THREAD.start()

        def _watch():
            if BOT_THREAD and BOT_THREAD.is_alive():
                root.after(500, _watch)
            else:
                _set_running(False)
                set_graphhopper_status("Stopped")

        root.after(500, _watch)

    def stop_bot():
        global BOT_THREAD
        STOP_EVENT.set()
        BOT_THREAD = None
        _set_running(False)
        set_graphhopper_status("Stopped")
        log("⏹  Bot stopped.")
        if _DRIVER_BOT_ENABLED and driver_bot is not None:
            threading.Thread(target=driver_bot.shutdown, daemon=True).start()

    start_btn.config(command=start_bot)
    stop_btn.config(command=stop_bot)

    def on_close():
        STOP_EVENT.set()
        if _DRIVER_BOT_ENABLED and driver_bot is not None:
            threading.Thread(target=driver_bot.shutdown, daemon=True).start()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def _on_root_resize(event):
        if event.widget is root:
            _update_canvas_height()

    root.bind("<Configure>", _on_root_resize)

    root.after(10, _apply_theme_to_all)
    root.after(80, lambda: _apply_titlebar(root, dark=(_current_theme == "dark")))

    root.mainloop()

# =============================================================
# DRIVER BOT — activation
#
# driver_bot.py is a fully self-contained module (own Telegram bot
# token/session/poll loop) that was written but never actually wired
# into the dispatcher — confirmed 2026-09-11: zero references to it
# anywhere in this file before this. Following its own documented
# integration contract (top of driver_bot.py): builds driver_config.json
# from whatever's currently in TRUCKS/CHAT_IDS/BOT_TOKEN (so drivers are
# configured the exact same place trucks already are — the CHAT_ID field
# on a truck's own line — instead of a separate file the client would
# have to hand-edit) and starts it.
# =============================================================

def _init_driver_bot():
    global _DRIVER_BOT_ENABLED
    if driver_bot is None:
        _DRIVER_BOT_ENABLED = False
        return  # import failed at startup — already logged, silent no-op

    # Guard against a second poll thread — driver_bot.init() itself has
    # no such check, and pressing START twice without STOP in between
    # would otherwise start two independent pollers on the same bot
    # token (duplicate driver notifications, same bug class as the
    # dispatcher-side reply/duplicate-notify fix from 2026-09-11).
    _existing = getattr(driver_bot, "_POLL_THREAD", None)
    if _DRIVER_BOT_ENABLED and _existing is not None and _existing.is_alive():
        print("Driver bot already running — not starting a second poller.")
        return

    _DRIVER_BOT_ENABLED = False
    drivers = [
        {"name": t.get("driver_name", ""), "telegram_chat_id": t["telegram_chat_id"],
         "truck_type": t.get("vehicle", "")}
        for t in TRUCKS if t.get("telegram_chat_id")
    ]
    if not drivers:
        return  # no truck line has a CHAT_ID set — nothing to activate
    if not DRIVER_BOT_TOKEN:
        print("Driver bot: drivers configured but DRIVER_BOT_TOKEN is "
              "blank — staying off. Set it in main copy.py and rebuild.")
        return

    with _CHAT_IDS_LOCK:
        dispatcher_ids = list(CHAT_IDS)
    driver_bot.save_config({
        "driver_bot_token":     DRIVER_BOT_TOKEN,
        "dispatcher_bot_token": BOT_TOKEN,
        "dispatcher_chat_ids":  dispatcher_ids,
        "drivers":              drivers,
    })
    try:
        _DRIVER_BOT_ENABLED = driver_bot.init(ACTIVE_LICENSE_KEY, _get_machine_id())
    except Exception as e:
        print(f"driver_bot.init() failed (driver bot disabled): {e}")
        _DRIVER_BOT_ENABLED = False
    if _DRIVER_BOT_ENABLED:
        print(f"Driver bot active — {len(drivers)} driver(s) configured.")


# =============================================================
# BOT RUNNER
# =============================================================

def run_bot_from_gui(vehicles, truck_text, radius_txt, poll_txt,
                     log_func, gh_status_func, delivery_states_txt="",
                     chat_ids_txt=""):
    global TRUCKS, CHAT_IDS

    parsed_ids = []
    for tok in (chat_ids_txt or "").split(","):
        tok = tok.strip()
        if re.fullmatch(r"-?\d+", tok):
            parsed_ids.append(int(tok))
    if parsed_ids:
        with _CHAT_IDS_LOCK:
            CHAT_IDS = parsed_ids
        log_func(f"Telegram Chat IDs: {CHAT_IDS}")
    else:
        log_func(f"Telegram Chat IDs: using default {CHAT_IDS}")

    TRUCKS = parse_truck_definitions(truck_text)
    _init_driver_bot()
    STOP_EVENT.clear()

    del_states_raw = (delivery_states_txt or "").strip()
    allowed_delivery_states = (
        {s.strip().upper() for s in del_states_raw.split(",") if s.strip()}
        if del_states_raw else None
    )

    gh_status_func("Server-side ✅")
    log_func("▶ GraphHopper runs server-side — no local startup needed.")

    main_loop(
        float(poll_txt),
        [v.strip().upper() for v in vehicles.split(",") if v.strip()],
        int(radius_txt),
        log_func=log_func,
        allowed_delivery_states=allowed_delivery_states,
    )

# =============================================================
# MARK ALL READ
#
# Rewritten 2026-09-08 — real-world bug: at 63,000 real unread messages,
# the original version (one Gmail messages().get() call per message,
# strictly serial, no progress output at all) was projected to take
# 1.5-4+ hours with zero visibility into whether it was even still
# running (confirmed live against the real account: ~279ms/message
# serial). Fixed: the per-message label-check now fires concurrently
# instead of one at a time, and progress is logged after every page
# (~500 messages) instead of only once at the very start and once at
# the very end.
#
# IMPORTANT: this deliberately does NOT reuse googleapiclient/httplib2
# for the concurrent calls — tried that first (a pool of separate
# httplib2-backed service objects, one per worker thread, the same
# pattern main_loop()'s service pool already uses elsewhere in this
# file) and it was NOT safe: reproduced both an intermittent SSL
# internal error and a full interpreter segfault under real concurrent
# load against the real account. Rewritten to call the Gmail REST API
# directly via `requests` instead (already a proven dependency here,
# built on urllib3, safe for real concurrent/multi-threaded use) —
# confirmed zero crashes across multiple real runs at up to 25
# concurrent workers. Only the per-message GET (the actual bottleneck)
# goes through this path; messages().list()/batchModify() stay on the
# original single-threaded googleapiclient service, unaffected by any
# of this.
# =============================================================

_MARK_READ_WORKERS = 15  # concurrent label-check calls. Confirmed live
                          # (real account): 12 workers -> 64ms/msg, 20 ->
                          # 43ms/msg, 25 -> 15ms/msg on a short burst.
                          # A real full run DOES hit Gmail's per-minute
                          # quota eventually at this concurrency — confirmed
                          # live 2026-09-08, ~12 minutes of sustained load
                          # (29,500 of ~63,000 messages) before a real HTTP
                          # 403 "rateLimitExceeded". Kept 15 rather than
                          # dropping concurrency to avoid it entirely —
                          # _gmail_execute_with_retry()/_check_one's own
                          # retry now turn that into a ~65s pause-and-
                          # continue instead of aborting the whole run, so
                          # there's no real cost to occasionally hitting it.


def _is_gmail_quota_error(status_code, body_text):
    """Confirmed live in production (2026-09-08, a real 63k-message run):
    Gmail's per-user-per-minute quota error surfaces as HTTP 403 with
    reason 'rateLimitExceeded' — NOT 429 the way most APIs signal rate
    limiting. Checking status alone isn't enough; this is the exact
    shape that crashed the whole run when only 429 was handled."""
    if status_code not in (403, 429):
        return False
    body_lower = (body_text or "").lower()
    return status_code == 429 or any(s in body_lower for s in (
        "ratelimitexceeded", "quotaexceeded", "userratelimitexceeded", "resource_exhausted",
    ))


def _gmail_execute_with_retry(fn, log_func=None, max_retries=4, wait_seconds=65):
    """fn: a zero-arg callable performing one googleapiclient .execute()
    call. This is Gmail's PER-MINUTE quota (confirmed by the error text
    itself: "Previous quota: Units per minute per user") — a short retry
    doesn't help, waiting out the window does. Applies to EVERY Gmail
    call in mark_all_unread_as_read(), not just the concurrent per-
    message checks — the actual production crash (2026-09-08, 29,500 of
    ~63,000 messages in) came from the UNPROTECTED messages().list()
    call, not the per-message GETs, which already had their own (also
    incomplete — see _is_gmail_quota_error) retry."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if _is_gmail_quota_error(status, str(e)) and attempt < max_retries - 1:
                msg = f"Gmail quota hit — waiting {wait_seconds}s before retrying ({attempt + 1}/{max_retries})…"
                print(msg)
                if log_func:
                    log_func(msg)
                time.sleep(wait_seconds)
                continue
            raise


def mark_all_unread_as_read(service, creds, log_func=None):
    label_map = get_label_map(service)

    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_maxsize=_MARK_READ_WORKERS,
                                           pool_connections=_MARK_READ_WORKERS))

    def _check_one(msg_id):
        headers = {"Authorization": f"Bearer {creds.token}"}
        for attempt in range(3):
            try:
                r = session.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
                    params={"format": "minimal"}, headers=headers, timeout=15,
                )
                if _is_gmail_quota_error(r.status_code, r.text) and attempt < 2:
                    time.sleep(65)
                    continue
                r.raise_for_status()
                full = r.json()
                if not get_custom_label_names(full, label_map):
                    return msg_id
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                print("message check error:", e)
                return None
        return None

    total_marked  = 0
    total_checked = 0
    page_token    = None
    with ThreadPoolExecutor(max_workers=_MARK_READ_WORKERS, thread_name_prefix="markread") as executor:
        while True:
            resp = _gmail_execute_with_retry(
                lambda: service.users().messages().list(
                    userId="me", q="is:unread", pageToken=page_token, maxResults=500
                ).execute(),
                log_func=log_func,
            )
            msgs = resp.get("messages", [])
            if not msgs:
                break

            # creds.token can expire mid-run on a large mailbox (a
            # standard OAuth access token is short-lived) — refresh once
            # per page if needed so _check_one's Bearer header stays valid.
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())

            results = list(executor.map(_check_one, (m["id"] for m in msgs)))
            ids_to_mark = [mid for mid in results if mid]
            total_checked += len(msgs)

            if ids_to_mark:
                try:
                    _gmail_execute_with_retry(
                        lambda: service.users().messages().batchModify(
                            userId="me",
                            body={"ids": ids_to_mark, "removeLabelIds": ["UNREAD"]},
                        ).execute(),
                        log_func=log_func,
                    )
                    total_marked += len(ids_to_mark)
                except Exception as e:
                    print("batchModify error:", e)

            if log_func:
                log_func(f"Mark all read: checked {total_checked}, marked {total_marked} so far…")

            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    return total_marked

def _mark_all_read_worker(log_func):
    try:
        creds = authenticate_gmail()
        http  = httplib2.Http(timeout=30)
        http.disable_ssl_certificate_validation = True
        service = build("gmail", "v1",
                        http=AuthorizedHttp(creds, http),
                        cache_discovery=False,
                        static_discovery=False)
        log_func("Marking all unread mail as read (labeled threads preserved)...")
        count = mark_all_unread_as_read(service, creds, log_func=log_func)
        log_func(f"Done. Marked {count} emails as read.")
    except Exception as e:
        log_func(f"Mark-read error: {e}")

def make_all_mail_read_from_gui(log_func):
    if not messagebox.askyesno(
        "Confirm",
        "Mark ALL unread Gmail messages as read?\n\n"
        "Labeled (broker reply) threads will be preserved as unread."
    ):
        return
    threading.Thread(target=_mark_all_read_worker,
                     args=(log_func,), daemon=True).start()

# =============================================================
# ENTRY POINT
# =============================================================

def on_license_valid(key):
    global ACTIVE_LICENSE_KEY
    ACTIVE_LICENSE_KEY = key
    _flog("info", f"License validated: {key[:8]}...")

    issues = _validate_startup_files()
    if issues:
        _flog("error", f"Startup validation failed: {issues}")
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror("Missing Required Files", "\n\n".join(issues))
            _r.destroy()
        except Exception:
            print("STARTUP ERROR:", "\n".join(issues))
        return

    _flog("info", "Startup validation passed — launching GUI.")
    try:
        create_app()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        _flog("critical", f"create_app() crashed:\n{err}")
        print("CRASH IN create_app():")
        print(err)
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror("MailBot Crashed",
                                 f"Error:\n{err[:500]}\n\nFull log:\n{LOG_FILE}")
            _r.destroy()
        except Exception:
            pass
        input("Press Enter to close...")

if __name__ == "__main__":
    print(f"PLUTUS BOT — build {BUILD_VERSION}", flush=True)
    _flog("info", f"PLUTUS BOT starting — build {BUILD_VERSION}")
    try:
        run_activation_gate(on_license_valid)
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Press Enter to close...")