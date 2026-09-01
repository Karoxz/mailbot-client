# =============================================================
# driver_bot.py  —  Driver-side Telegram bidding module
# =============================================================
# Standalone module. Import into dispatcher main.py with:
#
#   try:
#       import driver_bot
#       _DRIVER_BOT_ENABLED = driver_bot.init()
#   except Exception:
#       _DRIVER_BOT_ENABLED = False
#
# Then after send_to_telegram() for dispatcher, add:
#
#   if _DRIVER_BOT_ENABLED and formatted and order:
#       with LOAD_STORE_LOCK:
#           _ld = dict(LOAD_STORE.get(order, {}))
#       if _ld:
#           driver_bot.notify_drivers(order, _ld)
#
# Zero changes to server. Zero changes to existing dispatcher flow.
# =============================================================

import json
import os
import re
import threading
import time
import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from api_client import call_record_bid
from client.main import ACTIVE_LICENSE_KEY, _get_machine_id
# =============================================================
# LOGGING
# =============================================================

_log = logging.getLogger("driver_bot")
if not _log.handlers:
    _log.setLevel(logging.DEBUG)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [DRIVERBOT] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    _log.addHandler(_h)

def _l(msg: str, level: str = "info"):
    getattr(_log, level)(msg)

# =============================================================
# CONFIG
# =============================================================

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "driver_config.json"
)

_DEFAULT_CONFIG = {
    "driver_bot_token": "",
    "dispatcher_bot_token": "",
    "dispatcher_chat_ids": [],
    "drivers": []
}

# Loaded at init() time — never mutated after that
_CFG: dict = {}

# Active license/machine — injected by init(), but also read lazily
# from main.py globals at call time so they're always current even if
# init() was called before on_license_valid() set ACTIVE_LICENSE_KEY.
_LICENSE_KEY: str = ""
_MACHINE_ID:  str = ""

def _get_credentials() -> tuple:
    """
    Return (license_key, machine_id).
    Prefers the values injected at init() time.
    Falls back to reading ACTIVE_LICENSE_KEY from dispatcher main.py
    at call time — handles the case where init() was called before
    the license was validated.
    """
    key = _LICENSE_KEY
    mid = _MACHINE_ID
    if not key:
        try:
            import __main__ as _main
            key = getattr(_main, "ACTIVE_LICENSE_KEY", "") or ""
        except Exception:
            pass
    if not mid:
        try:
            import __main__ as _main
            fn = getattr(_main, "_get_machine_id", None)
            if callable(fn):
                mid = fn() or ""
        except Exception:
            pass
    return key, mid

# =============================================================
# HTTP SESSION  (driver bot's own, separate from dispatcher's)
# =============================================================

_session = requests.Session()
_retry   = Retry(
    total=3, backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))

# =============================================================
# STATE
# =============================================================

# PENDING_BIDS tracks drivers who tapped BID and haven't replied yet.
# Key: driver_chat_id (int)
# Value: {
#   "order_id":       str,
#   "load_data":      dict,
#   "driver_name":    str,
#   "prompt_msg_id":  int,   ← message_id of the ForceReply prompt we sent
# }
PENDING_BIDS: dict = {}
_PENDING_LOCK = threading.Lock()

# Track message IDs of load cards we sent to each driver so we can
# match callback queries back to the right order.
# Key: (driver_chat_id, order_id)  → message_id of the load card
_CARD_MSG_IDS: dict = {}
_CARD_LOCK    = threading.Lock()

STOP_EVENT    = threading.Event()
_POLL_THREAD: Optional[threading.Thread] = None

# Telegram update offset (per-driver-bot, independent of dispatcher)
_UPDATE_OFFSET = 0
_OFFSET_LOCK   = threading.Lock()

# =============================================================
# CONFIG LOADING
# =============================================================

