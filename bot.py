import os, requests, threading, time, logging
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.WARNING)

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "1553006303")
MORALIS_KEY = os.environ.get("MORALIS_API_KEY", "")

VIRTUAL_ADDRESS = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"
VIRTUAL_CHAIN   = "0x2105"

price_alerts = {}
last_snapshot = {}
offset = 0

# ── Telegram API ──────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=kwargs, timeout=10)
        return r.json()
    except:
        return {}

def send(msg, chat_id=None):
    tg("sendMessage", chat_id=chat_id or CHAT_ID, text=msg, parse_mode="HTML")

def get_updates():
    global offset
    r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
    updates = r.get("result", [])
    if updates:
        offset = updates[-1]["update_id"] + 1
    return updates

# ── Data ──────────────────────────────────────────────────

def get_prices():
    try:
        btc  = float(requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json().get("price", 0))
        virt = float(requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=VIRTUALUSDT", timeout=5).json().get("price", 0))
        return btc, virt
    except:
        return 0, 0

def get_holders():
    if not MORALIS_KEY:
        return []
    try:
        meta = requests.get("https://deep-index.moralis.io/api/v2.2/erc20/metadata",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": VIRTUAL_CHAIN, "addresses[0]": VIRTUAL_ADDRESS}, timeout=10).json()
        decimals = int(meta[0].get("decimals", 18)) if meta else 18
        total_raw = int(meta[0].get("total_supply", 0)) if meta else 0
        total = total_raw / (10 ** decimals) if total_raw else 0
        r = requests.get(f"https://deep-index.moralis.io/api/v2.2/erc20/{VIRTUAL_ADDRESS}/owners",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": VIRTUAL_CHAIN, "limit": 20, "order": "DESC"}, timeout=15)
        holders = []
        for h in r.json().get("result", []):
            bal = int(h.get("balance", 0)) / (10 ** decimals)
            pct = round(bal / total * 100, 4) if total else 0
            holders.append({"address": h.get("owner_address", ""), "name": h.get("owner_address_label") or "", "pct": pct})
        return holders
    except:
        return []

def sh(a):
    return a[:6] + "…" + a[-4:] if a else "?"

# ── Commands ──────────────────────────────────────────────

def handle_start(chat_id):
    send("🟢 <b>VIRTUAL Analyser Bot</b>\n\n"
         "/prices — BTC &amp; VIRTUAL live prices\n"
         "/summary — Top holder snapshot\n"
         "/analyse &lt;address&gt; — Analyse any token\n"
         "/setalert BTC 85000 — Price alert\n"
         "/alerts — List active alerts\n"
         "/clearalerts — Remove all alerts", chat_id)

def handle_prices(chat_id):
    btc, virt = get_prices()
    send(f"💰 <b>Live Prices</b>\n\nBTC:     <b>${btc:,.0f}</b>\nVIRTUAL: <b>${virt:.4f}</b>", chat_id)

def handle_summary(chat_id):
    send("⏳ Fetching holder data…", chat_id)
    holders = get_holders()
    if not holders:
        send("❌ Could not fetch holder data.", chat_id)
        return
    top10 = holders[:10]
    t10p = sum(h["pct"] for h in top10)
    t3p  = sum(h["pct"] for h in top10[:3])
    lines = ["📊 <b>VIRTUAL Top Holders</b>\n"]
    for i, h in enumerate(top10, 1):
        lines.append(f"{i}. {h['name'] or sh(h['address'])} — <b>{h['pct']:.2f}%</b>")
    lines.append(f"\nTop 3: <b>{t3p:.1f}%</b> | Top 10: <b>{t10p:.1f}%</b>")
    send("\n".join(lines), chat_id)

def handle_analyse(chat_id, args):
    if not args:
        send("Usage: /analyse &lt;contract_address&gt;", chat_id)
        return
    addr = args[0]
    chain = args[1] if len(args) > 1 else "0x2105"
    send(f"⏳ Analysing {sh(addr)}…", chat_id)
    try:
        meta = requests.get("https://deep-index.moralis.io/api/v2.2/erc20/metadata",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": chain, "addresses[0]": addr}, timeout=10).json()
        decimals = int(meta[0].get("decimals", 18)) if meta else 18
        total_raw = int(meta[0].get("total_supply", 0)) if meta else 0
        total = total_raw / (10 ** decimals) if total_raw else 0
        symbol = meta[0].get("symbol", "?") if meta else "?"
        name   = meta[0].get("name", "?") if meta else "?"
        r = requests.get(f"https://deep-index.moralis.io/api/v2.2/erc20/{addr}/owners",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": chain, "limit": 20, "order": "DESC"}, timeout=15)
        top = []
        for h in r.json().get("result", [])[:10]:
            bal = int(h.get("balance", 0)) / (10 ** decimals)
            pct = round(bal / total * 100, 4) if total else 0
            top.append({"name": h.get("owner_address_label") or sh(h.get("owner_address","")), "pct": pct})
        t10p = sum(h["pct"] for h in top)
        t3p  = sum(h["pct"] for h in top[:3])
        risk = "🟢 LOW" if t3p < 35 else "🟡 MODERATE" if t3p < 50 else "🔴 HIGH"
        lines = [f"🔍 <b>{name} ({symbol})</b>\nRisk: {risk}\n"]
        for i, h in enumerate(top, 1):
            lines.append(f"{i}. {h['name']} — <b>{h['pct']:.2f}%</b>")
        lines.append(f"\nTop 3: <b>{t3p:.1f}%</b> | Top 10: <b>{t10p:.1f}%</b>")
        send("\n".join(lines), chat_id)
    except Exception as e:
        send(f"❌ Error: {e}", chat_id)

def handle_setalert(chat_id, args):
    if len(args) < 2:
        send("Usage: /setalert BTC 85000", chat_id)
        return
    symbol = args[0].upper()
    try:
        threshold = float(args[1])
        if symbol not in price_alerts:
            price_alerts[symbol] = []
        price_alerts[symbol].append(threshold)
        send(f"✅ Alert set: {symbol} hits ${threshold:,}", chat_id)
    except:
        send("Invalid price.", chat_id)

def handle_alerts(chat_id):
    if not price_alerts:
        send("No active alerts.", chat_id)
        return
    lines = ["📋 <b>Active Alerts</b>\n"]
    for sym, thresholds in price_alerts.items():
        for t in thresholds:
            lines.append(f"{sym}: ${t:,}")
    send("\n".join(lines), chat_id)

def handle_clearalerts(chat_id):
    price_alerts.clear()
    send("✅ All alerts cleared.", chat_id)

# ── Polling loop ──────────────────────────────────────────

def process_updates():
    updates = get_updates()
    for u in updates:
        msg = u.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        if not text or not chat_id:
            continue
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        if cmd == "/start":       handle_start(chat_id)
        elif cmd == "/prices":    handle_prices(chat_id)
        elif cmd == "/summary":   handle_summary(chat_id)
        elif cmd == "/analyse":   handle_analyse(chat_id, args)
        elif cmd == "/setalert":  handle_setalert(chat_id, args)
        elif cmd == "/alerts":    handle_alerts(chat_id)
        elif cmd == "/clearalerts": handle_clearalerts(chat_id)

# ── Scheduled jobs ────────────────────────────────────────

def check_price_alerts():
    btc, virt = get_prices()
    prices = {"BTC": btc, "VIRTUAL": virt}
    for symbol, thresholds in list(price_alerts.items()):
        current = prices.get(symbol, 0)
        if current == 0:
            continue
        triggered = [t for t in thresholds if current >= t]
        price_alerts[symbol] = [t for t in thresholds if current < t]
        for t in triggered:
            send(f"🚨 <b>PRICE ALERT</b>\n{symbol} hit <b>${current:,}</b>\n(threshold: ${t:,})")

def check_holder_changes():
    global last_snapshot
    holders = get_holders()
    if not holders:
        return
    current = {h["address"]: h["pct"] for h in holders}
    if not last_snapshot:
        last_snapshot = current
        return
    alerts = []
    for addr, pct in current.items():
        prev = last_snapshot.get(addr, 0)
        change = pct - prev
        if abs(change) >= 0.5:
            name = next((h["name"] for h in holders if h["address"] == addr), sh(addr))
            direction = "📈 INCREASED" if change > 0 else "📉 DECREASED"
            alerts.append(f"{direction}: {name or sh(addr)}\n{prev:.2f}% -> {pct:.2f}% ({change:+.2f}%)")


def daily_summary():
    holders = get_holders()
    if not holders:
        return
    top10 = holders[:10]
    t10p = sum(h["pct"] for h in top10)
    t3p  = sum(h["pct"] for h in top10[:3])
    lines = ["📅 <b>Daily VIRTUAL Summary</b>\n"]
    for i, h in enumerate(top10, 1):
        lines.append(f"{i}. {h["name"] or sh(h["address"])} — <b>{h["pct"]:.2f}%</b>")
    lines.append(f"\nTop 3: <b>{t3p:.1f}%</b> | Top 10: <b>{t10p:.1f}%</b>")
    btc, virt = get_prices()
    lines.append(f"\nBTC: <b>${btc:,.0f}</b> | VIRTUAL: <b>${virt:.4f}</b>")
    send("\n".join(lines))

def run_bot():
    if not BOT_TOKEN:
        print("No TELEGRAM_BOT_TOKEN set — bot disabled")
        return
    print("Bot starting…")
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_price_alerts,  "interval", minutes=1)
    scheduler.add_job(check_holder_changes,"interval", minutes=30)
    scheduler.add_job(daily_summary,       "cron",     hour=8, minute=0)
    scheduler.start()
    while True:
        try:
            process_updates()
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(5)
        time.sleep(1)

def start_bot_thread():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
