"""
Insider-group scanner ("group 2") for any Solana meme coin.

On a DEX top-traders list, the wallets that SELL MORE THAN THEY BOUGHT in the pool got their tokens somewhere
else: a team / insider allocation, the pump.fun bonding curve before migration, another pool, or a transfer
from a wallet the same person controls. This finds them and groups them:

  1. rebuild the pool's trades for your date range (trade_export)
  2. group-2 wallets = sold >= 1.2x the tokens they bought here (and sold at least $MIN_SOLD)
  3. for each, read its own history (Helius parsed transactions, up to 300): where its tokens came from
     (transfer from X / bought on the pump.fun curve / bought on another pool) and, when we reach the start
     of the wallet's life, who first funded it with SOL
  4. cluster: wallets sharing a token sender, a SOL funder (exchanges excluded), or near-identical bag sizes
  5. one click to track a whole cluster
"""

import os
import threading
import time
import uuid
from collections import defaultdict

import requests

import trade_export as tx_exp

KEY = os.getenv("HELIUS_API_KEY", "").strip()
ADDR_TX = "https://api.helius.xyz/v0/addresses/{}/transactions"
MIN_SOLD = float(os.getenv("INSIDER_MIN_SOLD_USD", "500"))
MAX_WALLETS = int(os.getenv("INSIDER_MAX_WALLETS", "40"))
PAGES = 3
_s = requests.Session()
JOBS = {}


def _get(url, params, tries=5):
    for i in range(tries):
        r = _s.get(url, params=params, timeout=60)
        time.sleep(0.15)
        if r.status_code == 429:
            time.sleep(2 + 2 * i)
            continue
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Helius rate limit")


def wallet_story(w, mint):
    """Where this wallet's tokens came from, and who funded it (if we reach its first transaction)."""
    before, txs, exhausted = None, [], False
    for _ in range(PAGES):
        p = {"api-key": KEY, "limit": 100}
        if before:
            p["before"] = before
        page = _get(ADDR_TX.format(w), p) or []
        txs += page
        if len(page) < 100:
            exhausted = True
            break
        before = page[-1]["signature"]
    src = defaultdict(float)          # source label -> tokens
    senders = defaultdict(float)
    for t in txs:
        got = sum(float(x.get("tokenAmount") or 0) for x in t.get("tokenTransfers") or []
                  if x.get("mint") == mint and x.get("toUserAccount") == w and x.get("fromUserAccount") != w)
        if not got:
            continue
        paid = any((x.get("mint") in tx_exp.QUOTES and x.get("fromUserAccount") == w)
                   for x in t.get("tokenTransfers") or []) or any(
            n.get("fromUserAccount") == w and float(n.get("amount") or 0) > 5e6 for n in t.get("nativeTransfers") or [])
        if paid:
            srcname = (t.get("source") or "").upper()
            src["pump.fun curve buy" if "PUMP_FUN" in srcname and "AMM" not in srcname else f"bought on {srcname.lower() or 'a DEX'}"] += got
        else:
            frm = next((x.get("fromUserAccount") for x in t.get("tokenTransfers") or []
                        if x.get("mint") == mint and x.get("toUserAccount") == w), None)
            senders[frm] += got
            src["transfer in"] += got
    funder = None
    if exhausted and txs:
        for t in reversed(txs):   # oldest first
            f = next((n.get("fromUserAccount") for n in t.get("nativeTransfers") or []
                      if n.get("toUserAccount") == w and float(n.get("amount") or 0) >= 1e7), None)
            if f:
                funder = f
                break
    return {"sources": dict(src), "senders": dict(senders), "funder": funder,
            "born": txs[-1].get("timestamp") if exhausted and txs else None, "history_seen": len(txs),
            "whole_history": exhausted}


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def cluster(cands, stories, exchanges, bots):
    uf = _UF()
    why = defaultdict(set)
    for w in cands:
        uf.find(w)
        st = stories.get(w) or {}
        for snd, amt in (st.get("senders") or {}).items():
            if snd and snd not in exchanges and snd not in bots:
                uf.union(w, "send:" + snd)
        f = st.get("funder")
        if f and f not in exchanges and f not in bots:
            uf.union(w, "fund:" + f)
    # near-identical bag sizes (within 1%), 3+ wallets
    bags = sorted(((c["sold_tok"], w) for w, c in cands.items() if c["sold_tok"]), key=lambda x: x[0])
    i = 0
    while i < len(bags):
        j = i
        while j + 1 < len(bags) and bags[j + 1][0] <= bags[i][0] * 1.01:
            j += 1
        if j - i + 1 >= 3:
            node = f"bag:{bags[i][0]:.0f}"
            for k in range(i, j + 1):
                uf.union(bags[k][1], node)
        i = j + 1
    groups = defaultdict(list)
    for w in cands:
        groups[uf.find(w)].append(w)
    links = defaultdict(lambda: defaultdict(set))
    for node in list(uf.p):
        if ":" in node:
            kind, val = node.split(":", 1)
            members = [w for w in cands if uf.find(w) == uf.find(node)]
            if members:
                links[uf.find(node)][kind].add(val)
    out = []
    for root, ws in groups.items():
        if len(ws) < 2:
            continue
        ev = []
        for kind, vals in links[root].items():
            for v in vals:
                n = sum(1 for w in ws if (kind == "send" and v in (stories[w].get("senders") or {})) or
                        (kind == "fund" and stories[w].get("funder") == v) or
                        (kind == "bag" and f"{cands[w]['sold_tok']:.0f}" and abs(cands[w]["sold_tok"] - float(v)) <= float(v) * 0.011))
                label = {"send": "tokens sent by", "fund": "SOL funded by", "bag": "identical bags of ~"}[kind]
                ev.append({"kind": kind, "value": v, "wallets": n, "text": f"{n} wallets: {label} {v if kind == 'bag' else v[:4] + '…' + v[-4:]}"})
        out.append({"wallets": sorted(ws, key=lambda w: -cands[w]["sells"]), "evidence": sorted((e for e in ev if e["wallets"] >= 2), key=lambda e: -e["wallets"]),
                    "sold_usd": sum(cands[w]["sells"] for w in ws), "bought_usd": sum(cands[w]["buys"] for w in ws),
                    "sold_tok": sum(cands[w]["sold_tok"] for w in ws)})
    return sorted(out, key=lambda g: -g["sold_usd"])


