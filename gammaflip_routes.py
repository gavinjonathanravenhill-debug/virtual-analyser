"""
Flask blueprint for GammaFlip endpoints
"""

import requests
from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary, get_gamma_surface, get_term_oi, GammaFlipError,
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
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/sweep", methods=["GET"])
def gamma_flip_sweep(coin):
    """Systematic sweep: word x prefix x expiry-segment.

    Real endpoints answer with a JSON envelope containing meta.rate_limit;
    missing routes return Flask's HTML 404. So we detect success by
    content, not just status code.
    """
    import gammaflip_client as gf
    coin = coin.upper()

    words = [
        "by-strike", "strike-oi", "oi-strike", "strikes", "strike",
        "profile", "surface", "levels", "chain", "snapshot",
        "gamma-profile", "gex-profile", "strike-profile",
        "distribution", "walls", "heatmap", "spot-gex", "net-gex",
    ]

    # nearest expiry, for the variants that need one
    try:
        ed = gf._get(f"/expirations/all/{coin}")
        exp = (ed.get("data", {}) or {}).get("expirations", [None])[0]
    except Exception:
        exp = None

    paths = []
    for w in words:
        paths.append(f"/gex/{w}/all/{coin}")
        paths.append(f"/{w}/all/{coin}")
        if exp:
            paths.append(f"/gex/{w}/all/{coin}/{exp}")

    hits, misses = [], 0
    for p in paths:
        try:
            r = requests.get(f"{gf.API_BASE}{p}", headers=gf._headers(), timeout=8)
            # A real route returns JSON; a missing one returns HTML.
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ctype:
                hits.append({"path": p, "code": 200, "body": r.text[:1500]})
            elif r.status_code != 404:
                # 401/403/422 all mean the route EXISTS
                hits.append({"path": p, "code": r.status_code,
                             "body": r.text[:500]})
            else:
                misses += 1
        except Exception as e:
            hits.append({"path": p, "error": str(e)[:150]})

    return jsonify({
        "coin": coin, "expiry_used": exp,
        "paths_tried": len(paths), "dead_404s": misses,
        "HITS": hits or "none - none of these words are routes",
    })


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        return jsonify({"ok": True, "data": get_term_oi(coin)})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
