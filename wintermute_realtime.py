"""
Real-time Wintermute feed.

A background thread holds a WebSocket to an Ethereum node and pushes events to
browsers over Server-Sent Events (/api/wintermute/stream):

  * ERC-20 Transfer logs where a Wintermute wallet is sender or receiver  (mined)
  * native ETH transfers in each new block                               (mined)
  * pending transactions from/to Wintermute wallets        (mempool - Alchemy only)

Env:
  ETH_WSS_URL  WebSocket RPC. Default is the free public node (no key, mined only).
               Use wss://eth-mainnet.g.alchemy.com/v2/<KEY> to also get pending txs.
  ETH_HTTP_URL optional HTTP RPC; derived from ETH_WSS_URL when not set.
"""

import asyncio
import json
import os
import queue
import threading
import time
from collections import deque

import requests

from wintermute_client import (KNOWN_EXCHANGES, WINTERMUTE_WALLETS, CoinGecko,
                               LARGE_USD, cached)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC20_TRANSFER_SIG = "0xa9059cbb"
ERC20_TRANSFER_FROM_SIG = "0x23b872dd"
DEFAULT_WSS = "wss://ethereum-rpc.publicnode.com"

WALLETS = {k.lower(): v for k, v in WINTERMUTE_WALLETS.items()}


