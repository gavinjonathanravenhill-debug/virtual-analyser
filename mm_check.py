import statistics, time, requests
from collections import defaultdict

GT_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json"}
TIMEOUT = 12

CHAIN_HINT_ALIASES = {
    "eth": "eth", "ethereum": "eth",
    "base": "base", "base-protocol": "base",
    "bsc": "bsc", "binance-smart-chain": "bsc", "binance": "bsc",
    "solana": "solana",
    "polygon": "polygon_pos", "polygon-pos": "polygon_pos",
    "arbitrum": "arbitrum", "arbitrum-one": "arbitrum",
    "optimism": "optimism", "avax": "avax", "avalanche": "avax",
}

def _f(v, default=0.0):
    try: return float(v)
    except: return default

def _search_pools(query):
    r = requests.get(f"{GT_BASE}/search/pools", params={"query": query}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("data", [])

def _pool_network(pool, token_address):
    rel = (pool.get("relationships") or {}).get("network", {}).get("data", {}) or {}
    if rel.get("id"): return rel["id"]
    pool_id = pool.get("id", "")
    pool_addr = (pool.get("attributes") or {}).get("address", "")
    if pool_addr and pool_id.lower().endswith("_" + pool_addr.lower()):
        return pool_id[:-(len(pool_addr) + 1)]
    return None

def _matches_token(pool, token_address):
    rel = pool.get("relationships") or {}
    token_lc = token_address.lower()
    for side in ("base_token", "quote_token"):
        tid = (rel.get(side) or {}).get("data", {}).get("id", "")
        if tid and tid.lower().endswith("_" + token_lc): return True
    return not rel

def _get_trades(network, pool_address):
    r = requests.get(f"{GT_BASE}/networks/{network}/pools/{pool_address}/trades", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("data", [])

def _ts(iso):
    try: return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except: return None

def _cv(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2: return None
    mean = sum(vals) / len(vals)
    if mean == 0: return None
    return statistics.pstdev(vals) / mean

def _score_liquidity(attrs, pools_found):
    reserve = _f(attrs.get("reserve_in_usd"))
    vol_h24 = _f((attrs.get("volume_usd") or {}).get("h24"))
    chg_h1  = abs(_f((attrs.get("price_change_percentage") or {}).get("h1")))
    tx_h24  = (attrs.get("transactions") or {}).get("h24") or {}
    buys, sells = tx_h24.get("buys") or 0, tx_h24.get("sells") or 0

    if reserve >= 500_000: depth = 30
    elif reserve >= 100_000: depth = 22
    elif reserve >= 25_000: depth = 12
    elif reserve >= 5_000: depth = 5
    else: depth = 0

    turnover = (vol_h24 / reserve) if reserve > 0 else 0
    if 0.2 <= turnover <= 4: turn_score = 25
    elif 0.05 <= turnover < 0.2 or 4 < turnover <= 10: turn_score = 14
    elif turnover > 10: turn_score = 6
    else: turn_score = 3

    balance = (min(buys, sells) / max(buys, sells)) if max(buys, sells) > 0 else 0
    bal_score = round(25 * balance)
    stab_score = max(0, round(20 - chg_h1)) if chg_h1 <= 20 else 0

    score = min(100, depth + turn_score + bal_score + stab_score)
    return score, {"reserve_usd": round(reserve,2), "volume_h24_usd": round(vol_h24,2),
                   "turnover_ratio": round(turnover,3), "buy_sell_balance": round(balance,3),
                   "price_change_h1_abs": round(chg_h1,2), "pools_found": pools_found}

def _score_bot_pattern(trades):
    parsed = []
    for t in trades:
        a = t.get("attributes", {}) or {}
        ts = _ts(a.get("block_timestamp", ""))
        wallet = a.get("tx_from_address")
        kind = a.get("kind")
        if ts is not None and wallet and kind in ("buy", "sell"):
            parsed.append({"ts": ts, "vol": _f(a.get("volume_in_usd")), "wallet": wallet.lower(), "kind": kind})
    n = len(parsed)
    if n < 5: return None, {"trades_analysed": n, "confidence": "insufficient"}

    parsed.sort(key=lambda x: x["ts"])
    by_wallet = defaultdict(lambda: {"buy": 0, "sell": 0, "vols": []})
    for p in parsed:
        by_wallet[p["wallet"]][p["kind"]] += 1
        by_wallet[p["wallet"]]["vols"].append(p["vol"])

    distinct_wallets = len(by_wallet)
    top_wallets = sorted(by_wallet.items(), key=lambda kv: kv[1]["buy"]+kv[1]["sell"], reverse=True)[:5]
    two_sided = sum(1 for _, w in top_wallets if w["buy"] > 0 and w["sell"] > 0)
    two_sided_ratio = two_sided / max(1, min(5, distinct_wallets))

    busiest_wallet, busiest_data = top_wallets[0]
    busiest_trades = [p for p in parsed if p["wallet"] == busiest_wallet]
    gaps = [b["ts"]-a["ts"] for a,b in zip(busiest_trades, busiest_trades[1:])]
    timing_cv = _cv(gaps) if len(gaps) >= 2 else None
    timing_score = max(0.0, 1 - min((timing_cv if timing_cv is not None else 3)/3, 1))
    size_cv = _cv(busiest_data["vols"]) if len(busiest_data["vols"]) >= 2 else None
    size_score = max(0.0, 1 - min((size_cv if size_cv is not None else 2)/2, 1))

    score = min(100, round(50*two_sided_ratio + 30*timing_score + 20*size_score))
    confidence = "high" if n >= 60 else "medium" if n >= 20 else "low"
    return score, {"trades_analysed": n, "distinct_wallets": distinct_wallets,
                   "busiest_wallet": busiest_wallet[:6]+"…"+busiest_wallet[-4:],
                   "busiest_wallet_trade_share": round((busiest_data["buy"]+busiest_data["sell"])/n,3),
                   "two_sided_top_wallets_ratio": round(two_sided_ratio,3),
                   "timing_cv": round(timing_cv,3) if timing_cv else None,
                   "size_cv": round(size_cv,3) if size_cv else None,
                   "confidence": confidence}

def _verdict(score, low_confidence):
    if score is None:
        return ("NO POOLS FOUND","r","GeckoTerminal has no DEX pool for this address — may be CEX-only, unlisted, or wrong address/chain.")
    if score >= 70: label,tone,sub = "ACTIVE MM SIGNATURE","g","Depth and trading behaviour look consistent with an active market maker."
    elif score >= 45: label,tone,sub = "WEAK MM SIGNAL","y","Some supportive liquidity/behaviour but not conclusive either way."
    else: label,tone,sub = "NO MM DETECTED","r","Thin liquidity and/or one-sided irregular flow — likely no dedicated market maker."
    if low_confidence: sub += " ⚠ Low trade count — treat as low-confidence."
    return label, tone, sub

def analyse_market_maker(token_address, chain_hint=""):
    token_address = token_address.strip()
    hint = CHAIN_HINT_ALIASES.get((chain_hint or "").lower(), "")
    pools = [p for p in _search_pools(token_address) if _matches_token(p, token_address)]

    if not pools:
        label,tone,sub = _verdict(None, False)
        return {"address": token_address, "pools_found": 0, "mm_score": None,
                "liquidity_score": None, "bot_score": None,
                "verdict": label, "verdict_tone": tone, "verdict_sub": sub,
                "flags": [], "metrics": {},
                "disclaimer": "Heuristic from public on-chain data only — not proof of an active MM contract."}

    def sort_key(p):
        reserve = _f((p.get("attributes") or {}).get("reserve_in_usd"))
        net = _pool_network(p, token_address) or ""
        return reserve * (1.15 if hint and net == hint else 1.0)
    pools.sort(key=sort_key, reverse=True)

    primary = pools[0]
    primary_attrs = primary.get("attributes", {}) or {}
    network = _pool_network(primary, token_address) or hint or "eth"
    total_liquidity = sum(_f((p.get("attributes") or {}).get("reserve_in_usd")) for p in pools[:10])
    liquidity_score, liq_metrics = _score_liquidity(primary_attrs, len(pools))

    try: trades = _get_trades(network, primary_attrs.get("address", ""))
    except Exception: trades = []

    bot_score, bot_metrics = _score_bot_pattern(trades)
    low_confidence = bot_metrics.get("confidence") in ("insufficient", "low")
    mm_score = liquidity_score if bot_score is None else round(0.5*liquidity_score + 0.5*bot_score)
    label, tone, sub = _verdict(mm_score, low_confidence)

    flags = []
    if liq_metrics["reserve_usd"] >= 100_000: flags.append({"text":f"DEEP LIQUIDITY (${liq_metrics['reserve_usd']:,.0f})","tone":"g"})
    elif liq_metrics["reserve_usd"] < 10_000: flags.append({"text":f"THIN LIQUIDITY (${liq_metrics['reserve_usd']:,.0f})","tone":"r"})
    if liq_metrics["buy_sell_balance"] >= 0.7: flags.append({"text":"BALANCED BUY/SELL FLOW","tone":"g"})
    elif liq_metrics["buy_sell_balance"] < 0.3: flags.append({"text":"ONE-SIDED FLOW","tone":"r"})
    if bot_score is not None:
        if bot_metrics["two_sided_top_wallets_ratio"] >= 0.4: flags.append({"text":"REPEAT TWO-SIDED WALLETS","tone":"g"})
        else: flags.append({"text":"NO REPEAT TWO-SIDED WALLETS","tone":"y"})
    if len(pools) > 3: flags.append({"text":f"LIQUIDITY SPLIT ACROSS {len(pools)} POOLS","tone":"y"})
    if not flags: flags.append({"text":"NO MAJOR SIGNALS EITHER WAY","tone":"y"})

    return {"address": token_address, "network": network,
            "pool_address": primary_attrs.get("address"),
            "pool_name": primary_attrs.get("name"),
            "pools_found": len(pools), "total_liquidity_usd": round(total_liquidity,2),
            "mm_score": mm_score, "liquidity_score": liquidity_score, "bot_score": bot_score,
            "verdict": label, "verdict_tone": tone, "verdict_sub": sub,
            "flags": flags, "metrics": {**liq_metrics, **bot_metrics},
            "disclaimer": "Heuristic from public on-chain data only (GeckoTerminal) — not proof of an active MM contract."}
