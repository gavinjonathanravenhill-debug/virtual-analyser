"""
Turns tracked-wallet activity into testable signals.

  * Signal journal  - every meaningful key-wallet / Vine move is logged with the price at the
                      time, then re-priced at +1h / +24h / +7d so you can see which wallets
                      actually lead price (SQLite; set SIGNAL_DB to a Railway volume path to keep
                      it across deploys, e.g. /data/signals.db)
  * Risk checks     - mint/freeze authority, top-holder concentration, liquidity vs mcap, pool age
  * Clusters        - same token bought by 2+ of your wallets within 72h
  * Your levels     - price zones from solana_wallets.json ("levels")
  * Telegram alerts - via bot.py send() when TELEGRAM_BOT_TOKEN is set
"""

import os
import sqlite3
import threading
import time
from collections import defaultdict

import solana_client as sc

DB_PATH = os.getenv("SIGNAL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db"))
MIN_USD = float(os.getenv("SIGNAL_MIN_USD", "5000"))        # key-wallet moves at least this big
VINE_MIN_USD = float(os.getenv("SIGNAL_VINE_MIN_USD", "250"))  # any Vine trade at least this big
FRESH_SECONDS = 45 * 60   # only journal moves we saw soon after they happened (fair entry price)
HORIZONS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
LEVELS = sc._cfg.get("levels", [])

_db_lock = threading.Lock()


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _db_lock, db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS signals (
            sig TEXT, wallet TEXT, ts INTEGER, logged_at INTEGER, label TEXT, grp TEXT,
            kind TEXT, mint TEXT, symbol TEXT, usd REAL, price0 REAL,
            p1h REAL, p24h REAL, p7d REAL, PRIMARY KEY (sig, wallet))""")


# ---------------------------------------------------------------- alerts ---
_sent = set()


def alert(key, text):
    """Telegram (if configured) - each key only once per process."""
    if key in _sent:
        return
    _sent.add(key)
    try:
        import bot
        if bot.BOT_TOKEN:
            bot.send(text)
    except Exception as e:
        print(f"telegram alert failed: {e}")


def fmt_usd(v):
    if v is None:
        return "–"
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v / div:.2f}{suf}"
    return f"${v:.2f}"


# --------------------------------------------------------------- journal ---
def is_signal(e):
    if e.get("mint") in (None, "SOL") or e["mint"] in sc.QUOTES:
        return False
    usd = e.get("usd") or 0
    if e.get("is_vine") and usd >= VINE_MIN_USD:
        return True
    return bool(e.get("alert")) and usd >= MIN_USD


def on_new_events(events):
    """Called by the tracker after every poll with freshly parsed + enriched events."""
    now = time.time()
    fresh = [e for e in events if is_signal(e) and now - (e.get("ts") or 0) <= FRESH_SECONDS]
    if fresh:
        info = sc.token_info([e["mint"] for e in fresh])
        with _db_lock, db() as c:
            for e in fresh:
                p0 = (info.get(e["mint"]) or {}).get("price") or e.get("price")
                c.execute("""INSERT OR IGNORE INTO signals
                    (sig, wallet, ts, logged_at, label, grp, kind, mint, symbol, usd, price0)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (e["sig"], e["wallet"], e["ts"], int(now), e["label"], e["group"],
                           e["kind"], e["mint"], e.get("symbol"), e.get("usd"), p0))
                arrow = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "IN": "⬇️ IN", "OUT": "⬆️ OUT"}[e["kind"]]
                alert(("sig", e["sig"], e["wallet"]),
                      f"{arrow} <b>{e.get('symbol')}</b> {fmt_usd(e.get('usd'))}\n"
                      f"{e['label']} ({e['group']})\n"
                      f"mcap {fmt_usd(e.get('market_cap'))} · price {e.get('price') or p0}\n"
                      f"https://solscan.io/tx/{e['sig']}")
    for cl in clusters():
        if cl["fresh"]:
            alert(("cluster", cl["mint"], len(cl["wallets"])),
                  f"🫧 <b>Cluster buy: {cl['symbol']}</b> - {len(cl['wallets'])} of your wallets bought "
                  f"in 72h ({fmt_usd(cl['buy_usd'])})\n" + ", ".join(w["label"] for w in cl["wallets"]) +
                  f"\nhttps://v2.bubblemaps.io/map?address={cl['mint']}&chain=solana")
    for lv in level_status():
        if lv["inside"]:
            alert(("level", lv["mint"], lv["name"], int(now // 3600)),
                  f"🎯 <b>{lv['symbol']}</b> in your zone '{lv['name']}' "
                  f"({lv['low']}–{lv['high']}): now {lv['price']}")


def reprice_due():
    """Fill in +1h / +24h / +7d prices for journal rows that are due."""
    now = time.time()
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT sig, wallet, ts, mint, p1h, p24h, p7d FROM signals WHERE p7d IS NULL")]
    due = [(r, h) for r in rows for h, secs in HORIZONS.items()
           if r["p" + h] is None and now >= r["ts"] + secs]
    if not due:
        return 0
    for m in {r["mint"] for r, _ in due}:
        sc._tok.pop(m, None)  # force fresh prices
    prices = sc.token_info([r["mint"] for r, _ in due])
    with _db_lock, db() as c:
        for r, h in due:
            p = (prices.get(r["mint"]) or {}).get("price")
            if p:
                c.execute(f"UPDATE signals SET p{h}=? WHERE sig=? AND wallet=?", (p, r["sig"], r["wallet"]))
    return len(due)


def _ret(r, h):
    """Direction-adjusted return: positive = price went the way the move 'pointed'."""
    p0, p = r["price0"], r["p" + h]
    if not p0 or not p:
        return None
    raw = (p - p0) / p0 * 100
    return raw if r["kind"] in ("BUY", "IN") else -raw


def journal(limit=300):
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,))]
    for r in rows:
        for h in HORIZONS:
            r["r" + h] = _ret(r, h)
    return rows


