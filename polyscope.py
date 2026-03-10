"""
Polymarket Alert Bot for Telegram
─────────────────────────────────
User commands (DM):
  /start                  - Onboarding
  /watch [url|keyword]    - Watch a market + set threshold
  /alerts                 - List / remove personal alerts
  /portfolio [0x...]      - Link wallet, view positions

Admin commands (DM only, ADMIN_ID gated):
  /broadcast [url|kw]     - Set a channel-wide alert (posts to CHANNEL_ID when triggered)
  /broadcastlist          - List active channel alerts
  /broadcastremove <n>    - Remove a channel alert by index
  /post [url|kw]          - Immediately post a market snapshot to the channel (no threshold)
"""

import os
from dotenv import load_dotenv
load_dotenv()
import json
import asyncio
import logging
import re
import httpx
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN",    "YOUR_BOT_TOKEN_HERE")
AFFILIATE_REF = os.getenv("AFFILIATE_REF", "")       # e.g. "?ref=yourcode"
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))       # your Telegram user ID
CHANNEL_ID    = os.getenv("CHANNEL_ID",  "")          # e.g. "@yourchannel" or "-100123456789"

# Debug: log whether env vars are being picked up
logger.info(f"BOT_TOKEN set: {BOT_TOKEN != 'YOUR_BOT_TOKEN_HERE'}, ADMIN_ID: {ADMIN_ID}, CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"All env keys containing BOT/ADMIN/CHANNEL: {[k for k in os.environ if any(x in k.upper() for x in ['BOT','ADMIN','CHANNEL'])]}")

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# ─── Persistence ─────────────────────────────────────────────────────────────
DB_FILE = "db.json"

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {"users": {}, "channel_alerts": []}

def save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

def get_user(db: dict, user_id: int) -> dict:
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {"alerts": [], "wallet": None, "onboarded": False}
    return db["users"][uid]

def is_admin(user_id: int) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


# ─── Polymarket API ───────────────────────────────────────────────────────────

async def search_markets(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GAMMA_API}/markets", params={
            "limit": 5, "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false", "q": query,
        })
        r.raise_for_status()
        return r.json()

async def get_market_by_slug(slug: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{GAMMA_API}/markets", params={"slug": slug, "limit": 1})
        r.raise_for_status()
        results = r.json()
        return results[0] if results else None

async def get_positions(wallet: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{DATA_API}/positions", params={
            "user": wallet, "sizeThreshold": "0.01", "limit": 20,
        })
        r.raise_for_status()
        return r.json()

async def resolve_market_from_url(url: str) -> dict | None:
    match = re.search(r"polymarket\.com/(?:event|market)/([^/?#]+)", url)
    if not match:
        return None
    return await get_market_by_slug(match.group(1))

def market_url(market: dict) -> str:
    slug = market.get("slug", "")
    base = f"https://polymarket.com/event/{slug}"
    return base + AFFILIATE_REF if AFFILIATE_REF else base

def yes_probability(market: dict) -> float | None:
    try:
        for t in market.get("tokens") or []:
            if t.get("outcome", "").upper() == "YES":
                return round(float(t.get("price", 0)) * 100, 1)
        prices = json.loads(market.get("outcomePrices", "[]"))
        if prices:
            return round(float(prices[0]) * 100, 1)
    except Exception:
        pass
    return None

def format_volume(market: dict) -> str:
    vol = market.get("volume") or market.get("volume24hr") or 0
    try:
        v = float(vol)
        return f"${v:,.0f}" if v < 1_000_000 else f"${v/1_000_000:.1f}M"
    except Exception:
        return "N/A"


# ─── Market card formatter ────────────────────────────────────────────────────

def format_market_card(market: dict, note: str = "") -> str:
    prob = yes_probability(market)
    url  = market_url(market)
    vol  = format_volume(market)
    end  = (market.get("endDate", "") or "")[:10] or "TBD"

    prob_bar = _prob_bar(prob) if prob is not None else ""
    prob_str = f"{prob}%" if prob is not None else "N/A"

    lines = [
        f"📊 *{market['question']}*",
        "",
        prob_bar,
        f"YES: *{prob_str}*  |  Vol: {vol}  |  Closes: {end}",
    ]
    if note:
        lines += ["", f"_{note}_"]
    lines += ["", f"[Trade on Polymarket]({url})"]
    return "\n".join(lines)

