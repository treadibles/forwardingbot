import os
import asyncio
import json
from telethon import TelegramClient, events
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

try:
    SOURCE_CHANNEL = int(SOURCE_CHANNEL)
except:
    pass

# User session only — no bot token usage
client = TelegramClient("user_session", API_ID, API_HASH)

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

# --- Handle media albums ---
@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_album(event):
    print(f"[DEBUG] New album detected in source channel {SOURCE_CHANNEL}.")
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

# --- Handle all other messages (text or single media) ---
@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handle_message(event):
    print(f"[DEBUG] New message detected in source channel {SOURCE_CHANNEL}.")

    for cid in registered_channels:
        try:
            if event.media:
                print("[DEBUG] Media message detected. Downloading...")
                file_path = await event.download_media()
                print(f"[DEBUG] Media downloaded: {file_path}")
                await client.send_file(
                    cid,
                    file=file_path,
                    caption=event.message or ""
                )
                os.remove(file_path)
                print(f"[SUCCESS] Media message sent to {cid} ✅")
            elif event.text:
                await client.send_message(cid, event.text)
                print(f"[SUCCESS] Text message sent to {cid} ✅")
        except Exception as e:
            print(f"[ERROR] Failed to forward message to {cid}: {e}")

# --- Catch-all debugging handler ---
@client.on(events.NewMessage)
async def debug_all(event):
    print(f"[DEBUG][ALL] Received message from chat {event.chat_id}: {event.text or '[media]'}")

# --- Main loop ---
async def main():
    await client.start(phone=PHONE_NUMBER)

    print("🚀 Bot is running. Albums and all messages will be copied and reposted.")
    print(f"🔍 Listening to source channel: {SOURCE_CHANNEL}")
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
