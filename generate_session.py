#!/usr/bin/env python3
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# ─── Load your .env ─────────────────────────────────
load_dotenv()  # <-- this pulls in API_ID, API_HASH, etc.

API_ID   = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""

if not API_ID or not API_HASH:
    print("❌ ERROR: API_ID or API_HASH not set in .env")
    exit(1)

# ─── Interactive login to produce a StringSession ────
client = TelegramClient(StringSession(), API_ID, API_HASH)

async def main():
    print("Logging into your Telegram account…")
    await client.start()  # prompts phone & code
    print("\n✅ Logged in successfully!")
    print("\nCopy this `SESSION_STRING` into your .env:\n")
    print(client.session.save())  # prints a long session string

if __name__ == "__main__":
    asyncio.run(main())