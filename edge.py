"""
Edge layer on top of the signals engine (all chains):

  1. Copy-trade backtest  - every tracked BUY/SELL is stored with the price WE could have got when we
                            saw it; each buy is "copied" at a fixed size, exited when that wallet sells
                            (or after 7 days), minus fees + pool slippage. Wallets are ranked; losers are
                            auto-muted on Telegram.
  3. Chase / skip verdict - how far price has run since their buy, entry mcap, slippage for YOUR size.
  4. Exit alerts          - a tracked wallet selling a coin it bought (share of bag, their P&L), exit
                            clusters (2+ smart wallets dumping), and priority pings for coins you hold.
  5. Risk gate            - HIGH RISK buys are blocked or tagged before they reach Telegram.
  6. Confluence score     - smart buyers + exchange flow + momentum + news mentions - risk, one number.
  7. Trade links          - Jupiter / GMGN / Uniswap / Pancake / DexScreener on every alert.
  8. Alert hygiene        - per-group minimums, quiet hours, digest groups (settings stored in the DB).
"""

import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import solana_signals as sig

TZ = ZoneInfo("Europe/London")
FRESH = 45 * 60

DEFAULTS = {
    "copy_size_usd": 500,        # what you'd put in per copied trade
    "fee_pct": 1.0,              # round-trip fees + priority/gas, % of size
    "late_pct": 25,              # SKIP if price already ran this much since their buy
    "max_slip_pct": 3,           # SKIP if your size would move the pool this much
    "risk_gate": "block",        # block | tag | off  (for HIGH RISK buys)
    "auto_mute": True,           # stop Telegram buy alerts from wallets that lose money when copied
    "mute_min_trades": 8,        # ...once they have at least this many copied trades
    "copy_min_usd": 250,         # store trades at least this big for the backtest
    "group_min_usd": {},         # e.g. {"fomo top": 5000, "Wintermute": 25000}
    "quiet_hours": "",           # e.g. "00-07" (UK time) - only high-priority alerts get through
    "digest_groups": [],         # groups sent as one summary instead of one ping each
    "digest_minutes": 60,
    "confluence_alert": 70,      # alert when a coin's score reaches this
    "my_tokens": [],             # coins you hold (addresses) - exits on these are always sent
}