def _prob_bar(prob: float) -> str:
    filled = round(prob / 5)
    return "▓" * filled + "░" * (20 - filled) + f"  {prob}%"


# ─── /start ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    user = get_user(db, update.effective_user.id)

    await update.message.reply_text(
        "👋 *Welcome to PolyAlert!*\n\n"
        "I track [Polymarket](https://polymarket.com) markets and ping you when "
        "probabilities cross your threshold.\n\n"
        "• 🔔 `/watch` — follow a market & set an alert\n"
        "• 📋 `/alerts` — manage your alerts\n"
        "• 💼 `/portfolio` — link your wallet to track positions\n",
        parse_mode="Markdown", disable_web_page_preview=True,
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔔 Watch a market", callback_data="goto_watch"),
        InlineKeyboardButton("📋 My alerts",       callback_data="goto_alerts"),
    ]])
    await update.message.reply_text(
        "Paste a Polymarket URL or type a keyword to get started.\n\n"
        "_Friends you refer earn you $10 each_ 💸",
        parse_mode="Markdown", reply_markup=keyboard,
    )
    user["onboarded"] = True
    save_db(db)

async def start_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "goto_watch":
        await query.message.reply_text("Send me a Polymarket URL or keyword:")
        ctx.user_data["awaiting_watch_input"] = True
        ctx.user_data["watch_scope"] = "user"
    elif query.data == "goto_alerts":
        await _show_alerts(query.message, update.effective_user.id)


# ─── /watch (personal alerts) ────────────────────────────────────────────────

async def watch_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if args:
        await _resolve_watch_query(update, ctx, " ".join(args), scope="user")
    else:
        await update.message.reply_text(
            "Send me a Polymarket URL or keyword:\n\n"
            "`/watch bitcoin price`\n"
            "`/watch https://polymarket.com/event/...`",
            parse_mode="Markdown",
        )
        ctx.user_data["awaiting_watch_input"] = True
        ctx.user_data["watch_scope"] = "user"

async def handle_watch_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_watch_input"):
        return
    ctx.user_data["awaiting_watch_input"] = False
    scope = ctx.user_data.pop("watch_scope", "user")
    await _resolve_watch_query(update, ctx, update.message.text.strip(), scope=scope)

async def _resolve_watch_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                query_text: str, scope: str = "user"):
    msg = await update.message.reply_text("🔍 Searching...")
    ctx.user_data["watch_scope"] = scope

    if "polymarket.com" in query_text:
        market = await resolve_market_from_url(query_text)
        if not market:
            await msg.edit_text("❌ Couldn't find that market. Try a keyword instead.")
            return
        await _ask_threshold(update, ctx, msg, market)
    else:
        results = await search_markets(query_text)
        if not results:
            await msg.edit_text("❌ No markets found. Try a different keyword.")
            return
        if len(results) == 1:
            await _ask_threshold(update, ctx, msg, results[0])
        else:
            ctx.user_data["watch_candidates"] = {str(i): m for i, m in enumerate(results)}
            lines, buttons = [], []
            for i, m in enumerate(results):
                prob = yes_probability(m)
                prob_str = f"{prob}% YES" if prob is not None else "N/A"
                lines.append(f"{i+1}. *{m['question']}* — {prob_str}")
                buttons.append([InlineKeyboardButton(
                    f"{i+1}. {m['question'][:45]}…", callback_data=f"pick_market_{i}"
                )])
            await msg.edit_text(
                "Found multiple markets — pick one:\n\n" + "\n".join(lines),
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
            )

async def pick_market_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = query.data.replace("pick_market_", "")
    market = ctx.user_data.get("watch_candidates", {}).get(idx)
    if not market:
        await query.message.reply_text("❌ Selection expired. Try /watch again.")
        return
    await _ask_threshold(update, ctx, query.message, market)

