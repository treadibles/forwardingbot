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
from telethon.sessions import StringSession, MemorySession
from telethon.errors import FloodWaitError

# ─── Logging setup ──────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Load environment and config ────────────────────
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
SOURCE_CHAT = os.getenv("SOURCE_CHANNEL")
API_ID      = int(os.getenv("API_ID"))
API_HASH    = os.getenv("API_HASH")
CONFIG_FILE = "config.json"

# ─── Load or initialize persistent config ───────────
target_chats = []
inc_pound    = {}
inc_cart     = {}
try:
    _config = json.load(open(CONFIG_FILE))
    target_chats = _config.get("target_chats", [])
    inc_pound = _config.get("inc_pound", {})
    inc_cart = _config.get("inc_cart", {})
except:
    _config = {}

# ─── Constants and regex ───────────────────────────
THRESHOLD = 200
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=/\s*(?:[Pp]\s*for|[Ee][Aa]))", re.IGNORECASE)

# ─── Flask keep-alive app ──────────────────────────
webapp = Flask(__name__)
@webapp.route("/")
def ping():
    return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    thread = threading.Thread(target=lambda: webapp.run(host="0.0.0.0", port=port))
    thread.daemon = True
    thread.start()

# ─── Caption adjustment utility ────────────────────
def adjust_caption(text: str, chat: str) -> str:
    def repl(m):
        prefix, orig = m.group(1), m.group(2)
        val = float(orig)
        inc = inc_pound.get(chat, THRESHOLD) if val > THRESHOLD else inc_cart.get(chat, 15)
        new_val = val + inc
        if '.' in orig:
            dec_len = len(orig.split('.')[-1])
            new = f"{new_val:.{dec_len}f}"
        else:
            new = str(int(new_val))
        return f"{prefix}{new}"
    return _pattern.sub(repl, text)

# ─── /register handler ─────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        inc_pound[chat] = THRESHOLD
        inc_cart[chat] = 15
        _config["target_chats"] = target_chats
        _config["inc_pound"] = inc_pound
        _config["inc_cart"] = inc_cart
        with open(CONFIG_FILE, "w") as f:
            json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /increasepound handler ────────────────────────
