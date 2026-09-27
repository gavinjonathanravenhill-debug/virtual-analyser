"""
Solana wallet tracker - swaps/transfers for the wallets in solana_wallets.json.

Data: Solana JSON-RPC (free public endpoint by default, set SOLANA_RPC_URL for a
faster one e.g. Helius free tier), DexScreener for token symbol/price/mcap,
CoinGecko for SOL/USD. No API key needed.
"""

import json
import os
import threading
import time
from collections import deque

import requests

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
POLL_SECONDS = int(os.getenv("SOLANA_POLL_SECONDS", "180"))
SIGS_PER_POLL = int(os.getenv("SOLANA_SIGS_PER_POLL", "15"))
RPC_GAP = float(os.getenv("SOLANA_RPC_GAP", "0.35"))  # public RPC is rate limited

WSOL = "So11111111111111111111111111111111111111112"
QUOTES = {
    WSOL: "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
DUST_SOL = 0.002  # ignore SOL changes this small (fees, rent)

_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "solana_wallets.json")) as f:
    _cfg = json.load(f)
VINE_MINT = _cfg.get("vine_mint")
WALLETS = {w["address"]: w for w in _cfg["wallets"]}
# Extra wallets without editing the file: SOLANA_EXTRA_WALLETS="addr:Label,addr2:Label2"
for _item in os.getenv("SOLANA_EXTRA_WALLETS", "").split(","):
    _a, _, _l = _item.strip().partition(":")
    if 32 <= len(_a) <= 44:
        WALLETS[_a] = {"address": _a, "label": _l.strip() or _a[:4] + "…" + _a[-4:],
                       "group": "Extra", "note": "", "alert": False}


# ------------------------------------------------------------------ RPC ----
_s = requests.Session()
_rpc_lock = threading.Lock()


def rpc(method, params):
    with _rpc_lock:  # one request at a time keeps us under public limits
        for attempt in range(5):
            r = _s.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params}, timeout=30)
            time.sleep(RPC_GAP)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"Solana RPC: {d['error'].get('message')}")
            return d["result"]
        raise RuntimeError("Solana RPC rate limit - set SOLANA_RPC_URL to a private endpoint")


def signatures(address, limit=SIGS_PER_POLL, until=None):
    p = {"limit": limit}
    if until:
        p["until"] = until
    return rpc("getSignaturesForAddress", [address, p]) or []


def get_tx(sig):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 0}])


# ---------------------------------------------------------- token info -----
_tok = {}  # mint -> (fetched_at, info)


def token_info(mints):
    """symbol, price, mcap for many mints via DexScreener (30 per call)."""
    need = [m for m in set(mints) if m not in _tok or time.time() - _tok[m][0] > 600]
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        try:
            r = requests.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk),
                             timeout=20)
            pairs = r.json().get("pairs") or []
        except Exception:
            pairs = []
        best = {}
        for p in pairs:
            m = (p.get("baseToken") or {}).get("address")
            if m in chunk and (p.get("liquidity") or {}).get("usd", 0) >= \
                    ((best.get(m) or {}).get("liquidity") or {}).get("usd", -1):
                best[m] = p
        for m in chunk:
            p = best.get(m) or {}
            _tok[m] = (time.time(), {
                "symbol": (p.get("baseToken") or {}).get("symbol") or m[:4] + "…",
                "price": float(p["priceUsd"]) if p.get("priceUsd") else None,
                "market_cap": p.get("marketCap") or p.get("fdv"),
                "url": p.get("url"),
            })
    return {m: _tok[m][1] for m in mints if m in _tok}


_sol = [0, None]


def sol_usd():
    if time.time() - _sol[0] > 120:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "solana", "vs_currencies": "usd"}, timeout=15)
            _sol[1] = r.json()["solana"]["usd"]
            _sol[0] = time.time()
        except Exception:
            pass
    return _sol[1]


# ---------------------------------------------------------------- parse ----
def parse_tx(tx, owner):
    """Net balance changes for `owner` in one tx -> swap / transfer event (or None)."""
    if not tx or (tx.get("meta") or {}).get("err"):
        return None
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]

    deltas = {}  # mint -> change (ui amount)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != owner:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * amt
    sol = 0.0
    if owner in keys:
        i = keys.index(owner)
        sol = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        if i == 0:
            sol += meta.get("fee", 0) / 1e9  # don't count the fee as trading
    # wrapped SOL and native SOL are the same thing for our purposes
    sol += deltas.pop(WSOL, 0)
    if abs(sol) >= DUST_SOL:
        deltas["SOL"] = sol
    deltas = {m: d for m, d in deltas.items() if abs(d) > 1e-9}
    if not deltas:
        return None

    quotes = {m: d for m, d in deltas.items() if m == "SOL" or m in QUOTES}
    tokens = {m: d for m, d in deltas.items() if m not in quotes}
    ev = {"sig": tx["transaction"]["signatures"][0], "ts": tx.get("blockTime") or 0,
          "wallet": owner}
    if tokens:
        mint, amt = max(tokens.items(), key=lambda x: abs(x[1]))
        q = next(((m, d) for m, d in quotes.items() if (d > 0) != (amt > 0)), None)
        ev.update(mint=mint, amount=abs(amt),
                  kind=("BUY" if amt > 0 else "SELL") if q else ("IN" if amt > 0 else "OUT"))
        if q:
            ev["quote"] = "SOL" if q[0] == "SOL" else QUOTES[q[0]]
            ev["quote_amount"] = abs(q[1])
        if len(tokens) > 1:
            ev["other_tokens"] = len(tokens) - 1
    else:  # only SOL/stables moved
        m, d = max(quotes.items(), key=lambda x: abs(x[1]))
        ev.update(mint=m if m != "SOL" else "SOL", amount=abs(d), kind="IN" if d > 0 else "OUT")
    return ev


