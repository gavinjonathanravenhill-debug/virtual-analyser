



import os, requests
from flask import Flask, jsonify, render_template_string, request, Response
import functools
from flask_cors import CORS
try:
    from bot import start_bot_thread
except Exception as _e:
    print(f"bot import failed, continuing without it: {_e}")
    def start_bot_thread():
        pass
from mm_check import analyse_market_maker
from gammaflip_routes import gammaflip_bp
try:
    start_bot_thread()
except Exception as _e:
    # The bot must never be able to take the web server down with it.
    print(f"bot thread failed to start, continuing: {_e}")

app = Flask(__name__)
app.register_blueprint(gammaflip_bp)


@app.route("/health")
def health():
    return {"ok": True}, 200
CORS(app)

MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "virtual123")

def check_auth(password):
    return password == SITE_PASSWORD

def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.password):
            return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=\"Virtual Analyser\""})
        return f(*args, **kwargs)
    return decorated


# ---- X news feed: slide-out panel injected into every page ----
from xfeed import init_xfeed
init_xfeed(app, auth=requires_auth)


# ---- Wintermute tracker (/wintermute) - whole blueprint behind the site password ----
from wintermute_routes import wintermute_bp
from wintermute_realtime import start_realtime


@wintermute_bp.before_request
def _wintermute_auth():
    auth = request.authorization
    if not auth or not check_auth(auth.password):
        return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=\"Virtual Analyser\""})


app.register_blueprint(wintermute_bp)
try:
    from wintermute_client import start_warmer
    start_warmer()  # pre-load Wintermute data in the background
except Exception as _e:
    print(f"wintermute warmer failed to start, continuing: {_e}")

# ---- Solana wallet tracker (/solana) - also behind the site password ----
from solana_routes import solana_bp


@solana_bp.before_request
def _solana_auth():
    if request.path == "/api/solana/webhook":   # Helius push - checked against its own secret in the route
        return None
    auth = request.authorization
    if not auth or not check_auth(auth.password):
        return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=\"Virtual Analyser\""})


app.register_blueprint(solana_bp)
try:
    from solana_client import start_solana
    start_solana()  # starts the shared signals engine + registers Solana before the first poll
except Exception as _e:
    print(f"solana tracker failed to start, continuing: {_e}")

# ---- EVM wallet trackers (/ethereum, /base, /bsc, /robinhood) - same page as /solana, behind the password ----
try:
    from evm_routes import EVM_BLUEPRINTS
    from evm_chains import CHAINS as EVM_CHAINS

    def _evm_auth():
        auth = request.authorization
        if not auth or not check_auth(auth.password):
            return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=\"Virtual Analyser\""})

    for _name, _bp in EVM_BLUEPRINTS.items():
        _bp.before_request(_evm_auth)
        app.register_blueprint(_bp)
    for _name, _m in EVM_CHAINS.items():
        try:
            _m.start()
        except Exception as _e:
            print(f"{_name} tracker failed to start, continuing: {_e}")
except Exception as _e:
    print(f"EVM trackers failed to load, continuing: {_e}")
try:
    start_realtime()
except Exception as _e:
    print(f"wintermute realtime failed to start, continuing: {_e}")


# ---- MEXC perps scanner (/perps) - behind the site password ----
try:
    from perps_scanner import perps_bp

    @perps_bp.before_request
    def _perps_auth():
        auth = request.authorization
        if not auth or not check_auth(auth.password):
            return Response("Login required", 401, {"WWW-Authenticate": "Basic realm=\"Virtual Analyser\""})

    app.register_blueprint(perps_bp)
except Exception as _e:
    print(f"perps scanner failed to load, continuing: {_e}")
try:
    import listings
    listings.start()     # listings radar: exchange announcements + new markets -> Telegram
except Exception as _e:
    print(f"listings radar failed to start, continuing: {_e}")
try:
    import unlocks
    unlocks.start()      # token unlocks: Tokenomist sync + manual list, CoinGecko/MEXC enrichment, Telegram 24h/1h
except Exception as _e:
    print(f"token unlocks failed to start, continuing: {_e}")


@app.route("/healthz")
def healthz():
    """Unauthenticated - Railway's healthcheck has no credentials."""
    return {"ok": True}, 200
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

@app.route("/")
@requires_auth
def index():
    return render_template_string(open("index.html").read())

@app.route("/compare")
@requires_auth
def compare():
    return render_template_string(open("compare.html").read())


@app.route("/crypto-movers")
@requires_auth
def crypto_movers():
    return open("crypto-movers.html").read()

