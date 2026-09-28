"""
Generic EVM wallet tracker - one engine for Ethereum, Base, BSC and Robinhood Chain.

Not imported directly: evm_chains.py loads this file once per chain with a CFG dict
already in its globals, so every chain gets its own module (own wallets, tracker, caches).

Data: public JSON-RPC (ERC-20 Transfer logs for your wallets, polled every few seconds,
several RPCs with automatic failover); DexScreener for price / mcap / liquidity;
GeckoTerminal for holder concentration (via solana_signals.risk_checks).
"""

import json
import os
import statistics
import threading
import time
from collections import Counter, defaultdict, deque

import requests

CFG = globals()["CFG"]                     # injected by evm_chains.load_chain
_P = CFG["chain"].upper()                  # env prefix, e.g. BASE_RPC_URL

_env_rpcs = [u.strip() for u in os.getenv(f"{_P}_RPC_URL", "").split(",") if u.strip()]
RPC_URLS = _env_rpcs + [u for u in CFG["rpcs"] if u not in _env_rpcs]
RPC_URL = RPC_URLS[0]
POLL_SECONDS = int(os.getenv(f"{_P}_POLL_SECONDS", str(CFG.get("poll", 15))))
RPC_GAP = float(os.getenv(f"{_P}_RPC_GAP", str(CFG.get("gap", 0.06))))
BLOCK_SECONDS = CFG["block_seconds"]
MAX_RANGE = int(os.getenv(f"{_P}_MAX_RANGE", str(CFG.get("max_range", 10_000))))  # shrinks itself if the RPC complains
START_HOURS = float(os.getenv(f"{_P}_START_HOURS", str(CFG.get("start_hours", 6))))
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STABLE_SYMBOLS = {"USDC", "USDT", "USDG", "USDC.E", "USDBC", "DAI", "PYUSD", "FDUSD", "BUSD", "USDE", "USDS", "USD1"}
WRAPPED = set(CFG["wrapped"])              # wrapped / native symbols priced at the native coin
QUOTE_SYMBOLS = STABLE_SYMBOLS | WRAPPED

CHAIN = CFG["chain"]
NATIVE = CFG["native"]
NAME = CFG["name"]
GT_NETWORK = CFG["gt"]
BUBBLEMAPS_CHAIN = CFG["bubblemaps"]
DEXSCREENER = CFG["dexscreener"]
EXPLORER = CFG["explorer"]
EXPLORER_TX = EXPLORER + "/tx/"
PAGE = {"chain": CHAIN, "title": f"{NAME} Wallets", "native": NATIVE, "dexscreener": DEXSCREENER,
        "bubblemaps": BUBBLEMAPS_CHAIN, "explorer": EXPLORER, "explorer_name": CFG["explorer_name"],
        "tx": EXPLORER + "/tx/", "addr": EXPLORER + "/address/", "token": EXPLORER + "/token/",
        "holders_suffix": CFG.get("holders_suffix", "#balances"), "portfolio_suffix": CFG.get("portfolio_suffix", ""),
        "addr_hint": "0x address", "rpc_note": f"{POLL_SECONDS}-{POLL_SECONDS * 2}s"}

_here = os.path.dirname(os.path.abspath(__file__))
_wallet_file = os.path.join(_here, f"{CHAIN}_wallets.json")
try:
    with open(_wallet_file) as f:
        _cfg = json.load(f)
except FileNotFoundError:
    _cfg = {}
_wl = _cfg.get("wallets") or []
if not _wl:
    _wl = CFG.get("default_wallets", [])
WALLETS = {w["address"].lower(): {**w, "address": w["address"].lower()} for w in _wl}
EXCHANGES = {a.lower(): n for a, n in ({**CFG.get("default_exchanges", {}), **(_cfg.get("exchanges") or {})}).items()
             if not a.startswith("_")}
FILE_LEVELS = [{**lv, "mint": lv["mint"].lower()} for lv in _cfg.get("levels", [])]
# Extra wallets without editing files: BASE_EXTRA_WALLETS="0xabc:Label,0xdef:Label 2"
for _item in os.getenv(f"{_P}_EXTRA_WALLETS", "").split(","):
    _a, _, _l = _item.strip().partition(":")
    if _a.lower().startswith("0x") and len(_a) == 42:
        WALLETS[_a.lower()] = {"address": _a.lower(), "label": _l.strip() or _a[:6] + "…" + _a[-4:],
                               "group": "Extra", "note": "", "alert": False}
QUOTES = {}   # token address -> symbol, filled in as wrapped native / stables are discovered


