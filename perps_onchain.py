"""On-chain wallet forensics for a MEXC perp (/api/perps/onchain).

A perp is just a price feed - the wallets are on the coin's DEX pools. This:
  1. finds the coin's on-chain token (DexScreener search by symbol, biggest-liquidity match; you can override it)
  2. builds the Bubblemaps link for it
  3. runs the same spike forensics as the Migrated scanner (migrated.analyse): spikes + pattern (pump & dump / held...),
     wallets that loaded in the hour before the spike, wallets that sold into it, the ones that did both (operators /
     insiders), bot-like wallets, wash-trade share and single-wallet dominance, plus a 0-100 manipulation score.
Limits: GeckoTerminal only gives the pool's last ~6h of 1-min candles and last ~300 trades, so older spikes and
older wallets are missing. And a perp can move on MEXC with nothing happening on-chain.
"""
import time

import requests

import migrated as mg

# DexScreener chainId -> (GeckoTerminal network, Bubblemaps chain, explorer address URL)
NETS = {
    "solana": ("solana", "solana", "https://solscan.io/account/"),
    "ethereum": ("eth", "eth", "https://etherscan.io/address/"),
    "bsc": ("bsc", "bsc", "https://bscscan.com/address/"),
    "base": ("base", "base", "https://basescan.org/address/"),
    "arbitrum": ("arbitrum", "arbitrum", "https://arbiscan.io/address/"),
    "polygon": ("polygon_pos", "polygon", "https://polygonscan.com/address/"),
    "avalanche": ("avax", "avalanche", "https://snowtrace.io/address/"),
    "sonic": ("sonic", "sonic", "https://sonicscan.org/address/"),
    "optimism": ("optimism", "optimism", "https://optimistic.etherscan.io/address/"),
    "robinhood": ("robinhood", "robinhood", "https://robin.etherscan.io/address/"),
    "hyperevm": ("hyperevm", "hyperevm", "https://hyperevmscan.io/address/"),
    "tron": ("tron", "tron", "https://tronscan.org/#/address/"),
    "ton": ("ton", "ton", "https://tonviewer.com/"),
}
_s = requests.Session()
_cache = {}


def bubblemaps_url(chain, address):
    bm = (NETS.get(chain) or (None, None))[1]
    return f"https://v2.bubblemaps.io/map?address={address}&chain={bm}" if bm else None


def _sym(perp):
    b = perp.split("_", 1)[0].upper()
    for p in ("1000000", "10000", "1000", "1M"):
        if b.startswith(p) and len(b) > len(p) + 1:
            return b[len(p):]
    return b


def resolve(perp):
    """Candidate on-chain tokens for a perp symbol, best (most liquidity) first."""
    sym = _sym(perp)
    r = _s.get("https://api.dexscreener.com/latest/dex/search", params={"q": sym}, timeout=15)
    r.raise_for_status()
    agg = {}
    for p in r.json().get("pairs") or []:
        b = p.get("baseToken") or {}
        if (b.get("symbol") or "").upper().lstrip("$") != sym or p.get("chainId") not in NETS:
            continue
        k = (p["chainId"], b.get("address"))
        a = agg.setdefault(k, {"chain": p["chainId"], "address": b.get("address"), "name": b.get("name"),
                               "symbol": b.get("symbol"), "liq": 0.0, "vol_24h": 0.0, "pairs": 0,
                               "price": p.get("priceUsd"), "mcap": p.get("marketCap") or p.get("fdv"),
                               "dex_url": p.get("url")})
        a["liq"] += float((p.get("liquidity") or {}).get("usd") or 0)
        a["vol_24h"] += float((p.get("volume") or {}).get("h24") or 0)
        a["pairs"] += 1
    return sorted(agg.values(), key=lambda x: -(x["liq"] + x["vol_24h"] / 10))


def _explorer(chain, w):
    return (NETS.get(chain) or (None, None, None))[2] + w if NETS.get(chain) and w else None


