import os, time, requests, logging
from collections import defaultdict

logger = logging.getLogger(__name__)
API_BASE = "https://api.gammaflip.io/api/v1"
API_KEY = os.environ.get("GAMMAFLIP_API_KEY", "")
CACHE_TTL = 15 * 60
REQUEST_TIMEOUT = 10
_cache = {}

class GammaFlipError(Exception):
    pass

def _headers():
    if not API_KEY:
        raise GammaFlipError("GAMMAFLIP_API_KEY not set")
    return {"X-API-Key": API_KEY}

def _get(path, cache_key=None):
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
    if resp.status_code == 401:
        raise GammaFlipError("GammaFlip auth failed (401)")
    if resp.status_code == 429:
        raise GammaFlipError("GammaFlip rate limit hit (429)")
    if resp.status_code == 404:
        raise GammaFlipError(f"GammaFlip 404 on {path}")
    if not resp.ok:
        raise GammaFlipError(f"GammaFlip error {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise GammaFlipError("GammaFlip returned non-JSON response")
    _cache[cache_key] = (now, data)
    return data

def get_exchanges():
    return _get("/exchanges")

def get_term_oi(coin: str):
    coin = coin.upper().strip()
    return _get(f"/gex/term-oi/all/{coin}", cache_key=f"term-oi:{coin}")

def get_gamma_surface(coin: str, exchange: str = "Deribit", option_type: str = "all"):
    """Fetch gamma by strike. Tries multiple endpoint formats to find what works."""
    coin = coin.upper().strip()
    paths_to_try = [
        f"/gex/by-strike/{coin}/{exchange}/{option_type}",
        f"/gex/by-strike/{coin}/{exchange}",
        f"/gex/{exchange}/by-strike/{coin}/{option_type}",
        f"/gex/{exchange}/by-strike/{coin}",
        f"/gex/by-strike/{coin}",
    ]
    
    last_error = None
    for path in paths_to_try:
        try:
            return _get(path, cache_key=f"surface:{coin}:{exchange}")
        except GammaFlipError as e:
            last_error = e
            continue
    
    raise GammaFlipError(f"No working by-strike endpoint found for {coin}: {last_error}")

def parse_gamma_surface(raw_data: dict):
    """
    Parse gamma surface from GammaFlip and return:
    - strikes: sorted list of strike prices
    - net_gex_per_1pct: net GEX per 1% move (bar chart)
    - abs_gex: absolute GEX (line chart)
    - spot_price: current price
    - metadata: regime labels, vol triggers, etc.
    """
    
    # Handle multiple possible response formats
    data = raw_data.get("data", raw_data)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    
    strikes_dict = data.get("by_strike", data.get("strikes", {}))
    metadata = data.get("metadata", {})
    spot = metadata.get("current_price") or data.get("spot_price")
    
    if not strikes_dict or not spot:
        raise GammaFlipError("Invalid gamma surface response format")
    
    # Convert strikes to list and sort
    strikes_list = []
    for strike_str, strike_data in strikes_dict.items():
        try:
            strike = float(strike_str)
        except (ValueError, TypeError):
            continue
        
        net_gex = strike_data.get("net_gex", strike_data.get("total_gex", 0)) or 0
        abs_gex = strike_data.get("abs_gex", strike_data.get("absolute_gex", 0)) or 0
        
        strikes_list.append({
            "strike": strike,
            "net_gex": float(net_gex),
            "abs_gex": float(abs_gex),
            "call_volume": float(strike_data.get("call_volume", 0) or 0),
            "put_volume": float(strike_data.get("put_volume", 0) or 0),
        })
    
    strikes_list.sort(key=lambda x: x["strike"])
    
    # Identify gamma peaks & regime labels
    regime_labels = _identify_regimes(strikes_list, spot)
    vol_triggers = _identify_vol_triggers(strikes_list, spot)
    
    return {
        "spot": spot,
        "strikes": strikes_list,
        "regime_labels": regime_labels,
        "vol_triggers": vol_triggers,
        "metadata": metadata,
    }

def _identify_regimes(strikes_list, spot):
    """Identify P1, P2, A1, A2 labels based on net GEX peaks."""
    peaks = []
    for i, s in enumerate(strikes_list):
        is_peak = (
            s["net_gex"] > 0 and 
            (i == 0 or strikes_list[i-1]["net_gex"] <= s["net_gex"]) and
            (i == len(strikes_list) - 1 or strikes_list[i+1]["net_gex"] <= s["net_gex"])
        )
        if is_peak:
            peaks.append((s["strike"], s["net_gex"]))
    
    peaks.sort(key=lambda x: x[1], reverse=True)
    labels = {}
    
    # P1, P2 = positive peaks
    pos_peaks = [p for p in peaks if p[0] > spot][:2]
    if len(pos_peaks) >= 1:
        labels[pos_peaks[0][0]] = "P1*"
    if len(pos_peaks) >= 2:
        labels[pos_peaks[1][0]] = "P2"
    
    return labels

def _identify_vol_triggers(strikes_list, spot):
    """Identify N1, N2 vol trigger zones below spot."""
    below_spot = [s for s in strikes_list if s["strike"] < spot]
    if not below_spot:
        return {}
    
    triggers = {}
    if len(below_spot) >= 1:
        triggers[below_spot[-1]["strike"]] = "N2"
    if len(below_spot) >= 2:
        triggers[below_spot[-2]["strike"]] = "N1"
    
    return triggers

def get_gamma_summary(coin: str):
    raw = get_term_oi(coin)
    data = raw.get("data", raw)
    expirations = data.get("expirations", [])
    metadata = data.get("metadata", {})
    spot = metadata.get("current_price")
    agg_total_gex = sum(e.get("total", {}).get("total_gex", 0) or 0 for e in expirations)
    agg_call_oi = sum(e.get("total", {}).get("call_oi_usd", 0) or 0 for e in expirations)
    agg_put_oi = sum(e.get("total", {}).get("put_oi_usd", 0) or 0 for e in expirations)
    gamma_regime = "bullish" if agg_total_gex > 0 else "bearish"
    
    # Try to get surface data for upside/downside GEX
    upside_gex = 0
    downside_gex = 0
    try:
        surface = get_gamma_surface(coin)
        parsed = parse_gamma_surface(surface)
        spot_price = parsed["spot"]
        strikes_list = parsed["strikes"]
        upside_gex = sum(s["net_gex"] for s in strikes_list if s["strike"] > spot_price)
        downside_gex = sum(s["net_gex"] for s in strikes_list if s["strike"] < spot_price)
    except:
        pass
    
    return {
        "spot_price": spot,
        "total_gex": agg_total_gex,
        "call_oi": agg_call_oi,
        "put_oi": agg_put_oi,
        "call_oi_usd": agg_call_oi,
        "put_oi_usd": agg_put_oi,
        "upside_gex": upside_gex,
        "downside_gex": downside_gex,
        "gamma_regime": gamma_regime
    }