async def _ask_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE, msg, market: dict):
    prob = yes_probability(market)
    prob_str = f"*Current YES: {prob}%*" if prob is not None else ""
    ctx.user_data["watch_market"] = market

    buttons = [
        [
            InlineKeyboardButton("Drop < 10%", callback_data="thresh_drop_10"),
            InlineKeyboardButton("Drop < 25%", callback_data="thresh_drop_25"),
        ],
        [
            InlineKeyboardButton("Rise > 75%", callback_data="thresh_rise_75"),
            InlineKeyboardButton("Rise > 90%", callback_data="thresh_rise_90"),
        ],
        [InlineKeyboardButton("📝 Custom threshold", callback_data="thresh_custom")],
    ]
    text = f"📊 *{market['question']}*\n\n{prob_str}\n\nChoose alert threshold:"
    try:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def threshold_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    market = ctx.user_data.get("watch_market")
    if not market:
        await query.message.reply_text("❌ Session expired. Try /watch again.")
        return
    if query.data == "thresh_custom":
        await query.message.reply_text(
            "Enter your threshold:\n`drop below 35` or `rise above 60`",
            parse_mode="Markdown",
        )
        ctx.user_data["awaiting_custom_threshold"] = True
        return
    direction, value = query.data.replace("thresh_", "").split("_")
    await _save_alert(query, ctx, market, direction, int(value))

async def handle_custom_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_custom_threshold"):
        return
    market = ctx.user_data.get("watch_market")
    if not market:
        await update.message.reply_text("❌ Session expired. Try /watch again.")
        return
    text = update.message.text.strip().lower()
    match = re.search(r"(drop|rise|above|below|<|>)\D*(\d+)", text)
    if not match:
        await update.message.reply_text(
            "Couldn't parse that. Try: `drop below 35` or `rise above 60`",
            parse_mode="Markdown",
        )
        return
    keyword, value = match.group(1), int(match.group(2))
    direction = "drop" if keyword in ("drop", "below", "<") else "rise"
    ctx.user_data["awaiting_custom_threshold"] = False
    await _save_alert(update, ctx, market, direction, value, is_message=True)

async def _save_alert(source, ctx, market: dict, direction: str, value: int,
                      is_message: bool = False, scope: str = None):
    scope = scope or ctx.user_data.get("watch_scope", "user")
    db = load_db()
    user_id = (
        source.effective_user.id if hasattr(source, "effective_user")
        else source.from_user.id
    )
    alert = {
        "slug":       market.get("slug", ""),
        "question":   market["question"],
        "direction":  direction,
        "threshold":  value,
        "created_at": datetime.utcnow().isoformat(),
        "triggered":  False,
        "scope":      scope,
    }
    if scope == "channel":
        db.setdefault("channel_alerts", []).append(alert)
    else:
        get_user(db, user_id)["alerts"].append(alert)
    save_db(db)

    url = market_url(market)
    prob = yes_probability(market)
    prob_str = f"Current: *{prob}% YES*" if prob is not None else ""
    direction_str = f"drops below {value}%" if direction == "drop" else f"rises above {value}%"

    if scope == "channel":
        header = "📣 *Channel broadcast alert set!*"
        footer = f"Will post to `{CHANNEL_ID}` when triggered.\n/broadcastlist to manage"
    else:
        header = "✅ *Alert set!*"
        footer = "/alerts to manage your alerts"

    confirm = (
        f"{header}\n\n"
        f"📊 [{market['question']}]({url})\n"
        f"{prob_str}\n\n"
        f"🔔 Fires when YES *{direction_str}*\n\n"
        f"{footer}"
    )
    await (source.message if is_message else source.message).reply_text(
        confirm, parse_mode="Markdown", disable_web_page_preview=False
    )


# ─── /alerts ─────────────────────────────────────────────────────────────────

async def alerts_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_alerts(update.message, update.effective_user.id)

async def _show_alerts(message, user_id: int):
    db = load_db()
    user = get_user(db, user_id)
    alerts = [a for a in user["alerts"] if not a.get("triggered")]
    if not alerts:
        await message.reply_text("No active alerts.\n\nUse /watch to add one!")
        return
    lines = ["📋 *Your active alerts:*\n"]
    buttons = []
    for i, a in enumerate(alerts):
        direction_str = f"< {a['threshold']}%" if a["direction"] == "drop" else f"> {a['threshold']}%"
        lines.append(f"{i+1}. *{a['question'][:55]}*\n   YES {direction_str}")
        buttons.append([InlineKeyboardButton(f"🗑 Remove #{i+1}", callback_data=f"remove_alert_{i}")])
    buttons.append([InlineKeyboardButton("➕ Add alert", callback_data="goto_watch")])
    await message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def remove_alert_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.replace("remove_alert_", ""))
    db = load_db()
    user = get_user(db, query.from_user.id)
    active = [a for a in user["alerts"] if not a.get("triggered")]
    if idx >= len(active):
        await query.message.reply_text("Alert not found.")
        return
    removed = active[idx]
    user["alerts"] = [a for a in user["alerts"] if a is not removed]
    save_db(db)
    await query.message.reply_text(
        f"🗑 Removed: *{removed['question'][:60]}*", parse_mode="Markdown"
    )


