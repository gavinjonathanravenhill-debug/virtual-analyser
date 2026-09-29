"""
Real-time Solana alerts: a Helius webhook pushes every transaction of your tracked wallets to
/api/solana/webhook within ~1-3 s, instead of waiting for the 60 s poll (which keeps running as a backup).

Setup is one button on the Edge tab (needs HELIUS_API_KEY). The wallet list is re-synced whenever you
add or remove a Solana wallet.
"""

import os
import secrets
import threading

import requests

API = "https://api.helius.xyz/v0/webhooks"
KEY = os.getenv("HELIUS_API_KEY", "").strip()
_lock = threading.Lock()


def _edge():
    import edge
    return edge


def secret():
    s = _edge().get_secret("helius_secret")
    if not s:
        s = secrets.token_urlsafe(24)
        _edge().set_secret("helius_secret", s)
    return s


def status():
    import solana_client as sc
    return {"api_key": bool(KEY), "webhook_id": _edge().get_secret("helius_webhook_id"),
            "url": _edge().get_secret("helius_webhook_url"), "wallets": len(sc.WALLETS),
            "hits": sc.tracker.status.get("webhook_hits"), "last": sc.tracker.status.get("last_webhook")}


def setup(public_url):
    """Create or update the webhook so it covers every tracked Solana wallet."""
    import solana_client as sc
    if not KEY:
        raise RuntimeError("Set HELIUS_API_KEY on Railway first")
    url = public_url.rstrip("/") + "/api/solana/webhook"
    body = {"webhookURL": url, "transactionTypes": ["ANY"], "accountAddresses": sorted(sc.WALLETS),
            "webhookType": "raw", "authHeader": secret()}
    with _lock:
        wid = _edge().get_secret("helius_webhook_id")
        r = None
        if wid:
            r = requests.put(f"{API}/{wid}", params={"api-key": KEY}, json=body, timeout=20)
            if r.status_code == 404:
                wid, r = None, None
        if not wid:
            r = requests.post(API, params={"api-key": KEY}, json=body, timeout=20)
        if not r.ok:
            raise RuntimeError(f"Helius said {r.status_code}: {r.text[:200]}")
        wid = r.json().get("webhookID") or wid
        _edge().set_secret("helius_webhook_id", wid)
        _edge().set_secret("helius_webhook_url", public_url.rstrip("/"))
    return status()


def resync_async():
    """After a wallet is added/removed - only if a webhook was set up."""
    base = _edge().get_secret("helius_webhook_url")
    if not (KEY and base):
        return

    def run():
        try:
            setup(base)
        except Exception as e:
            print(f"helius resync failed: {e}")
    threading.Thread(target=run, daemon=True).start()


def check_auth(header):
    s = _edge().get_secret("helius_secret")
    return bool(s) and secrets.compare_digest(header or "", s)
