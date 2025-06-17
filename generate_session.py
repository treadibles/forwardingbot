from telethon import TelegramClient
from telethon.sessions import StringSession
import os

API_ID   = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# Use a temporary MemorySession to log in interactively
client = TelegramClient(StringSession(), API_ID, API_HASH)

async def main():
    # This will ask for your phone & code once
    await client.start()
    print("Here is your new session string:\n")
    print(client.session.save())  # copy this value!

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())