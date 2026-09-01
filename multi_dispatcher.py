"""
multi_dispatcher.py
===================
Multi-dispatcher / multi-driver extension for the Plutus freight bot.

Architecture
------------
* One backend process, multiple Telegram bot tokens.
* Each dispatcher has their own dedicated bot token registered via BotFather.
* One shared Driver Bot token used by all drivers.
* All bots run in daemon threads on the same backend.
* Routing rules (vehicle type → dispatcher, state → dispatcher, round-robin
  fallback) decide which dispatcher bot handles each inbound load.
* All dispatchers and drivers may share the same Telegram chat ID — routing is
  done by BOT TOKEN, not by chat ID, so each dispatcher only sees pings from
  their own bot even inside a shared group.

How to integrate with main.py
------------------------------
1. Place this file next to main.py.
2. In main.py replace the call to  main_loop(...)  inside run_bot_from_gui()
   with  multi_dispatcher_loop(...) from this module.
3. All Gmail parsing, draft creation, bid template, and LOAD_STORE logic in
   main.py remain completely unchanged — this module imports and reuses them.

Adding a new dispatcher
------------------------
Add one new DispatcherConfig entry to DISPATCHERS at the top of this file.
No other changes required anywhere.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ---------------------------------------------------------------------------
# These are imported from your existing main.py at runtime.
# They are referenced as strings here so the module can be read standalone.
# ---------------------------------------------------------------------------
# from main import (
#     LOAD_STORE, LOAD_STORE_LOCK, BID_TEMPLATE, BID_TEMPLATE_LOCK,
#     STOP_EVENT, TRUCKS, ACTIVE_LICENSE_KEY,
#     authenticate_gmail, get_label_map, get_current_history_id,
#     poll_new_messages_via_history, extract_text_from_full_message,
#     _has_custom_labels, _get_thread_info, _get_thread_label_names,
#     _safe_mark_read, _notify_labeled_thread, build_gmail_thread_url,
#     create_reply_draft, call_parse, call_build_bid, get_machine_id,
#     parse_truck_definitions, validate_truck_definitions,
#     FREIGHT_MARKERS, _US_STATES_SET, _SYSIDS,
#     mark_as_read, mark_as_unread,
# )


# =============================================================================
# ██████╗  ██████╗ ██╗   ██╗████████╗██╗███╗   ██╗ ██████╗
# ██╔══██╗██╔═══██╗██║   ██║╚══██╔══╝██║████╗  ██║██╔════╝
# ██████╔╝██║   ██║██║   ██║   ██║   ██║██╔██╗ ██║██║  ███╗
# ██╔══██╗██║   ██║██║   ██║   ██║   ██║██║╚██╗██║██║   ██║
# ██║  ██║╚██████╔╝╚██████╔╝   ██║   ██║██║ ╚████║╚██████╔╝
# ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝╚═╝  ╚═══╝ ╚═════╝
# ALL CONFIGURATION IS HERE — edit this section only to add/remove dispatchers
# =============================================================================

@dataclass
class DispatcherConfig:
    """One entry per dispatcher. Add a new dispatcher by adding a new instance."""
    name:            str             # Display name e.g. "Mike"
    bot_token:       str             # BotFather token for this dispatcher's bot
    chat_ids:        list[int]       # Telegram chat IDs this bot sends to
    vehicle_types:   list[str]       # e.g. ["LARGE STRAIGHT", "SMALL STRAIGHT"]
                                     # Empty list = no vehicle-type rule (uses states/fallback)
    allowed_states:  list[str]       # e.g. ["OH","PA","NY"] or [] for all states
                                     # Empty list = no state rule (uses vehicle/fallback)


@dataclass
class DriverBotConfig:
    """Single shared Driver Bot used by all drivers."""
    bot_token:  str
    chat_ids:   list[int]            # Driver chat IDs that receive load pings


# ---------------------------------------------------------------------------
# ── DISPATCHER DEFINITIONS — add one DispatcherConfig per dispatcher ────────
# ---------------------------------------------------------------------------
DISPATCHERS: list[DispatcherConfig] = [
    DispatcherConfig(
        name          = "Mike",
        bot_token     = "REPLACE_WITH_MIKE_BOT_TOKEN",
        chat_ids      = [1076034467],
        vehicle_types = ["LARGE STRAIGHT", "SMALL STRAIGHT"],
        allowed_states= [],                        # handles all states
    ),
    DispatcherConfig(
        name          = "Anna",
        bot_token     = "REPLACE_WITH_ANNA_BOT_TOKEN",
        chat_ids      = [1076034467],
        vehicle_types = ["CARGO VAN", "SPRINTER"],
        allowed_states= [],
    ),
    # ── Add more dispatchers here ──────────────────────────────────────────
    # DispatcherConfig(
    #     name          = "Tom",
    #     bot_token     = "REPLACE_WITH_TOM_BOT_TOKEN",
    #     chat_ids      = [1076034467],
    #     vehicle_types = [],                       # no vehicle filter
    #     allowed_states= ["CA","OR","WA"],         # west coast only
    # ),
]

# ---------------------------------------------------------------------------
# ── DRIVER BOT ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
DRIVER_BOT = DriverBotConfig(
    bot_token = "REPLACE_WITH_DRIVER_BOT_TOKEN",
    chat_ids  = [222222222],          # driver Telegram chat IDs
)

# ---------------------------------------------------------------------------
# ── ROUND-ROBIN fallback state (shared across threads, protected by lock) ──
# ---------------------------------------------------------------------------
_RR_INDEX      = 0
_RR_LOCK       = threading.Lock()

# ---------------------------------------------------------------------------
# ── DRIVER CONVERSATION STATE ───────────────────────────────────────────────
# Tracks drivers mid-conversation (waiting for price input).
# Key: (driver_chat_id, order_id)  →  {"order_id": str, "ts": float}
# ---------------------------------------------------------------------------
_DRIVER_STATE: dict[tuple[int, str], dict] = {}
_DRIVER_STATE_LOCK = threading.Lock()

# Key: driver_chat_id  →  order_id  (most recent load sent to that driver)
_DRIVER_PENDING: dict[int, str] = {}
_DRIVER_PENDING_LOCK = threading.Lock()


# =============================================================================
# HTTP SESSION — shared retry-enabled session for all bot API calls
# =============================================================================

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

_SESSION = _make_session()


# =============================================================================
# TELEGRAM HELPERS — token-aware versions of the original send helpers
# =============================================================================

def _tg_post(token: str, method: str, **kwargs) -> dict:
    """POST to any Telegram Bot API method. Returns parsed JSON or {}."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = _SESSION.post(url, timeout=8, **kwargs)
        if r.ok:
            return r.json()
        print(f"[TG:{method}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG:{method}] exception: {e}")
    return {}


