"""
One shared queue for every GeckoTerminal call on the site (free API ~10 calls/min per IP).

Everything that talks to GeckoTerminal (migrated scanner, spike forensics, risk checks, MM check, Wintermute)
goes through get(): calls are spaced out, a 429 pauses EVERYONE and retries with backoff instead of failing,
and clicks from the page (interactive=True) go ahead of the background scanner.
"""

import os
import threading
import time

import requests

GAP = float(os.getenv("GT_GAP_SECONDS", "6.5"))
_lock = threading.Lock()
_state = {"last": 0.0, "pause_until": 0.0, "waiting_interactive": 0, "calls": 0, "limited": 0}
_s = requests.Session()
HEADERS = {"accept": "application/json;version=20230302"}


def get(url, params=None, headers=None, timeout=20, interactive=False, tries=4):
    if interactive:
        _state["waiting_interactive"] += 1
    try:
        for attempt in range(tries):
            while not interactive and _state["waiting_interactive"] > 0:
                time.sleep(0.5)          # let the page's request go first
            with _lock:
                wait = max(_state["last"] + GAP, _state["pause_until"]) - time.time()
                if wait > 0:
                    time.sleep(wait)
                _state["last"] = time.time()
                _state["calls"] += 1
                r = _s.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=timeout)
            if r.status_code != 429:
                return r
            _state["limited"] += 1
            _state["pause_until"] = time.time() + 15 * (attempt + 1)   # everyone backs off together
        return r
    finally:
        if interactive:
            _state["waiting_interactive"] -= 1


def status():
    return {**_state, "paused_for": max(0, int(_state["pause_until"] - time.time()))}