# ─── /portfolio ───────────────────────────────────────────────────────────────

async def portfolio_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        db = load_db()
        user = get_user(db, update.effective_user.id)
        if user.get("wallet"):
            await _show_portfolio(update.message, user["wallet"])
        else:
            await update.message.reply_text(
                "💼 *Link your wallet*\n\n`/portfolio 0xYourAddress`",
                parse_mode="Markdown",
            )
        return
    wallet = args[0].strip()
    if not re.match(r"^0x[0-9a-fA-F]{40}$", wallet):
        await update.message.reply_text("❌ Invalid Ethereum address.")
        return
    db = load_db()
    get_user(db, update.effective_user.id)["wallet"] = wallet
    save_db(db)
    await update.message.reply_text(f"✅ Wallet linked: `{wallet}`\n\nFetching...", parse_mode="Markdown")
    await _show_portfolio(update.message, wallet)

async def _show_portfolio(message, wallet: str):
    try:
        positions = await get_positions(wallet)
    except Exception as e:
        await message.reply_text(f"❌ Could not fetch positions: {e}")
        return
    if not positions:
        await message.reply_text(f"💼 No open positions for `{wallet[:10]}...`", parse_mode="Markdown")
        return
    lines = [f"💼 *Positions for* `{wallet[:8]}...{wallet[-4:]}`\n"]
    total = 0.0
    for p in positions[:15]:
        question = p.get("title") or p.get("market", {}).get("question", "Unknown")
        outcome  = p.get("outcome", "?")
        size     = float(p.get("size", 0))
        price    = float(p.get("currentPrice") or p.get("price") or 0)
        value    = size * price
        total   += value
        pnl      = p.get("cashPnl") or p.get("pnl")
        pnl_str  = f" | {'🟢' if float(pnl) >= 0 else '🔴'} ${float(pnl):.2f}" if pnl else ""
        lines.append(f"• *{question[:50]}*\n  {outcome} | {size:.1f} @ ${price:.2f} = *${value:.2f}*{pnl_str}")
    lines.append(f"\n💰 *Total: ${total:.2f}*")
    lines.append(f"[View on Polymarket](https://polymarket.com/profile/{wallet}{AFFILIATE_REF})")
    await message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


# ─── Admin: /broadcast ────────────────────────────────────────────────────────

async def broadcast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: set a threshold alert that broadcasts to the channel when fired."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❌ Set CHANNEL_ID in your .env first.")
        return
    args = ctx.args
    if args:
        await _resolve_watch_query(update, ctx, " ".join(args), scope="channel")
    else:
        await update.message.reply_text(
            "📣 *Set a channel broadcast alert*\n\n"
            "Usage: `/broadcast [market URL or keyword]`\n\n"
            "When the threshold fires, the full market card posts to the channel.",
            parse_mode="Markdown",
        )
        ctx.user_data["awaiting_watch_input"] = True
        ctx.user_data["watch_scope"] = "channel"

async def broadcastlist_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: list active channel alerts."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    db = load_db()
    alerts = [a for a in db.get("channel_alerts", []) if not a.get("triggered")]
    if not alerts:
        await update.message.reply_text("No active channel alerts.\n\nUse /broadcast to add one.")
        return
    lines = ["📣 *Channel alerts:*\n"]
    for i, a in enumerate(alerts):
        direction_str = f"< {a['threshold']}%" if a["direction"] == "drop" else f"> {a['threshold']}%"
        lines.append(f"{i+1}. *{a['question'][:55]}*\n   YES {direction_str}")
    lines.append("\n`/broadcastremove <n>` to remove")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def broadcastremove_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: remove a channel alert by 1-based index."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/broadcastremove 2`", parse_mode="Markdown")
        return
    idx = int(args[0]) - 1
    db = load_db()
    active = [a for a in db.get("channel_alerts", []) if not a.get("triggered")]
    if idx < 0 or idx >= len(active):
        await update.message.reply_text("❌ Index out of range.")
        return
    removed = active[idx]
    db["channel_alerts"] = [a for a in db["channel_alerts"] if a is not removed]
    save_db(db)
    await update.message.reply_text(
        f"🗑 Removed: *{removed['question'][:60]}*", parse_mode="Markdown"
    )


# ─── Admin: /post (immediate channel snapshot) ───────────────────────────────