def _tg_get(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = _SESSION.get(url, params=params or {}, timeout=8)
        if r.ok:
            return r.json()
        print(f"[TG:{method}] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TG:{method}] exception: {e}")
    return {}


def send_message(token: str, chat_id: int, text: str,
                 reply_markup: dict | None = None) -> dict:
    """Send a plain or keyboard message to one chat via a specific bot token."""
    body: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = json.dumps(reply_markup)
    return _tg_post(token, "sendMessage", json=body)


def send_to_all_chats(token: str, chat_ids: list[int], text: str,
                      reply_markup: dict | None = None):
    """Broadcast to every chat ID associated with a bot token."""
    threads = []
    for cid in chat_ids:
        t = threading.Thread(
            target=send_message,
            args=(token, cid, text, reply_markup),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=8)


def answer_callback(token: str, callback_query_id: str, text: str = ""):
    _tg_post(token, "answerCallbackQuery",
             json={"callback_query_id": callback_query_id, "text": text})


def get_updates(token: str, offset: int) -> tuple[int, list[dict]]:
    data = _tg_get(token, "getUpdates",
                   params={"offset": offset, "timeout": 0})
    if not data.get("ok"):
        return offset, []
    updates = data.get("result", [])
    if updates:
        offset = updates[-1]["update_id"] + 1
    return offset, updates


# =============================================================================
# ROUTING ENGINE
# =============================================================================

def _extract_delivery_state(load_data: dict) -> str | None:
    """Extract two-letter state code from delivery_loc stored in load_data."""
    delivery_loc = load_data.get("delivery_loc", "") or ""
    m = re.search(r",\s*([A-Z]{2})\b", delivery_loc.upper())
    if m:
        return m.group(1)
    # fallback: last token that looks like a state
    tokens = re.findall(r"\b([A-Z]{2})\b", delivery_loc.upper())
    if tokens:
        return tokens[-1]
    return None


def route_to_dispatcher(vehicle_required: str,
                         load_data: dict) -> DispatcherConfig:
    """
    Routing priority:
      1. Vehicle-type match (first dispatcher whose vehicle_types includes this vehicle)
      2. State match (first dispatcher whose allowed_states includes delivery state)
      3. Round-robin fallback across all dispatchers
    Returns the chosen DispatcherConfig.
    """
    global _RR_INDEX

    veh = (vehicle_required or "").upper().strip()
    delivery_state = _extract_delivery_state(load_data)

    # ── Rule 1: vehicle type ─────────────────────────────────────────────
    for d in DISPATCHERS:
        for vt in d.vehicle_types:
            if vt.upper() in veh or veh in vt.upper():
                return d

    # ── Rule 2: delivery state ───────────────────────────────────────────
    if delivery_state:
        for d in DISPATCHERS:
            if d.allowed_states and delivery_state in [s.upper() for s in d.allowed_states]:
                return d

    # ── Rule 3: round-robin fallback ─────────────────────────────────────
    with _RR_LOCK:
        chosen = DISPATCHERS[_RR_INDEX % len(DISPATCHERS)]
        _RR_INDEX += 1
    return chosen


# =============================================================================
# MESSAGE BUILDERS
# =============================================================================

def _build_dispatcher_keyboard(order_id: str, route_url: str) -> dict:
    """Inline keyboard for dispatcher load notification."""
    row1 = [
        {"text": "💵 BID PC",    "callback_data": f"bid:{order_id}"},
        {"text": "💵 BID PHONE", "callback_data": f"phone:{order_id}"},
        {"text": "📋 DRAFT",     "callback_data": f"text:{order_id}"},
    ]
    row2 = []
    if route_url:
        row2.append({"text": "🚩 ROUTE 🚩", "url": route_url})
    keyboard = [row1] + ([row2] if row2 else [])
    return {"inline_keyboard": keyboard}


def _build_driver_keyboard(order_id: str) -> dict:
    """Minimal inline keyboard for driver load notification."""
    return {"inline_keyboard": [[
        {"text": "💵 BID", "callback_data": f"driver_bid:{order_id}"}
    ]]}


def _load_summary_text(load_data: dict, driver_price: str | None = None) -> str:
    """Build the load summary text. Optionally prepend driver price."""
    lines = []
    if driver_price:
        lines.append(f"💰 <b>DRIVER PRICE: ${driver_price}</b>\n")
    lines.append(f"Order: {load_data.get('order', '?')}")
    lines.append(f"Vehicle: {load_data.get('vehicle_required', '?')}")
    lines.append(f"📍 Pickup: {load_data.get('pickup_loc', '?')}")
    lines.append(f"   Date: {load_data.get('pickup_dt', '?')}")
    lines.append(f"📍 Deliver: {load_data.get('delivery_loc', '?')}")
    lines.append(f"   Date: {load_data.get('delivery_dt', '?')}")
    if load_data.get("google_deadhead"):
        lines.append(f"Out Miles: {load_data['google_deadhead']}")
    if load_data.get("driver_name"):
        lines.append(f"Driver: {load_data['driver_name']}")
    return "\n".join(lines)


# =============================================================================
# DISPATCHER CALLBACK HANDLER
# Handles BID PC / BID PHONE / DRAFT / ROUTE callbacks for one dispatcher bot.
# =============================================================================

def handle_dispatcher_callbacks(dispatcher: DispatcherConfig,
                                  offset_holder: list[int],
                                  service,
                                  load_store: dict,
                                  load_store_lock: threading.Lock,
                                  bid_template_lock: threading.Lock,
                                  build_bid_body_fn,
                                  create_draft_fn,
                                  build_thread_url_fn):
    """
    Poll and process one round of updates for a dispatcher bot.
    Called in a tight loop from the dispatcher's daemon thread.
    """
    new_offset, updates = get_updates(dispatcher.bot_token, offset_holder[0])
    offset_holder[0] = new_offset

    for upd in updates:
        cq = upd.get("callback_query")
        if not cq:
            continue
        data  = cq.get("data", "")
        cqid  = cq.get("id", "")
        answer_callback(dispatcher.bot_token, cqid)

        if data.startswith("bid:"):
            order_id = data.split(":", 1)[1]
            with load_store_lock:
                load = load_store.get(order_id)
            if not load:
                continue
            try:
                body = build_bid_body_fn(order_id)
                if not body:
                    continue
                import pyperclip, webbrowser
                pyperclip.copy(body)
                thread_id = load.get("original_msg_full", {}).get("threadId", "")
                if thread_id:
                    webbrowser.open(build_thread_url_fn(thread_id))
                else:
                    webbrowser.open("https://mail.google.com/mail/u/0/#all")
                send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                  "📋 Bid text copied. Press Reply and paste (Ctrl+V).")
            except Exception as e:
                print(f"[{dispatcher.name}] bid callback error: {e}")

        elif data.startswith("phone:"):
            order_id = data.split(":", 1)[1]
            with load_store_lock:
                load = load_store.get(order_id)
            if not load:
                continue
            try:
                body = build_bid_body_fn(order_id)
                if not body:
                    continue
                send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                  f"📋 Long-press to copy:\n\n{body}")
                original_msg = load.get("original_msg_full", {})
                draft    = create_draft_fn(service, original_msg, "", None, empty=True)
                draft_id = draft.get("id", "")
                if draft_id:
                    draft_url = f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"
                    markup = {"inline_keyboard": [[
                        {"text": "📨 Open Draft & Send", "url": draft_url}
                    ]]}
                    send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                      f"✅ Draft ready for Order #{order_id}",
                                      reply_markup=markup)
                else:
                    markup = {"inline_keyboard": [[
                        {"text": "📂 Open Gmail Drafts",
                         "url": "https://mail.google.com/mail/u/0/#drafts"}
                    ]]}
                    send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                      f"✅ Draft created for Order #{order_id}",
                                      reply_markup=markup)
            except Exception as e:
                print(f"[{dispatcher.name}] phone callback error: {e}")
                send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                  f"❌ Failed to create draft: {e}")

        elif data.startswith("text:"):
            order_id = data.split(":", 1)[1]
            body = build_bid_body_fn(order_id)
            if body:
                send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                                  f"📋 ORDER #{order_id}:\n\n{body}")