@app.route("/api/holders")
def holders():
    token = request.args.get("token", "")
    chain = request.args.get("chain", "base")
    chain_map = {"base": "0x2105", "eth": "0x1", "bsc": "0x38"}
    chain_id = chain_map.get(chain, "0x2105")
    if not MORALIS_API_KEY:
        return jsonify({"error": "No MORALIS_API_KEY set"}), 500
    try:
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/erc20/{token}/owners",
            headers={"X-API-Key": MORALIS_API_KEY},
            params={"chain": chain_id, "limit": 50, "order": "DESC"},
            timeout=15)
        r.raise_for_status()
        holders_raw = r.json().get("result", [])
        m = requests.get(
            "https://deep-index.moralis.io/api/v2.2/erc20/metadata",
            headers={"X-API-Key": MORALIS_API_KEY},
            params={"chain": chain_id, "addresses[0]": token},
            timeout=10)
        meta = m.json()
        decimals = int(meta[0].get("decimals", 18)) if meta else 18
        total_raw = int(meta[0].get("total_supply", 0)) if meta else 0
        total = total_raw / (10 ** decimals) if total_raw else 0
        symbol = meta[0].get("symbol", "?") if meta else "?"
        name = meta[0].get("name", "Unknown") if meta else "Unknown"
        nodes = [{"address": h.get("owner_address", ""), "name": h.get("owner_address_label") or "",
                  "percentage": round(int(h.get("balance", 0)) / (10 ** decimals) / total * 100, 4) if total else 0,
                  "is_contract": h.get("is_contract", False)} for h in holders_raw]
        return jsonify({"nodes": nodes, "links": [], "token_name": name, "symbol": symbol, "source": "Moralis"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/wallet")
def wallet():
    address = request.args.get("address", "")
    chain = request.args.get("chain", "base")
    if not address:
        return jsonify({"error": "No address"}), 400
    explorers = {"base": "https://api.basescan.org/api", "eth": "https://api.etherscan.io/api", "bsc": "https://api.bscscan.com/api"}
    try:
        r = requests.get(explorers.get(chain, explorers["base"]),
            params={"module": "account", "action": "txlist", "address": address,
                    "startblock": 0, "endblock": 99999999, "page": 1, "offset": 10, "sort": "desc"},
            timeout=10)
        txs = r.json().get("result", [])
        if isinstance(txs, str): txs = []
        return jsonify({"recent_txs": [{"hash": t.get("hash", "")[:14] + "…",
            "from": t.get("from", ""), "to": t.get("to", ""),
            "value_eth": round(int(t.get("value", 0)) / 1e18, 4)} for t in txs[:5]]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "RV30880XPRJM0RHA")

@app.route("/api/prices")
def prices():
    try:
        btc = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        return jsonify({"btc": float(btc.get("price", 0)), "oil": _get_oil_price(), "oil_name": "WTI Crude (USD/bbl)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

_oil_cache = {"time": 0, "candles": []}
def _get_oil_candles():
    import time as _t
    now = _t.time()
    if _oil_cache["candles"] and (now - _oil_cache["time"]) < 30:
        return _oil_cache["candles"]
    try:
        r = requests.get("https://contract.mexc.com/api/v1/contract/kline/USOIL_USDT", params={"interval": "Min1", "limit": 60}, timeout=10)
        d = r.json().get("data", {})
        times = d.get("time", [])
        closes = d.get("close", [])
        candles = [{"t": int(times[i]) * 1000, "c": float(closes[i])} for i in range(len(times))]
        if candles:
            _oil_cache["candles"] = candles
            _oil_cache["time"] = now
        return candles
    except Exception:
        return _oil_cache["candles"]

def _get_oil_price():
    candles = _get_oil_candles()
    if candles:
        return candles[-1]["c"]
    return 0

_us10y_cache = {"time": 0, "data": None}
def _get_us10y():
    """US 10-year Treasury yield (^TNX) 1-min candles from Yahoo, cached 60s."""
    import time as _t
    now = _t.time()
    if _us10y_cache["data"] and (now - _us10y_cache["time"]) < 60:
        return _us10y_cache["data"]
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
                         params={"interval": "1m", "range": "1d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        res = r.json()["chart"]["result"][0]
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        fix = lambda v: v / 10 if v and v > 20 else v   # old Yahoo quoted ^TNX x10
        candles = [{"t": int(ts[i]) * 1000, "c": round(fix(float(closes[i])), 4)}
                   for i in range(min(len(ts), len(closes))) if closes[i] is not None][-60:]
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        last = candles[-1]["c"] if candles else fix(meta.get("regularMarketPrice"))
        data = {"candles": candles, "symbol": "US10Y",
                "last": last, "prev_close": fix(prev) if prev else None,
                "change_bp": round((last - fix(prev)) * 100, 1) if (last and prev) else None,
                "live": bool(candles) and (now * 1000 - candles[-1]["t"]) < 10 * 60 * 1000}
        if candles:
            _us10y_cache.update(time=now, data=data)
        return data
    except Exception:
        return _us10y_cache["data"] or {"candles": [], "symbol": "US10Y", "error": "yield feed unavailable"}

@app.route("/api/mm-check")
def mm_check():
    address = request.args.get("address", "").strip()
    chain_hint = request.args.get("chain", "")
    if not address:
        return jsonify({"error": "No address"}), 400
    try:
        return jsonify(analyse_market_maker(address, chain_hint))
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "GeckoTerminal request failed: " + str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/candles")
def candles():
    try:
        symbol = request.args.get("symbol", "BTCUSDT")
        if symbol.upper() in ("CL=F", "OIL", "BRENT", "WTI"):
            return jsonify({"candles": _get_oil_candles(), "symbol": "OIL"})
        if symbol.upper() in ("US10Y", "^TNX", "TNX"):
            return jsonify(_get_us10y())
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 60},
            timeout=10)
        data = r.json()
        candles = [{"t": int(k[0]), "c": float(k[4])} for k in data]
        return jsonify({"candles": candles, "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("\n  VIRTUAL · Holder Intelligence")
    print("  Open: http://127.0.0.1:5000\n")
    if not MORALIS_API_KEY:
        print("  ⚠  Set key: export MORALIS_API_KEY=your_key\n")
    start_bot_thread()
    app.run(debug=False, port=5000)
