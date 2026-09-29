"""
Download a Solana token's DEX trade history as CSV - the DexScreener "Transactions" table, rebuilt from
the chain so it works for any date range (DexScreener itself has no export and no history API).

  1. find the token's main pool (DexScreener) unless you give one
  2. page the pool's signatures back in time until the start date (Helius RPC, 1000 per call)
  3. parse the swaps in batches of 100 (Helius Enhanced Transactions)
  4. trader = fee payer; token + SOL/USDC legs from that wallet's transfers; USD from the hourly SOL price
     (Binance public data mirror)

Runs as a background job; the page polls progress and gives you the CSV link. Helius free plan = 1M credits
a month; a big busy range (hundreds of thousands of swaps) can use a real chunk of that, so there's a row cap.
"""

import csv
import io
import os
import threading
import time
import uuid

import requests

KEY = os.getenv("HELIUS_API_KEY", "").strip()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
PARSE = f"https://api.helius.xyz/v0/transactions?api-key={KEY}"
WSOL = "So11111111111111111111111111111111111111112"
QUOTES = {WSOL: "SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
OUT_DIR = "/data/exports" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
MAX_ROWS = int(os.getenv("EXPORT_MAX_ROWS", "100000"))

_s = requests.Session()
JOBS = {}


def _post(url, body, tries=5):
    for i in range(tries):
        r = _s.post(url, json=body, timeout=60)
        time.sleep(0.15)
        if r.status_code == 429:
            time.sleep(2 + 2 * i)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Helius rate limit - try a shorter range")


def find_pool(mint):
    r = _s.get("https://api.dexscreener.com/latest/dex/tokens/" + mint, timeout=20).json()
    pairs = [p for p in r.get("pairs") or [] if p.get("chainId") == "solana"
             and (p.get("baseToken") or {}).get("address") == mint]
    if not pairs:
        raise RuntimeError("No Solana pool found for that token")
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return best["pairAddress"], (best.get("baseToken") or {}).get("symbol"), best.get("dexId"), pairs


_sol = {}


def sol_usd(ts):
    """Hourly SOL/USDT close for a unix time (cached by the hour, 1000 hours per fetch)."""
    h = int(ts // 3600) * 3600
    if h not in _sol:
        try:
            k = _s.get("https://data-api.binance.vision/api/v3/klines", timeout=20, params={
                "symbol": "SOLUSDT", "interval": "1h", "startTime": (h - 500 * 3600) * 1000,
                "endTime": (h + 500 * 3600) * 1000, "limit": 1000}).json()
            for row in k:
                _sol[int(row[0] // 1000)] = float(row[4])
        except Exception:
            pass
        _sol.setdefault(h, None)
    return _sol.get(h)


def parse(tx, mint):
    """One enhanced transaction -> a trade row (or None if it isn't a swap of this token)."""
    trader = tx.get("feePayer")
    tok = quote = 0.0
    qmint = None
    for t in tx.get("tokenTransfers") or []:
        amt = float(t.get("tokenAmount") or 0)
        frm, to = t.get("fromUserAccount"), t.get("toUserAccount")
        if frm == to:
            continue
        sign = 1 if to == trader else -1 if frm == trader else 0
        if not sign:
            continue
        if t.get("mint") == mint:
            tok += sign * amt
        elif t.get("mint") in QUOTES:
            quote += sign * amt
            qmint = qmint or t["mint"]
    if not qmint:   # pure native-SOL swaps (no wrapped SOL leg)
        for n in tx.get("nativeTransfers") or []:
            amt = float(n.get("amount") or 0) / 1e9
            if amt < 0.0005:
                continue
            if n.get("toUserAccount") == trader:
                quote += amt
            elif n.get("fromUserAccount") == trader:
                quote -= amt
        qmint = WSOL
    if abs(tok) < 1e-12 or abs(quote) < 1e-12 or (tok > 0) == (quote > 0):
        return None
    ts = tx.get("timestamp") or 0
    q = QUOTES[qmint]
    qusd = abs(quote) * (sol_usd(ts) or 0) if q == "SOL" else abs(quote)
    return {"time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), "type": "Buy" if tok > 0 else "Sell",
            "usd": round(qusd, 2) if qusd else "", "token_amount": abs(tok), "quote_amount": abs(quote), "quote": q,
            "price_usd": qusd / abs(tok) if qusd else "", "trader": trader, "dex": tx.get("source"),
            "signature": tx.get("signature"), "unix": ts}


def run(job):
    j = JOBS[job]
    try:
        if not KEY:
            raise RuntimeError("Set HELIUS_API_KEY on Railway first")
        mint, start, end = j["mint"], j["start"], j["end"]
        if not j.get("pool"):
            j["stage"] = "finding the pool"
            j["pool"], j["symbol"], j["dex"], _ = find_pool(mint)
        pool, before, sigs = j["pool"], None, []
        j["stage"] = "walking back through the pool's history"
        while True:
            p = {"limit": 1000}
            if before:
                p["before"] = before
            page = _post(RPC, {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                               "params": [pool, p]}).get("result") or []
            if not page:
                break
            before = page[-1]["signature"]
            j["scanned"] += len(page)
            j["reached"] = page[-1].get("blockTime")
            sigs += [s["signature"] for s in page if not s.get("err") and start <= (s.get("blockTime") or 0) <= end]
            if (page[-1].get("blockTime") or 0) < start or len(sigs) >= MAX_ROWS:
                break
            if j.get("cancel"):
                raise RuntimeError("cancelled")
        sigs = sigs[:MAX_ROWS]
        j.update(stage="parsing swaps", found=len(sigs))
        rows = []
        for i in range(0, len(sigs), 100):
            for tx in _post(PARSE, {"transactions": sigs[i:i + 100]}) or []:
                r = parse(tx, mint)
                if r:
                    rows.append(r)
            j["parsed"] = min(len(sigs), i + 100)
            j["rows"] = len(rows)
            if j.get("cancel"):
                raise RuntimeError("cancelled")
        rows.sort(key=lambda r: r["unix"])
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f"{j['symbol'] or mint[:6]}_{time.strftime('%Y%m%d', time.gmtime(start))}-"
                                     f"{time.strftime('%Y%m%d', time.gmtime(end))}_{job[:6]}.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time_utc", "type", "usd", "token_amount", "quote_amount", "quote",
                                              "price_usd", "trader", "dex", "signature", "unix"])
            w.writeheader()
            w.writerows(rows)
        traders = {}
        for r in rows:
            t = traders.setdefault(r["trader"], {"buys": 0.0, "sells": 0.0, "n": 0, "bought_tok": 0.0, "sold_tok": 0.0,
                                                 "first": r["unix"], "last": r["unix"]})
            t["n"] += 1
            t["buys" if r["type"] == "Buy" else "sells"] += r["usd"] or 0
            t["bought_tok" if r["type"] == "Buy" else "sold_tok"] += r["token_amount"]
            t["last"] = r["unix"]
        j["traders"] = traders
        j.update(stage="done", path=path, file=os.path.basename(path), rows=len(rows), done=time.time(),
                 capped=len(sigs) >= MAX_ROWS,
                 top_traders=sorted(({"wallet": w, **v, "net": v["sells"] - v["buys"], "bot": _bot(w, v["n"])}
                                     for w, v in traders.items()),
                                    key=lambda x: -(x["buys"] + x["sells"]))[:25])
    except Exception as e:
        j.update(stage="error", error=str(e)[:300])


def _bot(w, n):
    try:
        import edge
        b = edge.bots().get(w)
        if b:
            return b.get("label") or "known bot"
    except Exception:
        pass
    return f"bot-like ({n} trades)" if n >= 200 else None


def start(mint, start_ts, end_ts, pool=None):
    job = uuid.uuid4().hex
    JOBS[job] = {"id": job, "mint": mint, "pool": pool, "start": int(start_ts), "end": int(end_ts), "stage": "queued",
                 "scanned": 0, "found": 0, "parsed": 0, "rows": 0, "reached": None, "symbol": None, "created": time.time()}
    threading.Thread(target=run, args=(job,), daemon=True, name="export-" + job[:6]).start()
    return JOBS[job]
