"""
Wallet profiler - "what is this address, what does it hold, what is it probably doing?"

Everything here is heuristic: it reads the wallet's recent on-chain behaviour and scores it
against common archetypes. Evidence is returned with every verdict so you can judge it.
"""

import statistics
import time
from collections import Counter, defaultdict

import solana_client as sc

TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]

PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M": "Jupiter DCA",
    "jupoNjAxXgZ4rjzxzPMP4oxduvQsQtZzyknqvzYNrNu": "Jupiter Limit",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
}
DEX_DIRECT = {"Raydium AMM", "Raydium CLMM", "Raydium CPMM", "Orca", "Meteora DLMM", "Meteora",
              "pump.fun", "PumpSwap"}
LIQUIDITY_VENUES = {"Raydium CLMM", "Meteora DLMM", "Orca"}
IGNORE_PROGRAMS = {"ComputeBudget111111111111111111111111111111", "11111111111111111111111111111111",
                   "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", *TOKEN_PROGRAMS,
                   "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}
JITO_TIPS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}
# Well-known entities (verify before relying on them; extend freely)
KNOWN = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance hot wallet",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance hot wallet",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Coinbase hot wallet",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase hot wallet",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken hot wallet",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit hot wallet",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX hot wallet",
}
MAJORS = {"SOL", "USDC", "USDT"}

_cache = {}


def label_of(a):
    if a in sc.WALLETS:
        return "Your list: " + sc.WALLETS[a]["label"]
    return KNOWN.get(a) or PROGRAMS.get(a)


