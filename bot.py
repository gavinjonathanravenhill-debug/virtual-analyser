import os, requests, threading, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

logging.basicConfig(level=logging.WARNING)

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "1553006303")
MORALIS_KEY = os.environ.get("MORALIS_API_KEY", "")

VIRTUAL_ADDRESS = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"
VIRTUAL_CHAIN   = "0x2105"

# Price alert thresholds — {symbol: {above: price, below: price}}
price_alerts = {}
# Last known holder snapshot — {address: percentage}
last_snapshot = {}

# ── Helpers ───────────────────────────────────────────────

def get_prices():
    try:
        btc  = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()
        virt = requests.get("https://api.mexc.com/api/v3/ticker/price?symbol=VIRTUALUSDT", timeout=5).json()
        return float(btc.get("price", 0)), float(virt.get("price", 0))
    except:
        return 0, 0

def get_holders():
    if not MORALIS_KEY:
        return []
    try:
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/erc20/{VIRTUAL_ADDRESS}/owners",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": VIRTUAL_CHAIN, "limit": 20, "order": "DESC"},
            timeout=15)
        meta = requests.get(
            "https://deep-index.moralis.io/api/v2.2/erc20/metadata",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": VIRTUAL_CHAIN, "addresses[0]": VIRTUAL_ADDRESS},
            timeout=10).json()
        decimals = int(meta[0].get("decimals", 18)) if meta else 18
        total_raw = int(meta[0].get("total_supply", 0)) if meta else 0
        total = total_raw / (10 ** decimals) if total_raw else 0
        holders = []
        for h in r.json().get("result", []):
            bal = int(h.get("balance", 0)) / (10 ** decimals)
            pct = round(bal / total * 100, 4) if total else 0
            holders.append({
                "address": h.get("owner_address", ""),
                "name": h.get("owner_address_label") or "",
                "pct": pct
            })
        return holders
    except:
        return []

def shorten(addr):
    return addr[:6] + "…" + addr[-4:] if addr else "?"

async def send(bot, msg):
    await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")

# ── Commands ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 <b>VIRTUAL Analyser Bot</b>\n\n"
        "/prices — BTC &amp; VIRTUAL live prices\n"
        "/summary — Top holder snapshot\n"
        "/analyse &lt;address&gt; — Analyse any token\n"
        "/setalert BTC 85000 — Price alert above threshold\n"
        "/alerts — List active alerts\n"
        "/clearalerts — Remove all alerts",
        parse_mode="HTML")

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    btc, virt = get_prices()
    await update.message.reply_text(
        f"💰 <b>Live Prices</b>\n\n"
        f"BTC:     <b>${btc:,.0f}</b>\n"
        f"VIRTUAL: <b>${virt:.4f}</b>",
        parse_mode="HTML")

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching holder data…")
    holders = get_holders()
    if not holders:
        await update.message.reply_text("❌ Could not fetch holder data.")
        return
    top10 = holders[:10]
    t10p = sum(h["pct"] for h in top10)
    t3p  = sum(h["pct"] for h in top10[:3])
    lines = [f"📊 <b>VIRTUAL Top Holders</b>\n"]
    for i, h in enumerate(top10, 1):
        name = h["name"] or shorten(h["address"])
        lines.append(f"{i}. {name} — <b>{h['pct']:.2f}%</b>")
    lines.append(f"\nTop 3:  <b>{t3p:.1f}%</b>")
    lines.append(f"Top 10: <b>{t10p:.1f}%</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /analyse &lt;contract_address&gt;", parse_mode="HTML")
        return
    addr = ctx.args[0].strip()
    chain = ctx.args[1] if len(ctx.args) > 1 else "0x2105"
    await update.message.reply_text(f"⏳ Analysing {shorten(addr)}…")
    try:
        meta = requests.get(
            "https://deep-index.moralis.io/api/v2.2/erc20/metadata",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": chain, "addresses[0]": addr},
            timeout=10).json()
        decimals = int(meta[0].get("decimals", 18)) if meta else 18
        total_raw = int(meta[0].get("total_supply", 0)) if meta else 0
        total = total_raw / (10 ** decimals) if total_raw else 0
        symbol = meta[0].get("symbol", "?") if meta else "?"
        name   = meta[0].get("name", "?") if meta else "?"
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/erc20/{addr}/owners",
            headers={"X-API-Key": MORALIS_KEY},
            params={"chain": chain, "limit": 20, "order": "DESC"},
            timeout=15)
        holders = r.json().get("result", [])
        top = []
        for h in holders[:10]:
            bal = int(h.get("balance", 0)) / (10 ** decimals)
            pct = round(bal / total * 100, 4) if total else 0
            top.append({"name": h.get("owner_address_label") or shorten(h.get("owner_address","")), "pct": pct})
        t10p = sum(h["pct"] for h in top)
        t3p  = sum(h["pct"] for h in top[:3])
        risk = "🟢 LOW" if t3p < 35 else "🟡 MODERATE" if t3p < 50 else "🔴 HIGH"
        lines = [f"🔍 <b>{name} ({symbol})</b>\n", f"Risk: {risk}\n"]
        for i, h in enumerate(top, 1):
            lines.append(f"{i}. {h['name']} — <b>{h['pct']:.2f}%</b>")
        lines.append(f"\nTop 3: <b>{t3p:.1f}%</b> | Top 10: <b>{t10p:.1f}%</b>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /setalert BTC 85000\nUse /setalert VIRTUAL 0.80")
        return
    symbol = ctx.args[0].upper()
    try:
        threshold = float(ctx.args[1])
    except:
        await update.message.reply_text("Invalid price. Example: /setalert BTC 85000")
        return
    if symbol not in price_alerts:
        price_alerts[symbol] = []
    price_alerts[symbol].append(threshold)
    await update.message.reply_text(f"✅ Alert set: {symbol} hits ${threshold:,}")

