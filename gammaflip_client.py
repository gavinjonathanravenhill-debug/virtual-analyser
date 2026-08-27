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


def discover_openapi_spec():
    """
    Try to fetch a machine-readable API spec (common on FastAPI-based
    APIs) to get the REAL list of routes instead of guessing paths.
    Tries a few conventional locations relative to the API root.
    """
    import re
    root = re.sub(r"/api/v1$", "", API_BASE)  # strip to bare domain
    candidates = [
        f"{root}/openapi.json",
        f"{API_BASE}/openapi.json",
        f"{root}/api/v1/openapi.json",
        f"{root}/docs",
        f"{root}/redoc",
    ]
    results = []
    for url in candidates:
        try:
            resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
            entry = {"url": url, "status": resp.status_code}
            if resp.ok:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        spec = resp.json()
                        paths = list(spec.get("paths", {}).keys())
                        entry["route_count"] = len(paths)
                        entry["routes"] = paths[:60]  # cap output size
                    except ValueError:
                        entry["sample"] = resp.text[:300]
                else:
                    entry["content_type"] = ct
                    entry["sample"] = resp.text[:200]
            results.append(entry)
        except requests.exceptions.RequestException as e:
            results.append({"url": url, "status": None, "error": str(e)})
    return results


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
    near = expirations[0] if
cat > gammaflip_routes.py << 'PYEOF'
"""
Flask blueprint for GammaFlip endpoints — register this in your app.py.

    from gammaflip_routes import gammaflip_bp
    app.register_blueprint(gammaflip_bp)

Exposes:
    GET /api/gamma-flip/<coin>       -> flat summary (regime, GEX, walls, spot)
    GET /api/gamma-flip/<coin>/raw   -> full raw GammaFlip term-oi payload
    GET /api/gamma-flip/exchanges    -> exchanges GammaFlip covers
"""

from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary,
    get_term_oi,
    get_exchanges,
    discover_by_strike,
    discover_openapi_spec,
    GammaFlipError,
)

gammaflip_bp = Blueprint("gammaflip", __name__, url_prefix="/api/gamma-flip")


@gammaflip_bp.route("/<coin>", methods=["GET"])
def gamma_flip_summary(coin):
    try:
        data = get_gamma_summary(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/exchanges", methods=["GET"])
def gamma_flip_exchanges():
    try:
        data = get_exchanges()
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/discover-by-strike", methods=["GET"])
def gamma_flip_discover(coin):
    """
    Temporary debug route — probes candidate by-strike endpoint paths
    and reports which one(s) actually respond. Remove once the real
    endpoint is confirmed and wired into get_gamma_summary properly.
    """
    try:
        results = discover_by_strike(coin)
        return jsonify({"ok": True, "results": results})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/discover-routes", methods=["GET"])
def gamma_flip_discover_routes():
    """
    Temporary debug route — tries to fetch GammaFlip's OpenAPI spec (or
    docs page) to get the real, complete route list. Remove once the
    by-strike endpoint is confirmed and wired in properly.
    """
    try:
        results = discover_openapi_spec()
        return jsonify({"ok": True, "results": results})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
