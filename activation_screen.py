# client/activation_screen.py
import tkinter as tk
import threading
import time
import os
import json
import platform
import hashlib
import uuid
import requests

SERVER_URL = "http://178.105.208.7:8000"   # ← change this later when server is ready
import sys, os

def _get_exe_dir() -> str:
    # sys.argv[0] always points to the real EXE location
    # even in Nuitka onefile (unlike __file__ which points to temp)
    path = os.path.abspath(sys.argv[0])
    if os.path.isfile(path):
        return os.path.dirname(path)
    return os.path.dirname(os.path.abspath(__file__))

_EXE_DIR = _get_exe_dir()

_EXE_DIR      = _get_exe_dir()
LICENSE_CACHE = os.path.join(_EXE_DIR, "license_cache.json")


def get_machine_id() -> str:
    parts = [
        platform.node(),
        platform.machine(),
        str(uuid.getnode()),
        platform.processor(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def load_cached_license() -> dict:
    try:
        if os.path.exists(LICENSE_CACHE):
            with open(LICENSE_CACHE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_cached_license(data: dict):
    try:
        with open(LICENSE_CACHE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _check_license_online(key: str) -> dict:
    try:
        r = requests.post(
            f"{SERVER_URL}/api/heartbeat",
            json={"license_key": key, "machine_id": get_machine_id()},
            timeout=8,
        )
        if r.status_code == 200:
            return {"valid": True}
        return {"valid": False, "reason": r.json().get("detail", "License rejected")}
    except requests.RequestException:
        return {"valid": None, "reason": "Cannot reach server"}


def _activate_online(key: str) -> dict:
    try:
        r = requests.post(
            f"{SERVER_URL}/api/activate",
            json={
                "license_key":  key,
                "machine_id":   get_machine_id(),
                "machine_name": platform.node(),
            },
            timeout=10,
        )
        if r.status_code == 200:
            return {"success": True}
        return {"success": False, "reason": r.json().get("detail", "Activation failed")}
    except requests.RequestException as e:
        return {"success": False, "reason": f"Cannot connect to server: {e}"}


def run_activation_gate(on_success_callback):
    """
    Shows activation window if needed.
    Calls on_success_callback(license_key) when license is confirmed valid.
    If server is unreachable but a valid cache exists, allows 2-hour offline grace.
    """

    # ── 1. Try cached license first ───────────────────────────────────────
    cache = load_cached_license()
    cached_key = cache.get("key", "")

    if cached_key:
        result = _check_license_online(cached_key)

        if result["valid"] is True:
            # Online check passed — refresh cache timestamp and proceed
            cache["last_valid"] = time.time()
            save_cached_license(cache)
            on_success_callback(cached_key)
            return

        elif result["valid"] is None:
            # Server unreachable — check offline grace (2 hours)
            elapsed = time.time() - cache.get("last_valid", 0)
            if elapsed < 7200:
                on_success_callback(cached_key)
                return
            # Grace expired — fall through to activation window
            _show_activation_window(
                on_success_callback,
                prefill_key=cached_key,
                error="Server unreachable and offline grace period expired. Reconnect to continue."
            )
            return

        else:
            # License explicitly rejected (revoked/expired/machine mismatch)
            _show_activation_window(
                on_success_callback,
                prefill_key=cached_key,
                error=result.get("reason", "License invalid.")
            )
            return

    # ── 2. No cache — show fresh activation window ────────────────────────
    _show_activation_window(on_success_callback)


def _show_activation_window(on_success_callback, prefill_key="", error=""):

    root = tk.Tk()
    root.title("PLUTUS BOT — Activate")
    root.geometry("500x340")
    root.configure(bg="#1c1c1e")
    root.resizable(False, False)
    # Dark titlebar for activation window
    try:
        import ctypes
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20,
            ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int(1))
        )
    except Exception:
        pass
    root.attributes("-toolwindow", True) # <--- Removes icon and min/max buttons

    # Center window on screen
    root.update_idletasks()
    x = (root.winfo_screenwidth()  - 500) // 2
    y = (root.winfo_screenheight() - 340) // 2
    root.geometry(f"500x340+{x}+{y}")

    # Title
    tk.Label(
        root, text="PLUTUS BOT",
        bg="#1c1c1e", fg="#ffffff",
        font=("Segoe UI", 18, "bold")
    ).pack(pady=(36, 4))

    tk.Label(
        root, text="Enter your license key to continue",
        bg="#1c1c1e", fg="#98989e",
        font=("Segoe UI", 10)
    ).pack(pady=(0, 20))

    # Key entry
    key_var = tk.StringVar(value=prefill_key)
    ent = tk.Entry(
        root, textvariable=key_var,
        font=("Consolas", 13),
        bg="#3a3a3c", fg="#ffffff",
        insertbackground="white",
        relief="flat", width=28,
        justify="center",
    )
    ent.pack(ipady=9, padx=52)
    ent.focus_set()
    if prefill_key:
        ent.select_range(0, "end")

    # Status label
    status_var = tk.StringVar(value=error)
    status_lbl = tk.Label(
        root, textvariable=status_var,
        bg="#1c1c1e", fg="#ff453a",
        font=("Segoe UI", 9),
        wraplength=400,
    )
    status_lbl.pack(pady=(10, 0))

    activated = [False]

    def do_activate():
        key = key_var.get().strip()
        if not key:
            status_var.set("Please enter a license key.")
            return

        activate_btn.config(state="disabled", text="Activating…")
        status_var.set("")
        root.update_idletasks()

        def _run():
            result = _activate_online(key)

            def _update():
                if result["success"]:
                    activated[0] = True
                    save_cached_license({
                        "key":        key,
                        "machine_id": get_machine_id(),
                        "last_valid": time.time(),
                        "activated_at": time.time(),
                    })
                    root.destroy()
                    on_success_callback(key)
                else:
                    status_var.set(result.get("reason", "Activation failed."))
                    activate_btn.config(state="normal", text="Activate")

            root.after(0, _update)

        threading.Thread(target=_run, daemon=True).start()

    activate_btn = tk.Button(
        root,
        text="Activate",
        command=do_activate,
        bg="#0a84ff", fg="white",
        activebackground="#0060cc", activeforeground="white",
        font=("Segoe UI", 11, "bold"),
        relief="flat", padx=28, pady=10,
        cursor="hand2",
    )
    activate_btn.pack(pady=(16, 0))

    ent.bind("<Return>", lambda e: do_activate())

    tk.Label(
        root,
        text="Need a license? Contact your administrator.",
        bg="#1c1c1e", fg="#48484a",
        font=("Segoe UI", 8),
    ).pack(pady=(18, 0))

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()