def norm(a):
    return (a or "").strip().lower()


def is_addr(a):
    return isinstance(a, str) and len(a) == 42 and a.startswith("0x")


# ------------------------------------------------------------------ RPC ----
_s = requests.Session()
_rpc_lock = threading.Lock()


_rpc_i = [0]
_RANGE_ERRORS = ("range", "limit", "too many", "exceed", "10000", "too large", "response size", "timeout")


def rpc(method, params):
    """JSON-RPC with retries; rotates to the next RPC in RPC_URLS when one keeps failing."""
    with _rpc_lock:
        last = None
        for attempt in range(6):
            url = RPC_URLS[_rpc_i[0] % len(RPC_URLS)]
            try:
                r = _s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            except requests.RequestException as e:
                last = f"{url.split('/')[2]}: {e.__class__.__name__}"
                _rpc_i[0] += 1
                time.sleep(0.5 + attempt)
                continue
            time.sleep(RPC_GAP)
            if r.status_code == 429 or r.status_code >= 500 or r.status_code in (401, 403):
                last = f"{url.split('/')[2]}: HTTP {r.status_code}"
                _rpc_i[0] += 1
                time.sleep(1 + attempt)
                continue
            try:
                d = r.json()
            except ValueError:
                last = f"{url.split('/')[2]}: bad response"
                _rpc_i[0] += 1
                continue
            if "error" in d:
                msg = str((d["error"] or {}).get("message"))
                if method == "eth_getLogs" and any(k in msg.lower() for k in _RANGE_ERRORS):
                    raise RangeTooBig(msg)
                raise RuntimeError(f"{NAME} RPC: {msg}")
            tracker_status_rpc(url)
            return d.get("result")
        raise RuntimeError(f"{NAME} RPC unavailable ({last})")


class RangeTooBig(RuntimeError):
    pass


def tracker_status_rpc(url):
    try:
        tracker.status["rpc"] = url.split("/")[2]
    except Exception:
        pass


def latest_block():
    return int(rpc("eth_blockNumber", []), 16)


_meta, _blk_ts, _code, _txs = {}, {}, {}, {}


