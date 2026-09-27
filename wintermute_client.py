"""
Wintermute tracker - data + analysis layer for the /wintermute page.

Sources (all free tiers):
  Etherscan V2      wallet txs, token transfers, token balances   ETHERSCAN_API_KEY (optional - falls back to Blockscout, no key)
  CoinGecko         token prices, market caps, price/volume history   COINGECKO_API_KEY (optional demo key)
  GeckoTerminal     current DEX liquidity
  Binance data API  which symbols are already listed (spot)

Everything is cached in memory so the page doesn't burn API quota on refresh.
"""

import math
import os
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

ETHERSCAN = "https://api.etherscan.io/v2/api"
BLOCKSCOUT = "https://eth.blockscout.com/api"  # keyless fallback
COINGECKO = "https://api.coingecko.com/api/v3"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
BINANCE_INFO = "https://data-api.binance.vision/api/v3/exchangeInfo"  # not geo-blocked
CHAIN_ID = 1
BLOCK_SECONDS = 12

# Publicly labelled on Etherscan - extend as you find more.
WINTERMUTE_WALLETS = {
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080": "Wintermute: 0xdbf...080",
    "0x000002cba8dfb0a86a47a415592835e17fac080a": "Wintermute 2",
    "0x4f3a120e72c76c22ae802d129f599bfdbc31cb81": "Wintermute: Multisig",
    "0xf8191d98ae98d2f7abdfb63a9b0b812b93c873aa": "Wintermute 4",
}

# Add more without code changes: Railway variable WINTERMUTE_EXTRA_WALLETS
# e.g. "0xabc...:Meme bot 1,0xdef...:Meme bot 2" (label optional)
for _item in os.getenv("WINTERMUTE_EXTRA_WALLETS", "").split(","):
    _addr, _, _label = _item.strip().partition(":")
    if _addr.lower().startswith("0x") and len(_addr) == 42:
        WINTERMUTE_WALLETS[_addr.lower()] = _label.strip() or f"Extra {_addr[:6]}…{_addr[-4:]}"

# Tokens that are never "meme" side of a swap
MAJORS = {"ETH", "WETH", "WBTC", "CBBTC", "STETH", "WSTETH", "WEETH", "RETH"} | {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS"}

# Exchange hot wallets - verify labels on Etherscan before adding.
KNOWN_EXCHANGES = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance 14",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase 10",
}

STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS"}
LARGE_USD = 250_000          # a transfer is "large" at or above this USD value
SIGNIFICANT_HOLDING_USD = 100_000
LOW_MCAP_USD = 50_000_000    # "low market cap" flag on holdings


# ---------------------------------------------------------------- cache ----
_cache = {}


_key_locks = defaultdict(threading.Lock)


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _key_locks[key]:  # several tabs asking at once -> one fetch, the rest wait for it
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:
            if hit:  # API busy/down: serve the last good data instead of an error
                return hit[1]
            raise
        _cache[key] = (time.time(), val)
        return val


_es_lock = threading.Lock()  # one explorer request at a time across all page tabs


# ------------------------------------------------------------ API clients --
class Etherscan:
    def __init__(self, api_key=None, chain_id=CHAIN_ID):
        # No key -> Blockscout's free Etherscan-compatible API (no signup needed)
        self.key = api_key or os.getenv("ETHERSCAN_API_KEY")
        self.url = ETHERSCAN if self.key else BLOCKSCOUT
        self.name = "Etherscan" if self.key else "Blockscout"
        self.chain_id = chain_id
        self.s = requests.Session()

    def get(self, **params):
        if self.key:
            params.update(chainid=self.chain_id, apikey=self.key)
        gap = 0.22 if self.key else 0.6  # keyless Blockscout is stricter
        for attempt in range(6):
            with _es_lock:
                r = self.s.get(self.url, params=params, timeout=30)
                time.sleep(gap)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 20))
                continue
            r.raise_for_status()
            data = r.json()
            result = data.get("result")
            if data.get("status") == "1" or "jsonrpc" in data:
                return result
            if isinstance(result, str) and "rate limit" in result.lower():
                time.sleep(1 + attempt)
                continue
            msg = str(data.get("message", "")).lower()
            if msg.startswith("no ") and "found" in msg:  # Etherscan/Blockscout "no results"
                return []
            raise RuntimeError(f"{self.name}: {data.get('message')} - {result}")
        raise RuntimeError(f"{self.name} rate limit")

    def latest_block(self):
        return cached("latest_block", 30, self._latest_block)

    def _latest_block(self):
        if self.key:
            return int(self.get(module="proxy", action="eth_blockNumber"), 16)
        return int(self.get(module="block", action="eth_block_number"), 16)


