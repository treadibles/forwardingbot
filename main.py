import os
import re
import json
import asyncio
import threading
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telethon import TelegramClient
from telethon.sessions import MemorySession

# ─── Load env & config ─────────────────────────────
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
SOURCE_CHAT = os.getenv("SOURCE_CHANNEL")  # source channel ID or @username
API_ID      = int(os.getenv("API_ID"))
API_HASH    = os.getenv("API_HASH")
CONFIG_FILE = "config.json"

# Load or initialize config
try:
    _config = json.load(open(CONFIG_FILE))
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

# List of target channels
target_chats = _config.get("target_chats", [])
# Per-channel increments for > and < threshold
inc_pound = _config.get("inc_pound", {})
inc_cart  = _config.get("inc_cart", {})

# Dollar/number regex: match optional '$', digits, optional decimals, before '/P for'
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=/[Pp]\s+for)")
# Threshold determines which increment to apply
THRESHOLD = 200

# ─── Telethon setup ─────────────────────────────────
tele_client = TelegramClient(MemorySession(), API_ID, API_HASH)

async def init_telethon():
    await tele_client.start(bot_token=BOT_TOKEN)
    await tele_client.get_entity(int(SOURCE_CHAT))

# ─── Keep-alive server ──────────────────────────────
app = Flask(__name__)
@app.route("/")
def ping(): return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port))
    t.daemon = True
    t.start()

# ─── Caption adjuster ──────────────────────────────
def adjust_caption(text: str, chat: str) -> str:
    """
    For values > THRESHOLD, add inc_pound[chat].
    For values < THRESHOLD, add inc_cart[chat].
    Preserves original decimal places.
    """
    def repl(m):
        prefix, orig_num = m.group(1), m.group(2)
        val = float(orig_num)
        if val > THRESHOLD:
            inc = inc_pound.get(chat, THRESHOLD)
        else:
            inc = inc_cart.get(chat, 0)
        new_val = val + inc
        # preserve decimal precision
        if "." in orig_num:
            dec_len = len(orig_num.split(".")[1])
            new_str = f"{new_val:.{dec_len}f}"
        else:
            new_str = str(int(new_val))
        return f"{prefix}{new_str}"
    return _pattern.sub(repl, text)

# ─── /register ──────────────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global target_chats, inc_pound, inc_cart, _config
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        # set defaults: +200 for pound, +15 for cart
        inc_pound[chat] = 200
        inc_cart[chat]  = 15
        _config.update({
            "target_chats": target_chats,
            "inc_pound": inc_pound,
            "inc_cart": inc_cart,
        })
        json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /increasepound ────────────────────────────────
async def increasepound(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global inc_pound, _config
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasepound <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Provide a valid number.")
    inc_pound[chat] = amt
    _config["inc_pound"] = inc_pound
    json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

# ─── /increasecart ─────────────────────────────────
async def increasecart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global inc_cart, _config
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasecart <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Provide a valid number.")
    inc_cart[chat] = amt
    _config["inc_cart"] = inc_cart
    json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

# ─── Media-group flush ─────────────────────────────
media_buf = {}
FLUSH_DELAY = 1.0

async def flush_media_group(gid: str, ctx: ContextTypes.DEFAULT_TYPE):
    msgs = media_buf.pop(gid, None)
    if not msgs: return
    msgs.sort(key=lambda m: m.message_id)
    orig = msgs[0].caption or ""
    for chat in target_chats:
        new_cap = adjust_caption(orig, chat)
        media = []
        for idx, m in enumerate(msgs):
            cap = new_cap if idx == 0 else None
            if m.photo:
                media.append(InputMediaPhoto(m.photo[-1].file_id, caption=cap))
            elif m.video:
                media.append(InputMediaVideo(m.video.file_id, caption=cap))
            else:
                media.append(InputMediaDocument(m.document.file_id, caption=cap))
        await ctx.bot.send_media_group(chat_id=chat, media=media)

# ─── Forward handler ─────────────────────────────────
async def forward_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if str(update.effective_chat.id) != SOURCE_CHAT or not target_chats:
        return
    if msg.media_group_id:
        media_buf.setdefault(msg.media_group_id, []).append(msg)
        loop = asyncio.get_event_loop()
        loop.call_later(FLUSH_DELAY, lambda: asyncio.create_task(flush_media_group(msg.media_group_id, ctx)))
        return
    if msg.photo or msg.video or msg.document:
        orig = msg.caption or ""
        for chat in target_chats:
            copy = await ctx.bot.copy_message(chat_id=chat, from_chat_id=msg.chat.id, message_id=msg.message_id)
            new_cap = adjust_caption(orig, chat)
            if new_cap != orig:
                await ctx.bot.edit_message_caption(chat_id=copy.chat_id, message_id=copy.message_id, caption=new_cap)
        return
    # ignore text-only

# ─── Entrypoint ────────────────────────────────────
def main():
    asyncio.get_event_loop().run_until_complete(init_telethon())
    keep_alive()
    bot = ApplicationBuilder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("register", register))
    bot.add_handler(CommandHandler("increasepound", increasepound))
    bot.add_handler(CommandHandler("increasecart", increasecart))
    bot.add_handler(MessageHandler(filters.ALL, forward_handler))
    print("Bot is up—running with decimal support.")
    bot.run_polling()

if __name__ == "__main__":
    main()