def onchain(perp, chain=None, address=None):
    key = (perp, chain, address)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    cands = resolve(perp) if not address else []
    if address:
        tok = {"chain": chain or "solana", "address": address, "symbol": _sym(perp), "name": None}
    elif cands:
        tok = cands[0]
    else:
        return {"perp": perp, "error": f"No on-chain token called {_sym(perp)} found on DexScreener - paste the contract",
                "candidates": []}
    gnet = NETS[tok["chain"]][0]
    ck = "x-" + gnet                  # private key so the Migrated scanner / chain pages never pick it up
    mg.CHAINS.setdefault(ck, {"gt": gnet, "name": tok["chain"], "dex_key": None, "fill_dexes": ()})
    out = {"perp": perp, "token": tok, "candidates": cands[:6], "bubblemaps": bubblemaps_url(tok["chain"], tok["address"]),
           "dexscreener": tok.get("dex_url") or f"https://dexscreener.com/{tok['chain']}/{tok['address']}"}
    try:
        an = mg.analyse(mint=tok["address"], interactive=True, chain=ck)
    except Exception as e:
        out["error"] = f"Wallet analysis failed: {e}"
        _cache[key] = (time.time(), out)
        return out
    f = an.get("forensics") or {}
    ex = lambda w: _explorer(tok["chain"], w)
    sp = an.get("spike")
    ops = set(f.get("operators") or [])
    pre = f.get("pre_buyers") or []
    out.update({
        "pool": an.get("pool"),
        "manipulation": an.get("manipulation"), "manipulation_parts": an.get("manipulation_parts"),
        "spike": ({"start": sp["start"], "end": sp["end"], "vol_x": sp["vol_x"], "dir": sp["dir"],
                   "pattern": an.get("pattern"), "run_pct": an.get("run_pct"), "giveback_pct": an.get("giveback_pct")}
                  if sp else None),
        "spike_count": len(an.get("spikes") or []),
        "insiders": [{**x, "url": ex(x["wallet"])} for x in pre if x["wallet"] in ops] +
                    [{"wallet": w, "usd": None, "sold_into": None, "bot": None, "url": ex(w)}
                     for w in ops if w not in {x["wallet"] for x in pre}],
        "pre_buyers": [{**x, "url": ex(x["wallet"])} for x in pre if x["wallet"] not in ops],
        "sold_into": [{**x, "url": ex(x["wallet"])} for x in f.get("sold_into") or [] if x["wallet"] not in ops],
        "bots": [{**x, "url": ex(x["wallet"])} for x in f.get("bots") or []],
        "top_wallets": [{**x, "url": ex(x["wallet"])} for x in f.get("top_wallets") or []],
        "bot_pct": f.get("bot_pct"), "wash_pct": f.get("wash_pct"), "top_wallet_pct": f.get("top_wallet_pct"),
        "trades_seen": f.get("trades_seen"), "window": f.get("window"),
    })
    out["verdict"] = verdict(out)
    _cache[key] = (time.time(), out)
    return out


def verdict(o):
    """Plain-English lines, worst first."""
    v = []
    sp = o.get("spike")
    if sp:
        v.append(f"{'Pump' if sp['dir'] == 'up' else 'Dump'} spike: {sp['vol_x']:.0f}x normal volume"
                 + (f", ran {sp['run_pct']:+.0f}%" if sp.get("run_pct") is not None else "")
                 + (f", pattern {sp['pattern']}" if sp.get("pattern") else "")
                 + (f" (gave back {sp['giveback_pct']:.0f}%)" if sp.get("giveback_pct") is not None else ""))
    else:
        v.append("No volume spike in the pool's recent candles")
    if o["insiders"]:
        v.append(f"{len(o['insiders'])} wallet(s) bought in the hour before the spike AND sold into it - the likely operators")
    if o["pre_buyers"]:
        v.append(f"{len(o['pre_buyers'])} more wallet(s) loaded in the hour before (still holding or sold later)")
    if (o.get("bot_pct") or 0) > 40:
        v.append(f"Bots: {o['bot_pct']:.0f}% of volume is bot-like wallets")
    if (o.get("wash_pct") or 0) > 20:
        v.append(f"Wash trading: ~{o['wash_pct']:.0f}% of volume is wallets buying and selling to themselves")
    if (o.get("top_wallet_pct") or 0) > 15:
        v.append(f"One wallet is {o['top_wallet_pct']:.0f}% of all volume")
    return v
