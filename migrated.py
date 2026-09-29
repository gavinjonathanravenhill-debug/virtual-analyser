"""
Migrated-coin scanner (Solana) - the "Migrated" column of Axiom / GMGN, with the Audit filters built in,
plus forensics on manipulation spikes.

  Discovery  - GeckoTerminal new pools on the DEXes pump.fun / LetsBonk coins graduate to (free, no key)
  Metrics    - liquidity, mcap, volume, buys/sells, unique buyers, price change (GeckoTerminal, batched)
  Audit      - bundlers %, snipers %, insiders %, dev %, top-10 %, holders, LP burned, authorities
               (Solana Tracker Data API - set SOLANATRACKER_API_KEY; free tier = 2.5k calls/month, so audits
               are capped per day and only run on coins that already pass the metric filters)
  Spikes     - 1-minute candles: finds volume+price outlier candles, classifies the move
               (pump & dump / held / still running), and from the pool's recent trades names the wallets
               that bought BEFORE the spike and the ones that SOLD INTO it, plus wash-trading share.
  Alerts     - "clean migration passes filters" and "manipulation spike" (group "Migrated", so they can go
               to the digest), one-click "track these wallets".
"""

import json
import os
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

import edge
import solana_signals as sig

GT = "https://api.geckoterminal.com/api/v2"
ST = "https://data.solanatracker.io"
ST_KEY = os.getenv("SOLANATRACKER_API_KEY", "").strip()
GT_GAP = float(os.getenv("GT_GAP_SECONDS", "6.5"))     # free GeckoTerminal ~10 calls/min
SCAN_SECONDS = int(os.getenv("MIGRATED_SCAN_SECONDS", "300"))

edge.DEFAULTS.update({
    "mg_dexes": ["pumpswap", "raydium-cpmm", "raydium-cp", "meteora-damm-v2"],
    "mg_max_age_h": 24,
    "mg_min_liq": 15000,
    "mg_min_mcap": 40000,
    "mg_max_mcap": 10000000,
    "mg_min_vol_1h": 20000,
    "mg_min_buyers_1h": 60,
    "mg_max_tx_per_buyer": 6,        # lots of trades per unique buyer = bots / wash
    "mg_max_vol_mcap_1h": 5,         # 1h volume above this many x the mcap = wash / bot churn
    "mg_min_liq_mcap_pct": 4,        # liquidity under this % of mcap = the mcap is fake / one sell moves it
    "mg_max_bundlers": 15,
    "mg_max_snipers": 10,
    "mg_max_insiders": 10,
    "mg_max_dev": 5,
    "mg_max_top10": 30,
    "mg_min_holders": 300,
    "mg_audit_per_day": 70,
    "mg_alerts": "both",             # clean | spikes | both | off
})
edge._set_cache[1] = None   # settings cached before these defaults existed must be rebuilt

_s = requests.Session()


# ------------------------------------------------------------ fetchers ----
def gt(path, interactive=False, **params):
    import gt_limit
    r = gt_limit.get(GT + path, params=params, interactive=interactive)
    if r.status_code == 429:
        raise RuntimeError("GeckoTerminal is rate limiting - try again in a minute")
    r.raise_for_status()
    return r.json()


