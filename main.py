import os
import asyncio
import json
from telethon import TelegramClient, events
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")

try:
    SOURCE_CHANNEL = int(SOURCE_CHANNEL)
except:
    pass

client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
app = ApplicationBuilder().token(BOT_TOKEN).build()

CHANNELS_FILE = "registered_channels.json"

# --- Registered Channels ---
def load_channels():
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_channels():
    with open(CHANNELS_FILE, "w") as f:
        json.dump(list(registered_channels), f)

registered_channels = load_channels()

# --- /register command ---
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

# --- Handle media album by downloading before sending ---
@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_album(event):
    print(f"[DEBUG] New post detected in source channel {SOURCE_CHANNEL}.")
    media_messages = event.messages
    print(f"[DEBUG] Detected {len(media_messages)} media items in album.")

    for cid in registered_channels:
        try:
            downloaded = []
            for index, m in enumerate(media_messages):
                print(f"[DEBUG] Downloading item {index + 1}/{len(media_messages)}...")
                file_path = await m.download_media()
                print(f"[DEBUG] Downloaded item {index + 1}: {file_path}")
                downloaded.append((file_path, m.message or ""))

            if downloaded:
                print(f"[INFO] All files downloaded. Uploading to channel {cid}...")
                await client.send_file(
                    cid,
                    files=[item[0] for item in downloaded],
                    caption=downloaded[0][1],
                    group=True,
                )
                print(f"[SUCCESS] Album sent to {cid} ✅")

                for f, _ in downloaded:
                    try:
                        os.remove(f)
                        print(f"[CLEANUP] Removed file: {f}")
                    except Exception as cleanup_err:
                        print(f"[WARN] Failed to delete {f}: {cleanup_err}")

        except Exception as e:
            print(f"[ERROR] Failed to send album to {cid}: {e}")

# --- Main loop ---
async def main():
    async def run_bot_commands():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()

    asyncio.create_task(run_bot_commands())
    print("🚀 Bot is running. Albums will be copied and posted after download completes.")
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