# =============================================================================
# DRIVER CALLBACK HANDLER
# Handles the BID button from drivers and manages the price conversation.
# =============================================================================

def handle_driver_callbacks(driver_bot: DriverBotConfig,
                              offset_holder: list[int],
                              load_store: dict,
                              load_store_lock: threading.Lock,
                              log_fn=None):
    """
    Poll and process one round of updates for the driver bot.
    Manages multi-step price conversation keyed by (driver_chat_id, order_id).
    """
    new_offset, updates = get_updates(driver_bot.bot_token, offset_holder[0])
    offset_holder[0] = new_offset

    for upd in updates:
        # ── Button tap: driver_bid:<order_id> ───────────────────────────
        cq = upd.get("callback_query")
        if cq:
            data = cq.get("data", "")
            cqid = cq.get("id", "")
            if data.startswith("driver_bid:"):
                order_id       = data.split(":", 1)[1]
                driver_chat_id = cq["from"]["id"]
                answer_callback(driver_bot.bot_token, cqid)

                with load_store_lock:
                    load = load_store.get(order_id)

                if not load:
                    send_message(driver_bot.bot_token, driver_chat_id,
                                 "⚠️ Load not found — it may have expired.")
                    continue

                # Store conversation state
                with _DRIVER_PENDING_LOCK:
                    _DRIVER_PENDING[driver_chat_id] = order_id
                with _DRIVER_STATE_LOCK:
                    _DRIVER_STATE[(driver_chat_id, order_id)] = {
                        "order_id": order_id,
                        "ts":       time.time(),
                    }

                send_message(
                    driver_bot.bot_token, driver_chat_id,
                    f"💵 <b>Order #{order_id}</b>\n\n"
                    f"Enter your price (numbers only, e.g. <code>1500</code>):"
                )
            continue  # callback handled, move on

        # ── Text message: driver typing a price ─────────────────────────
        msg = upd.get("message")
        if not msg:
            continue
        driver_chat_id = msg["chat"]["id"]
        text           = (msg.get("text") or "").strip()

        with _DRIVER_PENDING_LOCK:
            order_id = _DRIVER_PENDING.get(driver_chat_id)

        if not order_id:
            continue  # driver not in a bid conversation

        # Validate price input
        price_match = re.fullmatch(r"\d{1,7}(?:\.\d{1,2})?", text)
        if not price_match:
            send_message(driver_bot.bot_token, driver_chat_id,
                         "⚠️ Invalid input. Please enter a number only, e.g. <code>1500</code>")
            continue

        price = text.strip()

        # Clear conversation state
        with _DRIVER_PENDING_LOCK:
            _DRIVER_PENDING.pop(driver_chat_id, None)
        with _DRIVER_STATE_LOCK:
            _DRIVER_STATE.pop((driver_chat_id, order_id), None)

        # Confirm to driver
        send_message(driver_bot.bot_token, driver_chat_id,
                     f"✅ Your bid of <b>${price}</b> for Order #{order_id} "
                     f"has been sent to dispatch.")

        # Fetch load data
        with load_store_lock:
            load = load_store.get(order_id)

        if not load:
            if log_fn:
                log_fn(f"[DRIVER] Order #{order_id} not in store when price arrived")
            continue

        # Route price to the correct dispatcher bot
        vehicle_required = load.get("vehicle_required", "")
        dispatcher       = route_to_dispatcher(vehicle_required, load)

        summary = _load_summary_text(load, driver_price=price)
        route_url = load.get("route_url", "")
        markup    = _build_dispatcher_keyboard(order_id, route_url)

        send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                          summary, reply_markup=markup)

        if log_fn:
            log_fn(f"[DRIVER] Order #{order_id} — driver price ${price} "
                   f"→ routed to dispatcher {dispatcher.name}")


