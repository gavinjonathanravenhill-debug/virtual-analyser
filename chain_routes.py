"""One blueprint factory for every chain tracker page (/solana, /robinhood, ...)."""

import json

from flask import Blueprint, jsonify, render_template, request

import edge
import solana_signals as sig


def make_chain_bp(m, start, profile_fn, addr_ok):
    """m = chain module (solana_client / robinhood_client); start() starts its tracker."""
    name = m.CHAIN
    norm = getattr(m, "norm", lambda a: a)
    bp = Blueprint(name, __name__)
    api = f"/api/{name}"

    def safe(fn):
        try:
            return jsonify(fn())
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    def bad(msg):
        return jsonify({"error": msg}), 400

    def _resync():
        if name == "solana":
            try:
                import helius_hook
                helius_hook.resync_async()
            except Exception as e:
                print(f"helius resync skipped: {e}")

    @bp.route(f"/{name}")
    def page():
        start()
        return render_template("chain.html", cfg=json.dumps(m.PAGE))

    @bp.route(f"{api}/wallets", methods=["GET", "POST", "DELETE"])
    def wallets():
        start()
        if request.method == "POST":
            d = request.get_json(force=True) or {}
            a = norm((d.get("address") or "").strip())
            if not addr_ok(a):
                return bad(f"Not a valid {m.PAGE['addr_hint']}")
            res = safe(lambda: sig.add_wallet(m, a, d.get("label"), d.get("group"), d.get("note"), d.get("alert", True),
                                              d.get("min_usd")))
            _resync()
            return res
        if request.method == "DELETE":
            a = norm((request.args.get("address") or "").strip())
            res = safe(lambda: {"removed": sig.remove_wallet(m, a)})
            _resync()
            return res
        ev = m.tracker.query(limit=3000)
        last, sizes = {}, {}
        for e in ev:
            last.setdefault(e["wallet"], e["ts"])
            if e["kind"] in ("BUY", "SELL") and e.get("usd"):
                sizes.setdefault(e["wallet"], []).append(e["usd"])
        med = {w: sorted(v)[len(v) // 2] for w, v in sizes.items()}
        return jsonify({"status": m.tracker.status,
                        "db_persistent": sig.db_persistent(),
                        "wallets": [{**w, "last_seen": last.get(a), "last_tx": m.tracker.last_tx.get(a),
                                     "checked": a in m.tracker.checked, "median_trade": med.get(a),
                                     "trades": len(sizes.get(a, []))} for a, w in list(m.WALLETS.items())],
                        "exchanges": m.EXCHANGES})

    @bp.route(f"{api}/events")
    def events():
        start()
        kinds = [k for k in (request.args.get("kinds") or "").split(",") if k]
        return jsonify({"status": m.tracker.status, "events": m.tracker.query(
            wallet=request.args.get("wallet") or None, group=request.args.get("group") or None,
            kinds=kinds or None)})

    @bp.route(f"{api}/lookup")
    def lookup_route():
        a = (request.args.get("address") or "").strip()
        if not addr_ok(a):
            return bad(f"Not a valid {m.PAGE['addr_hint']}")
        return safe(lambda: {"address": a, "events": m.lookup(a)})

    @bp.route(f"{api}/token/<mint>")
    def token(mint):
        mint = norm(mint)
        if not addr_ok(mint):
            return bad("Not a valid token address")
        def rep_():
            r = m.token_report(mint)
            info = r.get("info") or {}
            return {**r, "risk": sig.risk_checks(m, mint),
                    "levels": [lv for lv in sig.level_status(m) if lv["mint"] == mint],
                    "trade_links": edge.links(m, mint, info.get("pair"), info.get("url"))}
        return safe(rep_)

    @bp.route(f"{api}/signals")
    def signals():
        start()
        return safe(lambda: {"journal": sig.journal(m), "scorecard": sig.scorecard(m),
                             "min_usd": sig.MIN_USD, "db": sig.DB_PATH})

    @bp.route(f"{api}/netflow")
    def netflow():
        start()
        hours = max(1, min(float(request.args.get("hours", 24)), 24 * 7))
        return safe(lambda: {"hours": hours, "rows": sig.net_flows(m, hours)[:100],
                             "alert_usd": sig.FLOW_ALERT_USD, "alert_pct": sig.FLOW_ALERT_PCT,
                             "alert_floor": sig.FLOW_ALERT_FLOOR})

    @bp.route(f"{api}/edge")
    def edge_route():
        start()
        def build():
            out = {"hot": edge.confluence(m), "backtest": edge.backtest(m, request.args.get("size")),
                   "settings": edge.settings(), "quiet_now": edge.quiet_now(), "groups": sorted(
                       {(w.get("group") or "") for w in m.WALLETS.values()})}
            if name == "solana":
                import helius_hook
                out["webhook"] = helius_hook.status()
            return out
        return safe(build)

    @bp.route(f"{api}/bots", methods=["GET", "POST", "DELETE"])
    def bots_route():
        if request.method == "POST":
            d = request.get_json(force=True) or {}
            return safe(lambda: edge.add_bot(d.get("address"), d.get("label"), d.get("note")))
        if request.method == "DELETE":
            return safe(lambda: {"removed": edge.remove_bot(request.args.get("address"))})
        return safe(lambda: {"bots": sorted(edge.bots().values(), key=lambda b: -(b.get("added") or 0))})

    @bp.route(f"{api}/settings", methods=["POST"])
    def settings_route():
        return safe(lambda: edge.save_settings(request.get_json(force=True) or {}))

    @bp.route(f"{api}/digest", methods=["POST"])
    def digest_route():
        return safe(lambda: {"sent": edge.flush_digest(force=True)})

    @bp.route(f"{api}/insights")
    def insights():
        start()
        return safe(lambda: {"clusters": sig.clusters(m), "levels": sig.level_status(m)})

    @bp.route(f"{api}/levels", methods=["GET", "POST", "DELETE"])
    def levels():
        start()
        if request.method == "POST":
            d = request.get_json(force=True) or {}
            mint = norm((d.get("mint") or "").strip())
            if not addr_ok(mint):
                return bad("Paste the coin's token address")
            try:
                low, high = float(d.get("low")), float(d.get("high"))
            except (TypeError, ValueError):
                return bad("Low and high must be numbers")
            return safe(lambda: {"id": sig.add_level(m, mint, d.get("name"), low, high)})
        if request.method == "DELETE":
            return safe(lambda: {"removed": sig.remove_level(m, request.args.get("id"))})
        return safe(lambda: {"levels": sig.level_status(m)})

    @bp.route(f"{api}/profile")
    def profile_route():
        a = (request.args.get("address") or "").strip()
        if not addr_ok(a):
            return bad(f"Not a valid {m.PAGE['addr_hint']}")
        depth = max(10, min(int(request.args.get("depth", 40)), 100))
        return safe(lambda: profile_fn(a, depth))

    add_flow_route(bp, m, start)
    return bp


# ------------------------------------------------ market maker <-> venue flows ----
import os as _os
import time as _time

MM_GROUPS = [g.strip() for g in _os.getenv("FLOW_MM_GROUPS", "Wintermute,B2C2").split(",") if g.strip()]


def mm_flows(m, hours=72):
    """Per coin: what the market makers (Wintermute, B2C2) sent to / took back from brokers & exchanges.

    Robinhood fills app orders through these market makers, so MM -> Robinhood = Robinhood
    customers net buying that coin; Robinhood -> MM = net selling. For exchanges, MM -> exchange
    is inventory arriving to be sold there.
    """
    since = _time.time() - hours * 3600
    mm = {a: w.get("group") for a, w in m.WALLETS.items() if w.get("group") in MM_GROUPS}

    def venue(a):
        w = m.WALLETS.get(a) or {}
        name = m.EXCHANGES.get(a) or (w.get("label") if (w.get("group") or "") in ("Robinhood", "Exchange") else None)
        if not name:
            return None
        return "Robinhood" if "robinhood" in name.lower() or w.get("group") == "Robinhood" else name

    def is_storage(a):
        return (m.WALLETS.get(a) or {}).get("group") == "Robinhood"

    rows, sweeps, seen = {}, {}, set()
    for e in m.tracker.query(limit=5000):
        if e["ts"] < since or e["kind"] not in ("IN", "OUT") or not e.get("counterparty"):
            continue
        w, cp = e["wallet"], e["counterparty"]
        key = (e["sig"], e.get("mint"))
        if w in mm and venue(cp):                   # seen from the market maker's side
            grp, ven, to_venue = mm[w], venue(cp), e["kind"] == "OUT"
        elif venue(w) and cp in mm:                 # seen from a tracked broker wallet's side
            grp, ven, to_venue = mm[cp], venue(w), e["kind"] == "IN"
        elif is_storage(w) and venue(cp) == "Robinhood":   # Robinhood hot wallet <-> storage sweep
            if key in seen:
                continue
            seen.add(key)
            s = sweeps.setdefault(e.get("mint"), {"mint": e.get("mint"), "symbol": e.get("symbol"),
                                                 "in_usd": 0.0, "out_usd": 0.0, "in_amt": 0.0, "out_amt": 0.0,
                                                 "n": 0, "last": 0})
            k = "in" if e["kind"] == "IN" else "out"
            s[k + "_usd"] += e.get("usd") or 0
            s[k + "_amt"] += e.get("amount") or 0
            s["n"] += 1
            s["last"] = max(s["last"], e["ts"])
            continue
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        r = rows.setdefault((grp, ven, e.get("mint")), {
            "mm": grp, "venue": ven, "mint": e.get("mint"), "symbol": e.get("symbol"),
            "to_usd": 0.0, "from_usd": 0.0, "to_amt": 0.0, "from_amt": 0.0, "n": 0, "last": 0,
            "market_cap": e.get("market_cap")})
        k = "to" if to_venue else "from"
        r[k + "_usd"] += e.get("usd") or 0
        r[k + "_amt"] += e.get("amount") or 0
        r["n"] += 1
        r["last"] = max(r["last"], e["ts"])
        r["symbol"] = r["symbol"] or e.get("symbol")
    out = sorted(rows.values(), key=lambda r: -abs(r["to_usd"] - r["from_usd"]))
    for r in out:
        r["net_usd"] = r["to_usd"] - r["from_usd"]
    totals = {}
    for r in out:
        t = totals.setdefault(r["venue"], {"venue": r["venue"], "to_usd": 0.0, "from_usd": 0.0, "n": 0})
        t["to_usd"] += r["to_usd"]; t["from_usd"] += r["from_usd"]; t["n"] += r["n"]
    return {"hours": hours, "mm_groups": MM_GROUPS, "mm_wallets": len(mm),
            "venues": sorted(totals.values(), key=lambda t: -(t["to_usd"] + t["from_usd"])),
            "rows": out[:200], "sweeps": sorted(sweeps.values(), key=lambda s: -abs(s["in_usd"] - s["out_usd"])),
            "tracked_since": m.tracker.status.get("started")}


def add_flow_route(bp, m, start):
    @bp.route(f"/api/{m.CHAIN}/flows")
    def flows():
        start()
        try:
            hours = max(1, min(int(request.args.get("hours", 72)), 24 * 30))
            return jsonify(mm_flows(m, hours))
        except Exception as e:
            return jsonify({"error": str(e)}), 502