def load_config() -> dict:
    """Load driver_config.json. Returns empty dict if missing."""
    if not os.path.exists(_CONFIG_FILE):
        _l(f"driver_config.json not found at {_CONFIG_FILE} — driver bot disabled.", "warning")
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        _l(f"Config loaded: {len(cfg.get('drivers', []))} driver(s)")
        return cfg
    except Exception as e:
        _l(f"Failed to load driver_config.json: {e}", "error")
        return {}


def save_config(cfg: dict):
    """Write updated config back to disk."""
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        _l(f"Failed to save driver_config.json: {e}", "error")

# =============================================================
# TELEGRAM HELPERS — DRIVER BOT
# =============================================================

def _driver_api(method: str, payload: dict, timeout: int = 8) -> Optional[dict]:
    """Call Telegram Bot API using the DRIVER bot token."""
    token = _CFG.get("driver_bot_token", "")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = _session.post(url, json=payload, timeout=timeout)
        if not r.ok:
            _l(f"Driver API {method} error {r.status_code}: {r.text[:200]}", "warning")
            return None
        data = r.json()
        if not data.get("ok"):
            _l(f"Driver API {method} not ok: {data}", "warning")
            return None
        return data
    except Exception as e:
        _l(f"Driver API {method} exception: {e}", "error")
        return None


def _dispatcher_api(method: str, payload: dict, timeout: int = 8) -> Optional[dict]:
    """
    Send a message TO the dispatcher using the DISPATCHER bot token.
    This makes driver bids appear in the dispatcher's existing chat
    — same bot, same conversation they already use.
    """
    token = _CFG.get("dispatcher_bot_token", "")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = _session.post(url, json=payload, timeout=timeout)
        if not r.ok:
            _l(f"Dispatcher API {method} error {r.status_code}: {r.text[:200]}", "warning")
            return None
        data = r.json()
        if not data.get("ok"):
            _l(f"Dispatcher API {method} not ok: {data}", "warning")
            return None
        return data
    except Exception as e:
        _l(f"Dispatcher API {method} exception: {e}", "error")
        return None


def _send_to_driver(chat_id: int, text: str,
                    reply_markup: Optional[dict] = None) -> Optional[int]:
    """Send a message to a driver. Returns message_id or None."""
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = _driver_api("sendMessage", payload)
    if data:
        return data.get("result", {}).get("message_id")
    return None


def _get_fallback_dispatcher_ids() -> list:
    """
    When no group chat is configured, send driver bids to the same
    individual dispatcher chat(s) that regular load alerts already go
    to (CHAT_IDS in main copy.py), instead of dropping the message.
    """
    try:
        import __main__ as _main
        ids = getattr(_main, "CHAT_IDS", None)
        if ids:
            return list(ids)
    except Exception as e:
        _l(f"_get_fallback_dispatcher_ids failed: {e}", "warning")
    return []


from typing import Optional