def st(path):
    if not ST_KEY:
        return None
    r = _s.get(ST + path, headers={"x-api-key": ST_KEY}, timeout=20)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"Solana Tracker said {r.status_code} (key / monthly limit)")
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_pools(j):
    toks = {t["id"]: t["attributes"] for t in j.get("included") or [] if t.get("type") == "token"}
    out = []
    for p in j.get("data") or []:
        a, rel = p.get("attributes") or {}, p.get("relationships") or {}
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
        mint = base_id.split("_", 1)[-1]
        t = toks.get(base_id) or {}
        tx = a.get("transactions") or {}
        h1, h24, m5 = tx.get("h1") or {}, tx.get("h24") or {}, tx.get("m5") or {}
        vol, chg = a.get("volume_usd") or {}, a.get("price_change_percentage") or {}
        out.append({
            "pool": a.get("address"), "mint": mint, "symbol": t.get("symbol") or (a.get("name") or "").split(" /")[0],
            "name": t.get("name"), "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
            "created": _ts(a.get("pool_created_at")), "price": _num(a.get("base_token_price_usd")),
            "mcap": _num(a.get("market_cap_usd")) or _num(a.get("fdv_usd")), "liq": _num(a.get("reserve_in_usd")),
            "vol_m5": _num(vol.get("m5")), "vol_1h": _num(vol.get("h1")), "vol_24h": _num(vol.get("h24")),
            "chg_m5": _num(chg.get("m5")), "chg_1h": _num(chg.get("h1")), "chg_24h": _num(chg.get("h24")),
            "buys_1h": h1.get("buys"), "sells_1h": h1.get("sells"), "buyers_1h": h1.get("buyers"),
            "sellers_1h": h1.get("sellers"), "buys_24h": h24.get("buys"), "sells_24h": h24.get("sells"),
            "buyers_24h": h24.get("buyers"), "buys_m5": m5.get("buys"), "sells_m5": m5.get("sells"),
        })
    return out