def token_meta(addr):
    """symbol + decimals via eth_call (cached forever)."""
    if addr in _meta:
        return _meta[addr]
    sym, dec = None, 18
    try:
        raw = rpc("eth_call", [{"to": addr, "data": "0x313ce567"}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:
        pass
    try:
        raw = rpc("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])
        b = bytes.fromhex(raw[2:])
        if len(b) >= 96:
            n = int.from_bytes(b[32:64], "big")
            sym = b[64:64 + n].decode("utf-8", "ignore")
        elif b:
            sym = b.rstrip(b"\x00").decode("utf-8", "ignore")
    except Exception:
        pass
    sym = (sym or addr[:6]).strip().upper()
    _meta[addr] = {"symbol": sym, "decimals": dec}
    if sym in QUOTE_SYMBOLS:
        QUOTES[addr] = sym
    return _meta[addr]


def block_ts(n):
    if n not in _blk_ts:
        b = rpc("eth_getBlockByNumber", [hex(n), False])
        _blk_ts[n] = int(b["timestamp"], 16)
        if len(_blk_ts) > 20000:
            _blk_ts.clear()
    return _blk_ts[n]


def is_contract(a):
    if a not in _code:
        try:
            _code[a] = rpc("eth_getCode", [a, "latest"]) not in ("0x", "0x0", None)
        except Exception:
            _code[a] = False
    return _code[a]


def get_tx(h):
    if h not in _txs:
        _txs[h] = rpc("eth_getTransactionByHash", [h]) or {}
        if len(_txs) > 20000:
            _txs.clear()
    return _txs[h]


def _pad(a):
    return "0x" + "0" * 24 + a[2:].lower()


def _unpad(t):
    return "0x" + t[-40:].lower()


_range = [MAX_RANGE]


def _get_logs(topics, start, end):
    """eth_getLogs over start..end, splitting the range when the RPC says it's too big."""
    out = []
    while start <= end:
        stop = min(end, start + _range[0] - 1)
        try:
            out += rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(stop), "topics": topics}]) or []
            start = stop + 1
        except RangeTooBig:
            if _range[0] <= 50:
                raise
            _range[0] = max(50, _range[0] // 2)
    return out


def logs_for(addresses, frm, to):
    """All ERC-20 Transfer logs to/from any of `addresses` between blocks frm..to."""
    out = []
    addrs = list(addresses)
    for i in range(0, len(addrs), 40):
        pads = [_pad(a) for a in addrs[i:i + 40]]
        out += _get_logs([TRANSFER, pads], frm, to)
        out += _get_logs([TRANSFER, None, pads], frm, to)
    seen, uniq = set(), []
    for lg in out:
        k = (lg["transactionHash"], lg["logIndex"])
        if k not in seen and len(lg.get("topics", [])) == 3:   # 4 topics = NFT
            seen.add(k)
            uniq.append(lg)
    return uniq


# ---------------------------------------------------------------- parse ----
def parse_logs(logs, wallets):
    """Group transfer logs into one event per (tx, wallet), same shape as the Solana tracker."""
    wallets = {w.lower() for w in wallets}
    per = defaultdict(list)
    for lg in logs:
        frm, to = _unpad(lg["topics"][1]), _unpad(lg["topics"][2])
        for w in (frm, to):
            if w in wallets:
                per[(lg["transactionHash"], w)].append(lg)
    events = []
    for (h, w), lst in per.items():
        deltas, cps = defaultdict(float), defaultdict(list)
        for lg in lst:
            tok = lg["address"].lower()
            meta = token_meta(tok)
            amt = int(lg["data"], 16) / (10 ** meta["decimals"]) if lg["data"] not in ("0x", "") else 0
            frm, to = _unpad(lg["topics"][1]), _unpad(lg["topics"][2])
            if frm == w and to == w:
                continue
            sign = 1 if to == w else -1
            deltas[tok] += sign * amt
            cps[tok].append(frm if sign > 0 else to)
        deltas = {t: d for t, d in deltas.items() if abs(d) > 1e-12}
        if not deltas:
            continue
        blk = int(lst[0]["blockNumber"], 16)
        tx = get_tx(h)
        sender = (tx.get("from") or "").lower()
        eth_paid = int(tx.get("value") or "0x0", 16) / 1e18 if sender == w else 0
        quotes = {t: d for t, d in deltas.items() if t in QUOTES}
        tokens = {t: d for t, d in deltas.items() if t not in QUOTES}
        ev = {"sig": h, "ts": block_ts(blk), "block": blk, "wallet": w}
        if tokens:
            tok, amt = max(tokens.items(), key=lambda x: abs(x[1]))
            q = next(((t, d) for t, d in quotes.items() if (d > 0) != (amt > 0)), None)
            ev.update(mint=tok, amount=abs(amt))
            cp = cps[tok][0] if cps[tok] else None
            if q:
                ev.update(kind="BUY" if amt > 0 else "SELL", quote=QUOTES[q[0]], quote_amount=abs(q[1]))
            elif amt > 0 and eth_paid > 0:
                ev.update(kind="BUY", quote=NATIVE, quote_amount=eth_paid)
            elif amt < 0 and sender == w and cp and cp not in EXCHANGES and cp not in WALLETS and is_contract(cp):
                ev.update(kind="SELL", quote=NATIVE, quote_amount=None)   # sold into a pool for native ETH
            elif amt > 0 and sender == w and cp and cp not in EXCHANGES and is_contract(cp):
                ev.update(kind="BUY", quote=None, quote_amount=None)     # bought from a pool (paid via router)
            else:
                ev.update(kind="IN" if amt > 0 else "OUT", counterparty=cp)
            if len(tokens) > 1:
                ev["other_tokens"] = len(tokens) - 1
        else:
            tok, amt = max(quotes.items(), key=lambda x: abs(x[1]))
            ev.update(mint=tok, amount=abs(amt), kind="IN" if amt > 0 else "OUT",
                      counterparty=cps[tok][0] if cps[tok] else None)
        events.append(ev)
    return events


# ---------------------------------------------------------- token info -----
_tok = {}


def _sane_mcap(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 2e12 else None


def token_info(mints):
    mints = [m.lower() for m in mints if m and m != NATIVE]
    need = [m for m in set(mints) if m not in _tok or time.time() - _tok[m][0] > 600]
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        try:
            pairs = requests.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk),
                                 timeout=20).json().get("pairs") or []
        except Exception:
            pairs = []
        best = {}
        for p in pairs:
            a = ((p.get("baseToken") or {}).get("address") or "").lower()
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a in chunk and p.get("chainId") == DEXSCREENER and liq >= 1000 and \
                    liq > ((best.get(a) or {}).get("liquidity") or {}).get("usd", 0):
                best[a] = p
        for a in chunk:
            p = best.get(a) or {}
            _tok[a] = (time.time(), {
                "symbol": (p.get("baseToken") or {}).get("symbol") or (_meta.get(a) or {}).get("symbol") or a[:6],
                "price": float(p["priceUsd"]) if p.get("priceUsd") else None,
                "market_cap": _sane_mcap(p.get("marketCap") or p.get("fdv")), "url": p.get("url"),
                "pair": p.get("pairAddress"), "liquidity": (p.get("liquidity") or {}).get("usd"),
                "volume_24h": (p.get("volume") or {}).get("h24"),
                "change_24h": (p.get("priceChange") or {}).get("h24"), "change_1h": (p.get("priceChange") or {}).get("h1"),
                "dex": p.get("dexId"), "name": (p.get("baseToken") or {}).get("name"), "created": p.get("pairCreatedAt"),
            })
    return {m: _tok[m][1] for m in mints if m in _tok}


