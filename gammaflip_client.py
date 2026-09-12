import os, time, requests, logging

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

def parse_gamma_surface(raw_data: dict):
    """
    Parse gamma data by expiration (proxy for strike surface).
    GammaFlip API doesn't have by-strike endpoint, so we use expirations.
    """
    try:
        response = raw_data
        if 'data' in response:
            data = response['data']
        else:
            data = raw_data
        
        expirations = data.get('expirations', [])
        metadata = data.get('metadata', {})
        spot = metadata.get('current_price', 0)
        
        if not expirations or not spot:
            raise GammaFlipError("Invalid response format")
        
        # Use Deribit as primary, fallback to first available
        strikes_list = []
        for exp in expirations:
            exchanges = exp.get('exchanges', {})
            deribit = exchanges.get('deribit') or exchanges.get('bybit') or (list(exchanges.values())[0] if exchanges else None)
            
            if not deribit:
                continue
            
            # Use days_to_expiry as proxy for strike level
            dte = exp.get('days_to_expiry', 0)
            strike_proxy = spot + (dte * 10)  # Rough mapping: further expiry = higher strike
            
            strikes_list.append({
                "strike": strike_proxy,
                "net_gex": float(deribit.get('total_gex', 0) or 0),
                "abs_gex": float(deribit.get('abs_gex', 0) or 0),
                "upside_gex": float(deribit.get('upside_gex', 0) or 0),
                "downside_gex": float(deribit.get('downside_gex', 0) or 0),
                "expiry": exp.get('date', ''),
                "dte": dte,
            })
        
        strikes_list.sort(key=lambda x: x["strike"])
        
        return {
            "spot": spot,
            "strikes": strikes_list,
            "regime_labels": {},
            "vol_triggers": {},
            "metadata": metadata,
        }
    except Exception as e:
        raise GammaFlipError(f"Parse error: {str(e)}")

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
    
    upside_gex = sum(e.get("total", {}).get("upside_gex", 0) or 0 for e in expirations)
    downside_gex = sum(e.get("total", {}).get("downside_gex", 0) or 0 for e in expirations)
    
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
