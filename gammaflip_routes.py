"""
Flask blueprint for GammaFlip endpoints — register this in your app.py.

    from gammaflip_routes import gammaflip_bp
    app.register_blueprint(gammaflip_bp)

Exposes:
    GET /api/gamma-flip/<coin>       -> flat summary (regime, GEX, walls, spot)
    GET /api/gamma-flip/<coin>/raw   -> full raw GammaFlip term-oi payload
    GET /api/gamma-flip/exchanges    -> exchanges GammaFlip covers
"""

from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary,
    get_term_oi,
    get_exchanges,
    discover_by_strike,
    discover_openapi_spec,
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


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/exchanges", methods=["GET"])
def gamma_flip_exchanges():
    try:
        data = get_exchanges()
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/discover-by-strike", methods=["GET"])
def gamma_flip_discover(coin):
    """
    Temporary debug route — probes candidate by-strike endpoint paths
    and reports which one(s) actually respond. Remove once the real
    endpoint is confirmed and wired into get_gamma_summary properly.
    """
    try:
        results = discover_by_strike(coin)
        return jsonify({"ok": True, "results": results})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/discover-routes", methods=["GET"])
def gamma_flip_discover_routes():
    """
    Temporary debug route — tries to fetch GammaFlip's OpenAPI spec (or
    docs page) to get the real, complete route list. Remove once the
    by-strike endpoint is confirmed and wired in properly.
    """
    try:
        results = discover_openapi_spec()
        return jsonify({"ok": True, "results": results})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
