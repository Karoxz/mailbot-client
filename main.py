import ssl
ssl._create_default_https_context = ssl.create_default_context
import certifi
import os
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.chdir(os.path.dirname(os.path.abspath(__file__)))


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
from datetime import datetime, timezone
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
import logging
from logging.handlers import RotatingFileHandler

from activation_screen import run_activation_gate
from api_client import call_parse, call_build_bid
from license_manager import get_machine_id

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

BOT_TOKEN              = "8157082619:AAHqoxicji5_awWjDmd1Ia7FGxpgp2R6Vkc"
TELEGRAM_UPDATE_OFFSET = 0
TELEGRAM_OFFSET_LOCK   = threading.Lock()

CHAT_IDS: list = [0000]
_CHAT_IDS_LOCK = threading.Lock()

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
LOAD_STORE        = {}
LOAD_STORE_LOCK   = threading.Lock()
BID_TEMPLATE_LOCK = threading.Lock()
BID_TEMPLATE      = """Rate: $
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

def _telegram_send_one(chat_id: int, payload: dict):
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
            print(f"Telegram send error (chat {chat_id}):", r.text[:200])
    except Exception as e:
        print(f"Telegram exception (chat {chat_id}):", e)

def send_to_telegram(text, bid_order_id=None, mobile_thread_url=None,
                     reply_msg_id=None, open_url=None, open_url_text="OPEN GMAIL",
                     route_url=None):
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
    if len(ids) == 1:
        _telegram_send_one(ids[0], payload)
    else:
        threads = [threading.Thread(target=_telegram_send_one,
                                    args=(cid, payload), daemon=True) for cid in ids]
        for t in threads: t.start()
        for t in threads: t.join(timeout=6)

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
    last_err = None
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
    raise last_err
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
        machine_id=get_machine_id(),
        load_data={k: v for k, v in load.items() if k != "original_msg_full"},
    )

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
        cq = upd.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        cqid = cq.get("id")
        answer_callback_query(cqid, "")

        if data.startswith("bid:"):
            order_id = data.split(":", 1)[1]
            with LOAD_STORE_LOCK:
                load = LOAD_STORE.get(order_id)
            if not load:
                answer_callback_query(cqid, "")
                continue
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
                send_to_telegram("📋 Bid text copied. Press Reply and paste (Ctrl+V).")
            except Exception as e:
                print("Bid callback failed:", e)

        elif data.startswith("openurl:"):
            key = data[len("openurl:"):]
            url = _retrieve_url(key)
            if url:
                webbrowser.open(url)

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
                    answer_callback_query(cqid, "Failed to open reply thread.")
            except Exception as e:
                print("Reply callback failed:", e)
                answer_callback_query(cqid, "Failed to open reply thread.")

        elif data.startswith("text:"):
            order_id = data.split(":", 1)[1]
            body = _build_bid_body_for_order(order_id)
            if not body:
                answer_callback_query(cqid, "Load data not found.")
                continue
            send_to_telegram(f"📋 ORDER #{order_id}:\n\n{body}")

        elif data.startswith("phone:"):
            order_id = data.split(":", 1)[1]
            with LOAD_STORE_LOCK:
                load = LOAD_STORE.get(order_id)
            if not load:
                answer_callback_query(cqid, "Load data not found.")
                continue
            body = _build_bid_body_for_order(order_id)
            if not body:
                continue
            try:
                send_to_telegram(f"📋 Long-press to copy:\n\n{body}")
                original_msg = load.get("original_msg_full", {})
                draft    = create_reply_draft(service, original_msg, "", None, empty=True)
                draft_id = draft.get("id", "")
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
                answer_callback_query(cqid, "")
            except Exception as e:
                print("Phone callback failed:", e)
                send_to_telegram(f"❌ Failed to create draft: {e}")
                answer_callback_query(cqid, "")

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
            time.sleep(0.5)

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
        if STOP_EVENT.is_set():
            return
        svc = _get_svc()
        try:
            # STEP 1: cheap metadata — label check first
            try:
                meta_pre = svc.users().messages().get(
                    userId="me", id=msg_id, format="minimal",
                ).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    processed_ids.add(msg_id)
                    return
                raise

            if STOP_EVENT.is_set():
                return

            if _has_custom_labels(meta_pre.get("labelIds", [])):
                processed_ids.add(msg_id)
                return

            _thread_id_pre = meta_pre.get("threadId", "")
            if _thread_id_pre:
                _tl, _ts = _get_thread_info(svc, _thread_id_pre, label_map)
                if _tl:
                    _notify_labeled_thread(_tl, _ts, _thread_id_pre)
                    processed_ids.add(msg_id)
                    return

            # STEP 2: full fetch
            try:
                full = svc.users().messages().get(
                    userId="me", id=msg_id, format="full"
                ).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    processed_ids.add(msg_id)
                    return
                raise

            if STOP_EVENT.is_set():
                return

            # STEP 3: race-condition guard
            if _has_custom_labels(full.get("labelIds", [])):
                processed_ids.add(msg_id)
                return
            _thread_id_full = full.get("threadId", "")
            if _thread_id_full:
                _tl2 = _get_thread_label_names(svc, _thread_id_full, label_map)
                if _tl2:
                    _subj = ""
                    for _h in full.get("payload", {}).get("headers", []):
                        if _h.get("name", "").lower() == "subject":
                            _subj = _h.get("value", "")
                            break
                    _notify_labeled_thread(_tl2, _subj, _thread_id_full)
                    processed_ids.add(msg_id)
                    return

            # STEP 4: extract body, send to server
            # THIS is the only difference from the original —
            # instead of process_bid_email() we call the server API
            body          = extract_text_from_full_message(full)
            internal_date = int(full.get("internalDate", "0"))

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
                })

            try:
                result = call_parse(
                    license_key      = ACTIVE_LICENSE_KEY,
                    machine_id       = get_machine_id(),
                    email_body       = body,
                    internal_date_ms = internal_date,
                    allowed_vehicles = allowed_vehicles,
                    max_radius_miles = radius,
                    trucks           = trucks_payload,
                    bid_template     = BID_TEMPLATE,
                )
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
            route_url = result.get("route_url", "")

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
                _log(f"[{ts}] ✅ #{order}{gmid_tag}  →  sent to Telegram")
                _route_url = None
                with LOAD_STORE_LOCK:
                    _ld = LOAD_STORE.get(order)
                    if _ld:
                        _route_url = _ld.get("route_url")
                send_to_telegram(formatted, bid_order_id=order,
                                 mobile_thread_url=mobile_bid_url,
                                 route_url=_route_url)
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
        if msg_id in processed_ids:
            return
        with in_flight_lock:
            if msg_id in in_flight:
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

    # ── Real-time history loop — identical to original ────────────────────
    _log("[SYS] Entering history-based real-time mode...")

    while not STOP_EVENT.is_set():
        try:
            _reap_futures()

            if time.time() - last_rebuild > 1800:
                try:
                    service      = _make_service()
                    label_map    = get_label_map(service)
                    last_rebuild = time.time()
                    _log("[SYS] Gmail credentials refreshed.")
                except Exception as e:
                    _log(f"[SYS] Credential refresh failed: {e}")

            if history_id is None:
                try:
                    _veh_q = " OR ".join(f'"{v}"' for v in allowed_vehicles)
                    _fb_q  = f'is:unread newer_than:{FRESH_WINDOW} ({_veh_q})'
                    _fpage = None
                    while True:
                        _fr = service.users().messages().list(
                            userId="me", q=_fb_q, maxResults=500, pageToken=_fpage,
                        ).execute()
                        for _fm in _fr.get("messages", []):
                            if STOP_EVENT.is_set():
                                break
                            if _fm["id"] not in processed_ids:
                                _submit(_fm["id"])
                        _fpage = _fr.get("nextPageToken")
                        if not _fpage or STOP_EVENT.is_set():
                            break
                except Exception as e:
                    _log(f"[LOOP ERR] {e}")
                time.sleep(poll_seconds)
                continue

            new_history_id, new_msg_ids = poll_new_messages_via_history(service, history_id)

            if new_history_id is None:
                _log("[SYS] historyId expired — resetting cursor.")
                try:
                    history_id = get_current_history_id(service)
                except Exception as e:
                    _log(f"[SYS] historyId reset failed: {e}")
                    history_id = None
                time.sleep(poll_seconds)
                continue

            history_id = new_history_id

            for msg_id in new_msg_ids:
                if STOP_EVENT.is_set():
                    break
                if msg_id in processed_ids:
                    continue
                try:
                    meta = service.users().messages().get(
                        userId="me", id=msg_id, format="metadata",
                        metadataHeaders=["Subject"],
                    ).execute()
                except HttpError as e:
                    if e.resp.status == 404:
                        processed_ids.add(msg_id)
                        continue
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {msg_id[-6:]}: {e}")
                    continue
                except Exception as e:
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ {msg_id[-6:]}: {e}")
                    continue

                msg_labels = meta.get("labelIds", [])

                if _has_custom_labels(msg_labels):
                    processed_ids.add(msg_id)
                    continue

                _hist_thread_id = meta.get("threadId", "")
                if _hist_thread_id:
                    _htl, _hts = _get_thread_info(service, _hist_thread_id, label_map)
                    if _htl:
                        _notify_labeled_thread(_htl, _hts, _hist_thread_id)
                        processed_ids.add(msg_id)
                        continue

                subject = ""
                for h in meta.get("payload", {}).get("headers", []):
                    if h.get("name", "").lower() == "subject":
                        subject = h.get("value", "").upper()
                        break

                if not any(m in subject for m in FREIGHT_MARKERS):
                    _safe_mark_read(service, msg_id, _hist_thread_id,
                                    label_map, subject, _log)
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"🗑 Non-freight [{msg_id[-8:]}]: {subject[:70]}")
                    processed_ids.add(msg_id)
                    continue

                if not any(v.upper() in subject for v in allowed_vehicles):
                    _safe_mark_read(service, msg_id, _hist_thread_id,
                                    label_map, subject, _log)
                    processed_ids.add(msg_id)
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"🗑 Cleaned [{msg_id[-8:]}]: {subject[:70]}")
                    continue

                _submit(msg_id)

        except Exception as e:
            _log(f"[LOOP ERR] {e}")

        if len(processed_ids) > 2000:
            stale = list(processed_ids)[:len(processed_ids) - 2000]
            for _id in stale:
                processed_ids.discard(_id)

        time.sleep(poll_seconds)

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
    root.title("")
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
                img.thumbnail((320, 110), Image.LANCZOS)
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
            logo_lbl.image = ph
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
        w, h = 600, 460
        win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

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

        tbox = tk.Text(outer, font=("Consolas", 10), height=10,
                       bg=_C["input"], fg=_C["text"],
                       insertbackground=_C["text"],
                       selectbackground=_C["accent"],
                       relief="flat", highlightthickness=1,
                       highlightbackground=_C["border"],
                       highlightcolor=_C["accent"])
        tbox.insert("1.0", BID_TEMPLATE)
        tbox.pack(fill="both", expand=True)

        ref_frame = tk.Frame(outer, bg=_C["input"], padx=10, pady=8)
        ref_frame.pack(fill="x", pady=(8, 0))

        tk.Label(ref_frame, text="AVAILABLE VARIABLES:",
                 bg=_C["input"], fg=_C["accent"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")

        for var, desc in [
            ("{truck_dimensions}", "Your truck's dimensions  e.g. 264x97x103"),
            ("{google_deadhead}",  "Miles from your truck to the pickup"),
            ("{truck_equipment}",  "Equipment list  e.g. Dock High, Air Ride"),
            ("{deadhead_eta_str}", "Estimated travel time to pickup"),
        ]:
            r = tk.Frame(ref_frame, bg=_C["input"])
            r.pack(fill="x", pady=1)
            tk.Label(r, text=var, bg=_C["input"], fg=_C["green"],
                     font=("Consolas", 10), width=22, anchor="w").pack(side="left")
            tk.Label(r, text=f"—  {desc}", bg=_C["input"], fg=_C["text"],
                     font=("Segoe UI", 10)).pack(side="left")

        btn_f = tk.Frame(outer, bg=_C["bg"])
        btn_f.pack(fill="x", pady=(10, 0))

        def _save_template():
            global BID_TEMPLATE
            with BID_TEMPLATE_LOCK:
                BID_TEMPLATE = tbox.get("1.0", "end").strip()
            cfg_saved = _load_config()
            cfg_saved["bid_template"] = BID_TEMPLATE
            _save_config(cfg_saved)
            win.destroy()

        tk.Button(btn_f, text="💾  Save & Close", command=_save_template,
                  bg="#1a7f4b", fg="#ffffff",
                  activebackground="#22a05e", activeforeground="#ffffff",
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  padx=16, pady=7, cursor="hand2").pack(side="left", padx=(0, 8))

        tk.Button(btn_f, text="Cancel", command=win.destroy,
                  bg=_C["input"], fg=_C["text2"],
                  activebackground=_C["border"], activeforeground=_C["text"],
                  font=("Segoe UI", 10), relief="flat",
                  padx=12, pady=7, cursor="hand2").pack(side="left")

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
        "🚛  TRUCKS  ·  VEHICLE:DRIVER:DIMS:PAYLOAD:EQUIPMENT:STATES:ZIP[:DATE]")

    guide_frame = tk.Frame(trk_sec, bg=_C["input"], padx=8, pady=6)
    guide_frame.pack(fill="x", pady=(0, 6))
    _all_frames.append((guide_frame, "input"))

    tk.Label(guide_frame,
             text="FORMAT — separate each field with a colon  :",
             bg=_C["input"], fg=_C["accent"],
             font=("Segoe UI", 10, "bold")).pack(anchor="w")
    tk.Label(guide_frame,
             text="VEHICLE : DRIVER : LxWxH : MAX LBS : EQUIPMENT : STATES : ZIP : DATE",
             bg=_C["input"], fg=_C["text"],
             font=("Consolas", 10)).pack(anchor="w", pady=(2, 2))
    tk.Label(guide_frame,
             text="Example:  LARGE STRAIGHT:John Smith:264x97x103:26000"
                  ":Dock High,Air Ride:OH,PA,NY:44129:05/29/26",
             bg=_C["input"], fg=_C["green"],
             font=("Consolas", 10)).pack(anchor="w")
    tk.Label(guide_frame,
             text="STATES: blank = all states  |  codes: OH,PA,NY  |  "
                  "regions: East Coast · Midwest · West Coast          "
                  "DATE: blank = any  |  format: MM/DD/YY",
             bg=_C["input"], fg=_C["text"],
             font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 0))

    t_box = tk.Text(trk_sec, font=("Consolas", 12), height=15)
    _theme_text(t_box)
    t_box.insert("1.0",
        "LARGE STRAIGHT:John Smith:264x97x103:8000"
        ":Dock High,Air Ride:OH,PA,NY:44129"
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
             "DATE      — optional, e.g. 05/29/26")

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
        global BOT_THREAD, BID_TEMPLATE
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
            "bid_template":   BID_TEMPLATE,
        })
        STOP_EVENT.clear()
        _set_running(True)
        set_graphhopper_status("Server-side ✅")
        log("▶  Starting MailBot…")
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

    start_btn.config(command=start_bot)
    stop_btn.config(command=stop_bot)

    def on_close():
        STOP_EVENT.set()
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
# MARK ALL READ — identical to original
# =============================================================

def mark_all_unread_as_read(service):
    label_map    = get_label_map(service)
    total_marked = 0
    page_token   = None
    while True:
        resp = service.users().messages().list(
            userId="me", q="is:unread", pageToken=page_token, maxResults=500
        ).execute()
        msgs = resp.get("messages", [])
        if not msgs:
            break
        ids_to_mark = []
        for m in msgs:
            try:
                full = service.users().messages().get(
                    userId="me", id=m["id"], format="minimal"
                ).execute()
                if not get_custom_label_names(full, label_map):
                    ids_to_mark.append(m["id"])
            except Exception as e:
                print("message check error:", e)
        if ids_to_mark:
            try:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": ids_to_mark, "removeLabelIds": ["UNREAD"]},
                ).execute()
                total_marked += len(ids_to_mark)
            except Exception as e:
                print("batchModify error:", e)
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
                        cache_discovery=False)
        log_func("Marking all unread mail as read (labeled threads preserved)...")
        count = mark_all_unread_as_read(service)
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
    try:
        run_activation_gate(on_license_valid)
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Press Enter to close...")