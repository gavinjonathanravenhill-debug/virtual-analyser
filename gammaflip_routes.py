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
    try:
        parsed = get_gamma_surface(coin)
        return jsonify({"ok": True, "data": parsed})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e), "source": "by-strike"}), 502


@gammaflip_bp.route("/<coin>/expiries", methods=["GET"])
def gamma_flip_expiries(coin):
    """List available expirations - the working endpoint we found."""
    import gammaflip_client as gf
    try:
        return jsonify({"ok": True,
                        "data": gf._get(f"/expirations/all/{coin.upper()}")})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/discover2", methods=["GET"])
def gamma_flip_discover2(coin):
    """Probe by-strike using a REAL expiry code.

    /expirations/all/{coin} works and returns codes like 14SEP26. GEX by
    strike is computed per expiration, so the endpoint most likely wants
    one of those as a path segment or query param.
    """
    import gammaflip_client as gf
    coin = coin.upper()
    out = {"coin": coin}

    # Pull a live expiry to test with
    try:
        exp_data = gf._get(f"/expirations/all/{coin}")
        expiries = (exp_data.get("data", {}) or {}).get("expirations", [])
    except GammaFlipError as e:
        return jsonify({"error": f"could not list expiries: {e}"}), 502

    if not expiries:
        return jsonify({"error": "no expiries returned"}), 502

    exp = expiries[0]          # nearest, e.g. 14SEP26
    out["using_expiry"] = exp
    out["all_expiries"] = expiries

    templates = [
        "/gex/by-strike/all/{c}/{e}",
        "/by-strike/all/{c}/{e}",
        "/strikes/all/{c}/{e}",
        "/gex/strikes/all/{c}/{e}",
        "/gex/by-strike/all/{c}?expiry={e}",
        "/gex/by-strike/all/{c}?expiration={e}",
        "/by-strike/all/{c}?expiry={e}",
        "/strikes/all/{c}?expiry={e}",
        "/gex/all/{c}/{e}",
        "/gex/oi/all/{c}/{e}",
        "/gex/strike-oi/all/{c}/{e}",
        # aggregate-across-expiries variants
        "/gex/by-strike/all/{c}/ALL",
        "/by-strike/all/{c}/ALL",
        "/strikes/all/{c}",
        "/by-strike/all/{c}",
    ]

    results = []
    for t in templates:
        path = t.format(c=coin, e=exp)
        url = f"{gf.API_BASE}{path}"
        try:
            r = requests.get(url, headers=gf._headers(), timeout=8)
            entry = {"path": path, "code": r.status_code}
            if r.ok:
                entry["body"] = r.text[:1500]
                results.append(entry)
                out["RESOLVED"] = path
                out["results"] = results
                return jsonify(out)      # stop on first success
            results.append(entry)
        except Exception as e:
            results.append({"path": path, "error": str(e)[:200]})

    out["results"] = results
    return jsonify(out)


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        data = get_term_oi(coin)
        return jsonify({"ok": True, "data": data})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
