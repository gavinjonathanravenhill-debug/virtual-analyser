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


# Candidate paths for the by-strike GEX endpoint (chart data behind the
# gammaflip.io "GEX by Strike" view) — schema/path unconfirmed, so we
# probe several plausible options and report which one actually works.
BY_STRIKE_CANDIDATES = [
    "/gex/by-strike/all/{coin}",
    "/gex/by-strike/{coin}",
    "/gex/strikes/all/{coin}",
    "/gex/strikes/{coin}",
    "/gex/by-strike/all/{coin}/0",
    "/gex/profile/all/{coin}",
]


def discover_by_strike(coin: str):
    """
    Probe candidate by-strike endpoint paths with the real API key and
    report status codes + a snippet of any successful response, so we
    can confirm the real path/schema instead of guessing.
    """
    coin = coin.upper().strip()
    results = []
    for template in BY_STRIKE_CANDIDATES:
        path = template.format(coin=coin)
        url = f"{API_BASE}{path}"
        try:
            resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            entry = {"path": path, "status": resp.status_code}
            if resp.ok:
                try:
                    body = resp.json()
                    entry["sample"] = str(body)[:500]
                except ValueError:
                    entry["sample"] = resp.text[:300]
            results.append(entry)
        except requests.exceptions.RequestException as e:
            results.append({"path": path, "status": None, "error": str(e)})
    return results


def get_gamma_summary(coin: str):
    """
    Convenience wrapper: pulls term-oi and reshapes it into the flat
    summary shape the /api/gamma-flip/<coin> route returns to the frontend.

    Real response shape (confirmed from a live call):
        {
          "data": {
            "expirations": [
              {
                "date": "28AUG26", "days_to_expiry": 0,
                "exchanges": {"bybit": {...}, "deribit": {...}, "okx": {...}},
                "total": {
                  "abs_gex": ..., "total_gex": ..., "call_oi_usd": ...,
                  "put_oi_usd": ..., "upside_gex": ..., "downside_gex": ...,
                  "oi_usd": ..., "contracts": ...
                },
                "timestamp": "..."
              },
              ...
            ],
            "metadata": {
              "basecoin": "BTC", "current_price": 79445.1,
              "exchanges_used": [...], "expiration_count": 13,
              "timestamp": "...", "timestamp_ms": ...
            }
          },
          "meta": {...}
        }

    There is NO gamma-regime label, wall level, or gamma-flip price in
    this endpoint — those live in /gex/by-strike, which isn't wired in
    yet. We derive a regime label from the sign of aggregate total_gex
    (positive = dealers net long gamma / dampening; negative = dealers
    net short gamma / amplifying) — this is the standard convention,
    not a value GammaFlip returns directly.
    """
    raw = get_term_oi(coin)
    data = raw.get("data", raw)  # handle either wrapped or unwrapped
    expirations = data.get("expirations", [])
    metadata = data.get("metadata", {})

    spot = metadata.get("current_price")

    # Aggregate GEX across all expirations (whole term structure)
    agg_total_gex = sum(e.get("total", {}).get("total_gex", 0) or 0 for e in expirations)
    agg_call_oi = sum(e.get("total", {}).get("call_oi_usd", 0) or 0 for e in expirations)
    agg_put_oi = sum(e.get("total", {}).get("put_oi_usd", 0) or 0 for e in expirations)
    agg_upside_gex = sum(e.get("total", {}).get("upside_gex", 0) or 0 for e in expirations)
    agg_downside_gex = sum(e.get("total", {}).get("downside_gex", 0) or 0 for e in expirations)

    # Nearest-dated expiry (0DTE / front-week) — where dealer hedging
    # pressure is usually most acute
    near = expirations[0] if expirations else {}
    near_total = near.get("total", {})

    regime = "Positive" if agg_total_gex > 0 else "Negative" if agg_total_gex < 0 else None

    summary = {
        "coin": coin,
        "raw_fetched_at": time.time(),
        "spot": spot,
        "gamma_regime": regime,
        "total_gex": agg_total_gex,
        "call_oi_usd": agg_call_oi,
        "put_oi_usd": agg_put_oi,
        "upside_gex": agg_upside_gex,
        "downside_gex": agg_downside_gex,
        "near_expiry_date": near.get("date"),
        "near_expiry_total_gex": near_total.get("total_gex"),
        "near_expiry_call_oi_usd": near_total.get("call_oi_usd"),
        "near_expiry_put_oi_usd": near_total.get("put_oi_usd"),
        "expiration_count": metadata.get("expiration_count"),
    }
    return summary
