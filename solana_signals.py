"""
Signals engine shared by every chain tracker (Solana, Robinhood Chain, ...).

  * Signal journal  - meaningful key-wallet moves logged with the price at the time, re-priced at
                      +1h / +24h / +7d (SQLite; set SIGNAL_DB=/data/signals.db on a Railway volume)
  * Price zones     - your own zones for any coin, added from the page; alert when price enters one
  * Wallets         - add / remove tracked wallets from the page (stored in the same DB)
  * Risk checks     - GeckoTerminal holders + authorities, liquidity vs mcap, pool age, wash volume
  * Clusters        - same token bought by 2+ of your wallets within 72h
  * Telegram alerts - via bot.py send() when TELEGRAM_BOT_TOKEN is set

A chain module registers itself with register(module). It must expose:
  CHAIN, NATIVE, QUOTES, WALLETS, EXCHANGES, GT_NETWORK, EXPLORER_TX, BUBBLEMAPS_CHAIN,
  tracker (query/listeners), token_info(), _tok
"""

import os
import sqlite3
import threading
import time
from collections import defaultdict

import requests

DB_PATH = os.getenv("SIGNAL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db"))
MIN_USD = float(os.getenv("SIGNAL_MIN_USD", "5000"))        # key-wallet moves at least this big
FRESH_SECONDS = 45 * 60   # only journal moves we saw soon after they happened (fair entry price)
HORIZONS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}

