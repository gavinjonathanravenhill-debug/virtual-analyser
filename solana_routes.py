"""Flask blueprint for the Solana wallet tracker (/solana)."""

from flask import Blueprint, jsonify, render_template, request

from solana_client import VINE_MINT, WALLETS, lookup, start_solana, tracker

solana_bp = Blueprint("solana", __name__)


def _safe(fn):
    try:
        return jsonify(fn())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@solana_bp.route("/solana")
def page():
    start_solana()
    return render_template("solana.html")


@solana_bp.route("/api/solana/wallets")
def wallets():
    start_solana()
    ev = tracker.query(limit=3000)
    last = {}
    for e in ev:
        last.setdefault(e["wallet"], e["ts"])
    return jsonify({"vine_mint": VINE_MINT, "status": tracker.status,
                    "wallets": [{**w, "last_seen": last.get(a)} for a, w in WALLETS.items()]})


@solana_bp.route("/api/solana/events")
def events():
    start_solana()
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