# ------------------------------------------------------------------ DB ----
def init_tables(c):
    c.execute("""CREATE TABLE IF NOT EXISTS copytrades (
        chain TEXT, sig TEXT, wallet TEXT, mint TEXT, ts INTEGER, logged_at INTEGER, kind TEXT,
        symbol TEXT, amount REAL, usd REAL, their_price REAL, our_price REAL, liq REAL, mcap REAL,
        fresh INTEGER, grp TEXT, label TEXT, p1h REAL, p24h REAL, p7d REAL,
        PRIMARY KEY (chain, sig, wallet, mint))""")
    c.execute("CREATE INDEX IF NOT EXISTS copytrades_wm ON copytrades (chain, wallet, mint, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS copytrades_ts ON copytrades (chain, ts)")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS digest (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, text TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS bots (address TEXT PRIMARY KEY, label TEXT, note TEXT, source TEXT,
                 added INTEGER)""")
    for a, (label, note) in SEED_BOTS.items():
        c.execute("INSERT OR IGNORE INTO bots VALUES (?,?,?,?,?)", (a, label, note, "seed", int(time.time())))


# ------------------------------------------------------------ known bots --
SEED_BOTS = {
    "9PHm2cYU8DhwBrbRsqqAjhW9uXVrNR1RaLsvo9oGVeaq": (
        "HFT meme bot farm (Gate-funded)",
        "~200 tx/h via private program 4DKSAV…, fixed 0.423 SOL buys, funded 5,352 wallets; #1 VINE trader by PnL"),
}
BOT_TRADES = 20      # this many trades in one pool's recent window = bot-like, even if not on the list
_bots_cache = [0, {}]


def _norm(a):
    a = (a or "").strip()
    return a.lower() if a.startswith("0x") else a


def bots():
    if time.time() - _bots_cache[0] < 30:
        return _bots_cache[1]
    try:
        with sig._db_lock, sig.db() as c:
            _bots_cache[:] = [time.time(), {r["address"]: dict(r) for r in c.execute("SELECT * FROM bots")}]
    except Exception:
        _bots_cache[:] = [time.time(), {a: {"address": a, "label": l, "note": n} for a, (l, n) in SEED_BOTS.items()}]
    return _bots_cache[1]


def is_bot(addr):
    return _norm(addr) in bots()


def add_bot(addr, label="", note="", source="manual"):
    a = _norm(addr)
    if not a:
        raise ValueError("address needed")
    with sig._db_lock, sig.db() as c:
        c.execute("INSERT OR REPLACE INTO bots VALUES (?,?,?,?,?)", (a, label or a[:6] + "…", note, source, int(time.time())))
    _bots_cache[0] = 0
    return bots()[a]


def remove_bot(addr):
    with sig._db_lock, sig.db() as c:
        n = c.execute("DELETE FROM bots WHERE address=?", (_norm(addr),)).rowcount
    _bots_cache[0] = 0
    return n


# ------------------------------------------------------------ settings ----
_set_cache = [0, None]


def settings():
    if _set_cache[1] is not None and time.time() - _set_cache[0] < 30:
        return _set_cache[1]
    s = dict(DEFAULTS)
    try:
        with sig._db_lock, sig.db() as c:
            for r in c.execute("SELECT key, value FROM settings"):
                if r["key"] in DEFAULTS:
                    s[r["key"]] = json.loads(r["value"])
    except Exception:
        pass
    _set_cache[:] = [time.time(), s]
    return s


def save_settings(d):
    clean = {}
    for k, v in (d or {}).items():
        if k not in DEFAULTS:
            continue
        dv = DEFAULTS[k]
        try:
            if isinstance(dv, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(dv, (int, float)):
                v = float(v)
            elif isinstance(dv, list):
                v = [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
            elif isinstance(dv, dict):
                if isinstance(v, str):   # "fomo top:5000, Wintermute:25000"
                    v = {a.strip(): float(b) for a, _, b in (p.partition(":") for p in v.split(",")) if a.strip() and b.strip()}
                v = {str(a): float(b) for a, b in v.items()}
            else:
                v = str(v).strip()
        except (TypeError, ValueError):
            continue
        clean[k] = v
    with sig._db_lock, sig.db() as c:
        for k, v in clean.items():
            c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, json.dumps(v)))
    _set_cache[1] = None
    return settings()


def get_secret(key):
    with sig._db_lock, sig.db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", ("_" + key,)).fetchone()
    return json.loads(r["value"]) if r else None


def set_secret(key, value):
    with sig._db_lock, sig.db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("_" + key, json.dumps(value)))


def group_min(group):
    gm = settings()["group_min_usd"] or {}
    for k, v in gm.items():
        if k.lower() == (group or "").lower():
            return v
    return None


# ------------------------------------------------------ record trades -----
def is_me(e_or_group):
    g = e_or_group.get("group") if isinstance(e_or_group, dict) else e_or_group
    return (g or "").strip().lower() == "me"


def record_trades(events, m, info):
    """Store every tracked BUY/SELL (for the backtest + exit tracking). info = fresh token_info."""
    s, now, rows = settings(), time.time(), []
    for e in events:
        if e.get("kind") not in ("BUY", "SELL") or e.get("mint") in (None, m.NATIVE) or e["mint"] in m.QUOTES:
            continue
        if (e.get("usd") or 0) < s["copy_min_usd"] and not is_me(e):
            continue
        t = info.get(e["mint"]) or {}
        rows.append((m.CHAIN, e["sig"], e["wallet"], e["mint"], int(e.get("ts") or now), int(now), e["kind"],
                     e.get("symbol") or t.get("symbol"), e.get("amount"), e.get("usd"), e.get("price"),
                     t.get("price") or e.get("price"), t.get("liquidity"), t.get("market_cap") or e.get("market_cap"),
                     1 if now - (e.get("ts") or 0) <= FRESH else 0, e.get("group"), e.get("label")))
    if rows:
        with sig._db_lock, sig.db() as c:
            c.executemany("""INSERT OR IGNORE INTO copytrades (chain, sig, wallet, mint, ts, logged_at, kind, symbol,
                amount, usd, their_price, our_price, liq, mcap, fresh, grp, label) VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def reprice_copytrades():
    now = time.time()
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT chain, sig, wallet, mint, ts, p1h, p24h, p7d FROM copytrades WHERE kind='BUY' AND p7d IS NULL "
            "AND ts > ?", (now - 9 * 86400,))]
    due = [(r, h) for r in rows for h, secs in sig.HORIZONS.items() if r["p" + h] is None and now >= r["ts"] + secs]
    n = 0
    for chain in {r["chain"] for r, _ in due}:
        m = sig.CHAINS.get(chain)
        if not m:
            continue
        mine = [(r, h) for r, h in due if r["chain"] == chain]
        for mint in {r["mint"] for r, _ in mine}:
            m._tok.pop(mint, None)
        prices = m.token_info(list({r["mint"] for r, _ in mine}))
        with sig._db_lock, sig.db() as c:
            for r, h in mine:
                p = (prices.get(r["mint"]) or {}).get("price")
                if p:
                    c.execute(f"UPDATE copytrades SET p{h}=? WHERE chain=? AND sig=? AND wallet=? AND mint=?",
                              (p, chain, r["sig"], r["wallet"], r["mint"]))
                    n += 1
    return n


