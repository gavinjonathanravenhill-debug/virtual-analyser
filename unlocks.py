"""Token unlocks (shown on /listings).

Upcoming vesting/cliff unlocks, ranked by how big they are against the coin's market cap - an unlock worth several %
of mcap is supply that can be sold into the market, so these are classic short / fade setups around the date.

Sources:
  * Tokenomist API (https://api.tokenomist.ai/v5/unlock/events/upcoming) when TOKENOMIST_API_KEY is set - synced
    every 6h for the next 30 days. Paid plan; without a key the table runs on the manual list.
  * Manual entries you add on the page (plus a seed list for this week).
Each unlock is enriched with CoinGecko price/mcap (shared cache with the listings radar) and the MEXC perp
(funding, OI, 24h move) so you can see how it's positioned going in.
Telegram: 24h and 1h before any MEDIUM/HIGH impact unlock (group "Unlocks").
"""
import os
import threading
import time
from datetime import datetime, timezone

import requests

import edge
import solana_signals as sig

TOKENOMIST_KEY = os.getenv("TOKENOMIST_API_KEY", "").strip()
SYNC_SECONDS = 6 * 3600
LOOP_SECONDS = 600
HIGH_PCT, MED_PCT = 2.0, 0.5      # unlock value as % of market cap

edge.DEFAULTS.update({"unl_alerts": True})
edge._set_cache[1] = None

# name, ticker, date (UTC, YYYY-MM-DD or YYYY-MM-DDTHH:MM), tokens, % of supply, allocation, note
SEED = [
    ("Ethena", "ENA", "2026-10-05", 171.88e6, 1.88, "", "~$41.5M"),
    ("Hyperliquid", "HYPE", "2026-10-06", 3.75e6, None, "",
     "~$340M - reportedly all going to one institutional buyer, so may not hit the market"),
    ("Ethos Network", "", "2026-10-08", None, None, "", "Token unlock 8 Oct (size TBC)"),
    ("Aptos", "APT", "2026-10-11", 11.31e6, 0.64, "", "~$9M"),
    ("Aerodrome", "AERO", "", None, None, "", "Unlock week of 5-11 Oct, date TBC"),
    ("Movement", "MOVE", "", None, None, "", "Unlock week of 5-11 Oct, date TBC"),
    ("Babylon", "BABY", "", None, None, "", "Unlock week of 5-11 Oct, date TBC"),
]

_status = {"last_sync": None, "last_enrich": None, "errors": {}}
_info = {}        # ticker -> {"cg": {...}, "perp": {...}, "t": ts}


# ------------------------------------------------------------------ DB ----
def _ts(date_txt):
    if not date_txt:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(date_txt[:20], fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    return None


def init_tables():
    with sig._db_lock, sig.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS unlocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ext_id TEXT UNIQUE, name TEXT, ticker TEXT, ts INTEGER,
            date_txt TEXT, amount REAL, value_usd REAL, pct_supply REAL, alloc TEXT, note TEXT, source TEXT,
            url TEXT, created INTEGER, a24 INTEGER DEFAULT 0, a1 INTEGER DEFAULT 0)""")
        if not c.execute("SELECT 1 FROM settings WHERE key='_unl_seeded'").fetchone():
            for n, t, d, amt, pct, alloc, note in SEED:
                c.execute("""INSERT OR IGNORE INTO unlocks (ext_id, name, ticker, ts, date_txt, amount, pct_supply,
                             alloc, note, source, url, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (f"seed:{n}:{d}", n, t, _ts(d), d, amt, pct, alloc, note, "manual", "", int(time.time())))
            c.execute("INSERT OR REPLACE INTO settings VALUES ('_unl_seeded', '1')")