# =============================================================================
# LOAD NOTIFIER
# Called once per new parsed load to send it to the right dispatcher + drivers.
# =============================================================================

def notify_load(order_id: str,
                formatted_text: str,
                load_store: dict,
                load_store_lock: threading.Lock,
                log_fn=None):
    """
    Send the parsed load to:
      1. The correct dispatcher bot (based on routing rules).
      2. All driver bots (with minimal BID button).
    """
    with load_store_lock:
        load = load_store.get(order_id)

    if not load:
        if log_fn:
            log_fn(f"[NOTIFY] Order #{order_id} not found in store")
        return

    vehicle_required = load.get("vehicle_required", "")
    dispatcher       = route_to_dispatcher(vehicle_required, load)
    route_url        = load.get("route_url", "")

    # ── Send to dispatcher ───────────────────────────────────────────────
    markup = _build_dispatcher_keyboard(order_id, route_url)
    send_to_all_chats(dispatcher.bot_token, dispatcher.chat_ids,
                      formatted_text, reply_markup=markup)
    if log_fn:
        log_fn(f"[ROUTE] Order #{order_id} ({vehicle_required}) "
               f"→ dispatcher {dispatcher.name}")

    # ── Send to drivers ──────────────────────────────────────────────────
    driver_text = (
        f"🚛 <b>New Load Available</b>\n\n"
        f"{_load_summary_text(load)}"
    )
    driver_markup = _build_driver_keyboard(order_id)

    # Track which loads this driver has been notified about
    with _DRIVER_PENDING_LOCK:
        for cid in DRIVER_BOT.chat_ids:
            # Only overwrite pending if driver is not mid-conversation
            if cid not in _DRIVER_PENDING:
                _DRIVER_PENDING[cid] = order_id

    send_to_all_chats(DRIVER_BOT.bot_token, DRIVER_BOT.chat_ids,
                      driver_text, reply_markup=driver_markup)


