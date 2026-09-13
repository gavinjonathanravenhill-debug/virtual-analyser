"""
Flask blueprint for GammaFlip endpoints
"""

import requests
from flask import Blueprint, jsonify
from gammaflip_client import (
    get_gamma_summary, get_gamma_surface, get_term_oi,
    parse_term_structure_fallback, GammaFlipError,
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
    """Real strikes if we can get them, term structure if not.

    The fallback is tagged with axis='days_to_expiry' so the front end
    can label it truthfully instead of passing expiries off as strikes.
    """
    try:
        return jsonify({"ok": True, "data": get_gamma_surface(coin)})
    except GammaFlipError:
        pass
    try:
        fb = parse_term_structure_fallback(get_term_oi(coin))
        # Shape it for the existing chart, but keep the axis honest.
        strikes = [{"strike": r["x"], "net_gex": r["net_gex"],
                    "abs_gex": r["abs_gex"], "label": r["label"]}
                   for r in fb["rows"]]
        return jsonify({"ok": True, "data": {
            "spot": fb["spot"], "strikes": strikes,
            "regime_labels": {}, "vol_triggers": {},
            "axis": "days_to_expiry",
            "source": "term-structure-fallback",
            "note": fb["note"],
        }})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@gammaflip_bp.route("/<coin>/sweep", methods=["GET"])
def gamma_flip_sweep(coin):
    """Systematic endpoint sweep. Detects real routes by JSON content type."""
    import gammaflip_client as gf
    coin = coin.upper()
    words = ["by-strike", "strike-oi", "oi-strike", "strikes", "strike",
             "profile", "surface", "levels", "chain", "snapshot",
             "gamma-profile", "gex-profile", "strike-profile",
             "distribution", "walls", "heatmap", "spot-gex", "net-gex"]
    try:
        ed = gf._get(f"/expirations/all/{coin}")
        exp = (ed.get("data", {}) or {}).get("expirations", [None])[0]
    except Exception:
        exp = None

    paths = []
    for w in words:
        paths += [f"/gex/{w}/all/{coin}", f"/{w}/all/{coin}"]
        if exp:
            paths.append(f"/gex/{w}/all/{coin}/{exp}")

    hits, misses = [], 0
    for p in paths:
        try:
            r = requests.get(f"{gf.API_BASE}{p}", headers=gf._headers(), timeout=8)
            if r.status_code != 404:
                hits.append({"path": p, "code": r.status_code, "body": r.text[:1200]})
            else:
                misses += 1
        except Exception as e:
            hits.append({"path": p, "error": str(e)[:150]})

    return jsonify({"coin": coin, "expiry_used": exp,
                    "tried": len(paths), "dead_404s": misses,
                    "HITS": hits or "none"})


@gammaflip_bp.route("/<coin>/raw", methods=["GET"])
def gamma_flip_raw(coin):
    try:
        return jsonify({"ok": True, "data": get_term_oi(coin)})
    except GammaFlipError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