def _send_to_dispatcher(text: str, reply_markup: Optional[dict] = None):
    ids = _CFG.get("dispatcher_chat_ids", [])
    fallback_used = False
    if not ids:
        ids = _get_fallback_dispatcher_ids()
        fallback_used = bool(ids)
    token = _CFG.get("dispatcher_bot_token", "")
    _l(f"_send_to_dispatcher: ids={ids} fallback={fallback_used} "
       f"token_present={bool(token)} text_len={len(text)}")
    if not ids:
        _l("_send_to_dispatcher: NO dispatcher_chat_ids and no fallback CHAT_IDS "
           "— message dropped", "warning")
        return
    if not token:
        _l("_send_to_dispatcher: NO dispatcher_bot_token — message dropped", "warning")
        return
    for cid in ids:
        payload = {"chat_id": cid, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = _dispatcher_api("sendMessage", payload)
        _l(f"_send_to_dispatcher: sent to {cid} result={result is not None}")


def _answer_callback(callback_query_id: str, text: str = ""):
    """Acknowledge a callback query (removes loading spinner in Telegram)."""
    _driver_api("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })

# =============================================================
# LOAD CARD FORMATTER
# =============================================================
def _format_driver_summary(driver_name: str, load_data: dict) -> str:
    base = load_data.get("formatted_message", "")
    
    # Lines to exclude (by prefix/content)
    EXCLUDE = ("⏱️ Email time", "🤝 Broker", "Name:", "Company:", "Phone:", "Email:","draft :","Driver:")
    
    lines = []
    for line in base.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(x) for x in EXCLUDE):
            continue
        lines.append(line)
    
    cleaned = "\n".join(lines).strip()
    return f"👤 {driver_name}\n{'─'*30}\n{cleaned}"

def _format_load_card(order_id: str, load_data: dict) -> str:
    """
    Short load summary shown to drivers.
    Intentionally brief — drivers just need pickup/delivery/deadhead.
    """
    vehicle   = load_data.get("vehicle_required", "")
    pickup    = load_data.get("pickup_loc",       "UNKNOWN")
    delivery  = load_data.get("delivery_loc",     "UNKNOWN")
    pu_dt     = load_data.get("pickup_dt",        "")
    dl_dt     = load_data.get("delivery_dt",      "")
    deadhead  = load_data.get("google_deadhead")
    driver_nm = load_data.get("driver_name", "")   # best-match driver name from parser

    lines = [
        f"🚛  LOAD #{order_id}",
        f"Vehicle:    {vehicle}",
        f"📍 Pickup:   {pickup}",
        f"📍 Delivery: {delivery}",
    ]
    if pu_dt:
        lines.append(f"📅 PU Date:  {pu_dt}")
    if dl_dt:
        lines.append(f"📅 DEL Date: {dl_dt}")
    if deadhead is not None:
        lines.append(f"📏 Deadhead: {deadhead} mi")
    if driver_nm:
        lines.append(f"👤 Matched:  {driver_nm}")
    return "\n".join(lines)

# =============================================================
# NOTIFY DRIVERS
# =============================================================

def notify_drivers(order_id: str, load_data: dict): # type: ignore
    """
    Called from dispatcher main.py after a load is matched.
    Sends a load card with a BID button to every configured driver.

    Only notifies drivers whose truck_type matches the load's
    vehicle_required (if truck_type is configured). If a driver's
    truck_type is empty/missing, they receive all loads.
    """
    if not load_data.get("formatted_message"):
        _l(f"notify_drivers called for {order_id} but formatted_message is empty — skipping", "warning")
        return
    drivers = _CFG.get("drivers", [])
    if not drivers:
        return

    vehicle_required = (load_data.get("vehicle_required") or "").upper().strip()
    card_text = _format_load_card(order_id, load_data)

    for driver in drivers:
        chat_id    = driver.get("telegram_chat_id")
        name       = driver.get("name", "Driver")
        truck_type = (driver.get("truck_type") or "").upper().strip()

        if not chat_id:
            continue

        # Vehicle type filter — skip if driver's truck doesn't match
        if truck_type and vehicle_required:
            if truck_type not in vehicle_required and vehicle_required not in truck_type:
                _l(f"Skipping {name} ({truck_type} ≠ {vehicle_required})")
                continue

        keyboard = {"inline_keyboard": [[
            {"text": "💰 BID", "callback_data": f"driverbid:{order_id}:{name}"},
            *([{"text": "🚩 ROUTE", "url": load_data.get("route_url", "")}]
            if load_data.get("route_url") else [])
        ]]}

        msg_id = _send_to_driver(chat_id, card_text, reply_markup=keyboard)
        if msg_id:
            _l(f"✅ Card sent to {name} order={order_id} msg_id={msg_id}")
        else:
            _l(f"❌ FAILED card to {name} order={order_id}", "warning")

# =============================================================
# RATE PARSING
# =============================================================

def _parse_rate(text: str) -> Optional[str]:
    """
    Extract a dollar amount from driver's reply.
    Accepts: 1400  |  $1400  |  1,400  |  $1,400.00  |  1400.00
    Returns cleaned string like "1400" or "1,400" — or None if unparseable.
    """
    text = text.strip()
    # Remove $ sign and surrounding whitespace
    text = text.replace("$", "").strip()
    # Match a number (with optional commas and decimal)
    m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", text)
    if not m:
        return None
    raw = m.group(1)
    # Remove cents if .00
    if raw.endswith(".00"):
        raw = raw[:-3]
    return raw

# =============================================================
# BID BUILDING + FORWARDING
# =============================================================

def _render_bid_locally(load_data: dict, rate_str: str) -> str:
    """
    Render bid text from template without calling the server.
    Used in standalone test mode (no valid license) and as fallback
    if call_build_bid fails.
    """
    template = load_data.get("bid_template") or _DEFAULT_TEMPLATE

    deadhead_mins = load_data.get("deadhead_eta_minutes") or 0
    h, m = divmod(round(deadhead_mins / 30) * 30, 60)
    if h and m == 0:
        eta_str = f"{h}hrs"
    elif h:
        eta_str = f"{h}hrs {m:02d}min"
    else:
        eta_str = f"{m}min"

    data = {
        "vehicle_type":      load_data.get("truck_type") or load_data.get("vehicle_required", ""),
        "truck_dimensions":  load_data.get("truck_dimensions", ""),
        "google_deadhead":   load_data.get("google_deadhead", ""),
        "truck_equipment":   load_data.get("truck_equipment", ""),
        "deadhead_eta_str":  eta_str,
        "driver_name":       load_data.get("driver_name", ""),
        "pickup_loc":        load_data.get("pickup_loc", ""),
        "pickup_dt":         load_data.get("pickup_dt", ""),
        "pickup_date_only":  (load_data.get("pickup_dt") or "").split()[0],
        "delivery_loc":      load_data.get("delivery_loc", ""),
        "delivery_dt":       load_data.get("delivery_dt", ""),
        "delivery_date_only":(load_data.get("delivery_dt") or "").split()[0],
        "deadhead_miles":    str(load_data.get("google_deadhead", "")),
        "order":             load_data.get("order", ""),
        "broker_name":       load_data.get("broker_name", ""),
        "vehicle_required":  load_data.get("vehicle_required", ""),
    }
    try:
        return template.format(**data)
    except KeyError as e:
        _l(f"Template key missing: {e} — returning raw template", "warning")
        return template
    
def _record_driver_bid(driver_name: str, order_id: str, load_data: dict, rate_str: str):
    """Driver-entered rate is the one case where bid_amount is known at
    record time — capture it instead of leaving another 'pending, no
    amount' row that the learning layers can never use."""
    try:
        from api_client import call_record_bid
        key, mid = _get_credentials()
        try:
            amount = float(rate_str.replace(",", ""))
        except ValueError:
            amount = None
        call_record_bid(
            license_key=key, machine_id=mid,
            bid_data={
                "order_id":        load_data.get("order", order_id),
                "thread_id":       (load_data.get("original_msg_full") or {}).get("threadId", ""),
                "bid_method":      "driver_bot",
                "vehicle_type":    load_data.get("truck_type") or load_data.get("vehicle_required", ""),
                "driver_name":     driver_name,
                "pickup_loc":      load_data.get("pickup_loc", ""),
                "delivery_loc":    load_data.get("delivery_loc", ""),
                "broker_name":     load_data.get("broker_name", ""),
                "broker_email":    load_data.get("broker_email", ""),
                "deadhead_miles":  load_data.get("google_deadhead"),
                "bid_amount":      amount,
                "verified_miles":  (load_data.get("maps_verification") or {}).get("verified_miles"),
                "verified_source": (load_data.get("maps_verification") or {}).get("verified_source"),
            },
        )
    except Exception as e:
        _l(f"_record_driver_bid failed (non-fatal): {e}", "warning")
        

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

    Returns the new bid_id (or None on failure) so the caller can
    prompt for the rate afterward and fill it in via
    call_update_bid_amount once the dispatcher types it.
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
                "verified_miles":  (load.get("maps_verification") or {}).get("verified_miles"),
                "verified_source": (load.get("maps_verification") or {}).get("verified_source"),
            },
        )
        return result.get("bid_id") if result else None
    except Exception as e:
        print(f"_record_bid failed (non-fatal): {e}")
        return None

