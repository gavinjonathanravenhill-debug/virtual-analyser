"""Flask blueprint for the Solana wallet tracker (/solana)."""

from flask import Blueprint, jsonify, render_template, request

from solana_client import VINE_MINT, WALLETS, lookup, start_solana, token_report, tracker
import solana_signals as sig


def _start():
    sig.start_signals()  # listener first, so the first poll is journaled
    start_solana()


solana_bp = Blueprint("solana", __name__)


def _safe(fn):
    try:
        return jsonify(fn())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@solana_bp.route("/solana")
def page():
    _start()
    return render_template("solana.html")


@solana_bp.route("/api/solana/wallets")
def wallets():
    _start()
    ev = tracker.query(limit=3000)
    last, sizes = {}, {}
    for e in ev:
        last.setdefault(e["wallet"], e["ts"])
        if e["kind"] in ("BUY", "SELL") and e.get("usd"):
            sizes.setdefault(e["wallet"], []).append(e["usd"])
    med = {w: sorted(v)[len(v) // 2] for w, v in sizes.items()}
    return jsonify({"vine_mint": VINE_MINT, "status": tracker.status,
                    "wallets": [{**w, "last_seen": last.get(a), "last_tx": tracker.last_tx.get(a),
                                 "checked": a in tracker.checked,
                                 "median_trade": med.get(a), "trades": len(sizes.get(a, []))} for a, w in WALLETS.items()]})


@solana_bp.route("/api/solana/events")
def events():
    _start()
    kinds = [k for k in (request.args.get("kinds") or "").split(",") if k]
    return jsonify({"status": tracker.status, "events": tracker.query(
        wallet=request.args.get("wallet") or None,
        group=request.args.get("group") or None,
        vine_only=request.args.get("vine") == "1",
        kinds=kinds or None)})


@solana_bp.route("/api/solana/lookup")
def lookup_route():
    addr = (request.args.get("address") or "").strip()
    if not 32 <= len(addr) <= 44:
        return jsonify({"error": "Not a Solana address"}), 400
    return _safe(lambda: {"address": addr, "events": lookup(addr)})


@solana_bp.route("/api/solana/token/<mint>")
def token(mint):
    if not 32 <= len(mint) <= 44:
        return jsonify({"error": "Not a Solana token address"}), 400
    return _safe(lambda: {**token_report(mint), "risk": sig.risk_checks(mint),
                          "levels": [l for l in sig.level_status() if l["mint"] == mint]})


@solana_bp.route("/api/solana/signals")
def signals():
    _start()
    return _safe(lambda: {"journal": sig.journal(), "scorecard": sig.scorecard(),
                          "min_usd": sig.MIN_USD, "vine_min_usd": sig.VINE_MIN_USD,
                          "db": sig.DB_PATH})


@solana_bp.route("/api/solana/insights")
def insights():
    _start()
    return _safe(lambda: {"clusters": sig.clusters(), "levels": sig.level_status()})


@solana_bp.route("/api/solana/profile")
def profile_route():
    import solana_profiler
    addr = (request.args.get("address") or "").strip()
    if not 32 <= len(addr) <= 44:
        return jsonify({"error": "Not a Solana address"}), 400
    depth = max(10, min(int(request.args.get("depth", 40)), 100))
    return _safe(lambda: solana_profiler.profile(addr, depth))