# --------------------------------------------------------------- sources ----
def sync_tokenomist(days=30):
    """Pull upcoming cliff unlocks from Tokenomist into the table. Needs TOKENOMIST_API_KEY."""
    if not TOKENOMIST_KEY:
        return 0
    start = datetime.now(timezone.utc)
    end = datetime.fromtimestamp(start.timestamp() + days * 86400, timezone.utc)
    n, page = 0, 1
    while page <= 5:
        r = requests.get("https://api.tokenomist.ai/v5/unlock/events/upcoming",
                         headers={"x-api-key": TOKENOMIST_KEY},
                         params={"start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}", "page": page,
                                 "pageSize": 100, "minValueToMarketCap": 0.1}, timeout=20)
        r.raise_for_status()
        j = r.json()
        for d in j.get("data") or []:
            ev = d.get("upcomingEvent") or {}
            cl = ev.get("cliffUnlocks") or {}
            date = ev.get("unlockDate") or ""
            ts = _ts(date)
            if not ts or not cl.get("cliffAmount"):
                continue
            allocs = sorted({a.get("standardAllocationName") or a.get("allocationName") or ""
                             for a in cl.get("allocationBreakdown") or []} - {""})
            with sig._db_lock, sig.db() as c:
                c.execute("""INSERT INTO unlocks (ext_id, name, ticker, ts, date_txt, amount, value_usd, pct_supply,
                             alloc, note, source, url, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(ext_id) DO UPDATE SET amount=excluded.amount, value_usd=excluded.value_usd,
                             alloc=excluded.alloc""",
                          (f"tkm:{d.get('tokenId')}:{ts}", d.get("tokenName"), (d.get("tokenSymbol") or "").upper(), ts,
                           date[:16], cl.get("cliffAmount"), cl.get("cliffValue"), None, ", ".join(allocs)[:200], "",
                           "tokenomist", f"https://tokenomist.ai/{d.get('tokenId')}", int(time.time())))
            n += 1
        meta = j.get("metadata") or {}
        if page >= (meta.get("totalPages") or 1):
            break
        page += 1
    return n


def _perps():
    """MEXC perp snapshot keyed by base ticker (reuses the perps scanner's cached ticker call)."""
    import perps_scanner as ps
    sizes = ps.contract_sizes()
    out = {}
    for t in ps.tickers():
        sym = t.get("symbol") or ""
        if not sym.endswith("_USDT"):
            continue
        p = ps._f(t.get("lastPrice"))
        out[sym[:-5]] = {"symbol": sym, "price": p, "chg_24h": ps._f(t.get("riseFallRate")) * 100,
                         "funding_pct": ps._f(t.get("fundingRate")) * 100, "vol_24h": ps._f(t.get("amount24")),
                         "oi_usd": ps._f(t.get("holdVol")) * sizes.get(sym, 1.0) * p}
    return out


def enrich(horizon_days=21):
    """Refresh CoinGecko + MEXC perp data for tickers unlocking soon. CoinGecko calls are cached 1h by listings."""
    import listings
    now = time.time()
    with sig._db_lock, sig.db() as c:
        ticks = {r["ticker"] for r in c.execute(
            "SELECT DISTINCT ticker FROM unlocks WHERE ticker!='' AND (ts IS NULL OR (ts>? AND ts<?))",
            (now - 86400, now + horizon_days * 86400))}
    try:
        perps = _perps()
        _status["errors"].pop("mexc", None)
    except Exception as e:
        perps = {}
        _status["errors"]["mexc"] = str(e)[:120]
    for t in ticks:
        cur = _info.get(t) or {}
        cg = cur.get("cg")
        if not cg or now - cur.get("t", 0) > 3600:
            try:
                cg = listings.token_info(t)
            except Exception as e:
                cg = cg or {"ticker": t, "found": False, "error": str(e)[:80]}
            time.sleep(1.5)       # stay under the free CoinGecko rate limit
        _info[t] = {"cg": cg, "perp": perps.get(t), "t": now if cg and cg.get("found") else cur.get("t", 0)}
    _status["last_enrich"] = int(now)


# ------------------------------------------------------------------ view ----
def _row(r):
    r = dict(r)
    inf = _info.get(r["ticker"]) or {}
    cg, perp = inf.get("cg") or {}, inf.get("perp")
    price = cg.get("price") or (perp or {}).get("price")
    value = r["amount"] * price if (r["amount"] and price) else r["value_usd"]
    mcap = cg.get("mcap")
    pct_mcap = value / mcap * 100 if (value and mcap) else None
    pct_supply = r["pct_supply"]
    if pct_supply is None and r["amount"] and cg.get("supply"):
        pct_supply = r["amount"] / cg["supply"] * 100
    impact = ("HIGH" if pct_mcap >= HIGH_PCT else "MED" if pct_mcap >= MED_PCT else "LOW") if pct_mcap is not None else "?"
    r.update(price=price, value=value, mcap=mcap, pct_mcap=pct_mcap, pct_supply=pct_supply, impact=impact,
             cg_name=cg.get("name"), cg_url=cg.get("url"), float_pct=cg.get("float_pct"), chg_24h=cg.get("chg_24h"),
             perp=perp)
    return r


