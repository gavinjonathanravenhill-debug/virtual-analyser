"""
GammaFlip.io API client — drop into virtual-analyser (Flask/Railway).

Docs:     https://docs.gammaflip.io
API base: https://api.gammaflip.io/api/v1

Requires: GAMMAFLIP_API_KEY env var (starts with gex_sk_...)
Get one at: gammaflip.io/account -> API Keys
(REST API is a paid-tier feature — 14-day trial gives 50 req/day,
Pro gives 500 req/day)

GEX data refreshes ~every 15 min server-side, so we cache locally
for 15 min to avoid burning your daily quota on repeat calls from
the frontend polling / chart refreshes.
"""

import os
import time
import requests

API_BASE = "https://api.gammaflip.io/api/v1"
API_KEY = os.environ.get("GAMMAFLIP_API_KEY", "")
CACHE_TTL = 15 * 60  # seconds — matches GammaFlip's own refresh cadence
REQUEST_TIMEOUT = 10  # seconds

_cache = {}  # {cache_key: (timestamp, data)}


class GammaFlipError(Exception):
    """Raised for any GammaFlip API failure (auth, network, bad response)."""
    pass


def _headers():
    if not API_KEY:
        raise GammaFlipError(
            "GAMMAFLIP_API_KEY not set. Add it in Railway -> your service "
            "-> Variables tab, then redeploy."
        )
    return {"X-API-Key": API_KEY}


def _get(path, cache_key=None):
    """GET with 15-min cache. Returns parsed JSON dict."""
    cache_key = cache_key or path
    now = time.time()

    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    url = f"{API_BASE}{path}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise GammaFlipError(f"Timed out calling {path}")
    except requests.exceptions.ConnectionError as e:
        raise GammaFlipError(f"Connection error calling {path}: {e}")

    if resp.status_code == 401:
        raise GammaFlipError("GammaFlip auth failed (401) — check your API key.")
    if resp.status_code == 429:
        raise GammaFlipError("GammaFlip rate limit hit (429) — daily quota exceeded.")
    if resp.status_code == 404:
        raise GammaFlipError(f"GammaFlip 404 on {path} — check coin symbol/endpoint.")
    if not resp.ok:
        raise GammaFlipError(f"GammaFlip error {resp.status_code} on {path}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        raise GammaFlipError(f"GammaFlip returned non-JSON response on {path}")

    _cache[cache_key] = (now, data)
    return data


def get_exchanges():
    """List exchanges GammaFlip aggregates options data from."""
    return _get("/exchanges")


def get_term_oi(coin: str):
    """
    Get term-structure open-interest / GEX data for a coin across all
    expirations — this is the payload with Gamma Regime, GEX Call/Put,
    GEX Up/Down, gamma flip level, and P1/P2 wall data.

    coin: e.g. "BTC", "ETH"
    """
    coin = coin.upper().strip()
    return _get(f"/gex/term-oi/all/{coin}", cache_key=f"term-oi:{coin}")


def get_gamma_summary(coin: str):
    """
    Convenience wrapper: pulls term-oi and reshapes it into the flat
    summary shape the /api/gamma-flip/<coin> route returns to the frontend.
    Adjust field extraction below once you've confirmed exact response
    keys from a live call (log raw payload once and check).
    """
    raw = get_term_oi(coin)

    def pick(d, *keys):
        for k in keys:
            if isinstance(d, dict) and k in d:
                return d[k]
        return None

    summary = {
        "coin": coin,
        "raw_fetched_at": time.time(),
        "gamma_regime": pick(raw, "gamma_regime", "regime"),
        "gex_call": pick(raw, "gex_call", "call_gex"),
        "gex_put": pick(raw, "gex_put", "put_gex"),
        "gamma_flip_level": pick(raw, "gamma_flip", "flip_level", "gamma_flip_level"),
        "p1_wall": pick(raw, "p1", "p1_wall"),
        "p2_wall": pick(raw, "p2", "p2_wall"),
        "spot": pick(raw, "spot", "underlying_price"),
    }
    return summary