def scorecard():
    """Per wallet: how often price moved the way their trades pointed."""
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM signals")]
    agg = defaultdict(lambda: {"n": 0, **{f"{k}{h}": [] for h in HORIZONS for k in ("r",)}})
    for r in rows:
        a = agg[(r["wallet"], r["label"], r["grp"])]
        a["n"] += 1
        for h in HORIZONS:
            v = _ret(r, h)
            if v is not None:
                a["r" + h].append(v)
    out = []
    for (w, label, grp), a in agg.items():
        row = {"wallet": w, "label": label, "group": grp, "signals": a["n"]}
        for h in HORIZONS:
            vs = a["r" + h]
            row["avg_" + h] = sum(vs) / len(vs) if vs else None
            row["hit_" + h] = sum(v > 0 for v in vs) / len(vs) * 100 if vs else None
            row["n_" + h] = len(vs)
        out.append(row)
    return sorted(out, key=lambda r: -(r["n_24h"] or 0))


# -------------------------------------------------------------- clusters ---
def clusters(hours=72, min_wallets=2):
    since = time.time() - hours * 3600
    by = defaultdict(dict)
    for e in sc.tracker.query(kinds=["BUY"], limit=3000):
        if e["ts"] < since or e["mint"] in sc.QUOTES or e["mint"] == "SOL":
            continue
        w = by[e["mint"]].setdefault(e["wallet"], {"wallet": e["wallet"], "label": e["label"],
                                                   "group": e["group"], "usd": 0.0, "last": 0,
                                                   "symbol": e.get("symbol"), "mcap": e.get("market_cap")})
        w["usd"] += e.get("usd") or 0
        w["last"] = max(w["last"], e["ts"])
    out = []
    for mint, ws in by.items():
        if len(ws) >= min_wallets:
            wl = sorted(ws.values(), key=lambda w: -w["usd"])
            out.append({"mint": mint, "symbol": wl[0]["symbol"], "market_cap": wl[0]["mcap"],
                        "wallets": wl, "buy_usd": sum(w["usd"] for w in wl),
                        "last": max(w["last"] for w in wl),
                        "fresh": time.time() - max(w["last"] for w in wl) < 3600})
    return sorted(out, key=lambda c: (-len(c["wallets"]), -c["buy_usd"]))


# ---------------------------------------------------------------- levels ---
def level_status():
    if not LEVELS:
        return []
    info = sc.token_info([lv["mint"] for lv in LEVELS])
    out = []
    for lv in LEVELS:
        t = info.get(lv["mint"]) or {}
        p = t.get("price")
        out.append({**lv, "symbol": t.get("symbol"), "price": p,
                    "inside": bool(p and lv["low"] <= p <= lv["high"]),
                    "distance_pct": None if not p else
                    (0 if lv["low"] <= p <= lv["high"] else
                     ((lv["low"] - p) / p * 100 if p < lv["low"] else (lv["high"] - p) / p * 100))})
    return out


# ----------------------------------------------------------- risk checks ---
_risk = {}