def enrich(events):
    mints = [e["mint"] for e in events if e["mint"] not in ("SOL",) and e["mint"] not in QUOTES]
    info = token_info(mints) if mints else {}
    sp = sol_usd()
    for e in events:
        w = WALLETS.get(e["wallet"], {})
        e.update(label=w.get("label", e["wallet"][:4] + "…" + e["wallet"][-4:]),
                 group=w.get("group", "Lookup"), alert=bool(w.get("alert")))
        if e["mint"] == "SOL" or e["mint"] in QUOTES:
            e["symbol"] = "SOL" if e["mint"] == "SOL" else QUOTES[e["mint"]]
            e["usd"] = e["amount"] * (sp or 0) if e["symbol"] == "SOL" else e["amount"]
            e["is_vine"] = False
            continue
        t = info.get(e["mint"], {})
        e["symbol"], e["market_cap"], e["dex_url"] = t.get("symbol"), t.get("market_cap"), t.get("url")
        e["is_vine"] = e["mint"] == VINE_MINT
        if e.get("quote"):  # price paid, from the swap itself
            qusd = e["quote_amount"] * (sp or 0) if e["quote"] == "SOL" else e["quote_amount"]
            e["usd"] = qusd or None
            e["price"] = qusd / e["amount"] if qusd and e["amount"] else None
        else:
            e["price"] = t.get("price")
            e["usd"] = e["amount"] * e["price"] if e["price"] else None
    return events


# --------------------------------------------------------------- poller ----
class Tracker:
    def __init__(self):
        self.events = deque(maxlen=3000)
        self.seen = set()
        self.last_sig = {}      # wallet -> newest signature seen
        self.status = {"started": None, "last_poll": None, "last_error": None, "polls": 0}
        self.lock = threading.Lock()

    def poll_wallet(self, addr, limit=SIGS_PER_POLL):
        sigs = signatures(addr, limit, self.last_sig.get(addr))
        if not sigs:
            return []
        self.last_sig[addr] = sigs[0]["signature"]
        out = []
        for s in sigs:
            key = (s["signature"], addr)
            if s.get("err") or key in self.seen:
                continue
            self.seen.add(key)
            ev = parse_tx(get_tx(s["signature"]), addr)
            if ev:
                out.append(ev)
        return out

    def poll_all(self):
        new = []
        for addr in list(WALLETS):
            try:
                new += self.poll_wallet(addr)
            except Exception as e:
                self.status["last_error"] = f"{WALLETS[addr]['label']}: {e}"
        enrich(new)
        with self.lock:
            for e in sorted(new, key=lambda e: e["ts"]):
                self.events.appendleft(e)
        self.status.update(last_poll=int(time.time()), polls=self.status["polls"] + 1)
        return new

    def loop(self):
        self.status["started"] = int(time.time())
        while True:
            try:
                self.poll_all()
            except Exception as e:
                self.status["last_error"] = str(e)
            time.sleep(POLL_SECONDS)

    def query(self, wallet=None, group=None, vine_only=False, kinds=None, limit=500):
        with self.lock:
            ev = list(self.events)
        if wallet:
            ev = [e for e in ev if e["wallet"] == wallet]
        if group:
            ev = [e for e in ev if e["group"] == group]
        if vine_only:
            ev = [e for e in ev if e.get("is_vine")]
        if kinds:
            ev = [e for e in ev if e["kind"] in kinds]
        return sorted(ev, key=lambda e: -e["ts"])[:limit]


tracker = Tracker()
_started = False


def start_solana():
    global _started
    if _started or os.getenv("SOLANA_TRACKER_OFF"):
        return
    _started = True
    threading.Thread(target=tracker.loop, daemon=True, name="solana-tracker").start()


def lookup(address, limit=25):
    """One-off scan of any address (not stored)."""
    evs = []
    for s in signatures(address, limit):
        if s.get("err"):
            continue
        ev = parse_tx(get_tx(s["signature"]), address)
        if ev:
            evs.append(ev)
    return enrich(evs)