async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not price_alerts:
        await update.message.reply_text("No active alerts.")
        return
    lines = ["📋 <b>Active Alerts</b>\n"]
    for sym, thresholds in price_alerts.items():
        for t in thresholds:
            lines.append(f"{sym}: ${t:,}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_clearalerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price_alerts.clear()
    await update.message.reply_text("✅ All alerts cleared.")

# ── Scheduled jobs ────────────────────────────────────────

async def check_price_alerts(bot):
    btc, virt = get_prices()
    prices = {"BTC": btc, "VIRTUAL": virt}
    for symbol, thresholds in list(price_alerts.items()):
        current = prices.get(symbol, 0)
        if current == 0:
            continue
        triggered = []
        remaining = []
        for t in thresholds:
            if current >= t:
                triggered.append(t)
            else:
                remaining.append(t)
        for t in triggered:
            await send(bot, f"🚨 <b>PRICE ALERT</b>\n{symbol} hit <b>${current:,}</b>\n(threshold: ${t:,})")
        price_alerts[symbol] = remaining

async def check_holder_changes(bot):
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
            name = next((h["name"] for h in holders if h["address"] == addr), shorten(addr))
            direction = "📈 INCREASED" if change > 0 else "📉 DECREASED"
            alerts.append(f"{direction}: {name or shorten(addr)}\n{prev:.2f}% → {pct:.2f}% ({change:+.2f}%)")
    if alerts:
        msg = "🔔 <b>VIRTUAL Holder Change</b>\n\n" + "\n\n".join(alerts)
        await send(bot, msg)
    last_snapshot = current

async def daily_summary(bot):
    holders = get_holders()
    if not holders:
        return
    top10 = holders[:10]
    t10p = sum(h["pct"] for h in top10)
    t3p  = sum(h["pct"] for h in top10[:3])
    lines = ["📅 <b>Daily VIRTUAL Summary</b>\n"]
    for i, h in enumerate(top10, 1):
        name = h["name"] or shorten(h["address"])
        lines.append(f"{i}. {name} — <b>{h['pct']:.2f}%</b>")
    lines.append(f"\nTop 3: <b>{t3p:.1f}%</b> | Top 10: <b>{t10p:.1f}%</b>")
    btc, virt = get_prices()
    lines.append(f"\nBTC: <b>${btc:,.0f}</b> | VIRTUAL: <b>${virt:.4f}</b>")
    await send(bot, "\n".join(lines))

# ── Main ──────────────────────────────────────────────────

def run_bot():
    async def main():
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start",       cmd_start))
        app.add_handler(CommandHandler("prices",      cmd_prices))
        app.add_handler(CommandHandler("summary",     cmd_summary))
        app.add_handler(CommandHandler("analyse",     cmd_analyse))
        app.add_handler(CommandHandler("setalert",    cmd_setalert))
        app.add_handler(CommandHandler("alerts",      cmd_alerts))
        app.add_handler(CommandHandler("clearalerts", cmd_clearalerts))

        scheduler = AsyncIOScheduler()
        scheduler.add_job(check_price_alerts,  "interval", minutes=1,  args=[app.bot])
        scheduler.add_job(check_holder_changes,"interval", minutes=30, args=[app.bot])
        scheduler.add_job(daily_summary,       "cron",     hour=8, minute=0, args=[app.bot])
        scheduler.start()

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()

    asyncio.run(main())

def start_bot_thread():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
