# client/license_manager.py
import hashlib, platform, uuid, os, json, time

import sys, os

def _get_exe_dir() -> str:
    # sys.argv[0] always points to the real EXE location
    # even in Nuitka onefile (unlike __file__ which points to temp)
    path = os.path.abspath(sys.argv[0])
    if os.path.isfile(path):
        return os.path.dirname(path)
    return os.path.dirname(os.path.abspath(__file__))

_EXE_DIR = _get_exe_dir()
LICENSE_CACHE = os.path.join(_EXE_DIR, "license_cache.json")
ACTIVE_LICENSE_KEY = None   # set by main.py after activation


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