def feed(days=30):
    init_tables()
    now = time.time()
    with sig._db_lock, sig.db() as c:
        rows = [_row(r) for r in c.execute(
            "SELECT * FROM unlocks WHERE ts IS NULL OR (ts>? AND ts<?) ORDER BY ts IS NULL, ts",
            (now - 2 * 86400, now + days * 86400))]
    return {"rows": rows, "status": {**_status, "tokenomist": bool(TOKENOMIST_KEY)},
            "settings": {"unl_alerts": edge.settings().get("unl_alerts")}}


def _num(v):
    """'171.88M' / '3,750,000' / '1.2k' -> float; blank -> None."""
    v = str(v or "").strip().upper().replace(",", "").replace("$", "")
    if not v:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(v[-1], 1)
    return float(v[:-1] if mult != 1 else v) * mult


def add(d):
    init_tables()
    date = (d.get("date") or "").strip()[:16]
    with sig._db_lock, sig.db() as c:
        c.execute("""INSERT INTO unlocks (ext_id, name, ticker, ts, date_txt, amount, pct_supply, alloc, note, source,
                     url, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (f"man:{time.time()}", (d.get("name") or "").strip()[:80], (d.get("ticker") or "").strip().upper()[:15],
                   _ts(date), date, _num(d.get("amount")), _num(d.get("pct_supply")),
                   (d.get("alloc") or "").strip()[:120], (d.get("note") or "").strip()[:300], "manual",
                   (d.get("url") or "").strip()[:300], int(time.time())))


def remove(uid):
    with sig._db_lock, sig.db() as c:
        return c.execute("DELETE FROM unlocks WHERE id=?", (int(uid),)).rowcount


# ---------------------------------------------------------------- alerts ----
def _fmt_perp(p):
    if not p:
        return "no MEXC perp"
    return (f"MEXC perp: funding {p['funding_pct']:+.4f}% · OI {sig.fmt_usd(p['oi_usd'])} · "
            f"24h {p['chg_24h']:+.1f}%")


def check_alerts():
    if not edge.settings().get("unl_alerts"):
        return
    now = time.time()
    with sig._db_lock, sig.db() as c:
        due = [r for r in c.execute("SELECT * FROM unlocks WHERE ts>? AND ts<? AND (a24=0 OR a1=0)",
                                    (now, now + 86400))]
    for raw in due:
        r = _row(raw)
        if r["impact"] not in ("HIGH", "MED"):
            continue
        hrs = (r["ts"] - now) / 3600
        col = "a1" if hrs <= 1.25 else "a24"
        if r[col]:
            continue
        when = datetime.fromtimestamp(r["ts"], edge.TZ).strftime("%a %d %b %H:%M")
        lines = [f"🔓 <b>{r['ticker'] or r['name']} unlock in {hrs:.0f}h</b> ({when} UK) · impact <b>{r['impact']}</b>",
                 f"{sig.fmt_usd(r['value'])} = {r['pct_mcap']:.1f}% of mcap"
                 + (f" · {r['pct_supply']:.2f}% of supply" if r['pct_supply'] else "")
                 + (f" · {r['alloc']}" if r['alloc'] else ""),
                 _fmt_perp(r["perp"])]
        if r["note"]:
            lines.append(r["note"])
        edge.dispatch("\n".join(lines), prio="high" if r["impact"] == "HIGH" else "normal", group="Unlocks")
        with sig._db_lock, sig.db() as c:
            c.execute(f"UPDATE unlocks SET {col}=1{', a24=1' if col == 'a1' else ''} WHERE id=?", (r["id"],))


# ------------------------------------------------------------------ loop ----
_started = False


def refresh(force_sync=False):
    if TOKENOMIST_KEY and (force_sync or not _status["last_sync"] or time.time() - _status["last_sync"] > SYNC_SECONDS):
        try:
            _status["synced"] = sync_tokenomist()
            _status["last_sync"] = int(time.time())
            _status["errors"].pop("tokenomist", None)
        except Exception as e:
            _status["errors"]["tokenomist"] = str(e)[:150]
    enrich()
    check_alerts()


def start():
    global _started
    if _started or os.getenv("UNLOCKS_OFF"):
        return
    _started = True
    init_tables()

    def loop():
        time.sleep(30)
        while True:
            try:
                refresh()
            except Exception as e:
                _status["errors"]["loop"] = str(e)[:150]
            time.sleep(LOOP_SECONDS)
    threading.Thread(target=loop, daemon=True, name="unlocks").start()
