import os
import re
import json
import asyncio
import threading
import logging
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

# ─── Setup logging ──────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
# Per-channel increments
inc_pound = _config.get("inc_pound", {})
inc_cart  = _config.get("inc_cart", {})

# Threshold divide
THRESHOLD = 200
# Regex match for '/P for' or '/ea'
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=\s*/\s*(?:[Pp]\s+for|[Ee][Aa]))", re.IGNORECASE)

# Initialize Telethon (persistent memory session)
tele_client = TelegramClient(MemorySession(), API_ID, API_HASH)

async def init_telethon():
    await tele_client.start(bot_token=BOT_TOKEN)
    # cache source channel
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
    def repl(m):
        prefix, orig = m.group(1), m.group(2)
        val = float(orig)
        inc = inc_pound.get(chat, 200) if val > THRESHOLD else inc_cart.get(chat, 15)
        new_val = val + inc
        # preserve decimals
        if '.' in orig:
            dec_len = len(orig.split('.')[-1])
            new = f"{new_val:.{dec_len}f}"
        else:
            new = str(int(new_val))
        return f"{prefix}{new}"
    return _pattern.sub(repl, text)

# ─── /register ──────────────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        inc_pound[chat] = 200
        inc_cart[chat]  = 15
        _config["target_chats"] = target_chats
        _config["inc_pound"] = inc_pound
        _config["inc_cart"] = inc_cart
        with open(CONFIG_FILE, "w") as f:
            json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /list ──────────────────────────────────────────
async def list_targets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not target_chats:
        return await update.message.reply_text("No registered targets.")
    text = "Registered channels:\n" + "\n".join(target_chats)
    await update.message.reply_text(text)

# ─── /check to test channel permissions ─────────────
async def check_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /check <chat_id_or_username>")
    chat = ctx.args[0]
    try:
        info = await ctx.bot.get_chat(chat)
        await update.message.reply_text(f"✅ Chat found: {info.title or info.id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error accessing {chat}: {e}")

# ─── /increasepound ────────────────────────────────
async def increasepound(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

# ─── /increasecart ─────────────────────────────────
async def increasecart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

# ─── Media-group flush ─────────────────────────────
media_buf = {}
FLUSH_DELAY = 1.0

async def flush_media_group(gid: str, ctx: ContextTypes.DEFAULT_TYPE):
    msgs = media_buf.pop(gid, [])
    if not msgs:
        return
    msgs.sort(key=lambda m: m.message_id)
    orig = msgs[0].caption or ""
    for chat in target_chats:
        try:
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
        except Exception as e:
            logger.error(f"Failed media-group to {chat}: {e}")

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
            try:
                copy = await ctx.bot.copy_message(chat_id=chat, from_chat_id=msg.chat.id, message_id=msg.message_id)
                new_cap = adjust_caption(orig, chat)
                if new_cap != orig:
                    await ctx.bot.edit_message_caption(chat_id=copy.chat_id, message_id=copy.message_id, caption=new_cap)
            except Exception as e:
                logger.error(f"Failed single media to {chat}: {e}")
        return

# ─── Entrypoint ────────────────────────────────────
def main():
    asyncio.get_event_loop().run_until_complete(init_telethon())
    keep_alive()
    bot = ApplicationBuilder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("register", register))
    bot.add_handler(CommandHandler("list", list_targets))
    bot.add_handler(CommandHandler("check", check_channel))
    bot.add_handler(CommandHandler("increasepound", increasepound))
    bot.add_handler(CommandHandler("increasecart", increasecart))
    bot.add_handler(MessageHandler(filters.ALL, forward_handler))
    logger.info("Bot is up—handlers registered.")
    bot.run_polling()

if __name__ == "__main__":
    main()
