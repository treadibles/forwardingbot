#!/usr/bin/env python3
import os, asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()
API_ID      = int(os.getenv("API_ID") or 0)
API_HASH    = os.getenv("API_HASH") or ""
PHONE       = os.getenv("PHONE_NUMBER") or ""

if not (API_ID and API_HASH and PHONE):
    print("❌ Make sure API_ID, API_HASH and PHONE_NUMBER are set in .env")
    exit(1)

client = TelegramClient(StringSession(), API_ID, API_HASH)

async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("Requesting login code…")
        # Try Telegram in-app first, but force SMS if you don’t see it
        await client.send_code_request(PHONE, force_sms=True)
        code = input("Enter the SMS code you received: ").strip()
        await client.sign_in(PHONE, code)
    print("\n✅ Authorized! Here is your SESSION_STRING:\n")
    print(client.session.save())

if __name__ == "__main__":
    asyncio.run(main())