# ----------------------------------------------------------- backtest -----
def _slip(size, liq):
    """Price impact of `size` USD in a constant-product pool holding `liq` USD in total."""
    if not liq:
        return 0.03
    return min(0.5, size / (liq / 2))


_bt_cache = {}


def backtest(m, size=None, days=60):
    s = settings()
    size = float(size or s["copy_size_usd"])
    key = (m.CHAIN, size)
    hit = _bt_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    since = time.time() - days * 86400
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM copytrades WHERE chain=? AND ts>=? ORDER BY ts", (m.CHAIN, since))]
    sells = defaultdict(list)
    for r in rows:
        if r["kind"] == "SELL" and r["fresh"] and r["our_price"]:
            sells[(r["wallet"], r["mint"])].append(r)
    trades = []
    now = time.time()
    bl = bots()
    for r in rows:
        if r["kind"] != "BUY" or not r["fresh"] or not r["our_price"] or _norm(r["wallet"]) in bl:
            continue
        entry, cost = r["our_price"], s["fee_pct"] / 100 + 2 * _slip(size, r["liq"])
        ex = next((x for x in sells[(r["wallet"], r["mint"])] if x["ts"] > r["ts"]), None)
        if ex:
            exit_p, closed, how, held = ex["our_price"], True, "followed their sell", ex["ts"] - r["ts"]
        elif r["p7d"]:
            exit_p, closed, how, held = r["p7d"], True, "7-day time stop", 7 * 86400
        else:
            exit_p = r["p24h"] or r["p1h"]
            closed, how, held = False, "open", now - r["ts"]
        ret = (exit_p / entry - 1 - cost) if exit_p else None
        chase = (entry / r["their_price"] - 1) if r["their_price"] else None
        trades.append({**{k: r[k] for k in ("wallet", "label", "grp", "mint", "symbol", "ts", "liq", "mcap")},
                       "entry": entry, "exit": exit_p, "ret": ret, "closed": closed, "how": how, "held": held,
                       "cost": cost, "chase": chase,
                       "ret24": (r["p24h"] / entry - 1 - cost) if r["p24h"] else None})
    per = defaultdict(list)
    for t in trades:
        per[t["wallet"]].append(t)
    out = []
    for w, ts in per.items():
        closed = [t for t in ts if t["closed"] and t["ret"] is not None]
        rets = sorted(t["ret"] for t in closed)
        r24 = [t["ret24"] for t in ts if t["ret24"] is not None]
        wi = sig.CHAINS[m.CHAIN].WALLETS.get(w) or {}
        row = {"wallet": w, "label": wi.get("label") or ts[-1]["label"], "group": wi.get("group") or ts[-1]["grp"],
               "copied": len(ts), "closed": len(closed),
               "win_pct": sum(x > 0 for x in rets) / len(rets) * 100 if rets else None,
               "avg_ret": sum(rets) / len(rets) * 100 if rets else None,
               "median_ret": rets[len(rets) // 2] * 100 if rets else None,
               "pnl_usd": sum(rets) * size if rets else 0.0,
               "best": rets[-1] * 100 if rets else None, "worst": rets[0] * 100 if rets else None,
               "avg_hold_h": sum(t["held"] for t in closed) / len(closed) / 3600 if closed else None,
               "hold24_avg": sum(r24) / len(r24) * 100 if r24 else None,
               "avg_chase": (sum(t["chase"] for t in ts if t["chase"] is not None) /
                             max(1, sum(t["chase"] is not None for t in ts)) * 100)}
        n_min = s["mute_min_trades"]
        if len(closed) < n_min:
            row["verdict"] = "LEARNING"
        elif row["avg_ret"] > 0 and row["win_pct"] >= 40:
            row["verdict"] = "COPY"
        elif row["avg_ret"] < 0:
            row["verdict"] = "MUTE"
        else:
            row["verdict"] = "MIXED"
        out.append(row)
    out.sort(key=lambda r: (r["verdict"] != "COPY", -(r["pnl_usd"] or 0)))
    res = {"size": size, "fee_pct": s["fee_pct"], "wallets": out,
           "trades": sorted(trades, key=lambda t: -t["ts"])[:200],
           "total_pnl": sum(r["pnl_usd"] for r in out)}
    _bt_cache[key] = (time.time(), res)
    return res


def verdicts(m):
    try:
        return {w["wallet"]: w["verdict"] for w in backtest(m)["wallets"]}
    except Exception:
        return {}


def muted(m, wallet):
    s = settings()
    return bool(s["auto_mute"]) and verdicts(m).get(wallet) == "MUTE"


# ---------------------------------------------------- chase / skip --------
def chase_verdict(e, t):
    """t = fresh token_info for the coin."""
    s = settings()
    their, now = e.get("price"), t.get("price")
    liq, mc = t.get("liquidity"), t.get("market_cap")
    size = s["copy_size_usd"]
    move = (now / their - 1) * 100 if their and now else None
    slip = _slip(size, liq) * 100 if liq else None
    entry_mc = mc / (1 + move / 100) if mc and move is not None and move > -99 else None
    if move is not None and move >= s["late_pct"]:
        v = f"⛔ SKIP - LATE, already {move:+.0f}% since their buy"
    elif slip is not None and slip >= s["max_slip_pct"]:
        v = f"⛔ SKIP - ~{slip:.1f}% slippage for ${size:,.0f} (pool {sig.fmt_usd(liq)})"
    elif move is not None and move < 5:
        v = f"✅ EARLY - you'd pay {move:+.1f}% vs them"
    elif move is not None:
        v = f"🟡 OK - {move:+.0f}% since their buy"
    else:
        v = "❔ no live price yet"
    detail = []
    if entry_mc:
        detail.append(f"their entry mcap {sig.fmt_usd(entry_mc)}")
    if slip is not None:
        detail.append(f"~{slip:.1f}% slip for ${size:,.0f}")
    if t.get("change_1h") is not None:
        detail.append(f"1h {t['change_1h']:+.0f}%")
    return {"move_pct": move, "slip_pct": slip, "entry_mcap": entry_mc, "line": v,
            "detail": " · ".join(detail), "skip": v.startswith("⛔")}


def copy_hint(m, e, t):
    """One-glance answer for a tracked BUY: CONSIDER / WATCH / SKIP, and why. Uses cached data only (cheap)."""
    if e.get("kind") != "BUY" or not e.get("mint") or e["mint"] in m.QUOTES:
        return None
    v = chase_verdict(e, t)
    wv = verdicts(m).get(e["wallet"], "LEARNING")
    cached = sig._risk.get((m.CHAIN, e["mint"]))
    rv = cached[1]["verdict"] if cached else None
    skip, why = [], []
    if is_bot(e["wallet"]):
        skip.append("known bot")
    if v["skip"]:
        skip.append(v["line"].replace("⛔ SKIP - ", ""))
    if rv and rv.startswith("HIGH"):
        skip.append("high-risk token")
    if wv == "MUTE":
        skip.append("this wallet loses money when copied")
    if e.get("ts") and time.time() - e["ts"] > 3600:
        why.append("over an hour old")
    if v["move_pct"] is not None and not v["skip"]:
        why.append(f"{v['move_pct']:+.0f}% since their buy")
    if v["slip_pct"] is not None:
        why.append(f"~{v['slip_pct']:.1f}% slippage for ${settings()['copy_size_usd']:,.0f}")
    why.append({"COPY": "proven wallet 🏅", "MIXED": "wallet: mixed record", "LEARNING": "wallet: not enough history yet"}.get(wv, ""))
    if rv:
        why.append("risk " + rv.lower())
    else:
        why.append("risk not checked yet - open the token")
    if skip:
        call = "⛔ SKIP"
    elif wv == "COPY" and v["move_pct"] is not None and v["move_pct"] < 15 and not (e.get("ts") and time.time() - e["ts"] > 3600):
        call = "✅ CONSIDER"
    else:
        call = "👀 WATCH"
    return {"call": call, "reasons": skip + [w for w in why if w], "move_pct": v["move_pct"], "slip_pct": v["slip_pct"],
            "wallet": wv, "risk": rv}


# ----------------------------------------------------------- risk gate ----
def risk(m, mint):
    try:
        r = sig.risk_checks(m, mint)
    except Exception:
        return None
    bad = [c["name"] for c in r["checks"] if c["status"] == "bad"]
    warn = [c["name"] for c in r["checks"] if c["status"] == "warn"]
    return {"verdict": r["verdict"], "bad": bad, "warn": warn,
            "line": ("🚩 HIGH RISK: " if r["verdict"].startswith("HIGH") else
                     "⚠️ CAUTION: " if r["verdict"].startswith("CAUTION") else "🛡 risk OK") +
                    (", ".join(bad + warn) if bad or warn else "")}


# ----------------------------------------------------------- trade links --
def links(m, mint, pair=None, dex_url=None):
    ch = m.CHAIN
    out = []
    if ch == "solana":
        out += [("Jupiter", f"https://jup.ag/swap/SOL-{mint}"), ("GMGN", f"https://gmgn.ai/sol/token/{mint}")]
        if pair:
            out.append(("Photon", f"https://photon-sol.tinyastro.io/en/lp/{pair}"))
    elif ch in ("base", "ethereum"):
        out += [("Uniswap", f"https://app.uniswap.org/swap?chain={'mainnet' if ch == 'ethereum' else 'base'}&outputCurrency={mint}"),
                ("GMGN", f"https://gmgn.ai/{'eth' if ch == 'ethereum' else 'base'}/token/{mint}")]
    elif ch == "bsc":
        out += [("PancakeSwap", f"https://pancakeswap.finance/swap?chain=bsc&outputCurrency={mint}"),
                ("GMGN", f"https://gmgn.ai/bsc/token/{mint}")]
    out.append(("DexScreener", dex_url or f"https://dexscreener.com/{getattr(m, 'PAGE', {}).get('dexscreener', ch)}/{mint}"))
    return out


def links_html(m, mint, pair=None, dex_url=None):
    return " · ".join(f'<a href="{u}">{n}</a>' for n, u in links(m, mint, pair, dex_url))


# ---------------------------------------------------------------- exits ---
def bag(m, wallet, mint, days=30):
    since = time.time() - days * 86400
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT kind, amount, usd, ts FROM copytrades WHERE chain=? AND wallet=? AND mint=? AND ts>=?",
            (m.CHAIN, wallet, mint, since))]
    b = {"bought": 0.0, "sold": 0.0, "buy_usd": 0.0, "sold_24h": 0.0, "first_buy": None}
    for r in rows:
        if r["kind"] == "BUY":
            b["bought"] += r["amount"] or 0
            b["buy_usd"] += r["usd"] or 0
            b["first_buy"] = min(b["first_buy"] or r["ts"], r["ts"])
        else:
            b["sold"] += r["amount"] or 0
            if r["ts"] >= time.time() - 86400:
                b["sold_24h"] += r["amount"] or 0
    b["avg_buy"] = b["buy_usd"] / b["bought"] if b["bought"] else None
    return b


