# --- Telegram Forward Bot with Native Forwarding & Caption Editing ---

import subprocess
subprocess.run(["pip", "install", "--upgrade", "--force-reinstall", "telethon"])

import os
import re
import json
import asyncio
import nest_asyncio
from telethon import TelegramClient, events
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Load from Environment ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")

try:
    SOURCE_CHANNEL = int(SOURCE_CHANNEL)
except:
    pass

client = TelegramClient("bot_session", API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN)
app = ApplicationBuilder().token(BOT_TOKEN).build()

# --- Persistent Channel Storage ---
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

# --- Price Adjustment Logic ---
def adjust_prices(text):
    def replacement(match):
        before_for = match.group(1)
        after_for = match.group(2)
        try:
            before = int(before_for)
            if before < 50:
                before += 3
            else:
                before += 200
            return f"{before} for {after_for}"
        except:
            return match.group(0)

    return re.sub(r"(\d{2,5})\s+for\s+(\d+)", replacement, text)

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

# --- Debug Command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot is active and ready.")

app.add_handler(CommandHandler("start", start))

# --- Handle Albums ---
@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_album(event):
    try:
        message_ids = [m.id for m in event.messages]
        print(f"[FORWARDING GROUP] Messages: {message_ids}")

        for cid in registered_channels:
            fwd = await client.forward_messages(
                entity=cid,
                messages=message_ids,
                from_peer=SOURCE_CHANNEL,
                hide_sender=True
            )
            if not isinstance(fwd, list):
                fwd = [fwd]

            editable = next((m for m in fwd if m.out and m.message), None)
            if editable:
                new_caption = adjust_prices(editable.message)
                await client.edit_message(cid, editable.id, new_caption)
            else:
                print("[ERROR] No editable message in forwarded group")

    except Exception as e:
        print(f"[ERROR] Native Forward or Edit Failed: {e}")

# --- Handle Single Media Posts ---
@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handle_single(event):
    if event.grouped_id:
        return  # Skip albums

    print(f"[FORWARDING SINGLE] Message: {event.id}")

    for cid in registered_channels:
        try:
            fwd = await client.forward_messages(
                entity=cid,
                messages=event.id,
                from_peer=SOURCE_CHANNEL,
                hide_sender=True
            )
            if fwd.out and fwd.message:
                new_caption = adjust_prices(fwd.message)
                await client.edit_message(cid, fwd.id, new_caption)
        except Exception as e:
            print(f"[ERROR] Native Forward/Edit Single Failed: {e}")

# --- Main Event Loop ---
async def main():
    await client.start(phone=PHONE_NUMBER)

    async def run_bot_commands():
        print("✅ Command handler (python-telegram-bot) running.")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    asyncio.create_task(run_bot_commands())

    print("🚀 Telethon client listening...")

    try:
        await asyncio.Future()  # Wait forever until interrupted
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Shutdown triggered")
    finally:
        await app.stop()
        await client.disconnect()

if __name__ == '__main__':
    nest_asyncio.apply()
    asyncio.run(main())