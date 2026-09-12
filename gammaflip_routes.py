"""
Flask blueprint for GammaFlip endpoints
"""

from flask import Blueprint, jsonify, request
from gammaflip_client import (
    get_gamma_summary,
    parse_gamma_surface,
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
    """Return gamma by expiration (proxy for surface chart)."""
    try:
        raw = get_term_oi(coin)
        parsed = parse_gamma_surface(raw)
        return jsonify({"ok": True, "data": parsed})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
