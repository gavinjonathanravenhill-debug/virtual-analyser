"""One blueprint factory for every chain tracker page (/solana, /robinhood, ...)."""

import json

from flask import Blueprint, jsonify, render_template, request

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
            return safe(lambda: sig.add_wallet(m, a, d.get("label"), d.get("group"), d.get("note"), d.get("alert", True),
                                               d.get("min_usd")))
        if request.method == "DELETE":
            a = norm((request.args.get("address") or "").strip())
            return safe(lambda: {"removed": sig.remove_wallet(m, a)})
        ev = m.tracker.query(limit=3000)
        last, sizes = {}, {}
        for e in ev:
            last.setdefault(e["wallet"], e["ts"])
            if e["kind"] in ("BUY", "SELL") and e.get("usd"):
                sizes.setdefault(e["wallet"], []).append(e["usd"])
        med = {w: sorted(v)[len(v) // 2] for w, v in sizes.items()}
        return jsonify({"status": m.tracker.status,
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
        return safe(lambda: {**m.token_report(mint), "risk": sig.risk_checks(m, mint),
                             "levels": [lv for lv in sig.level_status(m) if lv["mint"] == mint]})

    @bp.route(f"{api}/signals")
    def signals():
        start()
        return safe(lambda: {"journal": sig.journal(m), "scorecard": sig.scorecard(m),
                             "min_usd": sig.MIN_USD, "db": sig.DB_PATH})

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

    return bp
