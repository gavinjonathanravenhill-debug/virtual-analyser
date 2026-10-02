"""Listings radar (/listings).

Watches for new tokens arriving on the big exchanges and alerts on Telegram the moment they show up:
  * announcements - Binance (new listings), OKX (new listings), Bybit (new crypto), Upbit (new trading support)
  * new markets   - Coinbase spot products, MEXC perps, Bybit perps, Binance perps (diffed against what we've seen)
Each new token is checked on CoinGecko for price, market cap, FDV and float (circulating / max supply), with a
plain verdict: a low float + high FDV listing usually bleeds as unlocks hit, so it's a fade rather than a chase.
Plus a manual watchlist of upcoming TGEs / listings; it's ticked off automatically when the ticker gets listed.

The first run of each source is a silent baseline (stored, shown, not alerted) so restarts don't spam Telegram.
"""
import json
import os
import re
import threading
import time
from datetime import datetime

import requests

import edge
import solana_signals as sig

SCAN_SECONDS = int(os.getenv("LISTINGS_SECONDS", "90"))
CG = "https://api.coingecko.com/api/v3"
CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (virtual-analyser listings radar)", "Accept": "application/json"}
QUOTES = {"USDT", "USDC", "USD", "USD1", "FDUSD", "BTC", "ETH", "BNB", "TRY", "EUR", "KRW", "USDE", "BUSD", "DAI", "PERP"}
NOT_TICKERS = QUOTES | {"NEW", "LISTING", "SPOT", "THE", "AND", "FOR", "WITH", "WILL", "HODLER", "ALPHA", "API",
                        "OKX", "BYBIT", "BINANCE", "UPBIT", "MEXC", "UTC", "TGE", "AMA", "VIP", "APR", "KYC", "CEO",
                        "ON", "IN", "TO", "OF", "UP", "X", "NFT", "DEX", "CEX", "EVM", "ID", "AI"}

edge.DEFAULTS.update({
    "lst_alerts": True,            # Telegram alerts for new listings
    "lst_quiet_markets": False,    # True = only alert announcements, not raw new markets
})
edge._set_cache[1] = None

_s = requests.Session()
_s.headers.update(UA)
_status = {"last": None, "errors": {}, "running": False}
_lock = threading.Lock()

SEED_WATCH = [
    ("Concrete", "CT", "", "Listed 30 Sep (Binance Alpha), OKX spot 2 Oct. Coinbase deposits open - alert when Coinbase "
     "trading goes live. 28% investors / 22% team - watch unlocks.", "https://www.coingecko.com/en/coins/concrete"),
    ("Ethos Network", "", "2026-10-08", "Token unlock 8 Oct", ""),
    ("Soul Labs", "SO", "2026-10-19", "TGE ~19 Oct (mainnet)", ""),
    ("Ritual", "RITUAL", "2026-10", "TGE October, date TBC - raised $25M", ""),
    ("City Protocol", "CT", "2026-10", "TGE October, date TBC - SAME TICKER AS CONCRETE, check the contract", ""),
    ("Catapult", "PULT", "2026-10", "TGE October, date TBC", ""),
]


