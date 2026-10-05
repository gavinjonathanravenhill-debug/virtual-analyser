"""MEXC perpetuals scanner (/perps).

Ranks every MEXC USDT perp by 24h move, 1h move and volume, then checks the order book of the shortlist and
flags thin books - the ones where a modest order moves the price and leverage gets liquidated on a wick.

Flags (defaults, overridable per request):
  THIN   - less than $50k resting within ±2% of mid (bids + asks)
  WIDE   - bid/ask spread over 0.3%
  LOWVOL - under $1M traded in 24h
Data: contract.mexc.com public API (no key needed).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Blueprint, jsonify, request, render_template

BASE = "https://contract.mexc.com/api/v1/contract"
perps_bp = Blueprint("perps", __name__)

MEMES = {
    "DOGE", "SHIB", "PEPE", "1000PEPE", "WIF", "BONK", "1000BONK", "FLOKI", "1000FLOKI", "PENGU", "TRUMP",
    "FARTCOIN", "POPCAT", "BRETT", "MOG", "1000MOG", "SPX", "SPX6900", "PNUT", "NEIRO", "NEIROETH", "TURBO",
    "MEME", "BOME", "MEW", "GOAT", "MOODENG", "CHILLGUY", "PEOPLE", "WOJAK", "BABYDOGE", "1000SATS",
    "DOGS", "HMSTR", "CAT", "1000CAT", "SUNDOG", "GIGA", "MICHI", "PONKE", "SLERF", "MYRO", "WEN", "TOSHI",
    "DEGEN", "LADYS", "ELON", "BAN", "ACT", "LUCE", "FWOG", "RETARDIO", "ZEREBRO", "AI16Z", "GRIFFAIN",
    "PUMPFUN", "PUMP", "USELESS", "MELANIA", "LIBRA", "VINE", "HIPPO", "SIGMA", "KOMA", "MUBARAK", "BROCCOLI",
    "TST", "PIPPIN", "ALCH", "ANSEM", "HOSICO", "GORK", "HOUSE", "LAUNCHCOIN", "BUTTHOLE", "DADDY", "MOTHER",
}
DEFAULTS = {"thin_usd": 50_000, "wide_pct": 0.3, "lowvol_usd": 1_000_000, "depth_pct": 2.0, "top": 40}

_cache = {"ticker": (0, []), "detail": (0, {}), "depth": {}, "kline": {}}
_lock = threading.Lock()
_sess = requests.Session()
_sess.headers["User-Agent"] = "virtual-analyser/perps"


def _get(path, **params):
    r = _sess.get(f"{BASE}/{path}", params=params or None, timeout=10)
    r.raise_for_status()
    d = r.json()
    if not d.get("success", True) and d.get("code") not in (0, None):
        raise RuntimeError(f"MEXC {path}: {d.get('message') or d.get('code')}")
    return d.get("data")


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def tickers():
    with _lock:
        ts, rows = _cache["ticker"]
    if time.time() - ts < 20 and rows:
        return rows
    rows = _get("ticker") or []
    with _lock:
        _cache["ticker"] = (time.time(), rows)
    return rows


def contract_sizes():
    """symbol -> contractSize (base units per contract). Refreshed hourly."""
    with _lock:
        ts, d = _cache["detail"]
    if time.time() - ts < 3600 and d:
        return d
    d = {x["symbol"]: _f(x.get("contractSize"), 1.0) or 1.0 for x in (_get("detail") or []) if x.get("symbol")}
    with _lock:
        _cache["detail"] = (time.time(), d)
    return d


def book_stats(symbol, price, csize, depth_pct):
    """Notional resting within ±depth_pct and ±1% of mid, plus spread. Cached 45s."""
    key = (symbol, depth_pct)
    hit = _cache["depth"].get(key)
    if hit and time.time() - hit[0] < 45:
        return hit[1]
    d = _get(f"depth/{symbol}", limit=100) or {}
    bids = [(_f(p), _f(v)) for p, v, *_ in d.get("bids") or []]
    asks = [(_f(p), _f(v)) for p, v, *_ in d.get("asks") or []]
    if not bids or not asks:
        out = {"spread_pct": None, "depth_usd": 0.0, "bid_1pct": 0.0, "ask_1pct": 0.0}
    else:
        bb, ba = bids[0][0], asks[0][0]
        mid = (bb + ba) / 2 or price

        def side(levels, pct, below):
            lim = mid * (1 - pct / 100) if below else mid * (1 + pct / 100)
            return sum(p * v * csize for p, v in levels if (p >= lim if below else p <= lim))

        out = {"spread_pct": (ba - bb) / mid * 100 if mid else None,
               "depth_usd": side(bids, depth_pct, True) + side(asks, depth_pct, False),
               "bid_1pct": side(bids, 1.0, True), "ask_1pct": side(asks, 1.0, False)}
    _cache["depth"][key] = (time.time(), out)
    return out


def hour_stats(symbol):
    """1h price change and 1h turnover from 5-minute candles. Cached 60s."""
    hit = _cache["kline"].get(symbol)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    now = int(time.time())
    k = _get(f"kline/{symbol}", interval="Min5", start=now - 3900, end=now) or {}
    closes, opens, amts, times = k.get("close") or [], k.get("open") or [], k.get("amount") or [], k.get("time") or []
    last = [i for i, t in enumerate(times) if _f(t) >= now - 3600] or list(range(max(0, len(closes) - 12), len(closes)))
    out = {"chg_1h": None, "amt_1h": None}
    if last and closes:
        o, c = _f(opens[last[0]]), _f(closes[last[-1]])
        out = {"chg_1h": (c / o - 1) * 100 if o else None, "amt_1h": sum(_f(amts[i]) for i in last if i < len(amts))}
    _cache["kline"][symbol] = (time.time(), out)
    return out


def base_of(symbol):
    return symbol.rsplit("_", 1)[0]


def is_meme(symbol):
    b = base_of(symbol).upper()
    return b in MEMES or (b.startswith("1000") and b[4:] in MEMES)


def scan(memes_only=False, sort="move", **over):
    cfg = dict(DEFAULTS)
    for k, v in over.items():
        if k in DEFAULTS and _f(v, None) is not None and _f(v) >= 0:
            cfg[k] = _f(v)
    sizes = contract_sizes()
    rows = []
    for t in tickers():
        sym = t.get("symbol") or ""
        if not sym.endswith("_USDT"):
            continue
        meme = is_meme(sym)
        if memes_only and not meme:
            continue
        price = _f(t.get("lastPrice"))
        cs = sizes.get(sym, 1.0)
        rows.append({"symbol": sym, "base": base_of(sym), "meme": meme, "price": price,
                     "chg_24h": _f(t.get("riseFallRate")) * 100, "vol_24h": _f(t.get("amount24")),
                     "oi_usd": _f(t.get("holdVol")) * cs * price, "funding_pct": _f(t.get("fundingRate")) * 100,
                     "high_24h": _f(t.get("high24Price")), "low_24h": _f(t.get("lower24Price")), "csize": cs})
    # shortlist: biggest absolute movers + biggest volume, so both "what's moving" and "where the money is" get books
    n = int(cfg["top"])
    by_move = sorted(rows, key=lambda r: -abs(r["chg_24h"]))[:n]
    by_vol = sorted(rows, key=lambda r: -r["vol_24h"])[: n // 2]
    short = list({r["symbol"]: r for r in by_move + by_vol}.values())

    def enrich(r):
        try:
            r.update(book_stats(r["symbol"], r["price"], r["csize"], cfg["depth_pct"]))
        except Exception as e:
            r["book_error"] = str(e)[:80]
        try:
            r.update(hour_stats(r["symbol"]))
        except Exception:
            r.setdefault("chg_1h", None)
            r.setdefault("amt_1h", None)
        return r

    with ThreadPoolExecutor(8) as ex:
        short = list(ex.map(enrich, short))
    for r in short:
        flags = []
        if "depth_usd" in r and r["depth_usd"] < cfg["thin_usd"]:
            flags.append("THIN")
        if r.get("spread_pct") is not None and r["spread_pct"] > cfg["wide_pct"]:
            flags.append("WIDE")
        if r["vol_24h"] < cfg["lowvol_usd"]:
            flags.append("LOWVOL")
        r["flags"] = flags
        hourly_avg = r["vol_24h"] / 24 if r["vol_24h"] else 0
        r["vol_spike"] = (r["amt_1h"] / hourly_avg) if r.get("amt_1h") and hourly_avg else None
        # a move is only "real" if money traded and the book can take a position
        r["quality"] = "thin" if flags else "tradeable"
    key = {"move": lambda r: -r["chg_24h"], "drop": lambda r: r["chg_24h"], "move1h": lambda r: -(r.get("chg_1h") or -1e9),
           "volume": lambda r: -r["vol_24h"], "spike": lambda r: -(r.get("vol_spike") or 0),
           "depth": lambda r: -(r.get("depth_usd") or 0)}.get(sort, lambda r: -r["chg_24h"])
    short.sort(key=key)
    return {"rows": short, "cfg": cfg, "scanned": len(rows), "ts": int(time.time())}


@perps_bp.route("/perps")
def perps_page():
    return render_template("perps.html")


@perps_bp.route("/api/perps/scan")
def perps_scan():
    try:
        a = request.args
        return jsonify(scan(memes_only=a.get("memes") in ("1", "true"), sort=a.get("sort", "move"),
                            **{k: a.get(k) for k in DEFAULTS}))
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"MEXC request failed: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@perps_bp.route("/api/perps/onchain")
def perps_onchain():
    try:
        import perps_onchain as po
        a = request.args
        return jsonify(po.onchain(a.get("symbol", ""), a.get("chain") or None, (a.get("address") or "").strip() or None))
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Lookup failed: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------- listings radar ----
@perps_bp.route("/listings")
def listings_page():
    return render_template("listings.html")


@perps_bp.route("/api/listings")
def listings_feed():
    import listings
    try:
        return jsonify(listings.feed(hours=float(request.args.get("hours") or 72)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@perps_bp.route("/api/listings/scan", methods=["POST"])
def listings_scan():
    import listings
    try:
        new = listings.scan()
        listings.enrich_missing(6)
        return jsonify({"new": len(new)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@perps_bp.route("/api/listings/watch", methods=["POST"])
def listings_watch_add():
    import listings
    d = request.get_json(silent=True) or {}
    if not (d.get("name") or d.get("ticker")):
        return jsonify({"error": "name or ticker needed"}), 400
    listings.watch_add(d)
    return jsonify({"ok": True})


@perps_bp.route("/api/listings/watch/<int:wid>", methods=["DELETE"])
def listings_watch_remove(wid):
    import listings
    return jsonify({"removed": listings.watch_remove(wid)})


@perps_bp.route("/api/listings/settings", methods=["POST"])
def listings_settings():
    import edge
    d = request.get_json(silent=True) or {}
    edge.save_settings({k: v for k, v in d.items() if k in ("lst_alerts", "lst_quiet_markets")})
    return jsonify({"ok": True})


# ------------------------------------------------------------- token unlocks ----
@perps_bp.route("/api/unlocks")
def unlocks_feed():
    import unlocks
    try:
        return jsonify(unlocks.feed(days=float(request.args.get("days") or 30)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@perps_bp.route("/api/unlocks/refresh", methods=["POST"])
def unlocks_refresh():
    import unlocks
    try:
        unlocks.refresh(force_sync=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@perps_bp.route("/api/unlocks", methods=["POST"])
def unlocks_add():
    import unlocks
    d = request.get_json(silent=True) or {}
    if not (d.get("name") or d.get("ticker")):
        return jsonify({"error": "name or ticker needed"}), 400
    try:
        unlocks.add(d)
    except ValueError:
        return jsonify({"error": "amount / % must be numbers (e.g. 171.88M)"}), 400
    return jsonify({"ok": True})


@perps_bp.route("/api/unlocks/<int:uid>", methods=["DELETE"])
def unlocks_remove(uid):
    import unlocks
    return jsonify({"removed": unlocks.remove(uid)})


@perps_bp.route("/api/unlocks/settings", methods=["POST"])
def unlocks_settings():
    import edge
    d = request.get_json(silent=True) or {}
    edge.save_settings({k: v for k, v in d.items() if k == "unl_alerts"})
    return jsonify({"ok": True})