# =============================================================================
# DAEMON THREAD LAUNCHERS
# =============================================================================

def _dispatcher_thread(dispatcher: DispatcherConfig,
                        stop_event: threading.Event,
                        service_factory,
                        load_store: dict,
                        load_store_lock: threading.Lock,
                        bid_template_lock: threading.Lock,
                        build_bid_body_fn,
                        create_draft_fn,
                        build_thread_url_fn,
                        log_fn=None):
    """
    Daemon thread for one dispatcher bot.
    Polls Telegram, handles callbacks.
    """
    offset_holder = [0]
    service       = service_factory()

    if log_fn:
        log_fn(f"[{dispatcher.name}] dispatcher bot started")

    while not stop_event.is_set():
        try:
            handle_dispatcher_callbacks(
                dispatcher, offset_holder, service,
                load_store, load_store_lock, bid_template_lock,
                build_bid_body_fn, create_draft_fn, build_thread_url_fn,
            )
        except Exception as e:
            if log_fn:
                log_fn(f"[{dispatcher.name}] callback error: {e}")
            try:
                service = service_factory()
            except Exception:
                pass
        time.sleep(0.5)

    if log_fn:
        log_fn(f"[{dispatcher.name}] dispatcher bot stopped")


def _driver_thread(driver_bot: DriverBotConfig,
                    stop_event: threading.Event,
                    load_store: dict,
                    load_store_lock: threading.Lock,
                    log_fn=None):
    """Daemon thread for the shared driver bot."""
    offset_holder = [0]

    if log_fn:
        log_fn("[DRIVER BOT] started")

    while not stop_event.is_set():
        try:
            handle_driver_callbacks(
                driver_bot, offset_holder,
                load_store, load_store_lock, log_fn,
            )
        except Exception as e:
            if log_fn:
                log_fn(f"[DRIVER BOT] error: {e}")
        time.sleep(0.5)

    if log_fn:
        log_fn("[DRIVER BOT] stopped")