_eth = [0, None]


def eth_usd():
    """USD price of the chain's native coin (ETH, or BNB on BSC)."""
    if time.time() - _eth[0] > 120:
        try:
            _eth[1] = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                   params={"ids": CFG["coingecko_native"], "vs_currencies": "usd"},
                                   timeout=15).json()[CFG["coingecko_native"]]["usd"]
            _eth[0] = time.time()
        except Exception:
            pass
    return _eth[1]


native_usd = eth_usd


def enrich(events):
    info = token_info([e["mint"] for e in events if e.get("mint") not in QUOTES])
    ep = eth_usd() or 0
    for e in events:
        w = WALLETS.get(e["wallet"], {})
        e.update(label=w.get("label", e["wallet"][:6] + "…" + e["wallet"][-4:]),
                 group=w.get("group", "Lookup"), alert=bool(w.get("alert")), min_usd=w.get("min_usd"))
        cp = e.get("counterparty")
        if cp:
            e["counterparty_label"] = EXCHANGES.get(cp) or (WALLETS.get(cp) or {}).get("label")
            e["to_exchange"] = e["kind"] == "OUT" and cp in EXCHANGES
            e["from_exchange"] = e["kind"] == "IN" and cp in EXCHANGES
        if e["mint"] in QUOTES:
            sym = QUOTES[e["mint"]]
            e["symbol"] = sym
            e["usd"] = e["amount"] * (1 if sym in STABLE_SYMBOLS else ep)
            continue
        t = info.get(e["mint"], {})
        e.update(symbol=t.get("symbol") or (_meta.get(e["mint"]) or {}).get("symbol"),
                 market_cap=t.get("market_cap"), dex_url=t.get("url"), pair=t.get("pair"))
        qusd = None
        if e.get("quote") and e.get("quote_amount"):
            qusd = e["quote_amount"] * (1 if e["quote"] in STABLE_SYMBOLS else ep)
        if qusd:
            e["usd"], e["price"] = qusd, qusd / e["amount"] if e["amount"] else None
        else:
            e["price"] = t.get("price")
            e["usd"] = e["amount"] * e["price"] if e["price"] else None
    return events


# --------------------------------------------------------------- poller ----
class Tracker:
    def __init__(self):
        self.events = deque(maxlen=5000)
        self.seen = set()
        self.last_block = None
        self.last_tx = {}
        self.checked = set()
        self.listeners = []
        self.status = {"started": None, "last_poll": None, "last_error": None, "polls": 0,
                       "rpc": RPC_URL.split("/")[2], "block": None, "range": MAX_RANGE}
        self.lock = threading.Lock()

    def poll(self):
        wallets = list(WALLETS)
        latest = latest_block()
        if not wallets:
            self.last_block = latest
            self.status.update(last_poll=int(time.time()), polls=self.status["polls"] + 1, block=latest)
            return []
        if self.last_block and latest - self.last_block > int(START_HOURS * 3600 / BLOCK_SECONDS):
            self.last_block = latest - int(START_HOURS * 3600 / BLOCK_SECONDS)   # was down a long time
        frm = (self.last_block + 1) if self.last_block else latest - int(START_HOURS * 3600 / BLOCK_SECONDS)
        # wallets added since the last poll get their own look-back
        fresh = [w for w in wallets if w not in self.checked]
        logs = logs_for([w for w in wallets if w in self.checked], frm, latest) if self.last_block else []
        if fresh or not self.last_block:
            back = latest - int(START_HOURS * 3600 / BLOCK_SECONDS)
            logs += logs_for(fresh if self.last_block else wallets, back, latest)
        evs = [e for e in parse_logs(logs, wallets) if (e["sig"], e["wallet"]) not in self.seen]
        for e in evs:
            self.seen.add((e["sig"], e["wallet"]))
            self.last_tx[e["wallet"]] = max(self.last_tx.get(e["wallet"], 0), e["ts"])
        enrich(evs)
        try:
            flag_new_allocations(evs)
        except Exception as ex:
            self.status["last_error"] = f"allocation watch: {ex}"
        try:   # first poll just records each funder's nonce; later polls scan the new blocks
            evs += watch_funders((self.last_block + 1) if self.last_block else latest, latest)
        except Exception as ex:
            self.status["last_error"] = f"funding watch: {ex}"
        with self.lock:
            for e in sorted(evs, key=lambda e: e["ts"]):
                self.events.appendleft(e)
        self.checked.update(wallets)
        self.last_block = latest
        self.status.update(last_poll=int(time.time()), polls=self.status["polls"] + 1, block=latest, range=_range[0])
        for fn in self.listeners:
            try:
                fn(evs)
            except Exception as ex:
                self.status["last_error"] = f"listener: {ex}"
        return evs

    def loop(self):
        self.status["started"] = int(time.time())
        while True:
            try:
                self.poll()
                self.status["last_error"] = None
            except Exception as e:
                self.status["last_error"] = str(e)[:200]
            time.sleep(POLL_SECONDS)

    def query(self, wallet=None, group=None, kinds=None, limit=500):
        with self.lock:
            ev = list(self.events)
        if wallet:
            ev = [e for e in ev if e["wallet"] == wallet.lower()]
        if group:
            ev = [e for e in ev if e["group"] == group]
        if kinds:
            ev = [e for e in ev if e["kind"] in kinds]
        return sorted(ev, key=lambda e: -e["ts"])[:limit]


