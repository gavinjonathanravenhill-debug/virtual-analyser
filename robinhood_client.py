"""
Robinhood Chain (chain id 4663, Arbitrum Orbit, gas in ETH) wallet tracker.

Data: the official public RPC (ROBINHOOD_RPC_URL to override) - ERC-20 Transfer logs for your
wallets, polled every ~20s; DexScreener (chain 'robinhood') for price / mcap / liquidity;
GeckoTerminal for holder concentration. The Blockscout explorer API sits behind a bot check,
so nothing here depends on it.
"""

import json
import os
import statistics
import threading
import time
from collections import Counter, defaultdict, deque

import requests

RPC_URL = os.getenv("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
POLL_SECONDS = int(os.getenv("ROBINHOOD_POLL_SECONDS", "20"))
RPC_GAP = float(os.getenv("ROBINHOOD_RPC_GAP", "0.06"))
BLOCK_SECONDS = 0.1                       # measured ~0.1s blocks
MAX_RANGE = 1_500_000                     # blocks per eth_getLogs (~40h) - tested OK up to 2M
START_HOURS = float(os.getenv("ROBINHOOD_START_HOURS", "6"))
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
QUOTE_SYMBOLS = {"WETH", "ETH", "USDC", "USDT", "USDG", "USDC.E", "DAI", "PYUSD"}
STABLE_SYMBOLS = {"USDC", "USDT", "USDG", "USDC.E", "DAI", "PYUSD"}

CHAIN = "robinhood"
NATIVE = "ETH"
GT_NETWORK = "robinhood"
BUBBLEMAPS_CHAIN = "robinhood"
EXPLORER = "https://robinhoodchain.blockscout.com"
EXPLORER_TX = EXPLORER + "/tx/"
PAGE = {"chain": "robinhood", "title": "Robinhood Chain Wallets", "native": "ETH", "dexscreener": "robinhood",
        "bubblemaps": "robinhood", "explorer": EXPLORER, "explorer_name": "Blockscout",
        "tx": EXPLORER + "/tx/", "addr": EXPLORER + "/address/", "token": EXPLORER + "/token/",
        "holders_suffix": "?tab=holders", "portfolio_suffix": "?tab=tokens", "addr_hint": "0x address",
        "rpc_note": "10-30s"}

_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "robinhood_wallets.json")) as f:
    _cfg = json.load(f)
WALLETS = {w["address"].lower(): {**w, "address": w["address"].lower()} for w in _cfg.get("wallets", [])}
EXCHANGES = {a.lower(): n for a, n in (_cfg.get("exchanges") or {}).items()}
FILE_LEVELS = [{**lv, "mint": lv["mint"].lower()} for lv in _cfg.get("levels", [])]
QUOTES = {}   # token address -> symbol, filled in as WETH/stables are discovered


def norm(a):
    return (a or "").strip().lower()


def is_addr(a):
    return isinstance(a, str) and len(a) == 42 and a.startswith("0x")


# ------------------------------------------------------------------ RPC ----
_s = requests.Session()
_rpc_lock = threading.Lock()


def rpc(method, params):
    with _rpc_lock:
        for attempt in range(5):
            try:
                r = _s.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            time.sleep(RPC_GAP)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + attempt * 2)
                continue
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"Robinhood RPC: {d['error'].get('message')}")
            return d["result"]
        raise RuntimeError("Robinhood RPC rate limit")


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


def logs_for(addresses, frm, to):
    """All ERC-20 Transfer logs to/from any of `addresses` between blocks frm..to."""
    out = []
    addrs = list(addresses)
    for i in range(0, len(addrs), 40):
        pads = [_pad(a) for a in addrs[i:i + 40]]
        start = frm
        while start <= to:
            end = min(to, start + MAX_RANGE)
            out += rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "topics": [TRANSFER, pads]}]) or []
            out += rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                        "topics": [TRANSFER, None, pads]}]) or []
            start = end + 1
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
                ev.update(kind="BUY", quote="ETH", quote_amount=eth_paid)
            elif amt < 0 and sender == w and cp and cp not in EXCHANGES and cp not in WALLETS and is_contract(cp):
                ev.update(kind="SELL", quote="ETH", quote_amount=None)   # sold into a pool for native ETH
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
            if a in chunk and p.get("chainId") == "robinhood" and liq >= 1000 and \
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
    if time.time() - _eth[0] > 120:
        try:
            _eth[1] = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                   params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=15).json()["ethereum"]["usd"]
            _eth[0] = time.time()
        except Exception:
            pass
    return _eth[1]


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
                       "rpc": "Robinhood RPC", "block": None}
        self.lock = threading.Lock()

    def poll(self):
        wallets = list(WALLETS)
        latest = latest_block()
        if not wallets:
            self.last_block = latest
            self.status.update(last_poll=int(time.time()), polls=self.status["polls"] + 1, block=latest)
            return []
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
        with self.lock:
            for e in sorted(evs, key=lambda e: e["ts"]):
                self.events.appendleft(e)
        self.checked.update(wallets)
        self.last_block = latest
        self.status.update(last_poll=int(time.time()), polls=self.status["polls"] + 1, block=latest)
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


def start_robinhood():
    global _started
    if _started:
        return
    _started = True
    import sys
    import solana_signals as sig
    sig.start_signals()
    sig.register(sys.modules[__name__])
    if os.getenv("ROBINHOOD_TRACKER_OFF"):
        return
    threading.Thread(target=tracker.loop, daemon=True, name="robinhood-tracker").start()


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
        price = 1.0 if sym in STABLE_SYMBOLS else (ep if sym in ("WETH", "ETH") else ti.get("price"))
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
        summary=f"Most likely: {what} ({conf} confidence). Holds ~${total:,.0f} (ETH + tokens it traded in the last {hours}h). "
                f"{len(evs)} token transfers in {hours}h.",
        manipulation=top["manipulation"] if top and top["score"] >= 35 else "No strong manipulation pattern in the scanned activity.",
        note="Robinhood Chain: scans the last %dh of token transfers via RPC (the explorer API is bot-protected), "
             "so wallet age and first funder aren't available - open it on Blockscout for full history." % hours)
    _prof[a] = (time.time(), out)
    return out
