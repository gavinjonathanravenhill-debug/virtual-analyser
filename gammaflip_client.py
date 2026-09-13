import os, time, requests, logging

logger = logging.getLogger(__name__)
API_BASE = "https://api.gammaflip.io/api/v1"
API_KEY = os.environ.get("GAMMAFLIP_API_KEY", "")
CACHE_TTL = 15 * 60
REQUEST_TIMEOUT = 10
_cache = {}

# GammaFlip advertises five endpoints (exchanges, coins, expirations,
# term-oi, by-strike) but the by-strike path isn't in the public
# quickstart. Probe once, remember which one answers, reuse it.
BY_STRIKE_CANDIDATES = [
    "/gex/by-strike/all/{coin}",
    "/gex/strike/all/{coin}",
    "/gex/strikes/all/{coin}",
    "/gex/by-strike/{coin}",
    "/gex/strike-oi/all/{coin}",
    "/gex/oi-strike/all/{coin}",
]
_resolved_strike_path = None


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
    if resp.status_code == 403:
        raise GammaFlipError("GammaFlip 403 - endpoint requires Pro tier")
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


def get_by_strike(coin: str):
    """Fetch real per-strike GEX, probing for the endpoint path on first use."""
    global _resolved_strike_path
    coin = coin.upper().strip()

    if _resolved_strike_path:
        return _get(_resolved_strike_path.format(coin=coin),
                    cache_key=f"by-strike:{coin}")

    errors = []
    for template in BY_STRIKE_CANDIDATES:
        path = template.format(coin=coin)
        try:
            data = _get(path, cache_key=f"by-strike:{coin}")
            _resolved_strike_path = template
            logger.info("GammaFlip by-strike endpoint resolved: %s", template)
            return data
        except GammaFlipError as e:
            errors.append(f"{path}: {e}")
            # A 403 means the path is right but the plan is too low. Stop
            # probing and say so rather than burning the daily quota.
            if "403" in str(e):
                raise GammaFlipError(
                    f"by-strike endpoint found at {path} but requires Pro tier")
            continue

    raise GammaFlipError("No by-strike endpoint responded. Tried: " +
                         "; ".join(errors))


def _extract_strike_rows(data):
    """Pull per-strike dicts out of whatever shape came back."""
    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    for key in ("strikes", "by_strike", "levels", "gex_by_strike"):
        rows = data.get(key) if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            return rows
        if isinstance(rows, dict) and rows:
            out = []
            for k, v in rows.items():
                row = dict(v) if isinstance(v, dict) else {}
                row.setdefault("strike", float(k))
                out.append(row)
            return out
    return []


def _num(row, *names, default=0.0):
    for n in names:
        if n in row and row[n] is not None:
            try:
                return float(row[n])
            except (TypeError, ValueError):
                continue
    return default


def compute_levels(strikes_list, spot):
    """Derive the labelled levels GammaFlip shows on its chart.

    N1/N2 - two largest negative-gamma strikes (volatility triggers)
    P1/P2 - two largest positive-gamma strikes (gamma walls)
    A1/A2 - two largest absolute-gamma strikes (pinning magnets)
    flip  - where cumulative net GEX crosses zero
    """
    regime_labels = {}
    vol_triggers = {}

    negatives = sorted([s for s in strikes_list if s["net_gex"] < 0],
                       key=lambda s: s["net_gex"])
    positives = sorted([s for s in strikes_list if s["net_gex"] > 0],
                       key=lambda s: -s["net_gex"])
    absolutes = sorted(strikes_list, key=lambda s: -s["abs_gex"])

    for i, s in enumerate(negatives[:2]):
        vol_triggers[str(s["strike"])] = {
            "label": f"N{i+1}",
            "net_gex": s["net_gex"],
        }
    for i, s in enumerate(positives[:2]):
        regime_labels[str(s["strike"])] = f"P{i+1}"
    for i, s in enumerate(absolutes[:2]):
        key = str(s["strike"])
        if key not in regime_labels:
            regime_labels[key] = f"A{i+1}"

    flip = None
    cumulative = 0.0
    prev_cum, prev_strike = None, None
    for s in sorted(strikes_list, key=lambda s: s["strike"]):
        cumulative += s["net_gex"]
        if prev_cum is not None and (prev_cum < 0) != (cumulative < 0):
            span = cumulative - prev_cum
            flip = (prev_strike + (-prev_cum / span) * (s["strike"] - prev_strike)
                    if span else s["strike"])
            break
        prev_cum, prev_strike = cumulative, s["strike"]

    return regime_labels, vol_triggers, flip


def parse_gamma_surface(raw_data: dict):
    """Parse genuine per-strike GEX into the shape the chart expects."""
    rows = _extract_strike_rows(raw_data)
    if not rows:
        raise GammaFlipError("Response contained no per-strike rows")

    meta = {}
    if isinstance(raw_data, dict):
        meta = (raw_data.get("data", raw_data) or {}).get("metadata", {}) or {}
    spot = _num(meta, "current_price", "spot", "spot_price")

    strikes_list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        strike = _num(row, "strike", "strike_price")
        if not strike:
            continue
        net = _num(row, "net_gex", "total_gex", "gex")
        abs_gex = _num(row, "abs_gex", "absolute_gex") or abs(net)
        strikes_list.append({
            "strike": strike,
            "net_gex": net,
            "abs_gex": abs_gex,
            "call_gex": _num(row, "call_gex"),
            "put_gex": _num(row, "put_gex"),
            "upside_gex": _num(row, "upside_gex"),
            "downside_gex": _num(row, "downside_gex"),
        })

    if not strikes_list:
        raise GammaFlipError("No usable strikes after parsing")

    strikes_list.sort(key=lambda x: x["strike"])

    if not spot:
        spot = strikes_list[len(strikes_list) // 2]["strike"]

    regime_labels, vol_triggers, flip = compute_levels(strikes_list, spot)

    return {
        "spot": spot,
        "strikes": strikes_list,
        "regime_labels": regime_labels,
        "vol_triggers": vol_triggers,
        "gamma_flip": flip,
        "source": "by-strike",
        "metadata": meta,
    }


def get_gamma_surface(coin: str):
    """Preferred entry point: real strikes, or a clear error explaining why not."""
    return parse_gamma_surface(get_by_strike(coin))


def get_gamma_summary(coin: str):
    raw = get_term_oi(coin)
    data = raw.get("data", raw)
    expirations = data.get("expirations", [])
    metadata = data.get("metadata", {})
    spot = metadata.get("current_price")

    agg_total_gex = sum(e.get("total", {}).get("total_gex", 0) or 0 for e in expirations)
    agg_call_oi = sum(e.get("total", {}).get("call_oi_usd", 0) or 0 for e in expirations)
    agg_put_oi = sum(e.get("total", {}).get("put_oi_usd", 0) or 0 for e in expirations)

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
        "gamma_regime": "bullish" if agg_total_gex > 0 else "bearish",
    }