async def increasepound(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasepound <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Please provide a valid number.")
    inc_pound[chat] = amt
    _config["inc_pound"] = inc_pound
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

# ─── /increasecart handler ─────────────────────────
async def increasecart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasecart <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Please provide a valid number.")
    inc_cart[chat] = amt
    _config["inc_cart"] = inc_cart
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

# ─── Initialize persistent Telethon user client for history ────
# Requires a pre-generated string session in the .env (e.g. via Telethon’s session.export())
SESSION_STRING = os.getenv("SESSION_STRING")
if not SESSION_STRING:
    raise RuntimeError("SESSION_STRING not set in .env. Please generate a Telethon string session.")
history_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── /forward handler (history) ─────────────────────────────────
import tempfile
from telethon.utils import get_extension

async def forward_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Forward all historical media posts (skipping text-only) from the source into the specified target channel,
    grouping albums/media-groups correctly, applying per-channel pound/cart increments.
    """
    # Validate arguments
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /forward <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered. Use /register first.")

    notify = await update.message.reply_text("🔄 Forwarding history… please wait")
    count = 0

    # Ensure history_client is ready
    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            return await notify.edit_text("❌ History forwarding unavailable: user session not authorized.")
    except Exception as e:
        return await notify.edit_text(f"❌ History session error: {e}")

    # Fetch source channel entity
    try:
        src = await history_client.get_entity(int(SOURCE_CHAT))
    except Exception as e:
        return await notify.edit_text(f"❌ Cannot access source channel: {e}")

    # Gather all media messages
    media_msgs = []
    async for msg in history_client.iter_messages(src, reverse=True):
        if msg.photo or msg.video or msg.document:
            media_msgs.append(msg)

    # Group messages by album (grouped_id) or alone
    groups = {}
    for msg in media_msgs:
        key = msg.grouped_id or msg.id
        groups.setdefault(key, []).append(msg)

    # Forward each group
    temp_dir = tempfile.mkdtemp(prefix="history_")
    for key, group in groups.items():
        group.sort(key=lambda m: m.date)
        if len(group) > 1 and group[0].grouped_id:
            # Album: download all items and send as a media_group
            orig_cap = group[0].message or ''
            new_cap = adjust_caption(orig_cap, chat) if orig_cap else None
            media = []
            for idx, m in enumerate(group):
                # Download media into temp_dir, returns the file path
                path = await history_client.download_media(m, file=temp_dir)
                cap = new_cap if idx == 0 else None
                # Determine media type by file extension
                lower = path.lower()
                if lower.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    media.append(InputMediaPhoto(open(path, 'rb'), caption=cap))
                elif lower.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    media.append(InputMediaVideo(open(path, 'rb'), caption=cap))
                else:
                    media.append(InputMediaDocument(open(path, 'rb'), caption=cap))
            try:
                await ctx.bot.send_media_group(chat_id=chat, media=media)
                count += len(media)
            except Exception:
                pass
        else:
            # Single media message: native forward
            m = group[0]
            try:
                sent = await ctx.bot.copy_message(chat_id=chat, from_chat_id=SOURCE_CHAT, message_id=m.id)
                orig_cap = m.caption or m.message or ''
                new_cap = adjust_caption(orig_cap, chat) if orig_cap else None
                if new_cap and new_cap != orig_cap:
                    await ctx.bot.edit_message_caption(chat_id=sent.chat_id, message_id=sent.message_id, caption=new_cap)
                count += 1
            except Exception:
                pass

    # Cleanup temporary files
    ... # existing cleanup code

    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")
    try:
        for f in os.listdir(temp_dir):
            os.remove(os.path.join(temp_dir, f))
        os.rmdir(temp_dir)
    except Exception:
        pass

    # Done
    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")
    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")

# buffer for live media-groups
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
        except:
            continue

# ─── Live forward handler ───────────────────────────
async def forward_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    # Only handle messages from the source channel
    if str(update.effective_chat.id) != SOURCE_CHAT or not target_chats:
        return

    # Handle media groups
    if msg.media_group_id:
        media_buf.setdefault(msg.media_group_id, []).append(msg)
        loop = asyncio.get_event_loop()
        loop.call_later(
            FLUSH_DELAY,
            lambda: asyncio.create_task(flush_media_group(msg.media_group_id, ctx))
        )
        return

            # Handle single media items (photo, video, document)
    if msg.photo or msg.video or msg.document:
        orig_caption = msg.caption or ""
        for chat in target_chats:
            try:
                # Compute adjusted caption
                new_cap = adjust_caption(orig_caption, chat) if orig_caption else None
                # Copy with overridden caption if applicable
                await ctx.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    caption=new_cap
                )
            except Exception:
                continue
        return

    # Handle text-only pricing posts (cart or pound) (cart or pound)
    if msg.text:
        # Only forward if text contains a price slash pattern
        if _pattern.search(msg.text):
            for chat in target_chats:
                new_txt = adjust_caption(msg.text, chat)
                try:
                    await ctx.bot.send_message(chat_id=chat, text=new_txt)
                except Exception:
                    continue
        return

    # Otherwise, skip text-only posts
    return
    if msg.photo or msg.video or msg.document:
        orig = msg.caption or ""
        for chat in target_chats:
            try:
                sent = await ctx.bot.copy_message(chat_id=chat, from_chat_id=SOURCE_CHAT, message_id=msg.message_id)
                new_cap = adjust_caption(orig, chat)
                if new_cap != orig:
                    await ctx.bot.edit_message_caption(chat_id=sent.chat_id, message_id=sent.message_id, caption=new_cap)
            except:
                continue

# ─── Entrypoint ─────────────────────────────────────
def main():
    keep_alive()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("forward", forward_history))
    application.add_handler(CommandHandler("increasepound", increasepound))
    application.add_handler(CommandHandler("increasecart", increasecart))
    application.add_handler(MessageHandler(filters.ALL, forward_handler))
    logger.info("Bot up and entering polling loop.")
    application.run_polling()

if __name__ == "__main__":
    main()