def _build_and_forward_bid(driver_name: str, order_id: str,
                           load_data: dict, rate_str: str):
    order_id  = load_data.get("order", order_id)
    route_url = load_data.get("route_url", "")

    _l(f"_build_and_forward_bid called: driver={driver_name} order={order_id} rate=${rate_str}")
    _l(f"dispatcher_chat_ids={_CFG.get('dispatcher_chat_ids')}")
    _l(f"dispatcher_bot_token present={bool(_CFG.get('dispatcher_bot_token'))}")

    base_msg  = load_data.get("formatted_message", "")
    rate_line = f"💰 {driver_name} — Rate: ${rate_str}\n{'─'*30}\n"
    full_msg  = rate_line + base_msg

    keyboard = {"inline_keyboard": [
        [
            {"text": "💵 BID PC",    "callback_data": f"bid:{order_id}"},
            {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
            {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
        ],
        ([{"text": "🚩 ROUTE 🚩", "url": route_url}] if route_url else [])
    ]}
    keyboard["inline_keyboard"] = [r for r in keyboard["inline_keyboard"] if r]

    _l(f"Sending to dispatcher: msg_len={len(full_msg)}")
    _send_to_dispatcher(full_msg, reply_markup=keyboard)
    _record_driver_bid(driver_name, order_id, load_data, rate_str)   # ← NEW

# =============================================================
# DEFAULT TEMPLATE FALLBACK
# =============================================================

_DEFAULT_TEMPLATE = """Rate: $
{vehicle_type}
Dims: {truck_dimensions}
MC#

Truck is {google_deadhead} miles out
{truck_equipment}

ETA to PU: {deadhead_eta_str}

ALL BIDS ARE VALID 15 MIN"""

# =============================================================
# UPDATE PROCESSING
# =============================================================

def _handle_callback_query(cq: dict):
    """Process a button tap from a driver."""
    cq_id       = cq.get("id", "")
    data        = cq.get("data", "")
    from_user   = cq.get("from", {})
    chat_id     = from_user.get("id")
    driver_name_from_tg = from_user.get("first_name", "Driver")

    if not data.startswith("driverbid:"):
        return

    # Parse callback data: "driverbid:<order_id>:<driver_name>"
    parts = data.split(":", 2)
    if len(parts) < 3:
        _answer_callback(cq_id, "Invalid data")
        return

    order_id    = parts[1]
    driver_name = parts[2]   # name from config, more reliable than TG display name

    _answer_callback(cq_id, "💰 Enter your rate below")

    # Send ForceReply prompt to driver
    prompt_text = (
        f"💰 Order #{order_id}\n"
        f"Type your rate (numbers only):\n"
        f"Example:  1400"
    )
    prompt_msg_id = _send_to_driver(
        chat_id,
        prompt_text,
        reply_markup={"force_reply": True, "selective": True},
    )

    if not prompt_msg_id:
        _l(f"Failed to send ForceReply prompt to {driver_name}", "warning")
        return

    # Retrieve stored load_data
    with _CARD_LOCK:
        card_msg_id = _CARD_MSG_IDS.get((chat_id, order_id))

    # Find load_data from dispatcher's LOAD_STORE via the order_id
    # We need to reach into dispatcher's state — passed via notify_drivers()
    # We cache it in PENDING_BIDS at notify time, retrieved here
    load_data = None
    with _PENDING_LOCK:
        existing = PENDING_BIDS.get((chat_id, order_id))
        if existing:
            load_data = existing.get("load_data")

    # If not in pending (first tap), get from _LOAD_CACHE set by notify_drivers
    if load_data is None:
        with _LOAD_CACHE_LOCK:
            load_data = _LOAD_CACHE.get(order_id)

    # Store pending bid
    with _PENDING_LOCK:
        PENDING_BIDS[(chat_id, order_id)] = {
            "order_id":      order_id,
            "load_data":     load_data,
            "driver_name":   driver_name,
            "prompt_msg_id": prompt_msg_id,
        }

    _l(f"Awaiting rate from {driver_name} (chat {chat_id}) for order {order_id}")


def _handle_message(msg: dict):
    chat_id     = msg.get("chat", {}).get("id")
    text        = (msg.get("text") or "").strip()
    reply_to    = msg.get("reply_to_message", {})
    reply_to_id = reply_to.get("message_id") if reply_to else None

    _l(f"MSG from chat_id={chat_id} reply_to_id={reply_to_id} text={text[:30]!r}")

    if not chat_id or not text:
        _l("MSG dropped: no chat_id or no text")
        return

    with _PENDING_LOCK:
        pending     = None
        pending_key = None
        all_keys    = list(PENDING_BIDS.keys())
        _l(f"PENDING_BIDS keys: {all_keys}")

        if reply_to_id:
            for key, val in PENDING_BIDS.items():
                if key[0] == chat_id and val.get("prompt_msg_id") == reply_to_id:
                    pending     = val
                    pending_key = key
                    _l(f"Match by reply_to_id: key={key}")
                    break

        if not pending:
            for key, val in PENDING_BIDS.items():
                if key[0] == chat_id and val.get("prompt_msg_id") is not None:
                    pending     = val
                    pending_key = key
                    _l(f"Match by prompt_msg_id fallback: key={key}")
                    break

        if not pending:
            for key, val in PENDING_BIDS.items():
                if key[0] == chat_id:
                    pending     = val
                    pending_key = key
                    _l(f"Match by chat_id only: key={key}")
                    break

    if not pending or pending_key is None:
        _l(f"MSG dropped: no pending bid for chat_id={chat_id}")
        return

    order_id    = pending["order_id"]
    driver_name = pending["driver_name"]
    load_data   = pending.get("load_data")

    _l(f"Processing bid: driver={driver_name} order={order_id} rate_text={text!r}")

    rate_str = _parse_rate(text)
    if not rate_str:
        _l(f"MSG dropped: could not parse rate from {text!r}")
        _send_to_driver(
            chat_id,
            f"⚠️ Could not read your rate from \"{text}\".\n"
            f"Please reply with a number only, e.g.:  1400"
        )
        return

    with _PENDING_LOCK:
        PENDING_BIDS.pop(pending_key, None)

    if load_data:
        result = _build_and_forward_bid(driver_name, order_id, load_data, rate_str)
        _send_to_driver(chat_id, f"✅ Bid of ${rate_str} sent to dispatcher!\nOrder #{order_id}")
    else:
        _l(f"No load_data for order {order_id}", "warning")
        _send_to_driver(
            chat_id,
            f"⚠️ Load #{order_id} data not found — bid could not be sent.\n"
            f"Please contact dispatcher directly."
        )

# =============================================================
# LOAD CACHE
# Internal cache so driver_bot has load_data available when
# a driver taps BID (which may arrive seconds after notify_drivers).
# Capped at 200 entries — oldest dropped first.
# =============================================================

_LOAD_CACHE: dict = {}
_LOAD_CACHE_LOCK  = threading.Lock()
_MAX_LOAD_CACHE   = 200


def _cache_load(order_id: str, load_data: dict):
    with _LOAD_CACHE_LOCK:
        if len(_LOAD_CACHE) >= _MAX_LOAD_CACHE:
            # Drop oldest
            oldest = next(iter(_LOAD_CACHE))
            del _LOAD_CACHE[oldest]
        _LOAD_CACHE[order_id] = load_data


# =============================================================
# POLLING LOOP
# =============================================================

def _poll_loop():
    """Background thread: polls Telegram for driver updates every 0.3s."""
    global _UPDATE_OFFSET
    _l("Driver bot polling started")

    while not STOP_EVENT.is_set():
        token = _CFG.get("driver_bot_token", "")
        if not token:
            time.sleep(2)
            continue

        url = f"https://api.telegram.org/bot{token}/getUpdates"
        try:
            with _OFFSET_LOCK:
                offset = _UPDATE_OFFSET

            r = _session.get(
                url,
                params={"offset": offset, "timeout": 0, "limit": 50},
                timeout=6,
            )
            if not r.ok:
                time.sleep(1)
                continue

            data    = r.json()
            updates = data.get("result", [])

            if updates:
                with _OFFSET_LOCK:
                    _UPDATE_OFFSET = updates[-1]["update_id"] + 1

            for upd in updates:
                try:
                    if "callback_query" in upd:
                        _handle_callback_query(upd["callback_query"])
                    elif "message" in upd:
                        _handle_message(upd["message"])
                except Exception as e:
                    _l(f"Error handling update: {e}", "error")

        except Exception as e:
            _l(f"Poll loop error: {e}", "warning")
            time.sleep(1)
            continue

        time.sleep(0.3)

    _l("Driver bot polling stopped")

# =============================================================
# PUBLIC API
# =============================================================

def notify_drivers(order_id: str, load_data: dict):
    if not _CFG:
        return

    _cache_load(order_id, load_data)

    drivers = _CFG.get("drivers", [])
    if not drivers:
        return

    vehicle_required = (load_data.get("vehicle_required") or "").upper().strip()

    for driver in drivers:
        chat_id    = driver.get("telegram_chat_id")
        name       = driver.get("name", "Driver")
        truck_type = (driver.get("truck_type") or "").upper().strip()

        if not chat_id:
            continue

        if truck_type and vehicle_required:
            if truck_type not in vehicle_required and vehicle_required not in truck_type:
                _l(f"Skipping {name} ({truck_type} ≠ {vehicle_required})")
                continue

        # Use formatted summary if available, fall back to brief card
        if load_data.get("formatted_message"):
            card_text = _format_driver_summary(name, load_data)
        else:
            card_text = f"👤 {name}\n{'─'*30}\n" + _format_load_card(order_id, load_data)
            _l(f"No formatted_message for {order_id} — using brief card", "warning")

        keyboard = {"inline_keyboard": [[
            {"text": "💰 BID", "callback_data": f"driverbid:{order_id}:{name}"},
            *([{"text": "🚩 ROUTE", "url": load_data.get("route_url", "")}]
              if load_data.get("route_url") else [])
        ]]}

        msg_id = _send_to_driver(chat_id, card_text, reply_markup=keyboard)
        if msg_id:
            with _CARD_LOCK:
                _CARD_MSG_IDS[(chat_id, order_id)] = msg_id
            with _PENDING_LOCK:
                if (chat_id, order_id) not in PENDING_BIDS:
                    PENDING_BIDS[(chat_id, order_id)] = {
                        "order_id":      order_id,
                        "load_data":     load_data,
                        "driver_name":   name,
                        "prompt_msg_id": None,
                    }
            _l(f"✅ Card sent to {name} (chat {chat_id}) order={order_id} msg_id={msg_id}")
        else:
            _l(f"❌ FAILED card to {name} (chat {chat_id}) order={order_id}", "warning")


def init(license_key: str = "", machine_id: str = "") -> bool:
    global _CFG, _LICENSE_KEY, _MACHINE_ID, _POLL_THREAD

    _LICENSE_KEY = license_key
    _MACHINE_ID  = machine_id

    cfg = load_config()
    if not cfg:
        return False

    token = cfg.get("driver_bot_token", "").strip()
    if not token:
        _l("driver_bot_token is empty — driver bot disabled.", "warning")
        return False


    _CFG = cfg

    # Start polling thread immediately — it will self-disable if token is bad
    STOP_EVENT.clear()
    _POLL_THREAD = threading.Thread(
        target=_poll_loop,
        daemon=True,
        name="driver-bot-poll",
    )
    _POLL_THREAD.start()

    # Verify token in background — don't block caller
    def _verify():
        try:
            url  = f"https://api.telegram.org/bot{token}/getMe"
            resp = _session.get(url, timeout=8)
            info = resp.json()
            if info.get("ok"):
                _l(f"Driver bot connected: @{info['result'].get('username', '?')}")
            else:
                _l(f"Driver bot token rejected: {info}", "warning")
                STOP_EVENT.set()  # kill poll thread if token is bad
        except Exception as e:
            _l(f"Driver bot connectivity check failed: {e}", "warning")

    threading.Thread(target=_verify, daemon=True).start()

    _l(f"Driver bot initializing. {len(cfg['drivers'])} driver(s) configured.")
    return True


def shutdown():
    """Gracefully stop the polling thread."""
    STOP_EVENT.set()
    if _POLL_THREAD and _POLL_THREAD.is_alive():
        _POLL_THREAD.join(timeout=3)
    _l("Driver bot shut down.")


# =============================================================
# STANDALONE TEST  (run directly: python driver_bot.py)
# =============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Driver Bot — standalone test mode")
    print("=" * 60)

    cfg = load_config()
    if not cfg:
        print(f"\nCreate driver_config.json at:\n  {_CONFIG_FILE}")
        print("\nExample content:")
        print(json.dumps({
            "driver_bot_token":       "YOUR_DRIVER_BOT_TOKEN",
            "dispatcher_bot_token":   "YOUR_DISPATCHER_BOT_TOKEN",
            "dispatcher_chat_ids":    [123456789],
            "drivers": [
                {
                    "name":             "John Smith",
                    "telegram_chat_id": 111222333,
                    "truck_type":       "LARGE STRAIGHT"
                }
            ]
        }, indent=2))
        raise SystemExit(1)

    # Use dummy license for test — local rendering will be used (no server call)
    ok = init(license_key="TEST-KEY", machine_id="TEST-MACHINE")
    if not ok:
        print("Init failed — check driver_config.json")
        raise SystemExit(1)

    print("\nSending test load card to all configured drivers...")
    print("(In test mode, bids are rendered locally — no server license needed)\n")
    TEST_LOAD = {
        "order":             "TEST001",
        "vehicle_required":  "LARGE STRAIGHT",
        "pickup_loc":        "Columbus, OH 43215",
        "delivery_loc":      "Pittsburgh, PA 15201",
        "pickup_dt":         "06/29/2026 08:00 AM EST",
        "delivery_dt":       "06/29/2026 14:00 PM EST",
        "google_deadhead":   42,
        "driver_name":       "John Smith",
        "truck_type":        "LARGE STRAIGHT",
        "truck_dimensions":  "264x97x103",
        "truck_equipment":   "Dock High, Air Ride",
        "deadhead_eta_minutes": 56,
        "bid_template":      _DEFAULT_TEMPLATE,
        "formatted_message": """draft : TEST001
        LARGE STRAIGHT
        📍Pick-up: Columbus, OH 43215
        Pick-up date (EST): 06/29/2026 08:00 AM EST

        📍 Deliver to: Pittsburgh, PA 15201
        Deliver date (EST): 06/29/2026 14:00 PM EST

        Out Miles: 42
        Loaded Miles: 325
        Total Miles: 367
        Driver: John Smith
        Truck Dims: 264x97x103

        🕒 TT: 7hrs
        🕒 ETA: 1hrs""",
    }
    notify_drivers("TEST001", TEST_LOAD)
    print("Load cards sent. Tap 💰 BID in your Telegram driver chat.")
    print("Watching for driver replies... (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()
        print("Stopped.")
        