class CoinGecko:
    def __init__(self):
        self.s = requests.Session()
        key = os.getenv("COINGECKO_API_KEY")
        if key:
            self.s.headers["x-cg-demo-api-key"] = key

    def get(self, path, **params):
        for attempt in range(3):
            try:
                r = self.s.get(f"{COINGECKO}{path}", params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2)
                continue
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if not r.ok:  # 400/401/404 on the free tier: treat as "no data", never crash the page
                return None
            return r.json()
        return None

    def token_prices(self, contracts):
        """contract -> {usd, usd_market_cap, usd_24h_vol, usd_24h_change} (via DexScreener, 30 per call)"""
        out = {}
        contracts = [c.lower() for c in contracts if c and c != "eth"]
        for i in range(0, len(contracts), 30):
            chunk = contracts[i:i + 30]
            try:
                r = requests.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=20)
                pairs = r.json().get("pairs") or []
            except Exception:
                continue
            best = {}
            for p in pairs:
                a = ((p.get("baseToken") or {}).get("address") or "").lower()
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a in chunk and p.get("chainId") == "ethereum" and liq >= 1000 and \
                        liq > ((best.get(a) or {}).get("liquidity") or {}).get("usd", 0):
                    best[a] = p
            for a, p in best.items():
                mc = p.get("marketCap") or p.get("fdv")
                out[a] = {"usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
                          "usd_market_cap": mc if mc and mc < 5e12 else None,
                          "usd_24h_vol": (p.get("volume") or {}).get("h24"),
                          "usd_24h_change": (p.get("priceChange") or {}).get("h24")}
        return out

    def eth_price(self):
        d = self.get("/simple/price", ids="ethereum", vs_currencies="usd") or {}
        return d.get("ethereum", {}).get("usd", 0)

    def token_info(self, contract):
        d = self.get(f"/coins/ethereum/contract/{contract}")
        if not d:
            return {}
        md = d.get("market_data", {})
        return {
            "name": d.get("name"),
            "symbol": (d.get("symbol") or "").upper(),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "market_cap": (md.get("market_cap") or {}).get("usd"),
            "price": (md.get("current_price") or {}).get("usd"),
        }

    def history(self, contract, t_from, t_to):
        d = self.get(f"/coins/ethereum/contract/{contract}/market_chart/range",
                     vs_currency="usd", **{"from": int(t_from), "to": int(t_to)})
        if not d:
            return [], []
        return d.get("prices", []), d.get("total_volumes", [])


def dex_liquidity(contract):
    try:
        r = requests.get(f"{GECKOTERMINAL}/networks/eth/tokens/{contract}", timeout=20)
        if r.ok:
            v = r.json()["data"]["attributes"].get("total_reserve_in_usd")
            return float(v) if v else None
    except Exception:
        pass
    return None


def binance_listed_bases():
    def fetch():
        try:
            r = requests.get(BINANCE_INFO, timeout=30)
            r.raise_for_status()
            return {s["baseAsset"].upper() for s in r.json()["symbols"]
                    if s.get("status") == "TRADING"}
        except Exception:
            return None
    return cached("binance_bases", 6 * 3600, fetch)


