"""
GEX by strike from Deribit's public API.

No API key required. Deribit carries the large majority of BTC options
open interest, so this is a fair proxy for the whole market - and it is
REAL strike data, unlike the term-structure fallback.

book_summary_by_currency gives open interest, mark IV and the underlying
price but not gamma, so gamma is computed here with Black-Scholes.
"""

import math
import time
import requests

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
CACHE_TTL = 300          # 5 minutes
REQUEST_TIMEOUT = 15
_cache = {}


class DeribitError(Exception):
    pass


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(S, K, T, sigma, r=0.0):
    """Black-Scholes gamma. Same for calls and puts."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    except ValueError:
        return 0.0
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def _parse_instrument(name):
    """BTC-14SEP26-77000-C -> (77000.0, 'C', '14SEP26')"""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _, expiry, strike, kind = parts
    try:
        return float(strike), kind.upper(), expiry
    except ValueError:
        return None


def _expiry_years(expiry_code):
    """Days until expiry, as a fraction of a year.

    Deribit expiries are like 14SEP26 and settle at 08:00 UTC.
    """
    try:
        t = time.strptime(expiry_code, "%d%b%y")
    except ValueError:
        return None
    expiry_ts = time.mktime(t) + 8 * 3600
    seconds = expiry_ts - time.time()
    if seconds <= 0:
        return None
    return seconds / (365.25 * 24 * 3600)


def fetch_book_summary(currency="BTC"):
    key = f"book:{currency}"
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < CACHE_TTL:
        return hit[1]

    url = f"{DERIBIT_BASE}/get_book_summary_by_currency"
    try:
        r = requests.get(url, params={"currency": currency, "kind": "option"},
                         timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise DeribitError("Deribit request timed out")
    if not r.ok:
        raise DeribitError(f"Deribit returned {r.status_code}")
    payload = r.json()
    rows = payload.get("result", [])
    if not rows:
        raise DeribitError("Deribit returned no instruments")
    _cache[key] = (now, rows)
    return rows


def build_gex_by_strike(currency="BTC", strike_window=0.12, min_oi=0.0):
    """Aggregate GEX per strike across all expiries.

    strike_window limits output to strikes within +/- this fraction of
    spot, so the chart isn't squashed by far-out-of-the-money tails.
    """
    rows = fetch_book_summary(currency)

    spot = 0.0
    for row in rows:
        up = row.get("underlying_price")
        if up:
            spot = float(up)
            break
    if not spot:
        raise DeribitError("Could not determine spot price")

    lo = spot * (1 - strike_window)
    hi = spot * (1 + strike_window)

    by_strike = {}
    skipped = 0
    for row in rows:
        parsed = _parse_instrument(row.get("instrument_name", ""))
        if not parsed:
            skipped += 1
            continue
        strike, kind, expiry = parsed
        if strike < lo or strike > hi:
            continue

        oi = float(row.get("open_interest") or 0)
        if oi <= min_oi:
            continue

        iv = float(row.get("mark_iv") or 0) / 100.0     # pct -> decimal
        T = _expiry_years(expiry)
        if not T or iv <= 0:
            continue

        gamma = bs_gamma(spot, strike, T, iv)
        # Dollar gamma per 1% move. Calls add, puts subtract, which is
        # the standard dealer-positioning convention.
        gex = gamma * oi * spot * spot * 0.01
        signed = gex if kind == "C" else -gex

        rec = by_strike.setdefault(strike, {
            "strike": strike, "net_gex": 0.0, "abs_gex": 0.0,
            "call_gex": 0.0, "put_gex": 0.0, "oi": 0.0,
        })
        rec["net_gex"] += signed
        rec["abs_gex"] += gex
        rec["oi"] += oi
        if kind == "C":
            rec["call_gex"] += gex
        else:
            rec["put_gex"] += gex

    strikes = sorted(by_strike.values(), key=lambda r: r["strike"])
    if not strikes:
        raise DeribitError("No strikes within the window")

    return {
        "spot": spot,
        "strikes": strikes,
        "source": "deribit-by-strike",
        "instruments_seen": len(rows),
        "instruments_skipped": skipped,
    }


def get_surface(currency="BTC", strike_window=0.12):
    """Full surface with the labelled levels, ready for the chart."""
    from gammaflip_client import compute_levels
    base = build_gex_by_strike(currency, strike_window)
    regime_labels, vol_triggers, flip = compute_levels(base["strikes"],
                                                       base["spot"])
    base.update({
        "regime_labels": regime_labels,
        "vol_triggers": vol_triggers,
        "gamma_flip": flip,
        "axis": "price",
    })
    return base