def _pad(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def _unpad(topic):
    return "0x" + topic[-40:].lower()


class EventBus:
    """Fan-out of events to every connected browser, plus a replay buffer."""

    def __init__(self, keep=200):
        self.recent = deque(maxlen=keep)
        self.subs = set()
        self.lock = threading.Lock()
        self.status = {"state": "starting", "block": None, "pending": False,
                       "provider": None, "since": time.time(), "events": 0}

    def publish(self, kind, data):
        msg = {"type": kind, "data": data}
        if kind == "transfer":
            self.recent.appendleft(msg)
            self.status["events"] += 1
        with self.lock:
            dead = []
            for q in self.subs:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subs.discard(q)

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


bus = EventBus()


class RealtimeWatcher:
    def __init__(self):
        self.wss = os.getenv("ETH_WSS_URL", DEFAULT_WSS)
        self.http = os.getenv("ETH_HTTP_URL") or (
            self.wss.replace("wss://", "https://").replace("ws://", "http://"))
        self.alchemy = "alchemy.com" in self.wss
        self.cg = CoinGecko()
        self.s = requests.Session()
        self.seen = deque(maxlen=5000)
        bus.status["provider"] = self.wss.split("/v2/")[0]  # never expose the key
        bus.status["pending"] = self.alchemy

    # ---------- helpers ----------
    def rpc(self, method, params):
        r = self.s.post(self.http, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": params}, timeout=20)
        r.raise_for_status()
        return r.json().get("result")

    def token_meta(self, contract):
        def fetch():
            def call(sig):
                try:
                    return self.rpc("eth_call", [{"to": contract, "data": sig}, "latest"])
                except Exception:
                    return None
            dec_raw = call("0x313ce567")
            sym_raw = call("0x95d89b41")
            decimals = int(dec_raw, 16) if dec_raw and dec_raw != "0x" else 18
            symbol = "?"
            if sym_raw and len(sym_raw) > 2:
                b = bytes.fromhex(sym_raw[2:])
                try:
                    if len(b) >= 96:   # ABI-encoded string
                        n = int.from_bytes(b[32:64], "big")
                        symbol = b[64:64 + n].decode("utf-8", "ignore")
                    else:              # bytes32 (e.g. MKR)
                        symbol = b.rstrip(b"\0").decode("utf-8", "ignore")
                except Exception:
                    pass
            return {"symbol": symbol.upper() or "?", "decimals": decimals}
        return cached(f"meta:{contract}", 86400, fetch)

    def usd_price(self, contract):
        def fetch():
            try:
                if contract == "eth":
                    return self.cg.eth_price()
                return (self.cg.token_prices([contract]).get(contract) or {}).get("usd")
            except Exception:
                return None
        return cached(f"px:{contract}", 300, fetch)

    def emit(self, *, status, tx_hash, frm, to, contract, raw_amount, block=None, log_index=None):
        key = (tx_hash, contract, frm, to, raw_amount, status)
        if key in self.seen:
            return
        self.seen.append(key)
        if contract == "eth":
            symbol, amount = "ETH", raw_amount / 1e18
        else:
            m = self.token_meta(contract)
            symbol, amount = m["symbol"], raw_amount / (10 ** m["decimals"])
        wallet = frm if frm in WALLETS else to
        direction = "OUT" if frm in WALLETS else "IN"
        cp = to if direction == "OUT" else frm
        px = self.usd_price(contract)
        usd = amount * px if px else None
        bus.publish("transfer", {
            "status": status,  # "pending" | "mined"
            "ts": int(time.time()), "block": block, "hash": tx_hash,
            "wallet": wallet, "wallet_label": WALLETS.get(wallet, wallet),
            "direction": direction, "symbol": symbol, "contract": contract,
            "amount": amount, "usd": usd,
            "counterparty": cp,
            "counterparty_label": WALLETS.get(cp) or KNOWN_EXCHANGES.get(cp) or "",
            "internal": cp in WALLETS,
            "exchange": KNOWN_EXCHANGES.get(cp),
            "large": (usd or 0) >= LARGE_USD,
        })

    # ---------- handlers ----------
    def on_log(self, log):
        if log.get("removed") or len(log.get("topics", [])) != 3:  # 4 topics = NFT
            return
        data = log.get("data") or "0x"
        if len(data) < 3:
            return
        self.emit(status="mined", tx_hash=log["transactionHash"],
                  frm=_unpad(log["topics"][1]), to=_unpad(log["topics"][2]),
                  contract=log["address"].lower(), raw_amount=int(data, 16),
                  block=int(log["blockNumber"], 16), log_index=log.get("logIndex"))

    def on_head(self, head):
        n = int(head["number"], 16)
        bus.status.update(state="live", block=n)
        bus.publish("head", {"block": n, "ts": int(head.get("timestamp", "0x0"), 16)})
        threading.Thread(target=self.scan_block_eth, args=(n,), daemon=True).start()

    def scan_block_eth(self, n):
        """Native ETH moves aren't logs, so check each new block's transactions."""
        try:
            block = self.rpc("eth_getBlockByNumber", [hex(n), True])
        except Exception:
            return
        for tx in (block or {}).get("transactions", []):
            frm, to = (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
            if (frm in WALLETS or to in WALLETS) and int(tx.get("value", "0x0"), 16) > 0:
                self.emit(status="mined", tx_hash=tx["hash"], frm=frm, to=to,
                          contract="eth", raw_amount=int(tx["value"], 16), block=n)

    def on_pending(self, tx):
        """Alchemy pending tx from/to a Wintermute wallet - decode ETH or ERC-20 transfer."""
        frm, to = (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
        inp = tx.get("input") or "0x"
        value = int(tx.get("value", "0x0"), 16)
        if value > 0 and (frm in WALLETS or to in WALLETS):
            self.emit(status="pending", tx_hash=tx["hash"], frm=frm, to=to,
                      contract="eth", raw_amount=value)
        if inp.startswith(ERC20_TRANSFER_SIG) and len(inp) >= 138 and frm in WALLETS:
            self.emit(status="pending", tx_hash=tx["hash"], frm=frm,
                      to="0x" + inp[34:74].lower(), contract=to, raw_amount=int(inp[74:138], 16))
        elif inp.startswith(ERC20_TRANSFER_FROM_SIG) and len(inp) >= 202:
            src, dst = "0x" + inp[34:74].lower(), "0x" + inp[98:138].lower()
            if src in WALLETS or dst in WALLETS:
                self.emit(status="pending", tx_hash=tx["hash"], frm=src, to=dst,
                          contract=to, raw_amount=int(inp[138:202], 16))

    # ---------- websocket loop ----------
    async def session(self):
        import websockets
        async with websockets.connect(self.wss, ping_interval=20, ping_timeout=20,
                                      max_size=2 ** 23) as ws:
            padded = [_pad(w) for w in WALLETS]
            subs = [
                ["newHeads"],
                ["logs", {"topics": [TRANSFER_TOPIC, padded]}],        # WM sends
                ["logs", {"topics": [TRANSFER_TOPIC, None, padded]}],  # WM receives
            ]
            if self.alchemy:
                subs.append(["alchemy_pendingTransactions",
                             {"fromAddress": list(WALLETS), "toAddress": list(WALLETS)}])
            kinds = {}
            for i, params in enumerate(subs, 1):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": i,
                                          "method": "eth_subscribe", "params": params}))
            bus.status.update(state="connecting")
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg:   # subscription ack
                    if msg.get("result"):
                        kinds[msg["result"]] = subs[msg["id"] - 1][0]
                    elif msg.get("error"):
                        bus.publish("status", {"warning": f"{subs[msg['id']-1][0]}: "
                                                          f"{msg['error'].get('message')}"})
                    continue
                p = msg.get("params") or {}
                kind, res = kinds.get(p.get("subscription")), p.get("result")
                if not res:
                    continue
                try:
                    if kind == "newHeads":
                        self.on_head(res)
                    elif kind == "logs":
                        await asyncio.to_thread(self.on_log, res)
                    elif kind == "alchemy_pendingTransactions":
                        await asyncio.to_thread(self.on_pending, res)
                except Exception as e:
                    print(f"[wintermute realtime] handler error: {e}")

    def run_forever(self):
        backoff = 2
        while True:
            try:
                asyncio.run(self.session())
                backoff = 2
            except Exception as e:
                bus.status.update(state="reconnecting")
                bus.publish("status", {"state": "reconnecting", "error": str(e)[:200]})
                print(f"[wintermute realtime] {e} - reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


_started = False
_start_lock = threading.Lock()


def start_realtime():
    """Start the watcher thread once per process."""
    global _started
    with _start_lock:
        if _started or os.getenv("WINTERMUTE_REALTIME", "1") == "0":
            return
        _started = True
    threading.Thread(target=RealtimeWatcher().run_forever, name="wintermute-ws",
                     daemon=True).start()