# ----------------------------------------------------------------- DB -----
def init_tables():
    with sig._db_lock, sig.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS migrated (
            mint TEXT PRIMARY KEY, pool TEXT, symbol TEXT, name TEXT, dex TEXT, created REAL, first_seen REAL,
            metrics TEXT, audit TEXT, audited_at REAL, spike TEXT, spiked_at REAL, status TEXT, reasons TEXT,
            alerted TEXT DEFAULT '')""")


def _save(row):
    with sig._db_lock, sig.db() as c:
        c.execute("""INSERT INTO migrated (mint, pool, symbol, name, dex, created, first_seen, metrics, status, reasons)
                     VALUES (?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(mint) DO UPDATE SET pool=excluded.pool, symbol=excluded.symbol,
                     metrics=excluded.metrics, status=excluded.status, reasons=excluded.reasons""",
                  (row["mint"], row["pool"], row["symbol"], row.get("name"), row.get("dex"), row.get("created"),
                   time.time(), json.dumps(row), row.get("status"), json.dumps(row.get("reasons") or [])))


def _set(mint, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    with sig._db_lock, sig.db() as c:
        c.execute(f"UPDATE migrated SET {cols} WHERE mint=?",
                  [json.dumps(v) if isinstance(v, (dict, list)) else v for v in kw.values()] + [mint])


def load(hours=None):
    s = edge.settings()
    since = time.time() - (hours or s["mg_max_age_h"]) * 3600
    with sig._db_lock, sig.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM migrated WHERE created>=? OR first_seen>=?", (since, since))]
    for r in rows:
        for k in ("metrics", "audit", "spike", "reasons"):
            r[k] = json.loads(r[k]) if r.get(k) else None
    return rows


# ------------------------------------------------------------- filters ----
def rug_reason(p):
    """Price collapsed or liquidity pulled - dead, not just filtered."""
    ch1, ch24, liq, mc = p.get("chg_1h"), p.get("chg_24h"), p.get("liq"), p.get("mcap")
    if ch1 is not None and ch1 <= -90:
        return f"rugged: {ch1:.0f}% in 1h"
    if ch24 is not None and ch24 <= -95:
        return f"rugged: {ch24:.0f}% in 24h"
    if liq is not None and liq < 1500:
        return f"rugged: liquidity pulled ({sig.fmt_usd(liq)})"
    if mc is not None and mc < 2000:
        return f"rugged: mcap {sig.fmt_usd(mc)}"
    return None


def metric_reasons(p, s):
    why = []
    age_h = (time.time() - (p.get("created") or time.time())) / 3600
    if age_h > s["mg_max_age_h"]:
        why.append(f"older than {s['mg_max_age_h']:g}h")
    if (p.get("liq") or 0) < s["mg_min_liq"]:
        why.append(f"liquidity {sig.fmt_usd(p.get('liq'))} < {sig.fmt_usd(s['mg_min_liq'])}")
    mc = p.get("mcap") or 0
    if mc < s["mg_min_mcap"] or mc > s["mg_max_mcap"]:
        why.append(f"mcap {sig.fmt_usd(mc)} outside range")
    if (p.get("vol_1h") or 0) < s["mg_min_vol_1h"]:
        why.append(f"1h volume {sig.fmt_usd(p.get('vol_1h'))} too low")
    buyers = p.get("buyers_1h") or 0
    if buyers < s["mg_min_buyers_1h"]:
        why.append(f"only {buyers} buyers in 1h")
    if mc and (p.get("vol_1h") or 0) / mc > s["mg_max_vol_mcap_1h"]:
        why.append(f"1h volume {(p.get('vol_1h') or 0) / mc:.0f}x the mcap (wash / bots)")
    if mc and p.get("liq") is not None and p["liq"] / mc * 100 < s["mg_min_liq_mcap_pct"]:
        why.append(f"liquidity only {p['liq'] / mc * 100:.1f}% of mcap (fake mcap)")
    tx = (p.get("buys_1h") or 0) + (p.get("sells_1h") or 0)
    uniq = buyers + (p.get("sellers_1h") or 0)
    if uniq and tx / uniq > s["mg_max_tx_per_buyer"]:
        why.append(f"{tx / uniq:.1f} trades per wallet (bots / wash)")
    return why


def _pct(obj, *keys):
    """Pull a percentage out of Solana Tracker's nested risk objects, whatever the exact field name."""
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        for k in keys + ("totalPercentage", "percentage", "total_percentage", "pct"):
            if k in obj and isinstance(obj[k], (int, float)):
                return float(obj[k])
    return None


def audit(mint):
    j = st(f"/tokens/{mint}")
    if not j:
        return None
    risk = j.get("risk") or {}
    pools = j.get("pools") or []
    holders = j.get("holders") or (pools[0].get("holders") if pools else None)
    lp_burn = max([_num(p.get("lpBurn")) or 0 for p in pools] or [0])
    a = {
        "score": risk.get("score"), "rugged": risk.get("rugged"),
        "bundlers": _pct(risk.get("bundlers"), "totalPercentage") or _pct(risk, "totalBundlerPercentage"),
        "snipers": _pct(risk.get("snipers"), "totalPercentage"),
        "insiders": _pct(risk.get("insiders"), "totalPercentage"),
        "dev": _pct(risk.get("dev"), "percentage"),
        "top10": _pct(risk.get("top10")) if not isinstance(risk.get("top10"), dict) else _pct(risk.get("top10"), "percentage"),
        "holders": holders, "lp_burn": lp_burn,
        "flags": [f"{r.get('name')} ({r.get('level')})" for r in risk.get("risks") or [] if r.get("level") in ("danger", "warning")],
    }
    if a["top10"] is None:
        for r in risk.get("risks") or []:
            if "top 10" in (r.get("name") or "").lower():
                a["top10"] = _num("".join(ch for ch in (r.get("value") or "") if ch.isdigit() or ch == "."))
    return a


def audit_reasons(a, s):
    if not a:
        return []
    why = []
    for k, lim, label in (("bundlers", "mg_max_bundlers", "bundled"), ("snipers", "mg_max_snipers", "snipers"),
                          ("insiders", "mg_max_insiders", "insiders"), ("dev", "mg_max_dev", "dev holds"),
                          ("top10", "mg_max_top10", "top 10 hold")):
        if a.get(k) is not None and a[k] > s[lim]:
            why.append(f"{label} {a[k]:.0f}% > {s[lim]:g}%")
    if a.get("holders") is not None and a["holders"] < s["mg_min_holders"]:
        why.append(f"only {a['holders']} holders")
    if a.get("rugged"):
        why.append("flagged rugged")
    return why


_audits_today = {"day": None, "n": 0}


def _audit_budget():
    s = edge.settings()
    day = time.strftime("%Y-%m-%d")
    if _audits_today["day"] != day:
        _audits_today.update(day=day, n=0)
    return _audits_today["n"] < s["mg_audit_per_day"]


# --------------------------------------------------------------- spikes ---
def candles(pool, limit=360, interactive=False):
    j = gt(f"/networks/solana/pools/{pool}/ohlcv/minute", interactive=interactive, aggregate=1, limit=limit,
           currency="usd", token="base")
    rows = ((j.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in rows], key=lambda x: x["t"])


def trades(pool, interactive=False):
    j = gt(f"/networks/solana/pools/{pool}/trades", interactive=interactive)
    out = []
    for d in j.get("data") or []:
        a = d.get("attributes") or {}
        out.append({"t": _ts(a.get("block_timestamp")), "kind": a.get("kind"), "usd": _num(a.get("volume_in_usd")) or 0,
                    "wallet": a.get("tx_from_address"), "tx": a.get("tx_hash")})
    return sorted([t for t in out if t["t"]], key=lambda x: x["t"])


def find_spikes(cs, vol_x=8, move_pct=15):
    """Candles whose volume is an outlier vs the rolling median AND that move price hard."""
    spikes = []
    for i in range(10, len(cs)):
        win = [c["v"] for c in cs[max(0, i - 30):i] if c["v"]]
        med = statistics.median(win) if win else 0
        c = cs[i]
        move = (c["h"] / c["o"] - 1) * 100 if c["o"] else 0
        drop = (c["l"] / c["o"] - 1) * 100 if c["o"] else 0
        if med and c["v"] >= vol_x * med and (move >= move_pct or drop <= -move_pct):
            if spikes and c["t"] - spikes[-1]["end"] <= 180:      # merge back-to-back spike candles
                sp = spikes[-1]
                sp.update(end=c["t"], high=max(sp["high"], c["h"]), low=min(sp["low"], c["l"]), vol=sp["vol"] + c["v"])
                continue
            spikes.append({"start": c["t"], "end": c["t"], "pre": cs[i - 1]["c"], "high": c["h"], "low": c["l"],
                           "vol": c["v"], "vol_x": c["v"] / med, "dir": "up" if move >= -drop else "down"})
    return spikes


def classify(sp, cs):
    after = [c for c in cs if c["t"] > sp["end"]]
    now = cs[-1]["c"] if cs else None
    run = (sp["high"] / sp["pre"] - 1) * 100 if sp["pre"] else None
    if sp["dir"] == "down":
        return "dump", run, None
    if not after or not now:
        return "running", run, None
    giveback = (sp["high"] - now) / (sp["high"] - sp["pre"]) * 100 if sp["high"] > sp["pre"] else 0
    mins = (after[-1]["t"] - sp["end"]) / 60
    if giveback >= 70:
        return "pump & dump", run, giveback
    if giveback <= 35 and mins >= 30:
        return "held", run, giveback
    return "fading" if giveback > 35 else "running", run, giveback


def forensics(sp, trs):
    """Who bought in the 60 min before the spike, who sold into it, and how much looks like wash."""
    pre = defaultdict(float)
    into = defaultdict(float)
    per = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n": 0})
    for t in trs:
        w = t["wallet"]
        if not w:
            continue
        per[w]["n"] += 1
        per[w]["buy" if t["kind"] == "buy" else "sell"] += t["usd"]
        if t["kind"] == "buy" and sp["start"] - 3600 <= t["t"] < sp["start"]:
            pre[w] += t["usd"]
        if t["kind"] == "sell" and sp["start"] <= t["t"] <= sp["end"] + 900:
            into[w] += t["usd"]
    total = sum(v["buy"] + v["sell"] for v in per.values()) or 1
    botset = {w for w in per if edge.is_bot(w) or per[w]["n"] >= edge.BOT_TRADES}
    bot_usd = sum(per[w]["buy"] + per[w]["sell"] for w in botset)
    both = [w for w, v in per.items() if v["buy"] and v["sell"]]
    wash_usd = sum(2 * min(per[w]["buy"], per[w]["sell"]) for w in both)
    top = sorted(per.items(), key=lambda kv: -(kv[1]["buy"] + kv[1]["sell"]))[:5]
    insiders = [w for w in pre if w in into and w not in botset]   # loaded before, sold into the spike: the operators
    return {
        "pre_buyers": sorted(({"wallet": w, "usd": v, "sold_into": into.get(w, 0), "bot": _botlabel(w, per)}
                              for w, v in pre.items()), key=lambda x: -x["usd"])[:15],
        "sold_into": sorted(({"wallet": w, "usd": v, "bought_before": pre.get(w, 0), "bot": _botlabel(w, per)}
                             for w, v in into.items()), key=lambda x: -x["usd"])[:15],
        "bot_pct": bot_usd / total * 100,
        "bots": sorted(({"wallet": w, "usd": per[w]["buy"] + per[w]["sell"], "trades": per[w]["n"],
                         "bot": _botlabel(w, per)} for w in botset), key=lambda x: -x["usd"])[:10],
        "operators": insiders,
        "wash_pct": wash_usd / total * 100,
        "top_wallet_pct": ((top[0][1]["buy"] + top[0][1]["sell"]) / total * 100) if top else 0,
        "top_wallets": [{"wallet": w, "usd": v["buy"] + v["sell"], "trades": v["n"]} for w, v in top],
        "trades_seen": len(trs), "window": [trs[0]["t"], trs[-1]["t"]] if trs else None,
    }


def _botlabel(w, per):
    b = edge.bots().get(w)
    if b:
        return b.get("label") or "known bot"
    return f"bot-like ({per[w]['n']} trades)" if per.get(w, {}).get("n", 0) >= edge.BOT_TRADES else None


def manipulation_score(an, a):
    pts = {}
    sp = an.get("spike") or {}
    if sp:
        pts["spike"] = min(30, int(sp.get("vol_x", 0)))
        if an.get("pattern") == "pump & dump":
            pts["pump & dump"] = 25
    f = an.get("forensics") or {}
    if f.get("bot_pct", 0) > 40:
        pts["bot volume"] = min(20, int(f["bot_pct"] / 4))
    if f.get("wash_pct", 0) > 20:
        pts["wash trading"] = min(20, int(f["wash_pct"] / 2))
    if f.get("top_wallet_pct", 0) > 15:
        pts["one wallet dominates"] = min(15, int(f["top_wallet_pct"] / 2))
    if f.get("operators"):
        pts["bought-before + sold-into wallets"] = min(20, 5 * len(f["operators"]))
    if a:
        if (a.get("bundlers") or 0) > 15:
            pts["bundled"] = 15
        if (a.get("snipers") or 0) > 10:
            pts["snipers"] = 10
    return min(100, sum(pts.values())), pts


def analyse(mint=None, pool=None, interactive=False):
    """Full spike forensics for one coin (on demand or from the scanner)."""
    init_tables()
    if not pool:
        j = gt(f"/networks/solana/tokens/{mint}/pools", interactive=interactive, page=1)
        ps = parse_pools(j)
        if not ps:
            raise RuntimeError("No DEX pool found for that token")
        best = max(ps, key=lambda p: p.get("liq") or 0)
        pool, mint = best["pool"], best["mint"]
        info = best
    else:
        info = {}
    cs = candles(pool, interactive=interactive)
    spikes = find_spikes(cs)
    trs = trades(pool, interactive=interactive)
    main = max(spikes, key=lambda s: (s["high"] / s["pre"] if s["pre"] else 0) * s["vol_x"]) if spikes else None
    out = {"mint": mint, "pool": pool, "info": info, "candles": cs[-240:], "spikes": spikes, "spike": main,
           "analysed_at": time.time()}
    if main:
        pattern, run, giveback = classify(main, cs)
        out.update(pattern=pattern, run_pct=run, giveback_pct=giveback, forensics=forensics(main, trs))
    else:
        out.update(pattern=None, forensics=forensics({"start": time.time(), "end": time.time()}, trs))
    with sig._db_lock, sig.db() as c:
        r = c.execute("SELECT audit FROM migrated WHERE mint=?", (mint,)).fetchone()
    a = json.loads(r["audit"]) if r and r["audit"] else None
    out["manipulation"], out["manipulation_parts"] = manipulation_score(out, a)
    out["audit"] = a
    _set(mint, spike={k: v for k, v in out.items() if k != "candles"}, spiked_at=time.time()) if r else None
    return out


# ---------------------------------------------------------------- scan ----
def scan():
    init_tables()
    s = edge.settings()
    dexes = {d.lower() for d in s["mg_dexes"]}
    found = []
    for page in (1, 2):
        try:
            found += parse_pools(gt("/networks/solana/new_pools", page=page, include="base_token,dex"))
        except Exception as e:
            print(f"migrated scan (new pools) failed: {e}")
            break
    fresh = [p for p in found if (p.get("dex") or "").lower() in dexes]
    known = {r["mint"]: r for r in load()}
    # refresh metrics for coins already on the list (30 pools per call)
    stale = [r["pool"] for m, r in known.items() if m not in {p["mint"] for p in fresh}]
    for i in range(0, min(len(stale), 60), 30):
        try:
            fresh += parse_pools(gt("/networks/solana/pools/multi/" + ",".join(stale[i:i + 30]), include="base_token,dex"))
        except Exception as e:
            print(f"migrated refresh failed: {e}")
    results = []
    for p in {p["mint"]: p for p in fresh}.values():
        prev = known.get(p["mint"]) or {}
        rug = rug_reason(p)
        if rug:
            row = {**p, "reasons": [rug], "status": "rugged"}
            _save(row)
            results.append((row, prev.get("audit"), prev))
            continue
        why = metric_reasons(p, s)
        row = {**p, "reasons": why}
        a = prev.get("audit")
        if not why and not a and ST_KEY and _audit_budget():
            try:
                a = audit(p["mint"])
                _audits_today["n"] += 1
            except Exception as e:
                print(f"audit failed: {e}")
            if a:
                _save({**row, "status": "pending"})
                _set(p["mint"], audit=a, audited_at=time.time())
        row["reasons"] = why + audit_reasons(a, s)
        row["status"] = "pass" if not row["reasons"] else "fail"
        _save(row)
        results.append((row, a, prev))
    # spike forensics: coins moving hard on volume right now (keeps GeckoTerminal calls small)
    movers = sorted((r for r, _, _ in results if r.get("status") != "rugged" and (abs(r.get("chg_m5") or 0) >= 20 or abs(r.get("chg_1h") or 0) >= 60)
                     and (r.get("vol_1h") or 0) >= 10000), key=lambda r: -abs(r.get("chg_m5") or 0))[:3]
    spiked = {}
    for r in movers:
        prev = known.get(r["mint"]) or {}
        if prev.get("spiked_at") and time.time() - prev["spiked_at"] < 1800:
            continue
        try:
            spiked[r["mint"]] = analyse(r["mint"], r["pool"])
        except Exception as e:
            print(f"spike analysis failed: {e}")
    _alerts(results, spiked, s)
    return {"seen": len(found), "migrated": len(results), "pass": sum(r["status"] == "pass" for r, _, _ in results),
            "spikes": len(spiked)}


def _alerts(results, spiked, s):
    mode = s["mg_alerts"]
    if mode == "off":
        return
    import solana_client as sc
    for row, a, prev in results:
        if row["status"] != "pass" or "clean" in (prev.get("alerted") or "") or mode == "spikes":
            continue
        aud = (f"bundlers {a.get('bundlers') or 0:.0f}% · snipers {a.get('snipers') or 0:.0f}% · insiders "
               f"{a.get('insiders') or 0:.0f}% · dev {a.get('dev') or 0:.0f}% · top10 {a.get('top10') or 0:.0f}% · "
               f"{a.get('holders') or '?'} holders") if a else "audit: add SOLANATRACKER_API_KEY for bundler/sniper checks"
        sig.alert(("mg-clean", row["mint"]),
                  f"🆕 <b>MIGRATED {row['symbol']}</b> passes your filters\n"
                  f"mcap {sig.fmt_usd(row.get('mcap'))} · liq {sig.fmt_usd(row.get('liq'))} · 1h vol {sig.fmt_usd(row.get('vol_1h'))}"
                  f" · {row.get('buyers_1h')} buyers · 1h {row.get('chg_1h') or 0:+.0f}%\n{aud}\n"
                  f"{edge.links_html(sc, row['mint'], row.get('pool'))}", group="Migrated")
        _set(row["mint"], alerted=(prev.get("alerted") or "") + "clean,")
    if mode == "clean":
        return
    for mint, an in spiked.items():
        if not an.get("spike") or an.get("manipulation", 0) < 40:
            continue
        f = an["forensics"]
        sym = (an.get("info") or {}).get("symbol") or next((r["symbol"] for r, _, _ in results if r["mint"] == mint), mint[:6])
        sig.alert(("mg-spike", mint, int(an["spike"]["start"])),
                  f"🎢 <b>MANIPULATION SPIKE {sym}</b> score {an['manipulation']}/100\n"
                  f"{an['spike']['vol_x']:.0f}x volume · +{an.get('run_pct') or 0:.0f}% · pattern: <b>{an.get('pattern')}</b>"
                  f"{' (gave back ' + format(an.get('giveback_pct') or 0, '.0f') + '%)' if an.get('giveback_pct') is not None else ''}\n"
                  f"{len(f['pre_buyers'])} wallets loaded in the hour before · {len(f['operators'])} bought-before AND sold-into it"
                  f" · wash ~{f['wash_pct']:.0f}% · bots {f.get('bot_pct', 0):.0f}% · top wallet {f['top_wallet_pct']:.0f}% of volume\n"
                  f"{', '.join(k for k in an['manipulation_parts'])}\n"
                  f"{edge.links_html(sc, mint, an['pool'])}", group="Migrated")


def track_wallets(mint, which="operators", group="Spike insiders"):
    """Add the pre-spike buyers / operators of a coin to the Solana tracker."""
    import solana_client as sc
    with sig._db_lock, sig.db() as c:
        r = c.execute("SELECT symbol, spike FROM migrated WHERE mint=?", (mint,)).fetchone()
    an = json.loads(r["spike"]) if r and r["spike"] else analyse(mint)
    f = an.get("forensics") or {}
    ws = f.get("operators") if which == "operators" else [x["wallet"] for x in f.get("pre_buyers") or [] if not x.get("bot")]
    ws = [w for w in ws or [] if not edge.is_bot(w)]
    sym = (r["symbol"] if r else None) or mint[:6]
    added = []
    for w in (ws or [])[:15]:
        if w in sc.WALLETS:
            continue
        sig.add_wallet(sc, w, f"{sym} {'operator' if which == 'operators' else 'pre-spike'} {w[:4]}", group,
                       note=f"{'bought before + sold into' if which == 'operators' else 'bought before'} the {sym} spike",
                       alert_on=True, min_usd=1000)
        added.append(w)
    try:
        import helius_hook
        helius_hook.resync_async()
    except Exception:
        pass
    return {"added": added, "skipped": len(ws or []) - len(added)}


def summary():
    s = edge.settings()
    rows = load()
    rank = {"pass": 0, "pending": 1, "fail": 2, "rugged": 3}
    rows.sort(key=lambda r: (rank.get(r["status"], 2), -((r.get("metrics") or {}).get("vol_1h") or 0)))
    out = []
    for r in rows[:150]:
        m = r.get("metrics") or {}
        sp = r.get("spike") or {}
        out.append({**{k: m.get(k) for k in ("symbol", "name", "pool", "dex", "created", "mcap", "liq", "vol_1h",
                                             "buyers_1h", "buys_1h", "sells_1h", "chg_m5", "chg_1h", "price")},
                    "mint": r["mint"], "status": r["status"], "reasons": r.get("reasons") or [], "audit": r.get("audit"),
                    "pattern": sp.get("pattern"), "manipulation": sp.get("manipulation"), "spiked_at": r.get("spiked_at")})
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("pass", "fail", "rugged")}
    import gt_limit
    return {"rows": out, "st_key": bool(ST_KEY), "counts": counts, "gt": gt_limit.status(), "audits_today": _audits_today["n"], "status": _status,
            "settings": {k: v for k, v in s.items() if k.startswith("mg_")}}


_status = {"last": None, "last_result": None, "error": None}
_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    init_tables()

    def loop():
        time.sleep(20)
        while True:
            try:
                _status.update(last_result=scan(), last=int(time.time()), error=None)
            except Exception as e:
                _status["error"] = str(e)[:200]
            time.sleep(SCAN_SECONDS)
    if not os.getenv("MIGRATED_SCANNER_OFF"):
        threading.Thread(target=loop, daemon=True, name="migrated").start()