tracker = Tracker()
_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    import sys
    import solana_signals as sig
    sig.start_signals()
    sig.register(sys.modules[__name__])
    if os.getenv(f"{_P}_TRACKER_OFF"):
        tracker.status["last_error"] = f"Tracker switched off ({_P}_TRACKER_OFF is set)"
        return
    threading.Thread(target=tracker.loop, daemon=True, name=f"{CHAIN}-tracker").start()


# ------------------------------------------------------- lookup / token ----
def recent_events(address, hours=48):
    a = address.lower()
    latest = latest_block()
    logs = logs_for([a], latest - int(hours * 3600 / BLOCK_SECONDS), latest)
    return enrich(sorted(parse_logs(logs, [a]), key=lambda e: -e["ts"])), latest


def lookup(address, limit=25):
    return recent_events(address)[0][:limit]


def token_report(mint):
    mint = mint.lower()
    _tok.pop(mint, None)
    info = token_info([mint]).get(mint, {})
    ev = [e for e in tracker.query(limit=5000) if e.get("mint") == mint]
    per = {}
    for e in ev:
        w = per.setdefault(e["wallet"], {"wallet": e["wallet"], "label": e["label"], "group": e["group"],
                                         "alert": e["alert"], "bought": 0.0, "sold": 0.0,
                                         "buy_usd": 0.0, "sell_usd": 0.0, "trades": 0, "last": 0})
        w["trades"] += 1
        w["last"] = max(w["last"], e["ts"])
        k = "bought" if e["kind"] in ("BUY", "IN") else "sold"
        w[k] += e["amount"]
        w["buy_usd" if k == "bought" else "sell_usd"] += e.get("usd") or 0
    return {"mint": mint, "info": info, "events": ev[:100],
            "wallets": sorted(per.values(), key=lambda w: -(w["buy_usd"] + w["sell_usd"]))}


# ------------------------------------------------------------- profiler ----
_prof = {}


