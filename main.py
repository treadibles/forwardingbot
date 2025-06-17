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

# ─── Setup and config ───────────────────────────────
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

# Targets and increments
target_chats = _config.get("target_chats", [])
inc_pound    = _config.get("inc_pound", {})
inc_cart     = _config.get("inc_cart", {})

# Threshold and regex
THRESHOLD = 200
_pattern  = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=\s*/\s*(?:[Pp]\s+for|[Ee][Aa]))", re.IGNORECASE)

# ─── Initialize Telethon ─────────────────────────────
tele_client = TelegramClient(MemorySession(), API_ID, API_HASH)
async def init_telethon():
    await tele_client.start(bot_token=BOT_TOKEN)
    await tele_client.get_entity(int(SOURCE_CHAT))

# ─── Keep-alive server ───────────────────────────────
app = Flask(__name__)
@app.route("/")
def ping(): return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port))
    t.daemon = True
    t.start()

# ─── Caption adjuster ───────────────────────────────
def adjust_caption(text: str, chat: str) -> str:
    def repl(m):
        prefix, orig = m.group(1), m.group(2)
        val = float(orig)
        inc = inc_pound.get(chat, 200) if val > THRESHOLD else inc_cart.get(chat, 15)
        new_val = val + inc
        if '.' in orig:
            dec_len = len(orig.split('.')[-1])
            fmt = f"{{:.{dec_len}f}}"
            new = fmt.format(new_val)
        else:
            new = str(int(new_val))
        return f"{prefix}{new}"  
    return _pattern.sub(repl, text)

# ─── /register ───────────────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        inc_pound[chat] = 200
        inc_cart[chat]  = 15
        _config.update({"target_chats": target_chats, "inc_pound": inc_pound, "inc_cart": inc_cart})
        with open(CONFIG_FILE, "w") as f:
            json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /forward ────────────────────────────────────────
async def forward_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /forward <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered. Use /register first.")
    msg = await update.message.reply_text("🔄 Forwarding history… this may take a while")
    count = 0
    async for orig in tele_client.iter_messages(SOURCE_CHAT, reverse=True):
        try:
            if orig.photo or orig.video or orig.document:
                sent = await ctx.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=SOURCE_CHAT,
                    message_id=orig.id
                )
                if orig.caption:
                    new_cap = adjust_caption(orig.caption, chat)
                    if new_cap != orig.caption:
                        await ctx.bot.edit_message_caption(
                            chat_id=sent.chat_id,
                            message_id=sent.message_id,
                            caption=new_cap
                        )
            elif orig.text:
                new_txt = adjust_caption(orig.text, chat)
                await ctx.bot.send_message(chat_id=chat, text=new_txt)
            count += 1
        except Exception:
            continue
    await msg.edit_text(f"✅ History forwarding complete: {count} messages sent to {chat}.")

# ─── Forward live media groups ──────────────────────
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
        except Exception:
            continue

# ─── Live forward handler ───────────────────────────
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
                sent = await ctx.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=SOURCE_CHAT,
                    message_id=msg.message_id
                )
                new_cap = adjust_caption(orig, chat)
                if new_cap != orig:
                    await ctx.bot.edit_message_caption(chat_id=sent.chat_id, message_id=sent.message_id, caption=new_cap)
            except Exception:
                continue

# ─── Entrypoint ─────────────────────────────────────
def main():
    asyncio.get_event_loop().run_until_complete(init_telethon())
    keep_alive()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("forward", forward_history))
    app.add_handler(CommandHandler("increasepound", increasepound))
    app.add_handler(CommandHandler("increasecart", increasecart))
    app.add_handler(MessageHandler(filters.ALL, forward_handler))
    print("Bot is running with history forward support.")
    app.run_polling()

if __name__ == "__main__":
    main()
