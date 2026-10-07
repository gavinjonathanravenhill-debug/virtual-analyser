"""Oil intelligence for the dashboard.

- Brent 1-min candles (MEXC UKOIL_USDT perp, trades 24/7 like the WTI feed)
- Who is positioned: CFTC Disaggregated Commitments of Traders for NYMEX WTI
  (managed money = hedge funds/CTAs, producers, swap dealers). Weekly:
  positions as of Tuesday, published Friday 3:30pm ET.
- Oil news: Google News RSS (last 24h), oilprice.com RSS as fallback.
"""
import time
import email.utils
import xml.etree.ElementTree as ET

import requests
from flask import jsonify

UA = {"User-Agent": "Mozilla/5.0 (virtual-analyser)"}
_cache = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
        _cache[key] = (now, val)
        return val
    except Exception as e:
        print(f"oil_intel {key} failed: {e}")
        return hit[1] if hit else None


# ---------------------------------------------------------------- Brent
def get_brent_candles():
    def fetch():
        r = requests.get("https://contract.mexc.com/api/v1/contract/kline/UKOIL_USDT",
                         params={"interval": "Min1", "limit": 60}, timeout=10)
        d = r.json().get("data", {}) or {}
        t, c = d.get("time", []), d.get("close", [])
        candles = [{"t": int(t[i]) * 1000, "c": float(c[i])} for i in range(min(len(t), len(c)))]
        if not candles:
            raise ValueError("no brent candles")
        return candles
    return _cached("brent", 30, fetch) or []


def get_brent_price():
    c = get_brent_candles()
    return c[-1]["c"] if c else 0


# ---------------------------------------------------------------- CFTC COT
COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
WTI_CODE = "067651"   # WTI-PHYSICAL, NYMEX


def get_wti_cot(weeks=12):
    def fetch():
        r = requests.get(COT_URL, params={
            "cftc_contract_market_code": WTI_CODE,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": weeks,
        }, headers=UA, timeout=20)
        r.raise_for_status()
        rows = []
        for x in r.json():
            n = lambda k: int(float(x.get(k) or 0))
            mm_l, mm_s = n("m_money_positions_long_all"), n("m_money_positions_short_all")
            pm_l, pm_s = n("prod_merc_positions_long"), n("prod_merc_positions_short")
            sw_l, sw_s = n("swap_positions_long_all"), n("swap__positions_short_all")
            ot_l, ot_s = n("other_rept_positions_long"), n("other_rept_positions_short")
            rows.append({
                "date": (x.get("report_date_as_yyyy_mm_dd") or "")[:10],
                "oi": n("open_interest_all"), "oi_chg": n("change_in_open_interest_all"),
                "mm_long": mm_l, "mm_short": mm_s, "mm_net": mm_l - mm_s,
                "mm_long_chg": n("change_in_m_money_long_all"),
                "mm_short_chg": n("change_in_m_money_short_all"),
                "mm_traders_long": n("traders_m_money_long_all"),
                "mm_traders_short": n("traders_m_money_short_all"),
                "pm_net": pm_l - pm_s, "swap_net": sw_l - sw_s, "other_net": ot_l - ot_s,
            })
        if not rows:
            raise ValueError("no COT rows")
        rows.reverse()   # oldest -> newest
        for i in range(1, len(rows)):
            rows[i]["mm_net_chg"] = rows[i]["mm_net"] - rows[i - 1]["mm_net"]
            rows[i]["pm_net_chg"] = rows[i]["pm_net"] - rows[i - 1]["pm_net"]
            rows[i]["swap_net_chg"] = rows[i]["swap_net"] - rows[i - 1]["swap_net"]
        last = rows[-1]
        nets = [r["mm_net"] for r in rows]
        lo, hi = min(nets), max(nets)
        last["mm_net_pctile"] = round(100 * (last["mm_net"] - lo) / (hi - lo)) if hi > lo else 50
        last["mm_ls_ratio"] = round(last["mm_long"] / last["mm_short"], 2) if last["mm_short"] else None
        return {"market": "WTI crude (NYMEX)", "unit": "contracts of 1,000 bbl", "weeks": rows}
    return _cached("cot", 3 * 3600, fetch)


# ---------------------------------------------------------------- News
NEWS_FEEDS = [
    ("https://news.google.com/rss/search", {"q": '("oil price" OR brent OR "crude oil" OR OPEC) when:1d',
                                            "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}),
    ("https://oilprice.com/rss/main", None),
]


def _parse_rss(xml_text, default_source):
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = (src_el.text if src_el is not None and src_el.text else default_source).strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        ts = 0
        pd = it.findtext("pubDate")
        if pd:
            try:
                ts = int(email.utils.parsedate_to_datetime(pd).timestamp())
            except Exception:
                pass
        if title and link:
            out.append({"title": title, "link": link, "source": source, "ts": ts})
    return out


def get_oil_news(limit=15):
    def fetch():
        items, seen = [], set()
        for url, params in NEWS_FEEDS:
            try:
                r = requests.get(url, params=params, headers=UA, timeout=12)
                r.raise_for_status()
                src = "OilPrice.com" if "oilprice" in url else "Google News"
                for x in _parse_rss(r.text, src):
                    k = x["title"].lower()[:70]
                    if k not in seen:
                        seen.add(k)
                        items.append(x)
            except Exception as e:
                print(f"oil news feed {url} failed: {e}")
            if len(items) >= limit:
                break
        if not items:
            raise ValueError("no news")
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items[:limit]
    return _cached("news", 300, fetch) or []


def init_oil_intel(app, auth):
    def cot():
        d = get_wti_cot()
        return jsonify(d) if d else (jsonify({"error": "CFTC feed unavailable"}), 502)

    def news():
        return jsonify({"items": get_oil_news()})

    app.add_url_rule("/api/oil/cot", "oil_cot", auth(cot))
    app.add_url_rule("/api/oil/news", "oil_news", auth(news))
