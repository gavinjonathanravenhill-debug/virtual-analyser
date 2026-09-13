"""
Flask blueprint for GammaFlip endpoints
"""

from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary,
    get_gamma_surface,
    get_by_strike,
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
        # Surface the reason plainly rather than silently serving fake
        # strikes - a wrong axis is worse than an honest error.
        return jsonify({"ok": False, "error": str(e), "source": "by-strike"}), 502


@gammaflip_bp.route("/<coin>/diagnose", methods=["GET"])
def gamma_flip_diagnose(coin):
    """Report which by-strike endpoint works and what shape it returns.

    Hit this once after deploy to confirm the endpoint resolved.
    """
    import gammaflip_client as gf
    result = {"coin": coin.upper(), "candidates": []}
    for template in gf.BY_STRIKE_CANDIDATES:
        path = template.format(coin=coin.upper())
        try:
            data = gf._get(path, cache_key=f"diag:{path}")
            rows = gf._extract_strike_rows(data)
            result["candidates"].append({
                "path": path,
                "status": "OK",
                "rows": len(rows),
                "sample": rows[0] if rows else None,
                "top_level_keys": list(data.keys()) if isinstance(data, dict) else None,
            })
            result["resolved"] = path
            break
        except GammaFlipError as e:
            result["candidates"].append({"path": path, "status": str(e)})
    return jsonify(result)


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
