"""Solana wallet tracker page (/solana) - built from the shared chain blueprint."""

import solana_client as sc
from chain_routes import make_chain_bp


def _profile(addr, depth):
    import solana_profiler
    return solana_profiler.profile(addr, depth)


solana_bp = make_chain_bp(sc, sc.start_solana, _profile, lambda a: 32 <= len(a or "") <= 44)


# ---- real-time: Helius webhook (the POST from Helius skips the site password; it carries its own secret) ----
from flask import jsonify, request  # noqa: E402

import helius_hook  # noqa: E402


@solana_bp.route("/api/solana/webhook", methods=["POST"])
def helius_webhook():
    if not helius_hook.check_auth(request.headers.get("Authorization")):
        return jsonify({"error": "bad secret"}), 401
    sc.start_solana()
    data = request.get_json(silent=True) or []
    return jsonify({"events": sc.tracker.ingest(data if isinstance(data, list) else [data])})


@solana_bp.route("/api/solana/webhook/setup", methods=["GET", "POST"])
def helius_setup():
    try:
        if request.method == "POST":
            base = request.url_root.replace("http://", "https://")
            return jsonify(helius_hook.setup((request.get_json(silent=True) or {}).get("public_url") or base))
        return jsonify(helius_hook.status())
    except Exception as e:
        return jsonify({"error": str(e)}), 502
