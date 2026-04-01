import os, requests
from flask import Flask, jsonify, render_template_string, request, Response
import functools
from flask_cors import CORS

app = Flask(__name__)
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
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

@app.route("/")
@requires_auth
def index():
    return render_template_string(open("index.html").read())

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

@app.route("/api/prices")
def prices():
    try:
        # BTC price
        btc = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        # VIRTUAL price
        virtual = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=VIRTUALUSDT", timeout=5).json()
        return jsonify({
            "btc": float(btc.get("price", 0)),
            "virtual": float(virtual.get("price", 0))
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/candles")
def candles():
    try:
        symbol = request.args.get("symbol", "BTCUSDT")
        r = requests.get(
            "https://api.mexc.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 60},
            timeout=10
        )
        data = r.json()
        # MEXC kline format: [openTime, open, high, low, close, volume, closeTime, ...]
        candles = [{"t": int(k[0]), "c": float(k[4])} for k in data]
        return jsonify({"candles": candles, "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("\n  VIRTUAL · Holder Intelligence")
    print("  Open: http://127.0.0.1:5000\n")
    if not MORALIS_API_KEY:
        print("  ⚠  Set key: export MORALIS_API_KEY=your_key\n")
    app.run(debug=False, port=5000)