# ------------------------------------------------------------ per-tx view --
def analyse_tx(tx, owner):
    if not tx:
        return None
    meta, msg = tx.get("meta") or {}, tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    progs, cps = set(), Counter()
    ixs = list(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    jito = False
    for ix in ixs:
        pid = ix.get("programId")
        if pid and pid not in IGNORE_PROGRAMS:
            progs.add(PROGRAMS.get(pid, "other"))
        info = (ix.get("parsed") or {}).get("info") if isinstance(ix.get("parsed"), dict) else None
        if info:
            dest, src = info.get("destination"), info.get("source")
            if src == owner and dest in JITO_TIPS:
                jito = True
            elif src == owner and dest:
                cps[dest] += 1
            elif dest == owner and src:
                cps[src] += 1
    # token counterparties = other owners whose balance moved in the same tx
    for side in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(side) or []:
            o = b.get("owner")
            if o and o != owner:
                cps[o] += 0  # present, weight added below
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == owner:
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * amt
    sol = 0.0
    if owner in keys:
        i = keys.index(owner)
        sol = (meta.get("postBalances", [0])[i] - meta.get("preBalances", [0])[i]) / 1e9
        if i == 0:
            sol += meta.get("fee", 0) / 1e9
    sol += deltas.pop(sc.WSOL, 0)
    if abs(sol) >= sc.DUST_SOL:
        deltas["SOL"] = sol
    deltas = {m: d for m, d in deltas.items() if abs(d) > 1e-9}
    return {"sig": tx["transaction"]["signatures"][0], "ts": tx.get("blockTime") or 0,
            "slot": tx.get("slot"), "failed": bool(meta.get("err")), "fee_payer": keys[0] == owner,
            "programs": progs, "counterparties": cps, "jito": jito, "deltas": deltas,
            "event": sc.parse_tx(tx, owner)}


# ------------------------------------------------------------- holdings ---
def holdings(address):
    sol = (sc.rpc("getBalance", [address]) or {}).get("value", 0) / 1e9
    toks = defaultdict(float)
    for prog in TOKEN_PROGRAMS:
        try:
            res = sc.rpc("getTokenAccountsByOwner", [address, {"programId": prog},
                                                      {"encoding": "jsonParsed"}])
        except RuntimeError:
            continue
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = float(info["tokenAmount"].get("uiAmount") or 0)
            if amt > 0:
                toks[info["mint"]] += amt
    mints = list(toks)[:150]
    info = sc.token_info(mints) if mints else {}
    sp = sc.sol_usd() or 0
    rows = []
    for m in mints:
        t = info.get(m) or {}
        price = t.get("price") or (1.0 if m in sc.QUOTES and sc.QUOTES[m] != "SOL" else None)
        val = toks[m] * price if price else None
        rows.append({"mint": m, "symbol": t.get("symbol") or sc.QUOTES.get(m) or m[:4] + "…",
                     "amount": toks[m], "price": price, "usd": val, "market_cap": t.get("market_cap"),
                     "liquidity": t.get("liquidity"),
                     "flag": ("no market" if not t.get("liquidity") and m not in sc.QUOTES else
                              "micro-cap" if (t.get("market_cap") or 1e18) < 1e6 else
                              "thin liquidity" if t.get("liquidity") and t.get("market_cap") and
                              t["liquidity"] / t["market_cap"] < 0.02 else "")})
    rows.sort(key=lambda r: -(r["usd"] or 0))
    total = sol * sp + sum(r["usd"] or 0 for r in rows)
    for r in rows:
        r["pct"] = (r["usd"] or 0) / total * 100 if total else 0
    return {"sol": sol, "sol_usd": sol * sp, "tokens": rows, "token_count": len(toks),
            "total_usd": total, "priced": sum(1 for r in rows if r["usd"])}


# --------------------------------------------------------------- history ---
def history_span(address, pages=3):
    """Walk back through signatures to estimate age, activity and (if reachable) the funder."""
    sigs, before = [], None
    for _ in range(pages):
        p = {"limit": 1000}
        if before:
            p["before"] = before
        batch = sc.rpc("getSignaturesForAddress", [address, p]) or []
        sigs += batch
        if len(batch) < 1000:
            return sigs, True
        before = batch[-1]["signature"]
    return sigs, False


def funder(address, oldest_sig):
    tx = sc.get_tx(oldest_sig)
    if not tx:
        return None
    a = analyse_tx(tx, address)
    sol_in = (a["deltas"].get("SOL") or 0) > 0
    src = next((c for c, _ in a["counterparties"].most_common() if c != address), None)
    if not src and tx["transaction"]["message"]["accountKeys"]:
        k = tx["transaction"]["message"]["accountKeys"][0]
        src = k["pubkey"] if isinstance(k, dict) else k
    return {"address": src, "label": label_of(src), "sol": a["deltas"].get("SOL") if sol_in else None,
            "ts": a["ts"], "sig": oldest_sig}


# ------------------------------------------------------------- classify ---
def classify(txs, n_sigs, span_h, hold):
    ok = [t for t in txs if not t["failed"]]
    n = max(len(ok), 1)
    swaps = [t for t in ok if t["event"] and t["event"]["kind"] in ("BUY", "SELL")]
    transfers = [t for t in ok if t["event"] and t["event"]["kind"] in ("IN", "OUT")]
    prog = Counter(p for t in ok for p in t["programs"])
    jito_share = sum(t["jito"] for t in ok) / n
    fail_share = sum(t["failed"] for t in txs) / max(len(txs), 1)
    rate = n_sigs / span_h if span_h else 0
    direct = sum(1 for t in ok if t["programs"] & DEX_DIRECT and "Jupiter" not in t["programs"]) / n
    lp = sum(1 for t in ok if t["programs"] & LIQUIDITY_VENUES) / n
    # circular / arb: SOL or stable changes but no other token net change
    circ = sum(1 for t in ok if t["deltas"] and all(m in ("SOL",) or m in sc.QUOTES for m in t["deltas"])
               and len(t["programs"] & (DEX_DIRECT | {"Jupiter"})) > 0) / n
    # round trips: buy then sell (or reverse) of same token within 120s
    by_tok = defaultdict(list)
    for t in swaps:
        by_tok[t["event"]["mint"]].append(t)
    rt = same_slot = 0
    pair_sizes = []
    for mint, lst in by_tok.items():
        lst.sort(key=lambda t: t["ts"])
        for a, b in zip(lst, lst[1:]):
            if a["event"]["kind"] != b["event"]["kind"] and b["ts"] - a["ts"] <= 120:
                rt += 1
                same_slot += a["slot"] == b["slot"]
                x, y = a["event"]["amount"], b["event"]["amount"]
                pair_sizes.append(min(x, y) / max(x, y) if max(x, y) else 0)
    rt_share = rt / max(len(swaps), 1)
    equal_size = statistics.mean(pair_sizes) if pair_sizes else 0
    buys = sum(1 for t in swaps if t["event"]["kind"] == "BUY")
    sells = len(swaps) - buys
    balance = min(buys, sells) / max(buys, sells) if max(buys, sells) else 0
    tokens_traded = len(by_tok)
    top_tok_share = max((len(v) for v in by_tok.values()), default=0) / max(len(swaps), 1)
    ins = [t for t in transfers if t["event"]["kind"] == "IN" and t["event"]["mint"] not in ("SOL",)
           and t["event"]["mint"] not in sc.QUOTES]
    outs = [t for t in transfers if t["event"]["kind"] == "OUT"]
    out_cps = {c for t in outs for c in t["counterparties"]}
    to_cex = sum(1 for t in outs for c in t["counterparties"] if c in KNOWN)
    sells_after_in = sum(1 for m in {t["event"]["mint"] for t in ins}
                         if any(s["event"]["mint"] == m and s["event"]["kind"] == "SELL" for s in swaps))
    usd = [t["event"].get("usd") for t in swaps if t["event"].get("usd")]
    med_usd = statistics.median(usd) if usd else 0

    A = {}

    def arche(name, score, evidence, manip):
        A[name] = {"name": name, "score": max(0, min(100, round(score))), "evidence": [e for e in evidence if e],
                   "manipulation": manip}

    arche("MEV / sandwich bot",
          40 * jito_share + 30 * min(1, same_slot / 3) + 15 * direct + 15 * min(1, rate / 60) + 10 * fail_share,
          [f"Jito tips in {jito_share:.0%} of txs" if jito_share > .1 else "",
           f"{same_slot} buy/sell pairs in the same block" if same_slot else "",
           f"Hits DEX pools directly (not via Jupiter) in {direct:.0%} of txs" if direct > .3 else "",
           f"{fail_share:.0%} failed txs (typical of bots racing each other)" if fail_share > .15 else "",
           f"~{rate:.0f} txs/hour" if rate > 20 else ""],
          "Sandwich attacks: buys just before other traders' swaps and sells straight after, "
          "so victims get a worse price. Adds fake-looking volume around every real buy.")
    arche("Arbitrage bot",
          50 * circ + 25 * jito_share + 25 * min(1, rate / 30),
          [f"{circ:.0%} of txs are circular (start and end in SOL/stables)" if circ > .1 else "",
           f"Jito tips in {jito_share:.0%} of txs" if jito_share > .1 else ""],
          "Mostly price-levelling between pools rather than manipulation; can mask where real demand is.")
    arche("Wash trader",
          45 * rt_share * (1 - jito_share) + 35 * equal_size * (rt > 2) + 20 * top_tok_share * (rt > 2),
          [f"{rt} quick buy→sell round trips (≤2 min)" if rt else "",
           f"Round-trip legs are {equal_size:.0%} similar in size" if pair_sizes else "",
           f"{top_tok_share:.0%} of swaps in one token" if top_tok_share > .5 and swaps else ""],
          "Wash trading: buying and selling to itself to inflate volume and trending rank, "
          "luring buyers with fake activity.")
    arche("Market maker / liquidity bot",
          35 * balance * (len(swaps) > 8) + 30 * lp + 20 * min(1, rate / 10) + 15 * (tokens_traded <= 5 and len(swaps) > 8),
          [f"Buys vs sells balanced ({buys}/{sells})" if balance > .6 and len(swaps) > 8 else "",
           f"Uses concentrated-liquidity pools in {lp:.0%} of txs" if lp > .1 else "",
           f"Focused on {tokens_traded} token(s)" if 0 < tokens_traded <= 5 else ""],
          "Legitimate MMs tighten spreads, but paid token MMs can 'paint the tape': walk price up to "
          "a target band, then distribute into the buyers it attracts (your 0.069-0.071 Vine pattern).")
    arche("Insider / dev distributor",
          45 * min(1, sells_after_in / 2) + 30 * (sells > 2 * max(buys, 1)) + 25 * min(1, to_cex / 3),
          [f"Received {len(ins)} token transfers (not bought) - {sells_after_in} later sold" if ins else "",
           f"Sells outnumber buys {sells}:{buys}" if sells > 2 * max(buys, 1) else "",
           f"{to_cex} transfers to known exchanges" if to_cex else ""],
          "Pump & dump / insider selling: gets supply cheaply or for free (allocation, dev wallet) "
          "and sells into retail demand, often after a coordinated push.")
    arche("Distribution / exit wallet",
          40 * min(1, len(out_cps) / 15) + 30 * (len(transfers) > len(swaps)) + 30 * min(1, to_cex / 2),
          [f"Sends to {len(out_cps)} different wallets" if len(out_cps) > 5 else "",
           f"More transfers ({len(transfers)}) than swaps ({len(swaps)})" if len(transfers) > len(swaps) else "",
           f"{to_cex} sends to exchanges" if to_cex else ""],
          "Splitting supply across many wallets (to hide concentration) or moving it to exchanges ahead of selling.")
    arche("Sniper", 0, [], "Buys in the first minutes of a launch, then dumps on the people who follow.")
    arche("DCA / automated buyer",
          90 * (prog.get("Jupiter DCA", 0) / n > .2) + 10 * (buys > 3 * max(sells, 1)),
          [f"Jupiter DCA in {prog.get('Jupiter DCA', 0)} txs" if prog.get("Jupiter DCA") else ""],
          "Not manipulative - slow scheduled buying/selling.")
    arche("Retail / manual trader",
          40 * (rate < 3) + 30 * (prog.get("Jupiter", 0) / n > .3) + 30 * (jito_share < .05) * (tokens_traded >= 3),
          [f"Low activity (~{rate:.1f} txs/hour)" if rate < 3 else "",
           f"Trades through Jupiter" if prog.get("Jupiter", 0) / n > .3 else "",
           f"{tokens_traded} different tokens traded" if tokens_traded >= 3 else ""],
          "No manipulation pattern - looks like a normal trader.")
    if n_sigs < 5:
        arche("Dormant / holder", 90, [f"Only {n_sigs} transactions found"], "Nothing to see - barely used.")
    ranked = sorted(A.values(), key=lambda a: -a["score"])
    stats = {"swaps": len(swaps), "buys": buys, "sells": sells, "transfers": len(transfers),
             "round_trips": rt, "same_block": same_slot, "jito_share": jito_share, "fail_share": fail_share,
             "tokens_traded": tokens_traded, "median_trade_usd": med_usd, "tx_per_hour": rate,
             "programs": prog.most_common(8)}
    return ranked, stats


def sniper_check(swaps):
    """Share of buys made within 10 min of the token's pool being created."""
    buys = [t for t in swaps if t["event"]["kind"] == "BUY"]
    if not buys:
        return 0, 0
    info = sc.token_info([t["event"]["mint"] for t in buys])
    early = 0
    for t in buys:
        c = (info.get(t["event"]["mint"]) or {}).get("created")
        if c and 0 <= t["ts"] - c / 1000 <= 600:
            early += 1
    return early, len(buys)


# ------------------------------------------------------------------ main ---
def profile(address, depth=40):
    hit = _cache.get((address, depth))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = {"address": address, "label": label_of(address), "generated": int(time.time())}

    acc = (sc.rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}]) or {}).get("value")
    if acc:
        parsed = (acc.get("data") or {}).get("parsed") if isinstance(acc.get("data"), dict) else None
        if acc.get("executable"):
            out.update(kind="program", summary=f"This is a program (smart contract){': ' + PROGRAMS[address] if address in PROGRAMS else ''}, not a wallet.")
            return out
        if parsed and parsed.get("type") == "mint":
            out.update(kind="token", summary="This is a token (mint address), not a wallet - use Screen token.")
            return out
        if acc.get("owner") not in (None, "11111111111111111111111111111111"):
            out["note"] = f"Account is owned by program {PROGRAMS.get(acc['owner'], acc['owner'])} - may be a PDA/vault rather than a person."
    out["kind"] = "wallet"

    sigs, complete = history_span(address)
    n_sigs = len(sigs)
    newest = sigs[0]["blockTime"] if sigs else None
    oldest = sigs[-1]["blockTime"] if sigs else None
    span_h = max(((newest or 0) - (oldest or 0)) / 3600, 1 / 60) if sigs else 0
    out["activity"] = {"tx_seen": n_sigs, "complete_history": complete, "first_seen": oldest if complete else None,
                       "oldest_scanned": oldest, "last_seen": newest}
    if complete and sigs:
        out["funder"] = funder(address, sigs[-1]["signature"])

    txs = []
    for s in sigs[:depth]:
        a = analyse_tx(sc.get_tx(s["signature"]), address)
        if a:
            txs.append(a)
    sc.enrich([t["event"] for t in txs if t["event"]])
    ranked, stats = classify(txs, n_sigs, span_h, None)
    swaps = [t for t in txs if not t["failed"] and t["event"] and t["event"]["kind"] in ("BUY", "SELL")]
    early, nb = sniper_check(swaps)
    if nb:
        sn = next(a for a in ranked if a["name"] == "Sniper")
        sn["score"] = round(100 * early / nb) if nb >= 2 else 0
        sn["evidence"] = [f"{early} of {nb} buys were within 10 min of the pool launching"] if early else []
        ranked.sort(key=lambda a: -a["score"])
    if out.get("label", "") and (out["label"] or "").endswith("hot wallet"):
        ranked.insert(0, {"name": "Exchange", "score": 100, "evidence": [out["label"]],
                          "manipulation": "Custodial exchange wallet - flows are many users, not one trader."})
    out["archetypes"] = ranked[:4]
    out["stats"] = stats

    cps = Counter()
    for t in txs:
        for c, k in t["counterparties"].items():
            cps[c] += 1
    out["counterparties"] = [{"address": c, "count": k, "label": label_of(c)}
                             for c, k in cps.most_common(40) if c != address and c not in JITO_TIPS][:15]
    out["linked_to_your_wallets"] = [c for c in out["counterparties"] if (c["label"] or "").startswith("Your list")]
    out["holdings"] = holdings(address)
    out["recent"] = [t["event"] for t in txs if t["event"]][:40]

    top = ranked[0] if ranked else None
    what = top["name"] if top and top["score"] >= 35 else "Unclear / mixed behaviour"
    conf = "high" if top and top["score"] >= 70 else "medium" if top and top["score"] >= 45 else "low"
    out["summary"] = (f"Most likely: {what} ({conf} confidence). "
                      f"Holds {sc_fmt(out['holdings']['total_usd'])} across {out['holdings']['token_count']} tokens + SOL. "
                      f"~{stats['tx_per_hour']:.1f} txs/hour over the scanned period.")
    out["manipulation"] = top["manipulation"] if top and top["score"] >= 35 else \
        "No strong manipulation pattern in the scanned transactions."
    _cache[(address, depth)] = (time.time(), out)
    return out


def sc_fmt(v):
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if v >= d:
            return f"${v / d:.2f}{s}"
    return f"${v:.2f}"
