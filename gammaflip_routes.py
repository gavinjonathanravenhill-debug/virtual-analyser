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
        data = get_gamma_summary(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/surface", methods=["GET"])
def gamma_flip_surface(coin):
    """Real per-strike GEX surface."""
    try:
        parsed = get_gamma_surface(coin)
        return jsonify({"ok": True, "data": parsed})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e), "source": "by-strike"}), 502


@gammaflip_bp.route("/<coin>/discover", methods=["GET"])
def gamma_flip_discover(coin):
    """Ask the API to describe itself, then probe by-strike variants.

    Checks self-documenting routes first (root, openapi, docs) since those
    give us the real path list instead of us guessing at it.
    """
    import gammaflip_client as gf
    coin = coin.upper()
    out = {"coin": coin, "self_describing": [], "probes": []}

    # 1. Routes that might list the API surface for us
    for path in ["", "/", "/openapi.json", "/docs", "/endpoints", "/routes"]:
        url = f"{gf.API_BASE}{path}"
        try:
            r = requests.get(url, headers=gf._headers(), timeout=8)
            entry = {"url": url, "code": r.status_code}
            if r.ok:
                entry["body"] = r.text[:1200]
            out["self_describing"].append(entry)
        except Exception as e:
            out["self_describing"].append({"url": url, "error": str(e)[:200]})

    # 2. Wider set of by-strike shapes, including expiry segment and
    #    query-param styles (the UI shows "BTC / All / TOTAL").
    candidates = [
        f"/gex/by-strike/all/{coin}/TOTAL",
        f"/gex/by-strike/all/{coin}?expiry=TOTAL",
        f"/gex/by-strike/all/{coin}?expiration=all",
        f"/gex/strike-gex/all/{coin}",
        f"/gex/gex-by-strike/all/{coin}",
        f"/gex/by_strike/all/{coin}",
        f"/gex/strikes/{coin}",
        f"/strike/all/{coin}",
        f"/by-strike/all/{coin}",
        f"/gex/all/{coin}",
        f"/gex/{coin}",
        f"/expirations/all/{coin}",
        f"/gex/expirations/all/{coin}",
        f"/coins",
    ]
    for path in candidates:
        url = f"{gf.API_BASE}{path}"
        try:
            r = requests.get(url, headers=gf._headers(), timeout=8)
            entry = {"path": path, "code": r.status_code}
            if r.ok:
                entry["body"] = r.text[:800]
            out["probes"].append(entry)
        except Exception as e:
            out["probes"].append({"path": path, "error": str(e)[:200]})

    return jsonify(out)


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