CHAINS = {}
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
        cols = [r[1] for r in c.execute("PRAGMA table_info(signals)")]
        if "chain" not in cols:
            c.execute("ALTER TABLE signals ADD COLUMN chain TEXT DEFAULT 'solana'")
        c.execute("""CREATE TABLE IF NOT EXISTS levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, mint TEXT, name TEXT,
            low REAL, high REAL, created INTEGER, inside INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS wallets (
            chain TEXT, address TEXT, label TEXT, grp TEXT, note TEXT, alert INTEGER,
            created INTEGER, PRIMARY KEY (chain, address))""")
        if "min_usd" not in [r[1] for r in c.execute("PRAGMA table_info(wallets)")]:
            c.execute("ALTER TABLE wallets ADD COLUMN min_usd REAL")
        c.execute("""CREATE TABLE IF NOT EXISTS flows (
            chain TEXT, sig TEXT, wallet TEXT, mint TEXT, ts INTEGER, symbol TEXT, label TEXT, grp TEXT,
            venue TEXT, usd REAL, sign INTEGER, market_cap REAL, PRIMARY KEY (chain, sig, wallet, mint))""")
        c.execute("CREATE INDEX IF NOT EXISTS flows_mint_ts ON flows (chain, mint, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS flow_alerts (
            chain TEXT, mint TEXT, sign INTEGER, bucket INTEGER, ts INTEGER, PRIMARY KEY (chain, mint, sign))""")


# --------------------------------------------------------------- chains ----
def register(m):
    """Called by each chain module once its tracker exists. Loads page-added wallets too."""
    CHAINS[m.CHAIN] = m
    init_db()
    if not hasattr(m, "_file_addrs"):   # wallets that come from the code / JSON file (not added on the page)
        m._file_addrs = set(m.WALLETS) | set(m.EXCHANGES)
    load_db_wallets(m)
    if on_new_events_for(m) not in m.tracker.listeners:
        m.tracker.listeners.append(on_new_events_for(m))


_listeners = {}


def on_new_events_for(m):
    if m.CHAIN not in _listeners:
        _listeners[m.CHAIN] = lambda events: on_new_events(events, m)
    return _listeners[m.CHAIN]


# -------------------------------------------------------------- wallets ----
def load_db_wallets(m):
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM wallets WHERE chain=?", (m.CHAIN,))]
    for r in rows:
        _apply_wallet(m, r)


REMOVED = "__removed__"   # DB marker: a built-in wallet you deleted from the page


def db_persistent():
    """True when the DB lives on a mounted volume (survives Railway redeploys)."""
    d = os.path.dirname(os.path.abspath(DB_PATH))
    return os.path.ismount(d) or d.startswith("/data")


def _apply_wallet(m, r):
    grp = (r.get("grp") or "").strip()
    if grp == REMOVED:
        m.WALLETS.pop(r["address"], None)
        m.EXCHANGES.pop(r["address"], None)
        return
    if grp.lower() == "exchange":
        m.WALLETS.pop(r["address"], None)
        m.EXCHANGES[r["address"]] = r["label"]
        return
    m.EXCHANGES.pop(r["address"], None)
    m.WALLETS[r["address"]] = {"address": r["address"], "label": r["label"], "group": r["grp"] or "Mine",
                               "note": r.get("note") or "", "alert": bool(r["alert"]), "source": "page",
                               "min_usd": r.get("min_usd")}


def add_wallet(m, address, label, group, note="", alert_on=True, min_usd=None):
    try:
        min_usd = float(min_usd) if min_usd not in (None, "") else None
    except (TypeError, ValueError):
        min_usd = None
    r = {"chain": m.CHAIN, "address": address, "label": label or address[:6] + "…" + address[-4:],
         "grp": group or "Mine", "note": note or "", "alert": 1 if alert_on else 0, "created": int(time.time()),
         "min_usd": min_usd}
    with _db_lock, db() as c:
        c.execute("""INSERT OR REPLACE INTO wallets (chain, address, label, grp, note, alert, created, min_usd)
                     VALUES (:chain,:address,:label,:grp,:note,:alert,:created,:min_usd)""", r)
    _apply_wallet(m, r)
    return r


def remove_wallet(m, address):
    """Stop tracking a wallet. Built-in ones get a 'removed' marker so they stay gone after a restart."""
    known = address in m.WALLETS or address in m.EXCHANGES
    with _db_lock, db() as c:
        n = c.execute("DELETE FROM wallets WHERE chain=? AND address=?", (m.CHAIN, address)).rowcount
        if address in getattr(m, "_file_addrs", ()):
            c.execute("""INSERT OR REPLACE INTO wallets (chain, address, label, grp, note, alert, created, min_usd)
                         VALUES (?,?,?,?,?,?,?,?)""", (m.CHAIN, address, "", REMOVED, "", 0, int(time.time()), None))
            n = 1
    m.WALLETS.pop(address, None)
    m.EXCHANGES.pop(address, None)
    return n or int(known)


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
def is_signal(e, m):
    if e.get("mint") in (None, m.NATIVE) or e["mint"] in m.QUOTES:
        return False
    usd = e.get("usd") or 0
    floor = e.get("min_usd")  # optional per-wallet threshold (e.g. $250k for a busy treasury)
    if not e.get("alert"):
        return False
    if e.get("to_exchange") or e.get("from_exchange"):
        return usd >= (floor or 1000)  # exchange deposit = likely sell; withdrawal = restocking / accumulation
    return usd >= (floor or MIN_USD)


# ------------------------------------------------------ net exchange flow ---
# Every exchange withdrawal / deposit by a tracked wallet is stored, then summed per token over a
# rolling window.  + = pulled OFF exchanges (restocking / accumulating), - = sent TO exchanges (likely sell).
FLOW_ALERT_USD = float(os.getenv("FLOW_ALERT_USD", "250000"))      # alert when 24h net passes this...
FLOW_ALERT_PCT = float(os.getenv("FLOW_ALERT_PCT", "0.05"))        # ...or this % of market cap
FLOW_ALERT_FLOOR = float(os.getenv("FLOW_ALERT_FLOOR", "25000"))   # but never below this (small caps)
FLOW_WINDOWS = {"1h": 3600, "24h": 86400}


def record_flows(events, m):
    rows = []
    for e in events:
        if not (e.get("to_exchange") or e.get("from_exchange")):
            continue
        if e.get("mint") in (None, m.NATIVE) or e["mint"] in m.QUOTES or not e.get("usd"):
            continue
        rows.append((m.CHAIN, e["sig"], e["wallet"], e["mint"], int(e.get("ts") or time.time()), e.get("symbol"),
                     e.get("label"), e.get("group"), e.get("counterparty_label"), float(e["usd"]),
                     1 if e.get("from_exchange") else -1, e.get("market_cap")))
    if rows:
        with _db_lock, db() as c:
            c.executemany("INSERT OR IGNORE INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return {r[3] for r in rows}


def net_flows(m, hours=24, mint=None):
    """Per token: net USD pulled off (+) / sent to (-) exchanges by your tracked wallets."""
    since = int(time.time() - hours * 3600)
    q = """SELECT mint, MAX(symbol) symbol, SUM(usd*sign) net_usd,
                  SUM(CASE WHEN sign>0 THEN usd ELSE 0 END) off_usd, SUM(CASE WHEN sign<0 THEN usd ELSE 0 END) on_usd,
                  COUNT(*) n, MAX(ts) last, GROUP_CONCAT(DISTINCT label) wallets, GROUP_CONCAT(DISTINCT venue) venues
           FROM flows WHERE chain=? AND ts>=?""" + (" AND mint=?" if mint else "") + " GROUP BY mint"
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(q, (m.CHAIN, since, mint) if mint else (m.CHAIN, since))]
        caps = {r["mint"]: r["market_cap"] for r in c.execute(
            "SELECT mint, market_cap, MAX(ts) FROM flows WHERE chain=? AND market_cap IS NOT NULL AND ts>=? "
            "GROUP BY mint", (m.CHAIN, since))}
    for r in rows:
        r["market_cap"] = caps.get(r["mint"])
        r["pct_mcap"] = r["net_usd"] / r["market_cap"] * 100 if r["market_cap"] else None
    return sorted(rows, key=lambda r: -abs(r["net_usd"] or 0))


def flow_line(m, mint, symbol=None):
    """One-line rolling summary for an alert, e.g. 'PENGU net off-exchange: 1h +$56k · 24h +$410k (7 moves)'."""
    parts, n24, pct = [], 0, None
    for w, secs in FLOW_WINDOWS.items():
        r = (net_flows(m, secs / 3600, mint) or [{}])[0]
        v = r.get("net_usd") or 0
        parts.append(f"{w} {'+' if v >= 0 else '-'}{fmt_usd(abs(v))}")
        if w == "24h":
            n24 = r.get("n") or 0
            pct = r.get("pct_mcap")
    tail = f" ({n24} move{'s' if n24 != 1 else ''}" + (f", {pct:+.3f}% mcap)" if pct is not None else ")")
    return f"📊 {symbol or mint[:6]} net off-exchange: " + " · ".join(parts) + tail


def _flow_threshold(mcap):
    t = FLOW_ALERT_USD
    if mcap:
        t = min(t, max(FLOW_ALERT_FLOOR, mcap * FLOW_ALERT_PCT / 100))
    return t


def check_flow_alerts(m, mints):
    """Alert when a token's 24h net flow crosses the threshold - again each time it doubles."""
    for mint in mints:
        r = (net_flows(m, 24, mint) or [None])[0]
        if not r or not r["net_usd"]:
            continue
        net, t = r["net_usd"], _flow_threshold(r.get("market_cap"))
        if abs(net) < t:
            continue
        sign, bucket = (1 if net > 0 else -1), int(abs(net) // t).bit_length()   # 1x, 2x, 4x, 8x...
        with _db_lock, db() as c:
            prev = c.execute("SELECT bucket, ts FROM flow_alerts WHERE chain=? AND mint=? AND sign=?",
                             (m.CHAIN, mint, sign)).fetchone()
            if prev and prev["bucket"] >= bucket and time.time() - prev["ts"] < 86400:
                continue
            c.execute("INSERT OR REPLACE INTO flow_alerts VALUES (?,?,?,?,?)",
                      (m.CHAIN, mint, sign, bucket, int(time.time())))
        head = ("🟩 NET ACCUMULATION (off exchanges)" if sign > 0 else "🟥 NET DISTRIBUTION (onto exchanges)")
        pct = f" · {r['pct_mcap']:+.3f}% of mcap" if r.get("pct_mcap") is not None else ""
        alert(("flow", m.CHAIN, mint, sign, bucket, int(time.time() // 86400)),
              f"{head} <b>{r['symbol']}</b> [{m.CHAIN}]\n"
              f"24h net {'+' if net > 0 else '-'}{fmt_usd(abs(net))}{pct}\n"
              f"off {fmt_usd(r['off_usd'])} / onto {fmt_usd(r['on_usd'])} · {r['n']} moves\n"
              f"wallets: {r['wallets'] or '–'}\nvenues: {r['venues'] or '–'}\n"
              f"mcap {fmt_usd(r.get('market_cap'))}")


def on_new_events(events, m):
    """Called by a tracker after every poll with freshly parsed + enriched events."""
    now = time.time()
    try:
        flow_mints = record_flows(events, m)
    except Exception as ex:
        print(f"flow record failed: {ex}")
        flow_mints = set()
    fresh = [e for e in events if is_signal(e, m) and now - (e.get("ts") or 0) <= FRESH_SECONDS]
    if fresh:
        info = m.token_info([e["mint"] for e in fresh])
        pending = []   # alerts are built after the DB lock is released (the flow summary reads the DB)
        with _db_lock, db() as c:
            for e in fresh:
                p0 = (info.get(e["mint"]) or {}).get("price") or e.get("price")
                c.execute("""INSERT OR IGNORE INTO signals
                    (sig, wallet, ts, logged_at, label, grp, kind, mint, symbol, usd, price0, chain)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (e["sig"], e["wallet"], e["ts"], int(now), e["label"], e["group"],
                           e["kind"], e["mint"], e.get("symbol"), e.get("usd"), p0, m.CHAIN))
                arrow = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "IN": "⬇️ IN", "OUT": "⬆️ OUT"}[e["kind"]]
                if e.get("to_exchange"):
                    arrow = f"🚨 SENT TO {(e.get('counterparty_label') or 'EXCHANGE').upper()} (likely sell)"
                elif e.get("from_exchange"):
                    arrow = f"🏦 WITHDREW FROM {(e.get('counterparty_label') or 'EXCHANGE').upper()} (restocking / accumulating)"
                pending.append((e, arrow, p0))
        for e, arrow, p0 in pending:
            flow = ""
            if e.get("to_exchange") or e.get("from_exchange"):
                try:
                    flow = flow_line(m, e["mint"], e.get("symbol")) + "\n"
                except Exception as ex:
                    print(f"flow line failed: {ex}")
            alert(("sig", e["sig"], e["wallet"]),
                  f"{arrow} <b>{e.get('symbol')}</b> {fmt_usd(e.get('usd'))} [{m.CHAIN}]\n"
                  f"{e['label']} ({e['group']})\n"
                  f"mcap {fmt_usd(e.get('market_cap'))} · price {e.get('price') or p0}\n"
                  f"{flow}"
                  f"{m.EXPLORER_TX}{e['sig']}")
    for cl in clusters(m):
        if cl["fresh"]:
            alert(("cluster", m.CHAIN, cl["mint"], len(cl["wallets"])),
                  f"🫧 <b>Cluster buy: {cl['symbol']}</b> [{m.CHAIN}] - {len(cl['wallets'])} of your wallets "
                  f"bought in 72h ({fmt_usd(cl['buy_usd'])})\n" + ", ".join(w["label"] for w in cl["wallets"]) +
                  f"\nhttps://v2.bubblemaps.io/map?address={cl['mint']}&chain={m.BUBBLEMAPS_CHAIN}")
    try:
        check_flow_alerts(m, flow_mints)
    except Exception as ex:
        print(f"flow alert failed: {ex}")
    check_levels(m)


def reprice_due():
    """Fill in +1h / +24h / +7d prices for journal rows that are due (all chains)."""
    now = time.time()
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT sig, wallet, ts, mint, chain, p1h, p24h, p7d FROM signals WHERE p7d IS NULL")]
    due = [(r, h) for r in rows for h, secs in HORIZONS.items()
           if r["p" + h] is None and now >= r["ts"] + secs]
    n = 0
    for chain in {r["chain"] or "solana" for r, _ in due}:
        m = CHAINS.get(chain)
        if not m:
            continue
        mine = [(r, h) for r, h in due if (r["chain"] or "solana") == chain]
        for mint in {r["mint"] for r, _ in mine}:
            m._tok.pop(mint, None)  # force fresh prices
        prices = m.token_info([r["mint"] for r, _ in mine])
        with _db_lock, db() as c:
            for r, h in mine:
                p = (prices.get(r["mint"]) or {}).get("price")
                if p:
                    c.execute(f"UPDATE signals SET p{h}=? WHERE sig=? AND wallet=?", (p, r["sig"], r["wallet"]))
                    n += 1
    return n


def _ret(r, h):
    """Direction-adjusted return: positive = price went the way the move 'pointed'."""
    p0, p = r["price0"], r["p" + h]
    if not p0 or not p:
        return None
    raw = (p - p0) / p0 * 100
    return raw if r["kind"] in ("BUY", "IN") else -raw


def journal(m, limit=300):
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM signals WHERE COALESCE(chain,'solana')=? ORDER BY ts DESC LIMIT ?", (m.CHAIN, limit))]
    for r in rows:
        for h in HORIZONS:
            r["r" + h] = _ret(r, h)
    return rows


def scorecard(m):
    """Per wallet: how often price moved the way their trades pointed."""
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM signals WHERE COALESCE(chain,'solana')=?", (m.CHAIN,))]
    agg = defaultdict(lambda: {"n": 0, **{f"r{h}": [] for h in HORIZONS}})
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
def clusters(m, hours=72, min_wallets=2):
    since = time.time() - hours * 3600
    by = defaultdict(dict)
    for e in m.tracker.query(kinds=["BUY"], limit=3000):
        if e["ts"] < since or e["mint"] in m.QUOTES or e["mint"] == m.NATIVE:
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


# ----------------------------------------------------------- price zones ---
def _file_levels(m):
    return [{**lv, "id": f"file-{i}", "source": "file"} for i, lv in enumerate(getattr(m, "FILE_LEVELS", []))]


def list_levels(m):
    with _db_lock, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM levels WHERE chain=? ORDER BY created", (m.CHAIN,))]
    return _file_levels(m) + [{**r, "source": "page"} for r in rows]


def add_level(m, mint, name, low, high):
    low, high = float(low), float(high)
    if low > high:
        low, high = high, low
    with _db_lock, db() as c:
        cur = c.execute("INSERT INTO levels (chain, mint, name, low, high, created) VALUES (?,?,?,?,?,?)",
                        (m.CHAIN, mint, name or "Zone", low, high, int(time.time())))
        return cur.lastrowid


def remove_level(m, level_id):
    with _db_lock, db() as c:
        return c.execute("DELETE FROM levels WHERE chain=? AND id=?", (m.CHAIN, int(level_id))).rowcount


def level_status(m):
    lvls = list_levels(m)
    if not lvls:
        return []
    info = m.token_info([lv["mint"] for lv in lvls])
    out = []
    for lv in lvls:
        t = info.get(lv["mint"]) or {}
        p = t.get("price")
        inside = bool(p and lv["low"] <= p <= lv["high"])
        out.append({**lv, "symbol": t.get("symbol"), "price": p, "inside": inside,
                    "distance_pct": None if not p else
                    (0 if inside else ((lv["low"] - p) / p * 100 if p < lv["low"] else (lv["high"] - p) / p * 100))})
    return out


def check_levels(m):
    """Alert once when price ENTERS a zone (and again only after it has left and come back)."""
    for lv in level_status(m):
        key = (m.CHAIN, lv["id"])
        prev = _zone_state.get(key)
        if prev is None and lv.get("source") == "page":
            with _db_lock, db() as c:
                row = c.execute("SELECT inside FROM levels WHERE id=?", (lv["id"],)).fetchone()
            prev = bool(row and row[0])
        _zone_state[key] = lv["inside"]
        if lv["inside"] and not prev and lv["price"]:
            _sent.discard(("level",) + key)
            alert(("level",) + key,
                  f"🎯 <b>{lv['symbol'] or lv['mint'][:6]}</b> [{m.CHAIN}] entered your zone '{lv['name']}' "
                  f"({lv['low']:g} – {lv['high']:g}): now {lv['price']:g}")
        if lv.get("source") == "page" and prev != lv["inside"]:
            with _db_lock, db() as c:
                c.execute("UPDATE levels SET inside=? WHERE id=?", (1 if lv["inside"] else 0, lv["id"]))


_zone_state = {}


# ----------------------------------------------------------- risk checks ---
_risk = {}


def geckoterminal_info(network, token):
    """Free, keyless: holders count/top-10 %, mint & freeze authority, GT score (beta coverage)."""
    try:
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token}/info",
                         headers={"accept": "application/json"}, timeout=15)
        if r.ok:
            return (r.json().get("data") or {}).get("attributes") or {}
    except Exception:
        pass
    return {}


def risk_checks(m, mint):
    key = (m.CHAIN, mint)
    hit = _risk.get(key)
    if hit and time.time() - hit[0] < (60 if any(c["status"] == "unknown" for c in hit[1]["checks"]) else 900):
        return hit[1]
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    gt = geckoterminal_info(m.GT_NETWORK, mint)
    holders = gt.get("holders") or {}
    top10 = (holders.get("distribution_percentage") or {}).get("top_10")
    have_auth = m.CHAIN == "solana" and "mint_authority" in gt and "freeze_authority" in gt
    if have_auth:
        ma, fa = gt.get("mint_authority"), gt.get("freeze_authority")
        ma = None if str(ma).lower() in ("no", "none", "null", "false", "") else ma
        fa = None if str(fa).lower() in ("no", "none", "null", "false", "") else fa
        add("Mint authority", "bad" if ma else "ok", "Can still mint more supply" if ma else "Revoked - supply is fixed")
        add("Freeze authority", "bad" if fa else "ok",
            "Can freeze your tokens (honeypot risk)" if fa else "Revoked")
    if top10 is not None:
        pct = float(top10)
        add("Top 10 holders", "bad" if pct > 50 else "warn" if pct > 30 else "ok",
            f"{pct:.1f}% of supply" + (f" · {int(holders['count']):,} holders" if holders.get("count") else "")
            + " (source: GeckoTerminal)")
    if gt.get("gt_score") is not None:
        sc_ = float(gt["gt_score"])
        add("GeckoTerminal trust score", "ok" if sc_ >= 60 else "warn" if sc_ >= 35 else "bad", f"{sc_:.0f}/100")
    if m.CHAIN == "solana" and not (have_auth and top10 is not None):
        _solana_rpc_checks(m, mint, add, need_auth=not have_auth, need_holders=top10 is None)
    elif top10 is None:
        add("Holder concentration", "unknown", "Not available yet - use the Bubblemaps / Holders buttons above")
    _market_checks(m, mint, add)
    score = sum({"ok": 0, "warn": 1, "bad": 3, "unknown": 0}[c["status"]] for c in checks)
    verdict = "HIGH RISK" if score >= 5 else "CAUTION" if score >= 2 else "OK"
    if any(c["status"] == "unknown" for c in checks):
        verdict += " (incomplete)"
    res = {"checks": checks, "verdict": verdict}
    _risk[key] = (time.time(), res)
    return res


def _solana_rpc_checks(m, mint, add, need_auth=True, need_holders=True):
    if need_auth:
        try:
            acc = m.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
            parsed = (((acc or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {})
            ma, fa = parsed.get("mintAuthority"), parsed.get("freezeAuthority")
            add("Mint authority", "bad" if ma else "ok", "Can still mint more supply" if ma else "Revoked - supply is fixed")
            add("Freeze authority", "bad" if fa else "ok", "Can freeze your tokens (honeypot risk)" if fa else "Revoked")
        except Exception as e:
            add("Authorities", "unknown", _nice(e))
    if need_holders:
        try:
            supply = float(m.rpc("getTokenSupply", [mint])["value"]["uiAmount"] or 0)
            largest = m.rpc("getTokenLargestAccounts", [mint])["value"]
            top = [float(a.get("uiAmount") or 0) for a in largest[:10]]
            pct = sum(top) / supply * 100 if supply else 0
            top1 = top[0] / supply * 100 if supply and top else 0
            add("Top 10 holders", "bad" if pct > 50 else "warn" if pct > 30 else "ok",
                f"{pct:.1f}% of supply (largest {top1:.1f}% - often the pool, check Bubblemaps)")
        except Exception as e:
            add("Holder concentration", "unknown", _nice(e) + " - use the Bubblemaps / Holders buttons above")


def _nice(e):
    msg = str(e)
    return "RPC is busy" if "rate limit" in msg.lower() or "429" in msg else msg[:120]


def _market_checks(m, mint, add):
    t = m.token_info([mint]).get(mint) or {}
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
    """Starts the shared re-pricing loop (chains register themselves separately)."""
    global _started
    if _started:
        return
    _started = True
    init_db()

    def loop():
        while True:
            try:
                reprice_due()
            except Exception as e:
                print(f"signal reprice failed: {e}")
            time.sleep(600)
    threading.Thread(target=loop, daemon=True, name="signal-reprice").start()
