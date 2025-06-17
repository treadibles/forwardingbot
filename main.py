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
from telethon.sessions import MemorySession
from telethon.errors import FloodWaitError

# ─── Logging setup ──────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Load env & config ─────────────────────────────
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
SOURCE_CHAT = os.getenv("SOURCE_CHANNEL")
API_ID      = int(os.getenv("API_ID"))
API_HASH    = os.getenv("API_HASH")
CONFIG_FILE = "config.json"

# Load or initialize config file
target_chats = []
inc_pound = {}
inc_cart = {}
try:
    _config = json.load(open(CONFIG_FILE))
    target_chats = _config.get("target_chats", [])
    inc_pound = _config.get("inc_pound", {})
    inc_cart = _config.get("inc_cart", {})
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

# Threshold and regex
THRESHOLD = 200
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=\s*/\s*(?:[Pp]\s+for|[Ee][Aa]))", re.IGNORECASE)

# ─── Telethon client init ───────────────────────────
tele_client = TelegramClient(MemorySession(), API_ID, API_HASH)
# Will hold the input entity for the source channel to avoid implicit contact lookups
SOURCE_ENTITY = None

async def init_telethon():
    """
    Start Telethon client and cache the source channel entity.
    Handles flood waits gracefully.
    """
    try:
        await tele_client.start(bot_token=BOT_TOKEN)
    except FloodWaitError as e:
        logger.error(f"Telethon FloodWait: wait {e.seconds}s; skipping init.")
        return
    except Exception as e:
        logger.error(f"Error starting Telethon: {e}")
        return

    try:
        entity = await tele_client.get_entity(int(SOURCE_CHAT))
        global SOURCE_ENTITY
