import os
import asyncio
import json
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Load from Environment ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")

try:
    SOURCE_CHANNEL = int(SOURCE_CHANNEL)
except:
    pass

# --- Setup Clients ---
client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
app = ApplicationBuilder().token(BOT_TOKEN).build()

# --- Persistent Channel Registry ---
CHANNELS_FILE = "registered_channels.json"

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_channels():
    with open(CHANNELS_FILE, "w") as f:
        json.dump(list(registered_channels), f)

registered_channels = load_channels()

# --- /register Command ---
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /register <channel_id>")
        return
    try:
        cid = int(context.args[0])
        registered_channels.add(cid)
        save_channels()
        await update.message.reply_text(f"✅ Registered channel: {cid}")
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID format")

app.add_handler(CommandHandler("register", register))

# --- Album Forwarding Handler ---
@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_album(event):
    message_ids = [m.id for m in event.messages]
    for cid in registered_channels:
        try:
            fwd = await client.forward_messages(
                entity=cid,
                messages=message_ids,
                from_peer=SOURCE_CHANNEL,
                hide_sender=True  # Requires Telethon >=1.41.0 from GitHub
            )
            print(f"[FORWARDED] Album to {cid} as native media group.")
        except Exception as e:
            print(f"[ERROR] Forwarding failed for {cid}: {e}")

# --- Main Event Loop ---
async def main():
    async def run_commands():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    asyncio.create_task(run_commands())
    print("🚀 Telethon client running...")
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())