# ------------------------------------------------------------------ DB ----
def init_tables():
    with sig._db_lock, sig.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY, source TEXT, kind TEXT, ltype TEXT, title TEXT, tickers TEXT, url TEXT,
            ts INTEGER, seen_at INTEGER, alerted INTEGER, info TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS listings_ts ON listings (ts)")
        c.execute("CREATE TABLE IF NOT EXISTS market_seen (source TEXT, symbol TEXT, PRIMARY KEY (source, symbol))")
        c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ticker TEXT, date TEXT, note TEXT, url TEXT,
            created INTEGER, listed TEXT)""")
        if not c.execute("SELECT 1 FROM settings WHERE key='_lst_seeded'").fetchone():
            for n, t, d, note, u in SEED_WATCH:
                c.execute("INSERT INTO watchlist (name, ticker, date, note, url, created, listed) VALUES (?,?,?,?,?,?,NULL)",
                          (n, t, d, note, u, int(time.time())))
            c.execute("INSERT OR REPLACE INTO settings VALUES ('_lst_seeded', '1')")


def _has_rows(source):
    with sig._db_lock, sig.db() as c:
        return bool(c.execute("SELECT 1 FROM listings WHERE source=? LIMIT 1", (source,)).fetchone())


# ------------------------------------------------------------- parsing ----
def tickers_in(title):
    """Pull ticker symbols out of an announcement title."""
    found = []
    for pat in (r"\(([A-Z0-9][A-Z0-9.]{1,14})\)",                          # "Concrete (CT)"
                r"\b([A-Z0-9]{2,15})[/-](?:USDT|USDC|USD1?|FDUSD|TRY|EUR|KRW|BTC)\b",   # "GRVT/USD", "CT-USD"
                r"\b(?:1000)?([A-Z0-9]{2,15}?)(?:USDT|USDC|PERP)\b",            # "XYZUSDT Perpetual"
                r"(?:Pre-Market|Listing|Launchpool|Pre-Launch|Alpha):\s*\$?([A-Z0-9]{2,12})(?=[\s,]|$)"):  # "Pre-Market: XYZ"
        for t in re.findall(pat, title or ""):
            t = t.upper().strip(".")
            if any(t.endswith(q) and len(t) > len(q) + 1 for q in ("USDT", "USDC", "PERP")):
                continue
            if t not in NOT_TICKERS and not t.isdigit() and t not in found:
                found.append(t)
    return found[:4]


def ltype_of(source, title):
    t = (title or "").lower()
    if any(k in t for k in ("delist", "removal", "will remove", "거래지원 종료", "cease")):
        return "delist"
    if "alpha" in t:
        return "alpha"
    if "hodler" in t or "airdrop" in t:
        return "airdrop"
    if "launchpool" in t or "launchpad" in t or "megadrop" in t:
        return "launchpool"
    if "pre-market" in t or "premarket" in t or "pre-launch" in t:
        return "pre-market"
    if any(k in t for k in ("perpetual", "futures", "perp", "contract")):
        return "perp"
    if any(k in t for k in ("margin", "earn", "convert", "loan", "copy trading")):
        return "other"
    return "spot"


# ----------------------------------------------------- announcement feeds ----
def _j(url, **params):
    r = _s.get(url, params=params or None, timeout=15)
    r.raise_for_status()
    return r.json()


def feed_binance():
    d = _j("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
           type=1, catalogId=48, pageNo=1, pageSize=20)
    out = []
    for cat in (d.get("data") or {}).get("catalogs") or []:
        for a in cat.get("articles") or []:
            out.append({"key": str(a.get("id") or a.get("code")), "title": a.get("title"),
                        "url": f"https://www.binance.com/en/support/announcement/{a.get('code')}",
                        "ts": int((a.get("releaseDate") or time.time() * 1000) / 1000)})
    return out


def feed_okx():
    d = _j("https://www.okx.com/api/v5/support/announcements", annType="announcements-new-listings")
    data = d.get("data") or []
    items = []
    for blk in data if isinstance(data, list) else [data]:
        items += (blk.get("details") if isinstance(blk, dict) and "details" in blk else [blk])
    return [{"key": x.get("url") or x.get("title"), "title": x.get("title"), "url": x.get("url"),
             "ts": int(int(x.get("pTime") or x.get("businessPTime") or time.time() * 1000) / 1000)}
            for x in items if isinstance(x, dict) and x.get("title")]


def feed_bybit():
    d = _j("https://api.bybit.com/v5/announcements/index", locale="en-US", type="new_crypto", limit=20)
    return [{"key": x.get("url") or x.get("title"), "title": x.get("title"), "url": x.get("url"),
             "ts": int((x.get("publishTime") or x.get("dateTimestamp") or time.time() * 1000) / 1000)}
            for x in (d.get("result") or {}).get("list") or []]


def feed_upbit():
    d = _j("https://api-manager.upbit.com/api/v1/announcements", os="web", page=1, per_page=20, category="trade")
    out = []
    for x in (d.get("data") or {}).get("notices") or []:
        title = x.get("title") or ""
        if "종료" in title or "유의" in title:          # trading ends / caution flag - not a listing
            continue
        if not any(k in title for k in ("신규", "추가", "거래지원", "listing", "Listing", "Market Support")):
            continue
        ts = x.get("listed_at") or x.get("first_listed_at")
        try:
            from datetime import datetime
            ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) if ts else int(time.time())
        except Exception:
            ts = int(time.time())
        out.append({"key": str(x.get("id")), "title": title, "ts": ts,
                    "url": f"https://upbit.com/service_center/notice?id={x.get('id')}"})
    return out


ANNOUNCE = {"Binance": feed_binance, "OKX": feed_okx, "Bybit": feed_bybit, "Upbit": feed_upbit}


# ------------------------------------------------------------ market feeds ----
def mk_coinbase():
    rows = _j("https://api.exchange.coinbase.com/products")
    out = {}
    for p in rows or []:
        base = (p.get("base_currency") or "").upper()
        if not base:
            continue
        live = p.get("status") == "online" and not p.get("trading_disabled") and not p.get("auction_mode")
        cur = out.get(base)
        if cur is None or (live and not cur["live"]):
            out[base] = {"symbol": base, "pair": p.get("id"), "live": live,
                         "url": f"https://www.coinbase.com/advanced-trade/spot/{p.get('id')}"}
    return out


def mk_mexc_perps():
    d = _j("https://contract.mexc.com/api/v1/contract/detail")
    return {x["symbol"]: {"symbol": x["symbol"], "base": x["symbol"].rsplit("_", 1)[0], "live": True,
                          "url": f"https://futures.mexc.com/exchange/{x['symbol']}"}
            for x in d.get("data") or [] if x.get("symbol", "").endswith("_USDT")}


def mk_bybit_perps():
    d = _j("https://api.bybit.com/v5/market/instruments-info", category="linear", limit=1000)
    return {x["symbol"]: {"symbol": x["symbol"], "base": x.get("baseCoin"), "live": x.get("status") == "Trading",
                          "url": f"https://www.bybit.com/trade/usdt/{x['symbol']}"}
            for x in (d.get("result") or {}).get("list") or [] if x.get("quoteCoin") == "USDT"}


def mk_binance_perps():
    d = _j("https://fapi.binance.com/fapi/v1/exchangeInfo")
    return {x["symbol"]: {"symbol": x["symbol"], "base": x.get("baseAsset"), "live": x.get("status") == "TRADING",
                          "url": f"https://www.binance.com/en/futures/{x['symbol']}"}
            for x in d.get("symbols") or [] if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"}


MARKETS = {"Coinbase": ("spot", mk_coinbase), "MEXC perps": ("perp", mk_mexc_perps),
           "Bybit perps": ("perp", mk_bybit_perps), "Binance perps": ("perp", mk_binance_perps)}


# ------------------------------------------------------------ enrichment ----
_cg_cache = {}


def _cg(path, **params):
    h = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
    r = _s.get(CG + path, params=params or None, headers=h, timeout=15)
    if r.status_code == 429:
        raise RuntimeError("CoinGecko rate limit")
    r.raise_for_status()
    return r.json()


def token_info(ticker):
    """Price / market cap / FDV / float for a ticker from CoinGecko (best-ranked coin with that symbol). Cached 1h."""
    t = ticker.upper()
    hit = _cg_cache.get(t)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    coins = [c for c in (_cg("/search", query=t).get("coins") or []) if (c.get("symbol") or "").upper() == t]
    if not coins:
        info = {"ticker": t, "found": False}
    else:
        coins.sort(key=lambda c: c.get("market_cap_rank") or 10 ** 9)
        c = coins[0]
        d = _cg(f"/coins/{c['id']}", localization="false", tickers="false", community_data="false",
                developer_data="false", sparkline="false")
        md = d.get("market_data") or {}
        g = lambda k: (md.get(k) or {}).get("usd") if isinstance(md.get(k), dict) else md.get(k)
        circ, mx, tot = md.get("circulating_supply"), md.get("max_supply"), md.get("total_supply")
        denom = mx or tot
        info = {"ticker": t, "found": True, "id": c["id"], "name": d.get("name"), "price": g("current_price"),
                "chg_24h": md.get("price_change_percentage_24h"), "mcap": g("market_cap"),
                "fdv": g("fully_diluted_valuation"), "circ": circ, "supply": denom,
                "float_pct": (circ / denom * 100) if circ and denom else None,
                "ath": g("ath"), "atl": g("atl"), "others": len(coins) - 1,
                "url": f"https://www.coingecko.com/en/coins/{c['id']}"}
        info["verdict"], info["vclass"] = verdict(info)
    _cg_cache[t] = (time.time(), info)
    return info


def verdict(i):
    f, mc, fdv = i.get("float_pct"), i.get("mcap") or 0, i.get("fdv") or 0
    ratio = fdv / mc if mc else None
    if f is not None and f <= 20 and (ratio is None or ratio >= 4):
        return "LOW FLOAT / HIGH FDV - unlocks will sell into it; usually a fade, not a chase", "bad"
    if f is not None and f < 30:
        return "Low float - check the unlock schedule before holding", "warn"
    if not mc and fdv:
        return "No circulating data - treat the float as unknown", "warn"
    if f is not None:
        return "Float OK - most of the supply is already out", "ok"
    return "Not enough supply data", "warn"


def links(ticker, info=None):
    t = ticker.upper()
    out = [("DexScreener", f"https://dexscreener.com/search?q={t}"),
           ("MEXC perp", f"https://futures.mexc.com/exchange/{t}_USDT"),
           ("Unlocks", "https://tokenomist.ai/")]
    if info and info.get("url"):
        out.insert(0, ("CoinGecko", info["url"]))
    return out


# ------------------------------------------------------------------ scan ----
def _store(source, kind, ltype, title, tickers, url, ts, key, alert):
    lid = f"{source}:{key}"
    with sig._db_lock, sig.db() as c:
        if c.execute("SELECT 1 FROM listings WHERE id=?", (lid,)).fetchone():
            return None
        c.execute("INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (lid, source, kind, ltype, title, json.dumps(tickers), url, int(ts), int(time.time()), 0, None))
    return {"id": lid, "source": source, "kind": kind, "ltype": ltype, "title": title, "tickers": tickers,
            "url": url, "ts": ts, "alert": alert}


def _enrich(item):
    info = {}
    for t in item["tickers"][:2]:
        try:
            info[t] = token_info(t)
        except Exception as e:
            info[t] = {"ticker": t, "found": False, "error": str(e)[:80]}
    with sig._db_lock, sig.db() as c:
        c.execute("UPDATE listings SET info=? WHERE id=?", (json.dumps(info), item["id"]))
    return info


def _watch_hits(item):
    hits = []
    with sig._db_lock, sig.db() as c:
        for w in c.execute("SELECT * FROM watchlist WHERE ticker!=''"):
            if w["ticker"].upper() in item["tickers"]:
                hits.append(dict(w))
                listed = (w["listed"] + "; " if w["listed"] else "") + f"{item['source']} {item['ltype']} " \
                         f"{datetime.fromtimestamp(item['ts'], edge.TZ):%d %b %H:%M}"
                c.execute("UPDATE watchlist SET listed=? WHERE id=?", (listed[-400:], w["id"]))
    return hits


def _alert(item, info, hits):
    s = edge.settings()
    if not s.get("lst_alerts") or not item["alert"]:
        return
    if item["kind"] == "market" and s.get("lst_quiet_markets") and not hits:
        return
    big = item["source"] in ("Binance", "Upbit", "Coinbase") and item["ltype"] in ("spot", "airdrop", "launchpool")
    icon = "📌" if hits else "🚨" if big else "🆕"
    lines = [f"{icon} <b>{item['source']} {item['ltype'].upper()}</b>: {item['title']}"]
    for t, i in info.items():
        if i.get("found"):
            lines.append(f"<b>{t}</b> {i.get('name')} · ${i['price']:.6g}" if i.get("price") else f"<b>{t}</b> {i.get('name')}")
            lines.append(f"mcap {sig.fmt_usd(i.get('mcap'))} · FDV {sig.fmt_usd(i.get('fdv'))} · float "
                         f"{'?' if i.get('float_pct') is None else format(i['float_pct'], '.0f') + '%'}"
                         + (f" · 24h {i['chg_24h']:+.0f}%" if i.get("chg_24h") is not None else ""))
            lines.append(i["verdict"] + (f" (+{i['others']} other coins share this ticker)" if i.get("others") else ""))
        else:
            lines.append(f"<b>{t}</b> not on CoinGecko yet - brand new")
    if hits:
        lines.append("On your watchlist: " + ", ".join(h["name"] for h in hits))
    if item.get("url"):
        lines.append(f'<a href="{item["url"]}">{"announcement" if item["kind"] == "announce" else "open market"}</a>')
    edge.dispatch("\n".join(lines), prio="high" if (big or hits) else "normal", group="Listings")
    with sig._db_lock, sig.db() as c:
        c.execute("UPDATE listings SET alerted=1 WHERE id=?", (item["id"],))


def scan():
    """One pass over every source. Returns the new items."""
    if not _lock.acquire(blocking=False):
        return []
    try:
        init_tables()
        new = []
        for src, fn in ANNOUNCE.items():
            try:
                first = not _has_rows(src)
                for a in fn():
                    lt = ltype_of(src, a["title"])
                    if lt == "delist":
                        continue
                    it = _store(src, "announce", lt, a["title"], tickers_in(a["title"]), a.get("url"), a["ts"],
                                a["key"], alert=not first and time.time() - a["ts"] < 6 * 3600)
                    if it:
                        new.append(it)
                _status["errors"].pop(src, None)
            except Exception as e:
                _status["errors"][src] = str(e)[:150]
        for src, (lt, fn) in MARKETS.items():
            try:
                cur = fn()
                with sig._db_lock, sig.db() as c:
                    seen = {r["symbol"] for r in c.execute("SELECT symbol FROM market_seen WHERE source=?", (src,))}
                    first = not seen
                    fresh = [m for k, m in cur.items() if k not in seen]
                    c.executemany("INSERT OR IGNORE INTO market_seen VALUES (?,?)", [(src, k) for k in cur])
                if not first:
                    for m in fresh:
                        base = (m.get("base") or m["symbol"]).upper()
                        base = base[4:] if base.startswith("1000") and len(base) > 5 else base
                        title = (f"New {src.replace(' perps', '')} perp: {m['symbol']}" + ("" if m["live"] else " (pre-launch)")
                                 if lt == "perp" else
                                 f"New Coinbase market {m.get('pair')} ({'trading live' if m['live'] else 'listed - trading not live yet'})")
                        it = _store(src, "market", lt, title, [base], m.get("url"), time.time(), m["symbol"], alert=True)
                        if it:
                            new.append(it)
                # Coinbase: an existing product switching ON (e.g. CT deposits open -> trading live)
                if src == "Coinbase" and not first:
                    for k, m in cur.items():
                        if m["live"] and k in seen:
                            _coinbase_went_live(k, m, new)
                    _cb_state.update({k: m["live"] for k, m in cur.items()})
                elif src == "Coinbase":
                    _cb_state.update({k: m["live"] for k, m in cur.items()})
                _status["errors"].pop(src, None)
            except Exception as e:
                _status["errors"][src] = str(e)[:150]
        for it in new[:12]:
            info = _enrich(it) if it["tickers"] else {}
            hits = _watch_hits(it)
            _alert(it, info, hits)
        _status["last"] = int(time.time())
        return new
    finally:
        _lock.release()


_cb_state = {}


def _coinbase_went_live(base, m, new):
    if _cb_state.get(base) is False:            # was listed but not trading last pass, now trading
        it = _store("Coinbase", "market", "spot", f"Coinbase trading now LIVE: {m.get('pair')}", [base], m.get("url"),
                    time.time(), f"{m['symbol']}:live", alert=True)
        if it:
            new.append(it)


# ------------------------------------------------------------------- API ----
def feed(hours=72, limit=200):
    init_tables()
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM listings WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                                           (int(time.time() - hours * 3600), limit))]
        watch = [dict(r) for r in c.execute("SELECT * FROM watchlist ORDER BY COALESCE(NULLIF(date,''),'9999'), id")]
    for r in rows:
        r["tickers"] = json.loads(r["tickers"] or "[]")
        r["info"] = json.loads(r["info"]) if r["info"] else {}
        r["links"] = links(r["tickers"][0], r["info"].get(r["tickers"][0])) if r["tickers"] else []
    return {"rows": rows, "watchlist": watch, "status": {**_status, "seconds": SCAN_SECONDS},
            "settings": {k: edge.settings().get(k) for k in ("lst_alerts", "lst_quiet_markets")}}


def enrich_missing(limit=6):
    """Fill CoinGecko data for rows that were stored before enrichment (e.g. baseline rows) - on demand."""
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute("SELECT id, tickers FROM listings WHERE info IS NULL AND tickers!='[]' "
                                           "ORDER BY ts DESC LIMIT ?", (limit,))]
    for r in rows:
        _enrich({"id": r["id"], "tickers": json.loads(r["tickers"])})
    return len(rows)


def watch_add(d):
    init_tables()
    with sig._db_lock, sig.db() as c:
        c.execute("INSERT INTO watchlist (name, ticker, date, note, url, created, listed) VALUES (?,?,?,?,?,?,NULL)",
                  ((d.get("name") or "").strip()[:80], (d.get("ticker") or "").strip().upper()[:15],
                   (d.get("date") or "").strip()[:20], (d.get("note") or "").strip()[:300],
                   (d.get("url") or "").strip()[:300], int(time.time())))


def watch_remove(wid):
    with sig._db_lock, sig.db() as c:
        return c.execute("DELETE FROM watchlist WHERE id=?", (int(wid),)).rowcount


_started = False


def start():
    global _started
    if _started or os.getenv("LISTINGS_OFF"):
        return
    _started = True
    init_tables()

    def loop():
        time.sleep(15)
        while True:
            _status["running"] = True
            try:
                scan()
                enrich_missing(3)
            except Exception as e:
                _status["errors"]["loop"] = str(e)[:150]
            _status["running"] = False
            time.sleep(SCAN_SECONDS)
    threading.Thread(target=loop, daemon=True, name="listings").start()
