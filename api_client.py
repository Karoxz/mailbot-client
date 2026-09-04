import requests, time

SERVER_URL = "http://178.105.208.7:8000"  # real server
# SERVER_URL = "http://127.0.0.1:8000"  # test server

_session   = requests.Session()

def call_parse(license_key, machine_id, email_body, internal_date_ms,
               allowed_vehicles, max_radius_miles, trucks, bid_template) -> Optional[dict]:
    payload = {
        "license_key":      license_key,
        "machine_id":       machine_id,
        "email_body":       email_body,
        "internal_date_ms": internal_date_ms,
        "allowed_vehicles": allowed_vehicles,
        "max_radius_miles": max_radius_miles,
        "bid_template":     bid_template,
        "trucks":           trucks,
    }
    for attempt in range(3):
        try:
            r = _session.post(f"{SERVER_URL}/api/parse",
                              json=payload, timeout=25)
            if r.status_code == 403:
                raise PermissionError(r.json().get("detail", "License rejected"))
            r.raise_for_status()
            return r.json()
        except PermissionError:
            raise
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.5)
    return None

from typing import Optional

def call_build_bid(license_key, machine_id, load_data) -> Optional[str]:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/build_bid",
            json={"license_key": license_key, "machine_id": machine_id,
                  "load_data": load_data},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("bid_text", "")
    except Exception as e:
        print(f"call_build_bid error: {e}")
    return None
def call_poll_push(license_key, machine_id):
    try:
        r = requests.get(
            f"{SERVER_URL}/webhook/poll",
            headers={
                "X-License-Key": license_key,
                "X-Machine-Id": machine_id,
            },
            timeout=5
        )
        if r.status_code == 200:
            return r.json().get("history_ids", [])
    except Exception:
        pass
    return []

def call_record_bid(license_key, machine_id, bid_data: dict) -> dict:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/record_bid",
            json={"license_key": license_key, "machine_id": machine_id, **bid_data},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_record_bid HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_record_bid error: {e}")
    return {}
def call_update_bid_amount(license_key, machine_id, bid_id, bid_amount) -> dict: # type: ignore
    try:
        r = _session.post(
            f"{SERVER_URL}/api/update_bid_amount",
            json={"license_key": license_key, "machine_id": machine_id,
                  "bid_id": bid_id, "bid_amount": bid_amount},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_update_bid_amount HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_update_bid_amount error: {e}")
    return {}
def call_classify_reply(license_key, machine_id, thread_id, subject, message_body) -> dict:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/classify_reply",
            json={"license_key": license_key, "machine_id": machine_id,
                  "thread_id": thread_id, "subject": subject, "message_body": message_body},
            timeout=15,   # server-side LLM call can take a few seconds
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_classify_reply HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_classify_reply error: {e}")
    return {}

def call_update_bid_amount(license_key, machine_id, bid_id, bid_amount) -> dict:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/update_bid_amount",
            json={"license_key": license_key, "machine_id": machine_id,
                  "bid_id": bid_id, "bid_amount": bid_amount},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_update_bid_amount HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_update_bid_amount error: {e}")
    return {}

def call_set_thread_learning(license_key, machine_id, enabled: bool) -> dict:
    endpoint = "enable" if enabled else "disable"
    try:
        r = _session.post(
            f"{SERVER_URL}/api/thread_learning/{endpoint}",
            json={"license_key": license_key, "machine_id": machine_id},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_set_thread_learning HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_set_thread_learning error: {e}")
    return {}


def call_get_thread_learning_status(license_key, machine_id) -> dict:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/thread_learning/status",
            json={"license_key": license_key, "machine_id": machine_id},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_get_thread_learning_status HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_get_thread_learning_status error: {e}")
    return {}

def call_set_telegram_enabled(license_key, machine_id, enabled: bool) -> dict:
    endpoint = "enable" if enabled else "disable"
    try:
        r = _session.post(
            f"{SERVER_URL}/api/telegram/{endpoint}",
            json={"license_key": license_key, "machine_id": machine_id},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_set_telegram_enabled HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_set_telegram_enabled error: {e}")
    return {}


def call_get_telegram_status(license_key, machine_id) -> dict:
    try:
        r = _session.post(
            f"{SERVER_URL}/api/telegram/status",
            json={"license_key": license_key, "machine_id": machine_id},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_get_telegram_status HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_get_telegram_status error: {e}")
    return {}


def call_backfill_thread(license_key, machine_id, thread_id, order_id, messages: list) -> dict:
    # The server makes up to ~2 throttled Gemini calls per message (rate
    # extraction, plus reply classification per broker turn), each paced
    # at ~4.5s with a possible 10s 429-retry — a flat 30s timeout was
    # routinely too short for any thread with more than a couple of
    # messages. Scale with message count instead, capped at 5 minutes.
    backfill_timeout = min(300, 20 + len(messages) * 12)
    try:
        r = _session.post(
            f"{SERVER_URL}/api/backfill_thread",
            json={"license_key": license_key, "machine_id": machine_id,
                  "thread_id": thread_id, "order_id": order_id, "messages": messages},
            timeout=backfill_timeout,
        )
        if r.status_code == 200:
            return r.json()
        print(f"call_backfill_thread HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"call_backfill_thread error: {e}")
    return {}