def risk_checks(mint):
    hit = _risk.get(mint)
    if hit and time.time() - hit[0] < (60 if any(c["status"] == "unknown" for c in hit[1]["checks"]) else 900):
        return hit[1]
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    gt = geckoterminal_info(mint)
    holders = gt.get("holders") or {}
    dist = holders.get("distribution_percentage") or {}
    top10 = dist.get("top_10")
    have_auth = "mint_authority" in gt and "freeze_authority" in gt
    if have_auth:
        ma, fa = gt.get("mint_authority"), gt.get("freeze_authority")
        ma = None if str(ma).lower() in ("no", "none", "null", "false", "") else ma
        fa = None if str(fa).lower() in ("no", "none", "null", "false", "") else fa
        add("Mint authority", "bad" if ma else "ok",
            "Can still mint more supply" if ma else "Revoked - supply is fixed")
        add("Freeze authority", "bad" if fa else "ok",
            "Can freeze your tokens (honeypot risk)" if fa else "Revoked")
    if top10 is not None:
        pct = float(top10)
        add("Top 10 holders", "bad" if pct > 50 else "warn" if pct > 30 else "ok",
            f"{pct:.1f}% of supply" + (f" · {int(holders['count']):,} holders" if holders.get("count") else "")
            + " (source: GeckoTerminal)")
    if have_auth and top10 is not None:
        pass  # everything came from GeckoTerminal - no RPC needed
    else:
        _rpc_checks(mint, add, need_auth=not have_auth, need_holders=top10 is None)
    _market_checks(mint, add)
    score = sum({"ok": 0, "warn": 1, "bad": 3, "unknown": 0}[c["status"]] for c in checks)
    verdict = "HIGH RISK" if score >= 5 else "CAUTION" if score >= 2 else "OK"
    if any(c["status"] == "unknown" for c in checks):
        verdict += " (incomplete)"
    res = {"checks": checks, "verdict": verdict}
    _risk[mint] = (time.time(), res)
    return res


def geckoterminal_info(mint):
    """Free, keyless: holders count/top-10 %, mint & freeze authority (beta, not every token)."""
    try:
        r = sc.requests.get(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/info",
                            headers={"accept": "application/json"}, timeout=15)
        if r.ok:
            return (r.json().get("data") or {}).get("attributes") or {}
    except Exception:
        pass
    return {}


def _rpc_checks(mint, add, need_auth=True, need_holders=True):
    if need_auth:
        try:
            acc = sc.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            parsed = (((acc or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {})
            ma, fa = parsed.get("mintAuthority"), parsed.get("freezeAuthority")
            add("Mint authority", "bad" if ma else "ok",
                "Can still mint more supply" if ma else "Revoked - supply is fixed")
            add("Freeze authority", "bad" if fa else "ok",
                "Can freeze your tokens (honeypot risk)" if fa else "Revoked")
        except Exception as e:
            add("Authorities", "unknown", _nice(e))
    if need_holders:
        try:
            supply = float(sc.rpc("getTokenSupply", [mint])["value"]["uiAmount"] or 0)
            largest = sc.rpc("getTokenLargestAccounts", [mint])["value"]
            top = [float(a.get("uiAmount") or 0) for a in largest[:10]]
            pct = sum(top) / supply * 100 if supply else 0
            top1 = top[0] / supply * 100 if supply and top else 0
            add("Top 10 holders", "bad" if pct > 50 else "warn" if pct > 30 else "ok",
                f"{pct:.1f}% of supply (largest {top1:.1f}% - often the pool, check Bubblemaps)")
        except Exception as e:
            add("Holder concentration", "unknown", _nice(e) + " - use the Bubblemaps / Holders buttons above")


def _nice(e):
    m = str(e)
    return "Free Solana RPC is busy" if "rate limit" in m.lower() or "429" in m else m[:120]


def _market_checks(mint, add):
    t = sc.token_info([mint]).get(mint) or {}
    liq, mc = t.get("liquidity"), t.get("market_cap")
    if liq and mc:
        r = liq / mc * 100
        add("Liquidity vs mcap", "bad" if r < 2 else "warn" if r < 5 else "ok",
            f"{r:.1f}% - {'very thin, big slippage / easy to dump' if r < 2 else 'thin' if r < 5 else 'healthy'}")
    else:
        add("Liquidity", "bad", "No DEX pool found")
    if t.get("created"):
        hrs = (time.time() - t["created"] / 1000) / 3600
        add("Pool age", "bad" if hrs < 24 else "warn" if hrs < 24 * 7 else "ok",
            f"{hrs / 24:.1f} days" if hrs >= 24 else f"{hrs:.1f} hours - brand new")
    if t.get("volume_24h") and liq:
        v = t["volume_24h"] / liq
        add("Volume vs liquidity", "warn" if v > 20 else "ok",
            f"{v:.1f}x in 24h{' - possible wash trading' if v > 20 else ''}")


# ----------------------------------------------------------------- loop ----
_started = False


def start_signals():
    global _started
    if _started:
        return
    _started = True
    init_db()
    sc.tracker.listeners.append(on_new_events)

    def loop():
        while True:
            try:
                reprice_due()
            except Exception as e:
                print(f"signal reprice failed: {e}")
            time.sleep(600)
    threading.Thread(target=loop, daemon=True, name="signal-reprice").start()