# ------------------------------------------------------------- analyzer ----
class WintermuteAnalyzer:
    def __init__(self, wallets=None):
        self.wallets = {k.lower(): v for k, v in (wallets or WINTERMUTE_WALLETS).items()}
        self.es = Etherscan()
        self.cg = CoinGecko()

    # ---------- raw transfers ----------
    def get_transactions_from_wallet(self, wallet, start_block, end_block):
        rows = []
        common = dict(module="account", address=wallet, startblock=start_block,
                      endblock=end_block, page=1, offset=10000, sort="desc")
        for tx in self.es.get(action="txlist", **common) or []:
            if tx.get("isError") == "1" or int(tx["value"]) == 0:
                continue
            rows.append(self._row(tx, "ETH", int(tx["value"]) / 1e18, wallet, "eth"))
        try:
            internal = self.es.get(action="txlistinternal", **common) or []
        except RuntimeError:
            internal = []
        for tx in internal:
            if tx.get("isError") == "1" or int(tx.get("value") or 0) == 0:
                continue
            rows.append(self._row(tx, "ETH", int(tx["value"]) / 1e18, wallet, "eth"))
        for tx in self.es.get(action="tokentx", **common) or []:
            dec = int(tx.get("tokenDecimal") or 0)
            amt = int(tx["value"]) / (10 ** dec) if dec else float(tx["value"])
            rows.append(self._row(tx, (tx.get("tokenSymbol") or "?").upper(), amt, wallet,
                                  tx["contractAddress"].lower(), dec))
        return rows

    def _row(self, tx, symbol, amount, wallet, contract, decimals=18):
        frm, to = tx["from"].lower(), tx["to"].lower()
        direction = "OUT" if frm == wallet else "IN"
        cp = to if direction == "OUT" else frm
        return {
            "ts": int(tx["timeStamp"]),
            "block": int(tx["blockNumber"]),
            "hash": tx["hash"],
            "wallet": wallet,
            "wallet_label": self.wallets.get(wallet, wallet),
            "direction": direction,
            "symbol": symbol,
            "contract": contract,
            "decimals": decimals,
            "amount": amount,
            "counterparty": cp,
            "counterparty_label": self.wallets.get(cp) or KNOWN_EXCHANGES.get(cp) or "",
            "internal": cp in self.wallets,
        }

    BASE_HOURS = 24 * 7

    def transfers(self, hours=24):
        """All transfers for all wallets over the last N hours, priced in USD.
        One 7-day download is shared by every window (6h/24h/3d/7d) and refreshed every 5 min."""
        span = max(hours, self.BASE_HOURS)

        def fetch():
            latest = self.es.latest_block()
            start = latest - int(span * 3600 / BLOCK_SECONDS)
            rows, errors = [], []
            for w in self.wallets:
                try:
                    rows += self.get_transactions_from_wallet(w, start, latest)
                except Exception as e:  # one wallet failing shouldn't blank the page
                    errors.append(f"{self.wallets.get(w, w)}: {e}")
            if errors and not rows:
                raise RuntimeError("; ".join(errors))
            self._price_rows(rows)
            rows.sort(key=lambda r: -r["ts"])
            return {"rows": rows, "latest_block": latest, "errors": errors}
        base = cached(f"transfers:{span}:{','.join(sorted(self.wallets))}", 300, fetch)
        cutoff = time.time() - hours * 3600
        return {**base, "rows": [r for r in base["rows"] if r["ts"] >= cutoff]}

    def _price_rows(self, rows):
        contracts = {r["contract"] for r in rows if r["contract"] != "eth"}
        prices = self.cg.token_prices(contracts) if contracts else {}
        eth = self.cg.eth_price()
        for r in rows:
            p = eth if r["contract"] == "eth" else (prices.get(r["contract"]) or {}).get("usd")
            r["price"] = p
            r["usd"] = r["amount"] * p if p else None
            md = prices.get(r["contract"]) or {}
            r["market_cap"] = md.get("usd_market_cap")

    # ---------- DEX swaps ----------
    def dex_trades(self, rows, max_mcap=2_000_000_000):
        """A swap = one wallet, one tx hash, token(s) out AND a different token in."""
        by_tx = defaultdict(list)
        for r in rows:
            if not r["internal"]:
                by_tx[(r["hash"], r["wallet"])].append(r)
        trades = []
        for (h, w), legs in by_tx.items():
            ins = [l for l in legs if l["direction"] == "IN"]
            outs = [l for l in legs if l["direction"] == "OUT"]
            if not ins or not outs:
                continue
            got, gave = max(ins, key=lambda l: l["usd"] or 0), max(outs, key=lambda l: l["usd"] or 0)
            if got["contract"] == gave["contract"]:
                continue
            got_major, gave_major = got["symbol"] in MAJORS, gave["symbol"] in MAJORS
            if got_major and gave_major:
                continue  # ETH<->stable etc, not a meme trade
            if not got_major and (gave_major or (got["market_cap"] or 0) <= (gave["market_cap"] or 0)):
                side, meme, other = "BUY", got, gave
            else:
                side, meme, other = "SELL", gave, got
            mcap = meme["market_cap"]
            if mcap is not None and mcap > max_mcap:
                continue
            usd = meme["usd"] or other["usd"]
            trades.append({
                "ts": meme["ts"], "hash": h, "wallet": w, "wallet_label": meme["wallet_label"],
                "side": side, "symbol": meme["symbol"], "contract": meme["contract"],
                "amount": meme["amount"], "paid_symbol": other["symbol"], "paid_amount": other["amount"],
                "usd": usd, "price": (usd / meme["amount"]) if usd and meme["amount"] else meme["price"],
                "market_cap": mcap, "venue": meme["counterparty"],
            })
        trades.sort(key=lambda t: -t["ts"])
        per_token = defaultdict(lambda: {"symbol": "", "contract": "", "buys": 0, "sells": 0,
                                         "buy_usd": 0.0, "sell_usd": 0.0, "market_cap": None})
        for t in trades:
            p = per_token[t["contract"]]
            p.update(symbol=t["symbol"], contract=t["contract"], market_cap=t["market_cap"])
            k = "buy" if t["side"] == "BUY" else "sell"
            p[k + "s"] += 1
            p[k + "_usd"] += t["usd"] or 0
        summary = sorted(per_token.values(), key=lambda p: -(p["buy_usd"] + p["sell_usd"]))
        for p in summary:
            p["net_usd"] = p["buy_usd"] - p["sell_usd"]
        return {"trades": trades[:300], "tokens": summary[:50]}

    # ---------- flows summary ----------
    def flow_summary(self, rows):
        net = defaultdict(lambda: {"symbol": "", "contract": "", "net": 0.0, "net_usd": 0.0,
                                   "in": 0, "out": 0})
        cex = defaultdict(float)
        large = []
        for r in rows:
            if r["internal"]:
                continue
            n = net[r["contract"]]
            n["symbol"], n["contract"] = r["symbol"], r["contract"]
            sign = 1 if r["direction"] == "IN" else -1
            n["net"] += sign * r["amount"]
            n["net_usd"] += sign * (r["usd"] or 0)
            n["in" if sign > 0 else "out"] += 1
            ex = KNOWN_EXCHANGES.get(r["counterparty"])
            if ex:
                cex[(ex, r["symbol"], r["direction"])] += r["usd"] or 0
            if (r["usd"] or 0) >= LARGE_USD:
                large.append(r)
        return {
            "net_flows": sorted(net.values(), key=lambda x: -abs(x["net_usd"]))[:30],
            "exchange_flows": [{"exchange": k[0], "symbol": k[1], "direction": k[2], "usd": v}
                               for k, v in sorted(cex.items(), key=lambda x: -x[1])],
            "large_transfers": sorted(large, key=lambda r: -(r["usd"] or 0))[:25],
        }

    # ---------- holdings ----------
    def analyze_token_holdings(self, min_usd=SIGNIFICANT_HOLDING_USD, history_hours=24 * 7):
        """Current balances for every token the wallets touched in the lookback window."""
        def fetch():
            rows = self.transfers(history_hours)["rows"]
            # rank tokens by activity; cap balance lookups to protect the rate limit
            activity = defaultdict(int)
            meta = {}
            for r in rows:
                if r["contract"] != "eth":
                    activity[r["contract"]] += 1
                    meta[r["contract"]] = r["symbol"]
            contracts = [c for c, _ in sorted(activity.items(), key=lambda x: -x[1])[:60]]
            prices = self.cg.token_prices(contracts)
            eth_px = self.cg.eth_price()
            decimals = {r["contract"]: r["decimals"] for r in rows}

            bal = defaultdict(float)
            for w in self.wallets:
                try:
                    wei = int(self.es.get(module="account", action="balance", address=w, tag="latest"))
                    bal["eth"] += wei / 1e18
                    if not self.es.key:  # Blockscout: every token balance in one call
                        for t in self.es.get(module="account", action="tokenlist", address=w) or []:
                            if t.get("type") not in (None, "ERC-20"):
                                continue
                            c = (t.get("contractAddress") or "").lower()
                            dec = int(t.get("decimals") or 18)
                            bal[c] += int(t.get("balance") or 0) / (10 ** dec)
                            meta.setdefault(c, (t.get("symbol") or "?").upper())
                            decimals.setdefault(c, dec)
                    else:  # Etherscan free tier: one call per token, so cap it
                        for c in contracts[:15]:
                            raw = int(self.es.get(module="account", action="tokenbalance",
                                                  contractaddress=c, address=w, tag="latest") or 0)
                            bal[c] += raw / (10 ** decimals.get(c, 18))
                except Exception as e:
                    print(f"holdings: {w} failed: {e}")
            missing = [c for c in bal if c != "eth" and c not in prices][:120]
            prices.update(self.cg.token_prices(missing))

            holdings = []
            for c, amt in bal.items():
                if amt <= 0:
                    continue
                if c == "eth":
                    px, mcap, sym = eth_px, None, "ETH"
                else:
                    p = prices.get(c) or {}
                    px, mcap, sym = p.get("usd"), p.get("usd_market_cap"), meta.get(c, "?")
                if not px:
                    continue
                holdings.append({
                    "symbol": sym, "contract": c, "amount": amt, "price": px,
                    "usd_value": amt * px, "market_cap": mcap or None,
                    "pct_of_mcap": (amt * px / mcap * 100) if mcap else None,
                })
            return holdings
        all_h = cached(f"holdings:{history_hours}", 900, fetch)

        significant = sorted([h for h in all_h if h["usd_value"] > min_usd],
                             key=lambda h: -h["usd_value"])
        low_mcap = [h for h in significant
                    if h["market_cap"] and h["market_cap"] < LOW_MCAP_USD
                    and h["symbol"] not in STABLES]
        return {
            "top_holdings": significant[:20],
            "total_value": sum(h["usd_value"] for h in significant),
            "low_market_cap_tokens": low_mcap,
            "note": "Balances cover tokens these wallets moved in the last 7 days.",
        }

    # ---------- market impact ----------
    def calculate_market_impact(self, contract, hours=24 * 7, after_hours=24):
        """Price / volume / liquidity effect around Wintermute's transfers of one token."""
        contract = contract.lower()

        def fetch():
            rows = [r for r in self.transfers(hours)["rows"]
                    if r["contract"] == contract and not r["internal"]]
            if not rows:
                return {"error": "No Wintermute transfers of this token in the window"}
            first = min(r["ts"] for r in rows)
            last = max(r["ts"] for r in rows)
            now = time.time()
            end = min(last + after_hours * 3600, now)
            prices, vols = self.cg.history(contract, first - 24 * 3600, end)
            if len(prices) < 2:
                return {"error": "No CoinGecko price history for this token"}

            def at(series, t, before=True):
                pts = [p for p in series if (p[0] / 1000 <= t if before else p[0] / 1000 >= t)]
                if not pts:
                    return None
                return (pts[-1] if before else pts[0])[1]

            p_before = at(prices, first)
            p_after = prices[-1][1]
            v_before = at(vols, first)
            v_after = vols[-1][1] if vols else None
            wm_usd = sum(r["usd"] or 0 for r in rows)
            wm_net_usd = sum((r["usd"] or 0) * (1 if r["direction"] == "IN" else -1) for r in rows)
            liq = dex_liquidity(contract)
            span_days = max((last - first) / 86400, 1)
            return {
                "symbol": rows[0]["symbol"],
                "contract": contract,
                "transfers": len(rows),
                "window": [first, int(end)],
                "price_before": p_before,
                "price_after": p_after,
                "price_impact_percent": ((p_after - p_before) / p_before * 100) if p_before else None,
                "volume_impact_percent": ((v_after - v_before) / v_before * 100)
                if v_before and v_after else None,
                "wintermute_share_of_volume_percent": (wm_usd / span_days / v_before * 100)
                if v_before else None,
                "wintermute_gross_usd": wm_usd,
                "wintermute_net_usd": wm_net_usd,
                "dex_liquidity_usd": liq,
                "liquidity_impact_percent": (abs(wm_net_usd) / liq * 100) if liq else None,
                "chart": [[int(p[0] / 1000), p[1]] for p in prices],
                "markers": [{"ts": r["ts"], "direction": r["direction"], "usd": r["usd"]}
                            for r in rows],
                "caveat": "Correlation only - price moves have many causes besides these transfers.",
            }
        return cached(f"impact:{contract}:{hours}", 900, fetch)

    # ---------- risk ----------
    def assess_risk(self, contract, position_hint_usd=None):
        """Risk factors for a token, plus mechanical sizing / stop outputs."""
        contract = contract.lower()

        def fetch():
            now = time.time()
            prices, _ = self.cg.history(contract, now - 14 * 86400, now)
            info = self.cg.token_info(contract)
            liq = dex_liquidity(contract)
            px = [p[1] for p in prices]
            rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
            hourly_vol = statistics.pstdev(rets) if len(rets) > 5 else None
            daily_vol = hourly_vol * math.sqrt(24) if hourly_vol else None
            return {"info": info, "liq": liq, "daily_vol": daily_vol,
                    "price": px[-1] if px else info.get("price")}
        d = cached(f"risk:{contract}", 900, fetch)
        mcap, liq, dvol = d["info"].get("market_cap"), d["liq"], d["daily_vol"]

        def band(v, lo, hi, invert=False):
            # 0 = low risk ... 100 = high risk
            if v is None:
                return 70
            x = (math.log10(v) - lo) / (hi - lo) if v > 0 else 0
            x = min(max(x, 0), 1)
            return round((x if invert else 1 - x) * 100)

        factors = {
            "market_cap": band(mcap, 6, 10),         # $1M -> 100, $10B -> 0
            "liquidity": band(liq, 5, 8),             # $100k -> 100, $100M -> 0
            "volatility": round(min((dvol or 0.15) / 0.20, 1) * 100),  # 20%/day -> 100
            "wintermute_position_size": band(position_hint_usd / mcap * 100
                                             if position_hint_usd and mcap else None,
                                             -2, 1, invert=True),  # >10% of mcap -> 100
        }
        overall = round(0.3 * factors["market_cap"] + 0.3 * factors["liquidity"]
                        + 0.25 * factors["volatility"] + 0.15 * factors["wintermute_position_size"])
        level = ("LOW" if overall < 35 else "MEDIUM" if overall < 55
                 else "HIGH" if overall < 75 else "EXTREME")
        stop_pct = round(min(max(2 * (dvol or 0.1) * 100, 5), 40), 1)
        max_pos = (liq * 0.005) if liq else None  # stay under 0.5% of pool liquidity
        return {
            "factors": factors,
            "overall_risk": overall,
            "risk_level": level,
            "daily_volatility_percent": round(dvol * 100, 2) if dvol else None,
            "stop_loss_percent": stop_pct,
            "max_position_usd_by_liquidity": max_pos,
            "price": d["price"],
            "market_cap": mcap,
            "dex_liquidity_usd": liq,
        }

    # ---------- signals ----------
    def generate_signals(self, max_mcap=5_000_000, hours=24 * 7):
        """Tokens Wintermute is accumulating below a market-cap ceiling (per the spec)."""
        patterns = PatternRecognition().run(self.transfers(hours)["rows"])
        out = []
        for p in patterns["accumulation"]:
            mcap = p.get("market_cap")
            if not mcap or mcap >= max_mcap or p["symbol"] in STABLES:
                continue
            risk = self.assess_risk(p["contract"], position_hint_usd=p["net_usd"])
            price = risk["price"]
            stop = risk["stop_loss_percent"]
            out.append({
                "symbol": p["symbol"], "contract": p["contract"],
                "strategy": "follow_accumulation",
                "pattern_score": p["score"],
                "reference_price": price,
                "stop_price": price * (1 - stop / 100) if price else None,
                "target_price": price * (1 + 2 * stop / 100) if price else None,  # 2:1 R:R
                "risk": risk,
            })
        return sorted(out, key=lambda s: -s["pattern_score"])

    # ---------- listing watch ----------
    def listing_watch(self):
        """Low-cap holdings not on Binance spot, with Wintermute holding >1% and accumulating."""
        listed = binance_listed_bases()
        holdings = self.analyze_token_holdings()["top_holdings"]
        acc = {p["contract"]: p for p in
               PatternRecognition().run(self.transfers(24 * 7)["rows"])["accumulation"]}
        out = []
        for h in holdings:
            if h["symbol"] in STABLES or h["contract"] == "eth":
                continue
            on_binance = (h["symbol"] in listed) if listed is not None else None
            if on_binance or h["contract"] not in acc:
                continue  # only look up supply (slow CoinGecko call) for tokens being accumulated
            info = cached(f"cginfo:{h['contract']}", 3600, lambda c=h["contract"]: self.cg.token_info(c))
            supply = info.get("circulating_supply") or info.get("total_supply")
            pct = h["amount"] / supply * 100 if supply else None
            p = acc.get(h["contract"])
            score = 0
            score += min(pct or 0, 10) * 5           # up to 50 for share of supply
            score += (p["score"] * 0.4) if p else 0   # up to 40 for accumulation strength
            score += 10 if h["usd_value"] > 1_000_000 else 0
            if (pct or 0) > 1 and p:
                out.append({
                    "symbol": h["symbol"], "contract": h["contract"],
                    "holding_pct_of_supply": pct, "usd_value": h["usd_value"],
                    "accumulation_score": p["score"],
                    "heuristic_score": round(min(score, 100)),
                    "binance_check": "not listed" if on_binance is False else "unknown",
                })
        return {
            "candidates": sorted(out, key=lambda x: -x["heuristic_score"]),
            "note": ("Heuristic score, not a calibrated probability. There is no public "
                     "dataset to back-test Wintermute holdings against Binance listings, "
                     "so no timeframe is estimated."),
        }

    # ---------- live ----------
    def live(self, since_block):
        latest = self.es.latest_block()
        if since_block >= latest:
            return {"latest_block": latest, "rows": []}
        start = max(since_block + 1, latest - 300)
        rows = []
        for w in self.wallets:
            rows += self.get_transactions_from_wallet(w, start, latest)
        self._price_rows(rows)
        return {"latest_block": latest, "rows": sorted(rows, key=lambda r: -r["ts"])}