def balance_of(token, owner):
    try:
        raw = rpc("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + owner[2:]}, "latest"])
        return int(raw, 16) / (10 ** token_meta(token)["decimals"]) if raw and raw != "0x" else 0
    except Exception:
        return 0


def profile(address, depth=40):
    a = address.lower()
    hit = _prof.get(a)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    label = (WALLETS.get(a) or {}).get("label") or EXCHANGES.get(a)
    out = {"address": a, "label": label, "generated": int(time.time())}
    if is_contract(a):
        try:
            meta = token_meta(a)
            tok = meta["symbol"] != a[:6].upper()
        except Exception:
            tok = False
        out.update(kind="token" if tok else "program",
                   summary="This is a token contract, not a wallet - use Screen token." if tok else
                   "This is a smart contract (pool, router or smart wallet), not a normal wallet.")
        return out
    out["kind"] = "wallet"
    hours = 96 if depth > 40 else 48
    evs, latest = recent_events(a, hours)
    swaps = [e for e in evs if e["kind"] in ("BUY", "SELL")]
    transfers = [e for e in evs if e["kind"] in ("IN", "OUT")]
    buys = sum(e["kind"] == "BUY" for e in swaps)
    sells = len(swaps) - buys
    by_tok = defaultdict(list)
    for e in swaps:
        by_tok[e["mint"]].append(e)
    rt = same_blk = 0
    sizes = []
    for lst in by_tok.values():
        lst.sort(key=lambda e: e["ts"])
        for x, y in zip(lst, lst[1:]):
            if x["kind"] != y["kind"] and y["ts"] - x["ts"] <= 120:
                rt += 1
                same_blk += abs(y.get("block", 0) - x.get("block", 0)) <= 5
                sizes.append(min(x["amount"], y["amount"]) / max(x["amount"], y["amount"]) if max(x["amount"], y["amount"]) else 0)
    n = max(len(evs), 1)
    span_h = max((evs[0]["ts"] - evs[-1]["ts"]) / 3600, 1 / 60) if len(evs) > 1 else hours
    rate = len(evs) / span_h if evs else 0
    rate *= min(1, len(evs) / 20)  # a handful of txs close together isn't "high frequency"
    balance = min(buys, sells) / max(buys, sells) if max(buys, sells) else 0
    toks = len(by_tok)
    top_share = max((len(v) for v in by_tok.values()), default=0) / max(len(swaps), 1)
    outs = [e for e in transfers if e["kind"] == "OUT"]
    out_cps = {e.get("counterparty") for e in outs if e.get("counterparty")}
    to_cex = sum(1 for e in outs if e.get("to_exchange"))
    ins = [e for e in transfers if e["kind"] == "IN" and e["mint"] not in QUOTES]
    sold_after_in = sum(1 for m in {e["mint"] for e in ins} if any(s["mint"] == m and s["kind"] == "SELL" for s in swaps))
    usd = [e["usd"] for e in swaps if e.get("usd")]
    early = 0
    if swaps:
        info = token_info([e["mint"] for e in swaps])
        for e in swaps:
            c = (info.get(e["mint"]) or {}).get("created")
            if e["kind"] == "BUY" and c and 0 <= e["ts"] - c / 1000 <= 600:
                early += 1
    A = []

    def arche(name, score, ev_, manip):
        A.append({"name": name, "score": max(0, min(100, round(score))), "evidence": [x for x in ev_ if x], "manipulation": manip})

    eq = statistics.mean(sizes) if sizes else 0
    arche("MEV / sandwich bot", 40 * min(1, same_blk / 3) + 30 * min(1, rate / 60) + 30 * (rt / max(len(swaps), 1)),
          [f"{same_blk} buy/sell pairs within a few blocks" if same_blk else "", f"~{rate:.0f} transfers/hour" if rate > 20 else ""],
          "Sandwich attacks: buys just before other traders' swaps and sells straight after.")
    arche("Wash trader", 45 * (rt / max(len(swaps), 1)) + 35 * eq * (rt > 2) + 20 * top_share * (rt > 2),
          [f"{rt} quick buy→sell round trips (≤2 min)" if rt else "", f"Legs {eq:.0%} similar in size" if sizes else "",
           f"{top_share:.0%} of swaps in one token" if top_share > .5 and swaps else ""],
          "Wash trading: trading with itself to inflate volume and trending rank.")
    arche("Market maker / liquidity bot", 40 * balance * (len(swaps) > 8) + 30 * min(1, rate / 10) + 30 * (0 < toks <= 5 and len(swaps) > 8),
          [f"Buys vs sells balanced ({buys}/{sells})" if balance > .6 and len(swaps) > 8 else "",
           f"Focused on {toks} token(s)" if 0 < toks <= 5 else ""],
          "Paid token MMs can walk price into a band and then distribute into the buyers it attracts.")
    arche("Insider / dev distributor", 45 * min(1, sold_after_in / 2) + 30 * (sells > 2 * max(buys, 1)) + 25 * min(1, to_cex / 3),
          [f"Received {len(ins)} token transfers (not bought), {sold_after_in} later sold" if ins else "",
           f"Sells outnumber buys {sells}:{buys}" if sells > 2 * max(buys, 1) else "", f"{to_cex} transfers to exchanges" if to_cex else ""],
          "Pump & dump / insider selling: supply received cheaply or free, sold into retail demand.")
    arche("Distribution / exit wallet", 40 * min(1, len(out_cps) / 15) + 30 * (len(transfers) > len(swaps)) + 30 * min(1, to_cex / 2),
          [f"Sends to {len(out_cps)} different wallets" if len(out_cps) > 5 else "",
           f"More transfers ({len(transfers)}) than swaps ({len(swaps)})" if len(transfers) > len(swaps) else ""],
          "Splitting supply across many wallets or moving it to exchanges ahead of selling.")
    arche("Sniper", 100 * early / max(buys, 1) if buys >= 2 else 0,
          [f"{early} of {buys} buys within 10 min of the pool launching" if early else ""],
          "Buys in the first minutes of a launch, then dumps on the people who follow.")
    arche("Retail / manual trader", 50 * (rate < 3) + 50 * (toks >= 3) * (rt == 0),
          [f"Low activity (~{rate:.1f}/hour)" if rate < 3 else "", f"{toks} different tokens traded" if toks >= 3 else ""],
          "No manipulation pattern - looks like a normal trader.")
    if len(evs) < 3:
        arche("Dormant / holder", 90, [f"Only {len(evs)} token transfers in the last {hours}h"], "Nothing to see recently.")
    A.sort(key=lambda x: -x["score"])
    if label and a in EXCHANGES:
        A.insert(0, {"name": "Exchange", "score": 100, "evidence": [label], "manipulation": "Custodial exchange wallet."})

    # holdings: native ETH + tokens it has touched recently
    eth = int(rpc("eth_getBalance", [a, "latest"]), 16) / 1e18
    ep = eth_usd() or 0
    touched = list({e["mint"] for e in evs})[:60]
    info = token_info(touched)
    rows = []
    for t in touched:
        bal = balance_of(t, a)
        if bal <= 0:
            continue
        ti = info.get(t) or {}
        sym = QUOTES.get(t) or ti.get("symbol") or token_meta(t)["symbol"]
        price = 1.0 if sym in STABLE_SYMBOLS else (ep if sym in WRAPPED else ti.get("price"))
        rows.append({"mint": t, "symbol": sym, "amount": bal, "price": price, "usd": bal * price if price else None,
                     "market_cap": ti.get("market_cap"), "liquidity": ti.get("liquidity"),
                     "flag": "no market" if not price else "micro-cap" if (ti.get("market_cap") or 1e18) < 1e6 else
                     "thin liquidity" if ti.get("liquidity") and ti.get("market_cap") and ti["liquidity"] / ti["market_cap"] < .02 else ""})
    rows.sort(key=lambda r: -(r["usd"] or 0))
    total = eth * ep + sum(r["usd"] or 0 for r in rows)
    for r in rows:
        r["pct"] = (r["usd"] or 0) / total * 100 if total else 0
    cps = Counter(e.get("counterparty") for e in evs if e.get("counterparty"))
    counter = [{"address": c, "count": k, "label": EXCHANGES.get(c) and EXCHANGES[c] + " (exchange)" or
                ("Your list: " + WALLETS[c]["label"] if c in WALLETS else None)} for c, k in cps.most_common(15)]
    top = A[0] if A else None
    what = top["name"] if top and top["score"] >= 35 else "Unclear / mixed behaviour"
    conf = "high" if top and top["score"] >= 70 else "medium" if top and top["score"] >= 45 else "low"
    out.update(
        archetypes=A[:4],
        stats={"swaps": len(swaps), "buys": buys, "sells": sells, "transfers": len(transfers), "round_trips": rt,
               "same_block": same_blk, "jito_share": 0, "fail_share": 0, "tokens_traded": toks,
               "median_trade_usd": statistics.median(usd) if usd else 0, "tx_per_hour": rate, "programs": []},
        activity={"tx_seen": len(evs), "complete_history": False, "first_seen": None,
                  "oldest_scanned": evs[-1]["ts"] if evs else None, "last_seen": evs[0]["ts"] if evs else None},
        counterparties=counter, linked_to_your_wallets=[c for c in counter if (c["label"] or "").startswith("Your list")],
        holdings={"sol": eth, "sol_usd": eth * ep, "tokens": rows, "token_count": len(rows), "total_usd": total,
                  "priced": sum(1 for r in rows if r["usd"])},
        recent=evs[:40],
        summary=f"Most likely: {what} ({conf} confidence). Holds ~${total:,.0f} ({NATIVE} + tokens it traded in the last {hours}h). "
                f"{len(evs)} token transfers in {hours}h.",
        manipulation=top["manipulation"] if top and top["score"] >= 35 else "No strong manipulation pattern in the scanned activity.",
        note=f"{NAME}: scans the last {hours}h of token transfers via RPC, so wallet age and first funder "
             f"aren't available - open it on {CFG['explorer_name']} for full history.")
    _prof[a] = (time.time(), out)
    return out


# ------------------------------------------------ allocation + funding watch ----
_known = set()          # (wallet, token) pairs already checked
_nonce = {}             # funder -> last seen nonce


def _balance_at(token, owner, block):
    raw = rpc("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + owner[2:]}, hex(block)])
    return int(raw, 16) if raw and raw != "0x" else 0


def _dex(mint):
    return f"https://dexscreener.com/{DEXSCREENER}/{mint}"


def flag_new_allocations(evs):
    """A watched wallet receiving a token it held none of the block before = new allocation (market-making deal)."""
    import solana_signals as sig
    for e in evs:
        w = WALLETS.get(e["wallet"]) or {}
        if not w.get("watch_new_tokens") or e["kind"] not in ("IN", "BUY") or e.get("mint") in QUOTES:
            continue
        if e.get("counterparty") in EXCHANGES:        # withdrawal from an exchange is a purchase, not a deal
            continue
        k = (e["wallet"], e["mint"])
        if k in _known:
            continue
        _known.add(k)
        try:
            before = _balance_at(e["mint"], e["wallet"], e["block"] - 1)
        except Exception:
            continue                                   # node has no state for that block - can't tell
        if before:
            continue
        e["new_allocation"] = True
        sig.alert(("alloc", CHAIN, e["sig"], e["mint"]),
                  f"🆕 <b>NEW TOKEN ALLOCATION: {e.get('symbol') or e['mint'][:8]}</b> [{NAME}]\n"
                  f"{w.get('label')} received {e['amount']:,.0f} ({sig.fmt_usd(e.get('usd'))}) - first time it has held this token.\n"
                  f"Likely a new market-making deal (listing / launch support). mcap {sig.fmt_usd(e.get('market_cap'))}\n"
                  f"From {e.get('counterparty_label') or e.get('counterparty')}\n{_dex(e['mint'])}\n{EXPLORER_TX}{e['sig']}")


def watch_funders(frm, to):
    """Native-coin sends from 'funder' wallets to brand-new addresses -> start tracking the new wallet."""
    import solana_signals as sig
    out = []
    for a, w in list(WALLETS.items()):
        if not w.get("funder"):
            continue
        n = int(rpc("eth_getTransactionCount", [a, "latest"]), 16)
        prev = _nonce.get(a)
        _nonce[a] = n
        if prev is None or n <= prev:
            continue                                   # it hasn't sent anything since last poll
        for b in range(max(frm, to - 400), to + 1):    # scan the new blocks for its transactions
            blk = rpc("eth_getBlockByNumber", [hex(b), True]) or {}
            for tx in blk.get("transactions") or []:
                if (tx.get("from") or "").lower() != a or not tx.get("to"):
                    continue
                dest, val = tx["to"].lower(), int(tx.get("value") or "0x0", 16)
                if val == 0 or dest in WALLETS or dest in EXCHANGES:
                    continue
                if int(rpc("eth_getTransactionCount", [dest, "latest"]), 16) > 0 or is_contract(dest):
                    continue                           # not a fresh wallet
                amount = val / 1e18
                label = f"Wintermute (auto) {dest[:6]}…{dest[-4:]}"
                sig.add_wallet(sys_mod(), dest, label, w.get("group") or "Wintermute",
                               f"Auto-added: funded by {w.get('label')} with {amount:.4f} {NATIVE} "
                               f"in block {b} ({tx['hash'][:10]}…) - same pattern that created Wintermute 4. Alerts on moves >= $2k",
                               True, 2000)   # alert on moves of $2k or more
                ep = eth_usd() or 0
                out.append({"sig": tx["hash"], "ts": int(blk.get("timestamp", "0x0"), 16), "block": b, "wallet": a,
                            "kind": "OUT", "mint": NATIVE, "symbol": NATIVE, "amount": amount, "usd": amount * ep,
                            "counterparty": dest, "counterparty_label": "NEW wallet - now tracked",
                            "new_wallet": True, "label": w.get("label"), "group": w.get("group"),
                            "alert": True, "min_usd": w.get("min_usd")})
                sig.alert(("funded", CHAIN, dest),
                          f"🐣 <b>{w.get('label')} funded a NEW wallet</b> [{NAME}]\n{dest}\n"
                          f"{amount:.4f} {NATIVE} - now tracked automatically (group {w.get('group')}).\n"
                          f"{EXPLORER}/address/{dest}")
    return out


def sys_mod():
    import sys
    return sys.modules[__name__]