def exit_line(m, e):
    b = bag(m, e["wallet"], e["mint"])
    if not b["bought"]:
        return "no tracked buy - may be an older bag or an airdrop"
    pct = min(100, (e.get("amount") or 0) / b["bought"] * 100)
    out = min(100, b["sold"] / b["bought"] * 100)
    pnl = f" · their P&L {((e.get('price') or 0) / b['avg_buy'] - 1) * 100:+.0f}%" if b["avg_buy"] and e.get("price") else ""
    return f"🔻 sold {pct:.0f}% of their bag · {out:.0f}% out in total{pnl}"


def my_holdings(m):
    s = settings()
    mine = {x.lower() for x in s["my_tokens"]}
    with sig._db_lock, sig.db() as c:
        for r in c.execute("""SELECT mint, SUM(CASE WHEN kind='BUY' THEN amount ELSE -amount END) net
                              FROM copytrades WHERE chain=? AND LOWER(COALESCE(grp,''))='me' GROUP BY mint""",
                           (m.CHAIN,)):
            if (r["net"] or 0) > 0:
                mine.add(r["mint"].lower())
    return mine


def check_exits(events, m):
    held = my_holdings(m)
    sells = [e for e in events if e.get("kind") == "SELL" and e.get("mint") and not is_me(e)]
    for e in sells:
        if e["mint"].lower() in held and e.get("alert") is False:   # alert wallets already get the exit line
            sig.alert(("held-exit", m.CHAIN, e["sig"], e["wallet"]),
                      f"⚠️ <b>YOU HOLD {e.get('symbol')}</b> - {e['label']} ({e['group']}) sold "
                      f"{sig.fmt_usd(e.get('usd'))} [{m.CHAIN}]\n{exit_line(m, e)}\n{links_html(m, e['mint'])}",
                      prio="high")
    for mint in {e["mint"] for e in sells}:
        since = time.time() - 14 * 86400
        with sig._db_lock, sig.db() as c:
            wallets = [r["wallet"] for r in c.execute(
                "SELECT DISTINCT wallet FROM copytrades WHERE chain=? AND mint=? AND kind='BUY' AND ts>=?",
                (m.CHAIN, mint, since))]
        dumping = []
        for w in wallets:
            b = bag(m, w, mint, 14)
            if b["bought"] and b["sold_24h"] >= 0.5 * b["bought"]:
                dumping.append(sig.CHAINS[m.CHAIN].WALLETS.get(w, {}).get("label") or w[:6])
        if len(dumping) >= 2:
            sym = next((e.get("symbol") for e in sells if e["mint"] == mint), mint[:6])
            you = mint.lower() in held
            sig.alert(("exit-cluster", m.CHAIN, mint, len(dumping), int(time.time() // 86400)),
                      f"🚪 <b>EXIT CLUSTER {sym}</b> [{m.CHAIN}]{' - ⚠️ YOU HOLD THIS' if you else ''}\n"
                      f"{len(dumping)} tracked wallets sold half+ of their bag in 24h: {', '.join(dumping)}\n"
                      f"{links_html(m, mint)}", prio="high")


# ----------------------------------------------------------- confluence ---
def _mentions(symbol):
    if not symbol:
        return 0
    try:
        import xfeed
        posts = xfeed._cache.get("data") or []
    except Exception:
        return 0
    tag = "$" + symbol.lower()
    return sum(tag in (p.get("text") or "").lower() for p in posts)


def confluence(m, hours=24, limit=25):
    since = time.time() - hours * 3600
    vd = verdicts(m)
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT wallet, mint, kind, usd, ts, symbol, their_price FROM copytrades WHERE chain=? AND ts>=?",
            (m.CHAIN, since))]
    by = defaultdict(lambda: {"buyers": {}, "sellers": set(), "buy_usd": 0.0, "first": None, "first_price": None,
                              "symbol": None})
    bl = bots()
    for r in rows:
        if r["mint"] in m.QUOTES or _norm(r["wallet"]) in bl:
            continue
        d = by[r["mint"]]
        d["symbol"] = d["symbol"] or r["symbol"]
        if r["kind"] == "BUY":
            d["buyers"][r["wallet"]] = vd.get(r["wallet"], "LEARNING")
            d["buy_usd"] += r["usd"] or 0
            if d["first"] is None or r["ts"] < d["first"]:
                d["first"], d["first_price"] = r["ts"], r["their_price"]
        else:
            d["sellers"].add(r["wallet"])
    if not by:
        return []
    flows = {f["mint"]: f for f in sig.net_flows(m, hours)}
    info = m.token_info(list(by))
    out = []
    for mint, d in by.items():
        if not d["buyers"]:
            continue
        t = info.get(mint) or {}
        comp = {}
        weight = sum({"COPY": 1.5, "MUTE": 0.3}.get(v, 1.0) for v in d["buyers"].values())
        comp["smart buyers"] = min(45, round(15 * weight))
        net_sellers = len(d["sellers"] - set(d["buyers"]))
        sold_back = len(d["sellers"] & set(d["buyers"]))
        if sold_back or net_sellers:
            comp["sellers"] = -min(25, 8 * sold_back + 4 * net_sellers)
        f = flows.get(mint)
        if f and f.get("net_usd"):
            comp["exchange flow"] = 12 if f["net_usd"] > 0 else -12
        vol, liq, ch1 = t.get("volume_24h"), t.get("liquidity"), t.get("change_1h")
        if vol and liq:
            r = vol / liq
            comp["volume"] = 10 if 1 <= r <= 20 else (-5 if r > 40 else 0)
        if ch1 is not None:
            comp["momentum 1h"] = 8 if 0 < ch1 <= 40 else (-6 if ch1 < -15 else 0)
        n = _mentions(d["symbol"] or t.get("symbol"))
        if n:
            comp["news mentions"] = min(10, 5 * n)
        cached = sig._risk.get((m.CHAIN, mint))
        if cached:
            v = cached[1]["verdict"]
            if v.startswith("HIGH"):
                comp["risk"] = -35
            elif v.startswith("CAUTION"):
                comp["risk"] = -10
        if d["first_price"] and t.get("price"):
            run = (t["price"] / d["first_price"] - 1) * 100
            if run > 100:
                comp["already ran"] = -12
        else:
            run = None
        score = max(0, min(100, 20 + sum(comp.values())))
        out.append({"mint": mint, "symbol": d["symbol"] or t.get("symbol"), "score": score, "parts": comp,
                    "buyers": len(d["buyers"]), "copy_buyers": sum(v == "COPY" for v in d["buyers"].values()),
                    "buy_usd": d["buy_usd"], "sellers": len(d["sellers"]), "run_pct": run,
                    "price": t.get("price"), "market_cap": t.get("market_cap"), "liquidity": liq,
                    "change_1h": ch1, "first": d["first"], "links": links(m, mint, t.get("pair"), t.get("url"))})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def check_confluence(m, mints):
    if not mints:
        return
    thr = settings()["confluence_alert"]
    for x in confluence(m):
        if x["mint"] not in mints or x["score"] < thr:
            continue
        rk = risk(m, x["mint"]) or {}
        if (rk.get("verdict") or "").startswith("HIGH") and settings()["risk_gate"] == "block":
            continue
        parts = ", ".join(f"{k} {v:+d}" for k, v in x["parts"].items())
        link_html = " · ".join('<a href="%s">%s</a>' % (u, n) for n, u in x["links"])
        sig.alert(("confluence", m.CHAIN, x["mint"], int(time.time() // 86400)),
                  f"🔥 <b>HOT: {x['symbol']}</b> score {x['score']}/100 [{m.CHAIN}]\n"
                  f"{x['buyers']} tracked buyers ({x['copy_buyers']} proven) · {sig.fmt_usd(x['buy_usd'])} in 24h"
                  f" · mcap {sig.fmt_usd(x['market_cap'])}\n{parts}\n{rk.get('line', '')}\n{link_html}",
                  prio="high" if x["score"] >= 85 else "normal")


# ---------------------------------------------------- alert text pieces ---
def buy_extra(m, e, t):
    """Extra lines for a BUY alert + whether to send it at all."""
    s = settings()
    v = chase_verdict(e, t)
    rk = risk(m, e["mint"]) or {}
    lines = [v["line"] + (f"\n{v['detail']}" if v["detail"] else "")]
    send, prefix = True, ""
    if rk:
        lines.append(rk["line"])
        if rk["verdict"].startswith("HIGH"):
            if s["risk_gate"] == "block":
                send = False
            elif s["risk_gate"] == "tag":
                prefix = "🚩 "
    vd = verdicts(m).get(e["wallet"])
    if vd == "COPY":
        lines.append("🏅 proven wallet (profitable to copy)")
    elif vd == "MUTE" and s["auto_mute"]:
        send = False
    lines.append(links_html(m, e["mint"], t.get("pair"), t.get("url") or e.get("dex_url")))
    return prefix, "\n".join(lines), send, v, rk


def sell_extra(m, e, t):
    return exit_line(m, e) + "\n" + links_html(m, e["mint"], t.get("pair"), t.get("url") or e.get("dex_url"))


# ------------------------------------------------------ alert hygiene -----
def quiet_now():
    q = (settings()["quiet_hours"] or "").strip()
    if not q or "-" not in q:
        return False
    try:
        a, b = (int(x) for x in q.split("-"))
    except ValueError:
        return False
    h = datetime.now(TZ).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def dispatch(text, prio="normal", group=None):
    """Send now, or park in the digest (quiet hours / digest groups). High priority always goes."""
    s = settings()
    in_digest = group and group.lower() in {g.lower() for g in s["digest_groups"]}
    if prio != "high" and (quiet_now() or in_digest):
        with sig._db_lock, sig.db() as c:
            c.execute("INSERT INTO digest (ts, text) VALUES (?,?)", (int(time.time()), text))
        return "queued"
    _send(text)
    return "sent"


def _send(text):
    try:
        import bot
        if bot.BOT_TOKEN:
            bot.send(text)
    except Exception as e:
        print(f"telegram alert failed: {e}")


_last_flush = [time.time()]


def flush_digest(force=False):
    s = settings()
    if not force and (quiet_now() or time.time() - _last_flush[0] < s["digest_minutes"] * 60):
        return 0
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute("SELECT id, ts, text FROM digest ORDER BY id")]
        if rows:
            c.execute("DELETE FROM digest WHERE id<=?", (rows[-1]["id"],))
    _last_flush[0] = time.time()
    if not rows:
        return 0
    lines = []
    for r in rows[:40]:
        first = r["text"].split("\n", 1)[0]
        lines.append(f"{datetime.fromtimestamp(r['ts'], TZ):%H:%M} {first}")
    more = f"\n…and {len(rows) - 40} more" if len(rows) > 40 else ""
    _send(f"🗞 <b>Digest - {len(rows)} alerts</b>\n" + "\n".join(lines) + more)
    return len(rows)


_started = False


def start():
    global _started
    if _started:
        return
    _started = True

    def loop():
        i = 0
        while True:
            time.sleep(60)
            i += 1
            try:
                flush_digest()
            except Exception as e:
                print(f"digest flush failed: {e}")
            if i % 10 == 0:
                try:
                    reprice_copytrades()
                except Exception as e:
                    print(f"copytrade reprice failed: {e}")
    threading.Thread(target=loop, daemon=True, name="edge").start()
