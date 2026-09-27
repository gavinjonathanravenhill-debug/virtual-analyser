"""Flask blueprint for the Wintermute tracker page (/wintermute)."""

import json
import queue

from flask import (Blueprint, Response, jsonify, render_template, request,
                   stream_with_context)

from wintermute_client import (KNOWN_EXCHANGES, WINTERMUTE_WALLETS, start_warmer,
                               PatternRecognition, WintermuteAnalyzer)
from wintermute_realtime import bus, start_realtime

wintermute_bp = Blueprint("wintermute", __name__)
_analyzer = None


def wm():
    global _analyzer
    if _analyzer is None:
        _analyzer = WintermuteAnalyzer()
        start_warmer()
    return _analyzer


def _safe(fn):
    try:
        return jsonify(fn())
    except Exception as e:  # surface API/key problems to the page instead of a 500 page
        return jsonify({"error": str(e)}), 502


def _hours():
    return max(1, min(int(request.args.get("hours", 24)), 24 * 14))


@wintermute_bp.route("/wintermute")
def page():
    return render_template("wintermute.html", wallets=WINTERMUTE_WALLETS,
                           exchanges=KNOWN_EXCHANGES)


@wintermute_bp.route("/api/wintermute/overview")
def overview():
    def run():
        t = wm().transfers(_hours())
        s = wm().flow_summary(t["rows"])
        return {"latest_block": t["latest_block"], "transfer_count": len(t["rows"]),
                "wallets": wm().wallets, **s}
    return _safe(run)


@wintermute_bp.route("/api/wintermute/holdings")
def holdings():
    min_usd = float(request.args.get("min_usd", 100_000))
    return _safe(lambda: wm().analyze_token_holdings(min_usd=min_usd))


@wintermute_bp.route("/api/wintermute/patterns")
def patterns():
    return _safe(lambda: PatternRecognition().run(wm().transfers(_hours())["rows"]))


@wintermute_bp.route("/api/wintermute/impact/<contract>")
def impact(contract):
    return _safe(lambda: wm().calculate_market_impact(contract, hours=_hours()))


@wintermute_bp.route("/api/wintermute/risk/<contract>")
def risk(contract):
    return _safe(lambda: wm().assess_risk(contract))


@wintermute_bp.route("/api/wintermute/signals")
def signals():
    max_mcap = float(request.args.get("max_mcap", 5_000_000))
    return _safe(lambda: {"signals": wm().generate_signals(max_mcap=max_mcap, hours=_hours())})


@wintermute_bp.route("/api/wintermute/listing-watch")
def listing_watch():
    return _safe(lambda: wm().listing_watch())


@wintermute_bp.route("/api/wintermute/live")
def live():
    since = int(request.args.get("since", 0))
    return _safe(lambda: wm().live(since))


@wintermute_bp.route("/api/wintermute/stream")
def stream():
    """Server-Sent Events: real-time transfers, new blocks and connection status."""
    start_realtime()

    def gen():
        q = bus.subscribe()
        try:
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'type': 'status', 'data': bus.status})}\n\n"
            for msg in reversed(list(bus.recent)[:50]):   # replay recent, oldest first
                yield f"data: {json.dumps({**msg, 'replay': True})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {json.dumps(msg, default=str)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@wintermute_bp.route("/api/wintermute/stream/status")
def stream_status():
    start_realtime()
    return jsonify(bus.status)


@wintermute_bp.route("/api/wintermute/stream/recent")
def stream_recent():
    """Plain JSON copy of the live feed - the page falls back to this if the event stream stalls."""
    start_realtime()
    return jsonify({"status": bus.status, "events": [m["data"] for m in list(bus.recent)[:100]]})


@wintermute_bp.route("/api/wintermute/dex-trades")
def dex_trades():
    max_mcap = float(request.args.get("max_mcap", 2_000_000_000))
    addr = (request.args.get("address") or "").strip().lower()

    def run():
        if addr:  # analyse any address you paste in
            if not (addr.startswith("0x") and len(addr) == 42):
                raise ValueError("Not a valid 0x address")
            a = WintermuteAnalyzer({addr: "Lookup " + addr[:6] + "…" + addr[-4:]})
            t = a.transfers(_hours())
            return {**a.dex_trades(t["rows"], max_mcap), "address": addr}
        t = wm().transfers(_hours())
        return wm().dex_trades(t["rows"], max_mcap)
    return _safe(run)
