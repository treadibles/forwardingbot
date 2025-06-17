#!/usr/bin/env python3
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()
API_ID        = int(os.getenv("API_ID") or 0)
API_HASH      = os.getenv("API_HASH") or ""
SESSION_STRING= os.getenv("SESSION_STRING") or ""

async def main():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    ok = await client.is_user_authorized()
    print("Authorized?" , ok)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())