"""
Flask blueprint for GammaFlip endpoints
"""

import requests
from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary,
    get_gamma_surface,
    get_term_oi,
    GammaFlipError,
)

gammaflip_bp = Blueprint("gammaflip", __name__, url_prefix="/api/gamma-flip")


@gammaflip_bp.route("/<coin>", methods=["GET"])
def gamma_flip_summary(coin):
    try:
        return jsonify({"ok": True, "data": get_gamma_summary(coin)})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/surface", methods=["GET"])
def gamma_flip_surface(coin):
    try:
        return jsonify({"ok": True, "data": get_gamma_surface(coin)})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e), "source": "by-strike"}), 502


@gammaflip_bp.route("/<coin>/discover3", methods=["GET"])
def gamma_flip_discover3(coin):
    """Capture 404 BODIES (they may name valid routes) and try real exchanges."""
    import gammaflip_client as gf
    coin = coin.upper()
    out = {"coin": coin}

    def probe(path):
        try:
            r = requests.get(f"{gf.API_BASE}{path}", headers=gf._headers(), timeout=8)
            return {"path": path, "code": r.status_code, "body": r.text[:600]}
        except Exception as e:
            return {"path": path, "error": str(e)[:200]}

    # What does a 404 body actually say? Use a nonsense path as control.
    out["error_body_sample"] = probe("/definitely-not-a-real-endpoint")

    # What exchanges exist?
    out["exchanges"] = probe("/exchanges")

    # Try the term-oi shape with each known exchange name, swapping the
    # resource word. If by-strike is exchange-specific, "all" would 404
    # while "deribit" succeeds.
    paths = []
    for ex in ["deribit", "bybit", "okx", "all"]:
        for word in ["by-strike", "strike", "strikes", "strike-oi", "oi"]:
            paths.append(f"/gex/{word}/{ex}/{coin}")

    # Also: maybe it mirrors term-oi exactly but with a different suffix
    for word in ["strike-gex", "gex-strike", "oi-strike", "strike-oi", "spot-oi"]:
        paths.append(f"/gex/{word}/all/{coin}")

    results = []
    for p in paths:
        r = probe(p)
        if r.get("code") == 200:
            out["RESOLVED"] = p
            out["winner"] = r
            out["results"] = results
            return jsonify(out)
        results.append({"path": p, "code": r.get("code")})
    out["results"] = results
    return jsonify(out)


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        return jsonify({"ok": True, "data": get_term_oi(coin)})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