def launch_all_bot_threads(stop_event: threading.Event,
                            service_factory,
                            load_store: dict,
                            load_store_lock: threading.Lock,
                            bid_template_lock: threading.Lock,
                            build_bid_body_fn,
                            create_draft_fn,
                            build_thread_url_fn,
                            log_fn=None) -> list[threading.Thread]:
    """
    Launch daemon threads for every dispatcher bot + the driver bot.
    Returns the list of threads (all daemon=True, caller need not join them).
    """
    threads: list[threading.Thread] = []

    # One thread per dispatcher
    for d in DISPATCHERS:
        t = threading.Thread(
            target=_dispatcher_thread,
            args=(d, stop_event, service_factory,
                  load_store, load_store_lock, bid_template_lock,
                  build_bid_body_fn, create_draft_fn, build_thread_url_fn,
                  log_fn),
            name=f"dispatcher-{d.name}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # One driver bot thread
    t = threading.Thread(
        target=_driver_thread,
        args=(DRIVER_BOT, stop_event, load_store, load_store_lock, log_fn),
        name="driver-bot",
        daemon=True,
    )
    t.start()
    threads.append(t)

    return threads


# =============================================================================
# MAIN LOOP REPLACEMENT
# Drop-in replacement for main_loop() in main.py.
# Identical Gmail polling logic — only the Telegram dispatch layer changes.
# =============================================================================

def multi_dispatcher_loop(poll_seconds: float,
                           allowed_vehicles: list[str],
                           radius: int,
                           log_func=None,
                           allowed_delivery_states=None):
    """
    Replace main_loop() in run_bot_from_gui() with this function.

    It imports everything it needs from main.py at call time so there are
    no circular-import issues.
    """
    # ── Late imports from main.py ────────────────────────────────────────
    import main as _m

    def _log(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            pass
        if log_func:
            try:
                log_func(msg)
            except Exception:
                pass
        level = "ERROR" if ("❌" in msg or "[ERR]" in msg) else "INFO"
        _m._flog(level, msg)

    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build as _build
    from googleapiclient.errors import HttpError
    from concurrent.futures import ThreadPoolExecutor
    import queue as _queue

    def _make_service():
        creds = _m.authenticate_gmail()
        http  = httplib2.Http(timeout=60)
        http.disable_ssl_certificate_validation = True
        http.force_exception_to_status_code     = False
        return _build("gmail", "v1",
                      http=AuthorizedHttp(creds, http),
                      cache_discovery=False,
                      static_discovery=False)

    service      = _make_service()
    label_map    = _m.get_label_map(service)
    last_rebuild = time.time()

    processed_ids  = set()
    in_flight      = set()
    in_flight_lock = threading.Lock()
    active_futures: dict = {}
    futures_lock   = threading.Lock()
    _retry_counts: dict  = {}

    _log(f"▶ [MULTI] Watching: {', '.join(allowed_vehicles)}")
    _log(f"  Dispatchers: {', '.join(d.name for d in DISPATCHERS)}")

    # ── Launch all bot threads ───────────────────────────────────────────
    _bot_threads = launch_all_bot_threads(
        stop_event        = _m.STOP_EVENT,
        service_factory   = _make_service,
        load_store        = _m.LOAD_STORE,
        load_store_lock   = _m.LOAD_STORE_LOCK,
        bid_template_lock = _m.BID_TEMPLATE_LOCK,
        build_bid_body_fn = _m._build_bid_body_for_order,
        create_draft_fn   = _m.create_reply_draft,
        build_thread_url_fn = _m.build_gmail_thread_url,
        log_fn            = _log,
    )
    _log(f"[MULTI] {len(_bot_threads)} bot threads launched "
         f"({len(DISPATCHERS)} dispatchers + 1 driver bot)")

    # ── Service pool ─────────────────────────────────────────────────────
    _NUM_WORKERS = 5
    _svc_q: _queue.SimpleQueue = _queue.SimpleQueue()

    def _fill_pool():
        for _ in range(_NUM_WORKERS + 2):
            try:
                _svc_q.put(_make_service())
            except Exception as ex:
                print(f"[POOL] {ex}")

    threading.Thread(target=_fill_pool, daemon=True, name="svc-pool").start()

    _deadline = time.time() + 30
    while time.time() < _deadline:
        if _m.STOP_EVENT.is_set():
            return
        if _svc_q.qsize() >= _NUM_WORKERS:
            break
        time.sleep(0.5)

    def _get_svc():
        try:
            return _svc_q.get_nowait()
        except _queue.Empty:
            return _make_service()

    def _ret_svc(svc):
        _svc_q.put(svc)

    executor = ThreadPoolExecutor(max_workers=_NUM_WORKERS,
                                   thread_name_prefix="multibot")

    _MAX_RETRIES = 3

    def _is_conn_reset(exc):
        import errno as _errno
        if isinstance(exc, ConnectionResetError):
            return True
        if isinstance(exc, OSError):
            if getattr(exc, "winerror", None) == 10054:
                return True
            if exc.errno in (_errno.ECONNRESET, _errno.ECONNABORTED):
                return True
        return "10054" in str(exc) or "ConnectionReset" in type(exc).__name__

    # ── Email processor ──────────────────────────────────────────────────
    def _process_email(msg_id: str):
        if _m.STOP_EVENT.is_set():
            return
        svc = _get_svc()
        try:
            # STEP 1: cheap metadata + label check
            try:
                meta_pre = svc.users().messages().get(
                    userId="me", id=msg_id, format="minimal").execute()
            except HttpError as e:
                if e.resp.status == 404:
                    processed_ids.add(msg_id)
                    return
                raise

            if _m.STOP_EVENT.is_set():
                return

            if _m._has_custom_labels(meta_pre.get("labelIds", [])):
                processed_ids.add(msg_id)
                return

            _tid_pre = meta_pre.get("threadId", "")
            if _tid_pre:
                _tl, _ts = _m._get_thread_info(svc, _tid_pre, label_map)
                if _tl:
                    _m._notify_labeled_thread(_tl, _ts, _tid_pre)
                    processed_ids.add(msg_id)
                    return

            # STEP 2: full fetch
            try:
                full = svc.users().messages().get(
                    userId="me", id=msg_id, format="full").execute()
            except HttpError as e:
                if e.resp.status == 404:
                    processed_ids.add(msg_id)
                    return
                raise

            if _m.STOP_EVENT.is_set():
                return

            # STEP 3: race-condition guard
            if _m._has_custom_labels(full.get("labelIds", [])):
                processed_ids.add(msg_id)
                return
            _tid_full = full.get("threadId", "")
            if _tid_full:
                _tl2 = _m._get_thread_label_names(svc, _tid_full, label_map)
                if _tl2:
                    _subj = next(
                        (h["value"] for h in full.get("payload", {})
                         .get("headers", []) if h["name"].lower() == "subject"),
                        ""
                    )
                    _m._notify_labeled_thread(_tl2, _subj, _tid_full)
                    processed_ids.add(msg_id)
                    return

            # STEP 4: parse via server API
            body          = _m.extract_text_from_full_message(full)
            internal_date = int(full.get("internalDate", "0"))

            trucks_payload = [
                {
                    "vehicle":         t["vehicle"],
                    "driver_name":     t["driver_name"],
                    "dimensions":      t["dimensions"],
                    "max_payload_lbs": t.get("max_payload_lbs"),
                    "zip_location":    t["zip"],
                    "equipment":       t.get("equipment", ""),
                    "allowed_states":  list(t["allowed_states"])
                                       if t.get("allowed_states") else None,
                    "pickup_date":     t.get("pickup_date", ""),
                }
                for t in _m.TRUCKS
            ]

            try:
                from api_client import call_parse
                from license_manager import get_machine_id
                result = call_parse(
                    license_key      = _m.ACTIVE_LICENSE_KEY,
                    machine_id       = get_machine_id(),
                    email_body       = body,
                    internal_date_ms = internal_date,
                    allowed_vehicles = allowed_vehicles,
                    max_radius_miles = radius,
                    trucks           = trucks_payload,
                    bid_template     = _m.BID_TEMPLATE,
                )
            except PermissionError as e:
                _log(f"⛔ License revoked: {e}")
                _m.STOP_EVENT.set()
                return

            if _m.STOP_EVENT.is_set():
                return

            if result is None:
                _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                     f"⚠ Server unreachable [{msg_id[-6:]}]")
                return

            formatted = result.get("formatted")
            info      = result.get("message", "")
            order     = result.get("order_id")
            ts        = datetime.now().strftime("%H:%M:%S")
            gmid_tag  = f" [gmid:{msg_id[-8:]}]"

            if order and result.get("load_data"):
                with _m.LOAD_STORE_LOCK:
                    if len(_m.LOAD_STORE) >= 500:
                        del _m.LOAD_STORE[next(iter(_m.LOAD_STORE))]
                    _m.LOAD_STORE[order] = result["load_data"]
                    _m.LOAD_STORE[order]["original_msg_full"] = full

            if formatted:
                _log(f"[{ts}] ✅ #{order}{gmid_tag} → routing to dispatcher")
                # ── KEY DIFFERENCE: use notify_load instead of send_to_telegram
                notify_load(
                    order_id        = order,
                    formatted_text  = formatted,
                    load_store      = _m.LOAD_STORE,
                    load_store_lock = _m.LOAD_STORE_LOCK,
                    log_fn          = _log,
                )
            else:
                _log(f"[{ts}] ⏭  SKIPPED #{order}{gmid_tag} → {info}")

            # STEP 5: safe mark-read
            _subj_final = next(
                (h["value"] for h in full.get("payload", {})
                 .get("headers", []) if h["name"].lower() == "subject"),
                ""
            )
            _m._safe_mark_read(svc, msg_id, full.get("threadId", ""),
                               label_map, _subj_final, _log)

        except Exception as e:
            if _is_conn_reset(e) and not _m.STOP_EVENT.is_set():
                attempt = _retry_counts.get(msg_id, 0) + 1
                if attempt <= _MAX_RETRIES:
                    _retry_counts[msg_id] = attempt
                    wait = 2 ** attempt
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"⚠ ConnReset {msg_id[-6:]} retry {attempt}/{_MAX_RETRIES}")
                    time.sleep(wait)
                    with in_flight_lock:
                        in_flight.discard(msg_id)
                    _submit(msg_id)
                    return
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
            remaining = set(active_futures.values())
        for future, mid in done:
            if mid not in remaining:
                with in_flight_lock:
                    in_flight.discard(mid)
                processed_ids.add(mid)
                _retry_counts.pop(mid, None)
            try:
                future.result()
            except Exception as e:
                _log(f"[ERR] worker {mid[-6:]}: {e}")

    # ── Initial history ID ───────────────────────────────────────────────
    try:
        history_id = _m.get_current_history_id(service)
        _log(f"[SYS] historyId = {history_id}")
    except Exception as e:
        _log(f"[SYS] historyId failed: {e}")
        history_id = None

    # ── Initial scan ─────────────────────────────────────────────────────
    _log("[SYS] Initial catch-up scan...")
    try:
        _veh_terms  = " OR ".join(f'"{v}"' for v in allowed_vehicles)
        _init_query = f'is:unread newer_than:{_m.FRESH_WINDOW} ({_veh_terms})'
        _page       = None
        _total      = 0
        while True:
            _resp = service.users().messages().list(
                userId="me", q=_init_query,
                maxResults=500, pageToken=_page).execute()
            for _msg in _resp.get("messages", []):
                if _m.STOP_EVENT.is_set():
                    break
                _submit(_msg["id"])
                _total += 1
            _page = _resp.get("nextPageToken")
            if not _page or _m.STOP_EVENT.is_set():
                break
        _log(f"[SYS] Initial scan: {_total} queued")

        # ── Startup cleanup ──────────────────────────────────────────────
        _cleanup_ids: list[str] = []
        _cp = None
        while True:
            _cr = service.users().messages().list(
                userId="me", q=f"is:unread newer_than:{_m.FRESH_WINDOW}",
                maxResults=500, pageToken=_cp).execute()
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
                        metadataHeaders=["Subject"]).execute()
                except Exception:
                    continue
                if _m._has_custom_labels(_cmeta.get("labelIds", [])):
                    processed_ids.add(_cid)
                    continue
                _ct = _cmeta.get("threadId", "")
                if _ct:
                    _ctl, _ = _m._get_thread_info(service, _ct, label_map)
                    if _ctl:
                        processed_ids.add(_cid)
                        continue
                _cleanup_ids.append(_cid)
                processed_ids.add(_cid)
            _cp = _cr.get("nextPageToken")
            if not _cp or _m.STOP_EVENT.is_set():
                break

        _safe_ids = []
        for _cid in _cleanup_ids:
            try:
                _rc = service.users().messages().get(
                    userId="me", id=_cid, format="metadata",
                    metadataHeaders=["Subject"]).execute()
                if not _m._has_custom_labels(_rc.get("labelIds", [])):
                    _safe_ids.append(_cid)
            except Exception:
                pass

        for _i in range(0, len(_safe_ids), 1000):
            try:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": _safe_ids[_i:_i+1000],
                          "removeLabelIds": ["UNREAD"]}).execute()
            except Exception as _ce:
                _log(f"[SYS] Cleanup batch error: {_ce}")

        _log(f"[SYS] Startup cleanup: {len(_safe_ids)} cleared")
    except Exception as e:
        _log(f"[SYS] Initial scan failed: {e}")

    # ── Real-time history loop ───────────────────────────────────────────
    _log("[SYS] Entering real-time mode (multi-dispatcher)...")

    while not _m.STOP_EVENT.is_set():
        try:
            _reap_futures()

            if time.time() - last_rebuild > 1800:
                try:
                    service      = _make_service()
                    label_map    = _m.get_label_map(service)
                    last_rebuild = time.time()
                    _log("[SYS] Gmail credentials refreshed")
                except Exception as e:
                    _log(f"[SYS] Credential refresh failed: {e}")

            if history_id is None:
                try:
                    _veh_q = " OR ".join(f'"{v}"' for v in allowed_vehicles)
                    _fb_q  = f'is:unread newer_than:{_m.FRESH_WINDOW} ({_veh_q})'
                    _fp    = None
                    while True:
                        _fr = service.users().messages().list(
                            userId="me", q=_fb_q,
                            maxResults=500, pageToken=_fp).execute()
                        for _fm in _fr.get("messages", []):
                            if _m.STOP_EVENT.is_set():
                                break
                            if _fm["id"] not in processed_ids:
                                _submit(_fm["id"])
                        _fp = _fr.get("nextPageToken")
                        if not _fp or _m.STOP_EVENT.is_set():
                            break
                except Exception as e:
                    _log(f"[LOOP ERR] {e}")
                time.sleep(poll_seconds)
                continue

            new_history_id, new_msg_ids = \
                _m.poll_new_messages_via_history(service, history_id)

            if new_history_id is None:
                _log("[SYS] historyId expired — resetting")
                try:
                    history_id = _m.get_current_history_id(service)
                except Exception as e:
                    _log(f"[SYS] historyId reset failed: {e}")
                    history_id = None
                time.sleep(poll_seconds)
                continue

            history_id = new_history_id

            for msg_id in new_msg_ids:
                if _m.STOP_EVENT.is_set():
                    break
                if msg_id in processed_ids:
                    continue
                try:
                    meta = service.users().messages().get(
                        userId="me", id=msg_id, format="metadata",
                        metadataHeaders=["Subject"]).execute()
                except HttpError as e:
                    if e.resp.status == 404:
                        processed_ids.add(msg_id)
                        continue
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"❌ {msg_id[-6:]}: {e}")
                    continue
                except Exception as e:
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"❌ {msg_id[-6:]}: {e}")
                    continue

                if _m._has_custom_labels(meta.get("labelIds", [])):
                    processed_ids.add(msg_id)
                    continue

                _htid = meta.get("threadId", "")
                if _htid:
                    _htl, _hts = _m._get_thread_info(service, _htid, label_map)
                    if _htl:
                        _m._notify_labeled_thread(_htl, _hts, _htid)
                        processed_ids.add(msg_id)
                        continue

                subject = next(
                    (h["value"].upper() for h in
                     meta.get("payload", {}).get("headers", [])
                     if h["name"].lower() == "subject"),
                    ""
                )

                if not any(m in subject for m in _m.FREIGHT_MARKERS):
                    _m._safe_mark_read(service, msg_id, _htid,
                                       label_map, subject, _log)
                    _log(f"[{datetime.now().strftime('%H:%M:%S')}] "
                         f"🗑 Non-freight [{msg_id[-8:]}]: {subject[:70]}")
                    processed_ids.add(msg_id)
                    continue

                if not any(v.upper() in subject for v in allowed_vehicles):
                    _m._safe_mark_read(service, msg_id, _htid,
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

    _log("[SYS] Shutting down multi-dispatcher worker pool…")
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)
    _log("[SYS] Multi-dispatcher stopped.")