# ---------------------------------------------------- pattern recognition --
class PatternRecognition:
    """Accumulation / distribution per token from daily external net flows."""

    def __init__(self, min_days=2):
        self.min_days = min_days
        self.accumulation_patterns = []
        self.distribution_patterns = []

    def _daily(self, rows):
        by_token = defaultdict(lambda: defaultdict(lambda: {"net": 0.0, "net_usd": 0.0,
                                                             "buys": [], "sells": [], "hours": []}))
        meta = {}
        for r in rows:
            if r["internal"]:
                continue
            day = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d")
            d = by_token[r["contract"]][day]
            sign = 1 if r["direction"] == "IN" else -1
            d["net"] += sign * r["amount"]
            d["net_usd"] += sign * (r["usd"] or 0)
            (d["buys"] if sign > 0 else d["sells"]).append(r["amount"])
            d["hours"].append(datetime.fromtimestamp(r["ts"], timezone.utc).hour)
            meta[r["contract"]] = (r["symbol"], r.get("market_cap"))
        return by_token, meta

    @staticmethod
    def _slope(xs):
        n = len(xs)
        if n < 2:
            return 0.0
        mx, my = (n - 1) / 2, sum(xs) / n
        den = sum((i - mx) ** 2 for i in range(n))
        return sum((i - mx) * (x - my) for i, x in enumerate(xs)) / den if den else 0.0

    def _score(self, token, days, sign, meta):
        ordered = [days[k] for k in sorted(days)]
        nets = [d["net"] * sign for d in ordered]
        pos_days = sum(1 for n in nets if n > 0)
        if pos_days < self.min_days:
            return None
        consistency = pos_days / len(nets)                       # share of days in direction
        sizes = [x for d in ordered for x in (d["buys"] if sign > 0 else d["sells"])]
        growth = self._slope(sizes) / (statistics.mean(sizes) or 1) if len(sizes) > 2 else 0
        hours = [h for d in ordered for h in d["hours"]]
        top_hour_share = (max(hours.count(h) for h in set(hours)) / len(hours)) if hours else 0
        net_usd = sum(d["net_usd"] for d in ordered) * sign
        if net_usd <= 0:
            return None
        score = (50 * consistency + 25 * min(max(growth * 5, 0), 1)
                 + 25 * min(math.log10(max(net_usd, 1)) / 7, 1))
        sym, mcap = meta[token]
        return {
            "symbol": sym, "contract": token, "market_cap": mcap,
            "days_active": len(nets), "days_in_direction": pos_days,
            "consistency": round(consistency, 2),
            "size_trend": "increasing" if growth > 0.05 else "decreasing" if growth < -0.05 else "flat",
            "peak_hour_utc": max(set(hours), key=hours.count) if hours else None,
            "peak_hour_share": round(top_hour_share, 2),
            "net_usd": net_usd,
            "score": round(score),
        }

    def detect_accumulation(self, rows):
        by_token, meta = self._daily(rows)
        self.accumulation_patterns = sorted(
            filter(None, (self._score(t, d, 1, meta) for t, d in by_token.items())),
            key=lambda p: -p["score"])
        return self.accumulation_patterns

    def detect_distribution(self, rows):
        by_token, meta = self._daily(rows)
        self.distribution_patterns = sorted(
            filter(None, (self._score(t, d, -1, meta) for t, d in by_token.items())),
            key=lambda p: -p["score"])
        return self.distribution_patterns

    def run(self, rows):
        return {"accumulation": self.detect_accumulation(rows),
                "distribution": self.detect_distribution(rows)}


# ------------------------------------------------------------- warmer -----
_warm_started = False


def start_warmer(interval=240):
    """Keep the shared 7-day data fresh in the background so page loads are instant."""
    global _warm_started
    if _warm_started:
        return
    _warm_started = True

    def loop():
        a = None
        while True:
            try:
                a = a or WintermuteAnalyzer()
                a.transfers(24)
                a.analyze_token_holdings()
            except Exception as e:
                print(f"wintermute warmer: {e}")
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="wintermute-warmer").start()