def run(job):
    j = JOBS[job]
    try:
        if not KEY:
            raise RuntimeError("Set HELIUS_API_KEY on Railway first")
        j["stage"] = "rebuilding the pool's trades"
        ex = tx_exp.start(j["mint"], j["start"], j["end"], j.get("pool"))
        j["export_id"] = ex["id"]
        while ex["stage"] not in ("done", "error"):
            time.sleep(2)
            j["export"] = {k: ex.get(k) for k in ("stage", "scanned", "found", "parsed", "rows", "reached")}
            if j.get("cancel"):
                ex["cancel"] = True
        if ex["stage"] == "error":
            raise RuntimeError("trade rebuild failed: " + ex.get("error", ""))
        j["symbol"] = ex.get("symbol")
        traders = ex.get("traders") or {}
        import edge
        import solana_client as sc
        bots = set(edge.bots())
        cands = {w: t for w, t in traders.items()
                 if t["sells"] >= MIN_SOLD and t["sold_tok"] >= 1.2 * t["bought_tok"] and w not in bots
                 and t["n"] < edge.BOT_TRADES * 10}
        all_cands = dict(sorted(cands.items(), key=lambda kv: -kv[1]["sells"]))
        cands = dict(list(all_cands.items())[:MAX_WALLETS])      # the biggest get traced; all get listed
        j.update(stage="tracing where their tokens came from", group2=len(all_cands), to_trace=len(cands), traced=0,
                 traders=len(traders))
        stories = {}
        for w in cands:
            if j.get("cancel"):
                raise RuntimeError("cancelled")
            try:
                stories[w] = wallet_story(w, j["mint"])
            except Exception as e:
                stories[w] = {"error": str(e)[:80], "sources": {}, "senders": {}}
            j["traced"] += 1
        groups = cluster(cands, stories, set(sc.EXCHANGES), bots)
        tot_sold = sum(t["sells"] for t in traders.values()) or 1
        rows = []
        for w, t in all_cands.items():
            st = stories.get(w) or {"sources": {}, "senders": {}, "untraced": True}
            main_src = max((st.get("sources") or {}).items(), key=lambda kv: kv[1], default=(None, 0))[0]
            rows.append({"wallet": w, "sold_usd": t["sells"], "bought_usd": t["buys"], "sold_tok": t["sold_tok"],
                         "bought_tok": t["bought_tok"], "outside_tok": t["sold_tok"] - t["bought_tok"], "trades": t["n"],
                         "no_buy": t["bought_tok"] == 0,
                         "source": main_src or ("not traced (outside the top %d)" % MAX_WALLETS if st.get("untraced") else
                                                "unknown (older than the last 300 txs)" if not st.get("whole_history") else "unknown"),
                         "sources": st.get("sources"),
                         "sender": max((st.get("senders") or {}).items(), key=lambda kv: kv[1], default=(None, 0))[0],
                         "funder": st.get("funder"), "funder_exchange": sc.EXCHANGES.get(st.get("funder") or ""),
                         "born": st.get("born"), "error": st.get("error")})
        j.update(stage="done", wallets=rows, groups=groups, done=time.time(),
                 no_buy=sum(1 for r in rows if r["no_buy"]),
                 group2_share=sum(r["sold_usd"] for r in rows) / tot_sold * 100,
                 clustered_share=sum(g["sold_usd"] for g in groups) / tot_sold * 100)
    except Exception as e:
        j.update(stage="error", error=str(e)[:300])


def start(mint, start_ts, end_ts, pool=None):
    job = uuid.uuid4().hex
    JOBS[job] = {"id": job, "mint": mint, "pool": pool, "start": int(start_ts), "end": int(end_ts),
                 "stage": "queued", "created": time.time()}
    threading.Thread(target=run, args=(job,), daemon=True, name="insider-" + job[:6]).start()
    return JOBS[job]


def track(job, wallets, label_prefix=None):
    import edge
    import solana_client as sc
    import solana_signals as sig
    j = JOBS.get(job) or {}
    sym = j.get("symbol") or (j.get("mint") or "")[:6]
    added = []
    for w in wallets[:30]:
        if w in sc.WALLETS or edge.is_bot(w):
            continue
        sig.add_wallet(sc, w, f"{label_prefix or sym + ' insider'} {w[:4]}", "Insider clusters",
                       note=f"group-2 seller of {sym} (sold more than bought in the pool)", alert_on=True, min_usd=1000)
        added.append(w)
    try:
        import helius_hook
        helius_hook.resync_async()
    except Exception:
        pass
    return {"added": added}
