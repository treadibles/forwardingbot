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
BOT_TOKEN    = os.getenv("BOT_TOKEN")
SOURCE_CHAT  = os.getenv("SOURCE_CHANNEL")  # source channel ID or @username
API_ID       = int(os.getenv("API_ID"))
API_HASH     = os.getenv("API_HASH")
CONFIG_FILE  = "config.json"

# Load or initialize config
try:
    _config = json.load(open(CONFIG_FILE))
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

# List of target channels
target_chats = _config.get("target_chats", [])
# Per-chat increments for values > threshold
inc_pound = _config.get("inc_pound", {})
# Per-chat increments for values < threshold
inc_cart  = _config.get("inc_cart", {})

# Threshold value
THRESHOLD = 200

# Initialize Telethon client (memory session)
tele_client = TelegramClient(MemorySession(), API_ID, API_HASH)

async def init_telethon():
    await tele_client.start(bot_token=BOT_TOKEN)
    # Cache source channel to avoid unresolved errors
    await tele_client.get_entity(int(SOURCE_CHAT))

# ─── Keep-alive webserver ───────────────────────────
webapp = Flask(__name__)

@webapp.route("/")
def ping():
    return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    thread = threading.Thread(
        target=lambda: webapp.run(host="0.0.0.0", port=port)
    )
    thread.daemon = True
    thread.start()

# ─── Caption adjustment ─────────────────────────────
_pattern = re.compile(r"(\$?)(\d+)(?=/P\s+for)")

def adjust_caption(text: str, chat: str) -> str:
    """
    If number > THRESHOLD: add inc_pound[chat]
    If number < THRESHOLD: add inc_cart[chat]
    """
    def repl(m):
        prefix, val = m.group(1), int(m.group(2))
        if val > THRESHOLD:
            inc = inc_pound.get(chat, 200)
            new_val = val + inc
        elif val < THRESHOLD:
            inc = inc_cart.get(chat, 15)
            new_val = val + inc
        else:
            return m.group(0)
        return f"{prefix}{new_val}"
    return _pattern.sub(repl, text)

# ─── /register handler ──────────────────────────────
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global target_chats, inc_pound, inc_cart, _config
    if not context.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = context.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        # set defaults
        inc_pound[chat] = 200
        inc_cart[chat]  = 15
        _config["target_chats"] = target_chats
        _config["inc_pound"]    = inc_pound
        _config["inc_cart"]     = inc_cart
        json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /increasepound handler ─────────────────────────
# Usage: /increasepound <chat_id_or_username> <amount>
async def increasepound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global inc_pound, _config
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /increasepound <chat> <amount>")
    chat, val_str = context.args
    if chat not in target_chats:
        return await update.message.reply_text(f"Channel {chat} not registered.")
    try:
        amt = int(val_str)
    except ValueError:
        return await update.message.reply_text("Please provide an integer value.")
    inc_pound[chat] = amt
    _config["inc_pound"] = inc_pound
    json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

# ─── /increasecart handler ──────────────────────────
# Usage: /increasecart <chat_id_or_username> <amount>
async def increasecart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global inc_cart, _config
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /increasecart <chat> <amount>")
    chat, val_str = context.args
    if chat not in target_chats:
        return await update.message.reply_text(f"Channel {chat} not registered.")
    try:
        amt = int(val_str)
    except ValueError:
        return await update.message.reply_text("Please provide an integer value.")
    inc_cart[chat] = amt
    _config["inc_cart"] = inc_cart
    json.dump(_config, open(CONFIG_FILE, "w"), indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

# ─── Media-group flushing ───────────────────────────
media_buffers = {}
FLUSH_DELAY   = 1.0

async def flush_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    group = media_buffers.pop(media_group_id, None)
    if not group or not target_chats:
        return
    group.sort(key=lambda m: m.message_id)
    orig = group[0].caption or ""
    for chat in target_chats:
        new_cap = adjust_caption(orig, chat)
        media = []
        for idx, msg in enumerate(group):
            cap = new_cap if idx == 0 else None
            if msg.photo:
                media.append(InputMediaPhoto(msg.photo[-1].file_id, caption=cap))
            elif msg.video:
                media.append(InputMediaVideo(msg.video.file_id, caption=cap))
            else:
                media.append(InputMediaDocument(msg.document.file_id, caption=cap))
        await context.bot.send_media_group(chat_id=chat, media=media)

# ─── Forward handler ─────────────────────────────────
async def forward_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if str(update.effective_chat.id) != SOURCE_CHAT or not target_chats:
        return

    # media-group only
    if msg.media_group_id:
        media_buffers.setdefault(msg.media_group_id, []).append(msg)
        loop = asyncio.get_event_loop()
        loop.call_later(
            FLUSH_DELAY,
            lambda: asyncio.create_task(flush_media_group(msg.media_group_id, context))
        )
        return

    # single-media only
    if msg.photo or msg.video or msg.document:
        orig = msg.caption or ""
        for chat in target_chats:
            new_cap = adjust_caption(orig, chat)
            kwargs = {
                'chat_id': chat,
                'from_chat_id': msg.chat.id,
                'message_id': msg.message_id,
            }
            if new_cap != orig:
                kwargs['caption'] = new_cap
            await context.bot.copy_message(**kwargs)
        return
    # ignore text-only

# ─── Entrypoint ────────────────────────────────────
def main():
    # Init Telethon
    asyncio.get_event_loop().run_until_complete(init_telethon())
    # Keep-alive server
    keep_alive()
    # Build Telegram bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("increasepound", increasepound))
    app.add_handler(CommandHandler("increasecart", increasecart))
    app.add_handler(MessageHandler(filters.ALL, forward_handler))
    print("Bot is up—running with per-channel increments.")
    app.run_polling()

if __name__ == "__main__":
    main()