async def post_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: immediately post a market card to the channel, no threshold needed."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not CHANNEL_ID:
        await update.message.reply_text("❌ Set CHANNEL_ID in your .env first.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/post [market URL or keyword]`", parse_mode="Markdown")
        return

    query_text = " ".join(args)
    msg = await update.message.reply_text("🔍 Fetching market...")

    if "polymarket.com" in query_text:
        market = await resolve_market_from_url(query_text)
    else:
        results = await search_markets(query_text)
        market = results[0] if results else None

    if not market:
        await msg.edit_text("❌ Market not found.")
        return

    card = format_market_card(market)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 View market", url=market_url(market)),
        InlineKeyboardButton("🤖 Set your alert", url=f"https://t.me/{ctx.bot.username}"),
    ]])
    await ctx.bot.send_message(
        chat_id=CHANNEL_ID,
        text=card,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=False,
    )
    await msg.edit_text(f"✅ Posted to {CHANNEL_ID}")


# ─── Background poller ────────────────────────────────────────────────────────

async def poll_alerts(app: Application):
    """
    Every 5 min:
      - User alerts   → DM the individual
      - Channel alerts → broadcast rich card to CHANNEL_ID
    """
    logger.info("Alert poller started")
    while True:
        await asyncio.sleep(300)
        try:
            db = load_db()

            # User alerts → DM
            for uid, user in db["users"].items():
                for alert in user["alerts"]:
                    if alert.get("triggered"):
                        continue
                    await _check_and_fire(app, alert, target=int(uid), is_channel=False)

            # Channel alerts → broadcast
            if CHANNEL_ID:
                for alert in db.get("channel_alerts", []):
                    if alert.get("triggered"):
                        continue
                    await _check_and_fire(app, alert, target=CHANNEL_ID, is_channel=True)

            save_db(db)
        except Exception as e:
            logger.error(f"Poller error: {e}")

async def _check_and_fire(app: Application, alert: dict, target, is_channel: bool):
    try:
        market = await get_market_by_slug(alert["slug"])
        if not market:
            return
        prob = yes_probability(market)
        if prob is None:
            return

        fired = (
            (alert["direction"] == "drop" and prob < alert["threshold"]) or
            (alert["direction"] == "rise" and prob > alert["threshold"])
        )
        if not fired:
            return

        alert["triggered"] = True
        direction_str = (
            f"dropped below {alert['threshold']}%"
            if alert["direction"] == "drop"
            else f"risen above {alert['threshold']}%"
        )

        if is_channel:
            # Rich card with prob bar + CTA buttons
            card = format_market_card(
                market,
                note=f"⚡ YES probability has {direction_str}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Trade now", url=market_url(market)),
                InlineKeyboardButton("🤖 Set your alert", url=f"https://t.me/{app.bot.username}"),
            ]])
            await app.bot.send_message(
                chat_id=target, text=card,
                parse_mode="Markdown", reply_markup=keyboard,
                disable_web_page_preview=False,
            )
        else:
            # Compact DM for personal alert
            url = market_url(market)
            await app.bot.send_message(
                chat_id=target,
                text=(
                    f"🚨 *Your alert triggered!*\n\n"
                    f"[{alert['question']}]({url})\n\n"
                    f"YES has *{direction_str}*\n"
                    f"Current: *{prob}%*"
                ),
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
    except Exception as e:
        logger.warning(f"Alert check failed ({alert.get('slug')}): {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("watch",           watch_command))
    app.add_handler(CommandHandler("alerts",          alerts_command))
    app.add_handler(CommandHandler("portfolio",       portfolio_command))

    # Admin commands
    app.add_handler(CommandHandler("broadcast",       broadcast_command))
    app.add_handler(CommandHandler("broadcastlist",   broadcastlist_command))
    app.add_handler(CommandHandler("broadcastremove", broadcastremove_command))
    app.add_handler(CommandHandler("post",            post_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(start_button,          pattern="^goto_"))
    app.add_handler(CallbackQueryHandler(pick_market_callback,  pattern="^pick_market_"))
    app.add_handler(CallbackQueryHandler(threshold_callback,    pattern="^thresh_"))
    app.add_handler(CallbackQueryHandler(remove_alert_callback, pattern="^remove_alert_"))

    # Free-text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_watch_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_threshold))

    # Background poller
    loop = asyncio.get_event_loop()
    loop.create_task(poll_alerts(app))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
