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
from telethon.sessions import StringSession, MemorySession
from telethon.errors import FloodWaitError

# ─── Logging setup ──────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Load environment and config ────────────────────
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
SOURCE_CHAT = os.getenv("SOURCE_CHANNEL")
API_ID      = int(os.getenv("API_ID"))
API_HASH    = os.getenv("API_HASH")
CONFIG_FILE = "config.json"

# ─── Load or initialize persistent config ───────────
target_chats = []
inc_pound    = {}
inc_cart     = {}
text_targets = []
album_index  = {} 

try:
    _config = json.load(open(CONFIG_FILE))
    target_chats = _config.get("target_chats", [])
    inc_pound = _config.get("inc_pound", {})
    inc_cart = _config.get("inc_cart", {})
    text_targets = _config.get("text_targets", [])
    album_index  = _config.get("album_index", {})
except:
    _config = {}

# ─── Constants and regex ───────────────────────────
THRESHOLD = 200
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=/\s*(?:[Pp]\s*for|[Ee][Aa]))", re.IGNORECASE)
_pattern_takefor = re.compile(r'(?i)(\btake\s*for\s*)(\$?)(\d+(?:\.\d+)?)\b')

# ─── Flask keep-alive app ──────────────────────────
webapp = Flask(__name__)
@webapp.route("/")
def ping():
    return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 8080))
    thread = threading.Thread(target=lambda: webapp.run(host="0.0.0.0", port=port))
    thread.daemon = True
    thread.start()

# ─── Caption adjustment utility ────────────────────
def adjust_caption(text: str, chat: str) -> str:
    def repl(m):
        prefix, orig = m.group(1), m.group(2)
        val = float(orig)
        inc = inc_pound.get(chat, THRESHOLD) if val > THRESHOLD else inc_cart.get(chat, 15)
        new_val = val + inc
        if '.' in orig:
            dec_len = len(orig.split('.')[-1])
            new = f"{new_val:.{dec_len}f}"
        else:
            new = str(int(new_val))
        return f"{prefix}{new}"
    return _pattern.sub(repl, text)

import unicodedata, string
_WS = re.compile(r"\s+")
def _norm(s: str) -> str:
    """Normalize for safe prefix/contains checks."""
    if not s: return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = _WS.sub(" ", s).strip()
    return s

def _first_non_empty_caption(msgs):
    """Return the first non-empty caption/message string within a media group."""
    for x in msgs:
        cap = (x.message or x.caption or "").strip()
        if cap:
            return cap
    return ""

# Detect plain URLs, t.me links, and Markdown-style [text](url)
URL_PATTERN = re.compile(
    r'(?ix)'
    r'(?:\b(?:https?://|www\.)\S+)'            # http(s) or www.
    r'|(?:\bt\.me/\S+|\btelegram\.me/\S+)'     # Telegram shortlinks
    r'|\[[^\]]+\]\((?:https?://|www\.)[^)]+\)' # markdown link
)

def contains_link(update_text: str, update_obj: Update) -> bool:
    if update_text and URL_PATTERN.search(update_text):
        return True
    # also honor Telegram’s entity parsing just in case
    ent = getattr(getattr(update_obj, "message", None), "entities", None) or []
    return any(e.type in ("url", "text_link") for e in ent)

# ─── /register handler ─────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        target_chats.append(chat)
        inc_pound[chat] = THRESHOLD
        inc_cart[chat] = 15
        _config["target_chats"] = target_chats
        _config["inc_pound"] = inc_pound
        _config["inc_cart"] = inc_cart
        with open(CONFIG_FILE, "w") as f:
            json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Added target channel: {chat}")

# ─── /increasepound handler ────────────────────────
async def increasepound(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasepound <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Please provide a valid number.")
    inc_pound[chat] = amt
    _config["inc_pound"] = inc_pound
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

# ─── /increasecart handler ─────────────────────────
async def increasecart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasecart <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Please provide a valid number.")
    inc_cart[chat] = amt
    _config["inc_cart"] = inc_cart
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

# ─── Initialize persistent Telethon user client for history ────
# Requires a pre-generated string session in the .env (e.g. via Telethon’s session.export())
SESSION_STRING = os.getenv("SESSION_STRING")
if not SESSION_STRING:
    raise RuntimeError("SESSION_STRING not set in .env. Please generate a Telethon string session.")
history_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── /targets: show currently registered forwarding targets ────────
async def targets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not target_chats:
        return await update.message.reply_text("Targets: (none)")
    await update.message.reply_text("Targets:\n" + "\n".join(map(str, target_chats)))

# ─── /forward handler (history) ─────────────────────────────────
import tempfile
from telethon.utils import get_extension

async def forward_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Forward all historical media posts (skipping text-only) from the source into the specified target channel,
    grouping albums/media-groups correctly, applying per-channel pound/cart increments.
    """
    # Validate arguments
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /forward <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered. Use /register first.")

    notify = await update.message.reply_text("🔄 Forwarding history… please wait")
    count = 0

    # Ensure history_client is ready
    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            return await notify.edit_text("❌ History forwarding unavailable: user session not authorized.")
    except Exception as e:
        return await notify.edit_text(f"❌ History session error: {e}")

    # Fetch source channel entity
    try:
        src = await history_client.get_entity(int(SOURCE_CHAT))
    except Exception as e:
        return await notify.edit_text(f"❌ Cannot access source channel: {e}")

    # Gather all media messages
    media_msgs = []
    async for msg in history_client.iter_messages(src, reverse=True):
        if msg.photo or msg.video or msg.document:
            media_msgs.append(msg)

    # Group messages by album (grouped_id) or alone
    groups = {}
    for msg in media_msgs:
        key = msg.grouped_id or msg.id
        groups.setdefault(key, []).append(msg)

    # Forward each group
    temp_dir = tempfile.mkdtemp(prefix="history_")
    for key, group in groups.items():
        group.sort(key=lambda m: m.date)
        if len(group) > 1 and group[0].grouped_id:
            # Album: download all items and send as a media_group
            orig_cap = group[0].message or ''
            new_cap = adjust_caption(orig_cap, chat) if orig_cap else None
            media = []
            for idx, m in enumerate(group):
                # Download media into temp_dir, returns the file path
                path = await history_client.download_media(m, file=temp_dir)
                cap = new_cap if idx == 0 else None
                # Determine media type by file extension
                lower = path.lower()
                if lower.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    media.append(InputMediaPhoto(open(path, 'rb'), caption=cap))
                elif lower.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    media.append(InputMediaVideo(open(path, 'rb'), caption=cap))
                else:
                    media.append(InputMediaDocument(open(path, 'rb'), caption=cap))
            try:
                sent = await ctx.bot.send_media_group(chat_id=chat, media=media)
                count += len(sent)
                msg_ids = [m.message_id for m in sent]
                _add_album_record(chat, new_cap or "", msg_ids)
            except Exception:
                pass
        else:
            # Single media message: native forward
            m = group[0]
            try:
                sent = await ctx.bot.copy_message(chat_id=chat, from_chat_id=SOURCE_CHAT, message_id=m.id)
                orig_cap = m.caption or m.message or ''
                new_cap = adjust_caption(orig_cap, chat) if orig_cap else None
                if new_cap and new_cap != orig_cap:
                    await ctx.bot.edit_message_caption(chat_id=sent.chat_id, message_id=sent.message_id, caption=new_cap)
                count += 1
            except Exception:
                pass

    # Cleanup temporary files
    ... # existing cleanup code

    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")
    try:
        for f in os.listdir(temp_dir):
            os.remove(os.path.join(temp_dir, f))
        os.rmdir(temp_dir)
    except Exception:
        pass

    # Done
    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")
    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")(f"✅ History forwarded: {count} messages to {chat}.")

# buffer for live media-groups
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
            sent = await ctx.bot.send_media_group(chat_id=chat, media=media)
            msg_ids = [m.message_id for m in sent]
            _add_album_record(chat, new_cap or "", msg_ids)
        except:
            continue

def _save_config():
    _config["target_chats"] = target_chats
    _config["inc_pound"]    = inc_pound
    _config["inc_cart"]     = inc_cart
    _config["text_targets"] = text_targets
    _config["album_index"]  = album_index
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)

def _add_album_record(chat: str, caption: str, message_ids: list[int]):
    cid = str(chat)
    album_index.setdefault(cid, [])
    album_index[cid].append({"caption": caption or "", "message_ids": message_ids})
    _save_config()

def _extract_phrase_before_sold_out(text: str) -> str:
    i = text.lower().find("sold out")
    if i == -1:
        return ""
    return text[:i].strip()

def _resolve_target(chat: str | int):
    """
    Accepts -100... numeric IDs or @username/str.
    For numeric strings, return the integer (keep the -100 prefix intact).
    """
    s = str(chat)
    if s.lstrip("-").isdigit():
        return int(s)
    return chat  # @username

async def _delete_matching_album(ctx: ContextTypes.DEFAULT_TYPE, chat: str, phrase: str) -> bool:
    """
    Find the most recent album in album_index[chat] whose caption STARTS with `phrase`
    (case-insensitive), delete all its messages, and remove it from the index.
    Returns True if something was deleted.
    """
    cid = str(chat)
    if cid not in album_index or not album_index[cid]:
        return False

    phrase_norm = phrase.lower().strip()
    # search from newest to oldest
    for idx in range(len(album_index[cid]) - 1, -1, -1):
        rec = album_index[cid][idx]
        cap = (rec.get("caption") or "").strip()
        if _norm(cap).startswith(_norm(phrase)) and rec.get("message_ids"):
            # try to delete every message in the album
            deleted_any = False
            for mid in rec["message_ids"]:
                try:
                    await ctx.bot.delete_message(chat_id=chat, message_id=mid)
                    deleted_any = True
                except Exception as e:
                    logger.exception(f"Failed to delete message {mid} in {chat}: {e}")
            # remove from index and persist
            album_index[cid].pop(idx)
            _save_config()
            return deleted_any
    return False

HISTORY_SCAN_LIMIT = 800  # a little deeper, adjust if needed

async def _delete_matching_album_fallback(ctx: ContextTypes.DEFAULT_TYPE, chat: str, phrase: str) -> bool:
    """
    Search the target channel history for the most-recent media post whose visible caption
    (first non-empty caption within its media-group, or the single item caption) starts
    with `phrase` (case-insensitive). Deletes the whole album (or the single message).
    """
    # Ensure Telethon user client is connected & authorized
    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            logger.warning("Telethon history_client not authorized; cannot fallback search.")
            return False
    except Exception as e:
        logger.exception(f"Telethon connect/authorize failed: {e}")
        return False

    # Resolve target entity
    try:
       tgt = await history_client.get_entity(_resolve_target(chat))
    except Exception as e:
        logger.exception(f"Cannot access target entity {chat}: {e}")
        return False

    # Group recent media by grouped_id; treat single media (no grouped_id) as its own "group"
    groups: dict[tuple, list] = {}
    async for m in history_client.iter_messages(tgt, limit=HISTORY_SCAN_LIMIT):
        if not (m.photo or m.video or m.document):
            continue
        gid = m.grouped_id
        key = (gid,) if gid else ("single", m.id)  # unique key for single-media
        groups.setdefault(key, []).append(m)

    logger.info(f"[fallback] scanning {len(groups)} media groups/singles in chat={chat}")

    phrase_norm = (phrase or "").strip().lower()
    if not phrase_norm or not groups:
        return False

    # Sort groups by newest first (max date in that group)
    def grp_date(kv):  # kv: (key, [msgs])
        return max(x.date for x in kv[1])
    for key, arr in sorted(groups.items(), key=grp_date, reverse=True):
        arr.sort(key=lambda x: x.date)  # chronological inside group

        # Pick the first NON-EMPTY caption across the group (many users caption the 2nd+ item)
        first_cap = _first_non_empty_caption(arr)
        if not first_cap:
            continue

        cap_norm    = _norm(first_cap)
        phrase_norm = _norm(phrase)

        # DEBUG: show what we’re evaluating (trim to avoid log spam)
        logger.info(f"[fallback] chat={chat} cap={cap_norm[:120]} … | phrase={phrase_norm}")

        if phrase_norm and phrase_norm in cap_norm:
            mids = [x.id for x in arr]
            deleted_any = False
            for mid in mids:
                try:
                    await ctx.bot.delete_message(chat_id=chat, message_id=mid)
                    deleted_any = True
                except Exception as e:
                    logger.exception(f"Fallback delete failed for {chat} mid={mid}: {e}")
            logger.info(f"[fallback] deleted={deleted_any} mids={mids} in chat={chat}")
            return deleted_any

    return False

# ─── Live forward handler ───────────────────────────
async def forward_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    # Only handle messages from the source channel
    if str(update.effective_chat.id) != SOURCE_CHAT or not target_chats:
        return

    # Handle media groups
    if msg.media_group_id:
        media_buf.setdefault(msg.media_group_id, []).append(msg)
        loop = asyncio.get_event_loop()
        loop.call_later(
            FLUSH_DELAY,
            lambda: asyncio.create_task(flush_media_group(msg.media_group_id, ctx))
        )
        return

            # Handle single media items (photo, video, document)
    if msg.photo or msg.video or msg.document:
        orig_caption = msg.caption or ""
        for chat in target_chats:
            try:
                # Compute adjusted caption
                new_cap = adjust_caption(orig_caption, chat) if orig_caption else None
                # Copy with overridden caption if applicable
                await ctx.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    caption=new_cap
                )
            except Exception:
                continue
        return

    # Handle text-only pricing posts (cart or pound) (cart or pound)
    if msg.text:
        # Only forward if text contains a price slash pattern
        if _pattern.search(msg.text):
            for chat in target_chats:
                new_txt = adjust_caption(msg.text, chat)
                try:
                    await ctx.bot.send_message(chat_id=chat, text=new_txt)
                except Exception:
                    continue
        return

    # Otherwise, skip text-only posts
    return
    if msg.photo or msg.video or msg.document:
        orig = msg.caption or ""
        for chat in target_chats:
            try:
                sent = await ctx.bot.copy_message(chat_id=chat, from_chat_id=SOURCE_CHAT, message_id=msg.message_id)
                new_cap = adjust_caption(orig, chat)
                if new_cap != orig:
                    await ctx.bot.edit_message_caption(chat_id=sent.chat_id, message_id=sent.message_id, caption=new_cap)
            except:
                continue

# ─── /post: EXACT text to all registered targets; block hyperlinks; delete-on-sold-out ───
async def post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not target_chats:
        return await update.message.reply_text("No targets registered. Use /register <chat> first.")

    # text from args or from a replied message
    text = " ".join(ctx.args).strip() if ctx.args else (
        update.message.reply_to_message.text
        if (update.message.reply_to_message and update.message.reply_to_message.text)
        else ""
    )
    if not text:
        return await update.message.reply_text("Usage: /post <text> (or reply to a text with /post)")

    # hyperlink guard
    if contains_link(text, update):
        return await update.message.reply_text("⚠️ Link detected. For safety, send this update manually to the channels.")

    # delete matching album if 'sold out' present
    phrase = _extract_phrase_before_sold_out(text)
    deleted_in = []
    if phrase:
        for chat in target_chats:
            try:
                ok = await _delete_matching_album(ctx, chat, phrase)
                if not ok:
                    ok = await _delete_matching_album_fallback(ctx, chat, phrase)
                if ok:
                    deleted_in.append(str(chat))
            except Exception as e:
                logger.exception(f"Album delete attempt failed for {chat}: {e}")

    # broadcast text to all targets
    ok, fail = 0, 0
    for chat in target_chats:
        try:
            await ctx.bot.send_message(chat_id=chat, text=text)
            ok += 1
        except Exception as e:
            fail += 1
            logger.exception(f"/post failed for {chat}: {e}")

    note = f"\n🗑 Deleted album in: {', '.join(deleted_in)}" if deleted_in else ""
    return await update.message.reply_text(f"📣 Sent to {ok} targets" + (f", {fail} failed" if fail else "") + note)

# ─── /postadj: adjusted text to all registered targets; same delete logic (links allowed) ───
async def postadj(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not target_chats:
        return await update.message.reply_text("No targets registered. Use /register <chat> first.")

    base = " ".join(ctx.args).strip() if ctx.args else (
        update.message.reply_to_message.text
        if (update.message.reply_to_message and update.message.reply_to_message.text)
        else ""
    )
    if not base:
        return await update.message.reply_text("Usage: /postadj <text> (or reply to a text with /postadj)")

    phrase = _extract_phrase_before_sold_out(base)
    deleted_in = []
    if phrase:
        for chat in target_chats:
            try:
                ok = await _delete_matching_album(ctx, chat, phrase)
                if not ok:
                    ok = await _delete_matching_album_fallback(ctx, chat, phrase)
                if ok:
                    deleted_in.append(str(chat))
            except Exception as e:
                logger.exception(f"Album delete attempt failed for {chat}: {e}")

    ok, fail = 0, 0
    for chat in target_chats:
        try:
            await ctx.bot.send_message(chat_id=chat, text=adjust_caption(base, chat))
            ok += 1
        except Exception as e:
            fail += 1
            logger.exception(f"/postadj failed for {chat}: {e}")

    note = f"\n🗑 Deleted album in: {', '.join(deleted_in)}" if deleted_in else ""
    return await update.message.reply_text(f"📣 Sent (adjusted) to {ok} targets" + (f", {fail} failed" if fail else "") + note)

async def diag_soldout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Usage: /diag_soldout <chat_id_or_username> <phrase...>")
    chat = ctx.args[0]
    phrase = " ".join(ctx.args[1:])

    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            return await update.message.reply_text("Telethon user is not authorized.")
    except Exception as e:
        return await update.message.reply_text(f"Telethon connect error: {e}")

    try:
        tgt = await history_client.get_entity(_resolve_target(chat))
    except Exception as e:
        return await update.message.reply_text(f"Cannot access target: {e}")

    groups = {}
    async for m in history_client.iter_messages(tgt, limit=200):
        if not (m.photo or m.video or m.document):
            continue
        gid = m.grouped_id
        key = (gid,) if gid else ("single", m.id)
        groups.setdefault(key, []).append(m)

    lines = [f"Diag phrase: {phrase}"]
    shown = 0
    for key, arr in sorted(groups.items(), key=lambda kv: max(x.date for x in kv[1]), reverse=True):
        arr.sort(key=lambda x: x.date)
        cap = _first_non_empty_caption(arr)
        if not cap: continue
        mark = "MATCH" if _norm(phrase) in _norm(cap) else "no"
        kind = "album" if key[0] != "single" else "single"
        lines.append(f"- {kind}: {_norm(cap)[:90]}{'...' if len(cap)>90 else ''} -> {mark}")
        shown += 1
        if shown >= 12: break

    await update.message.reply_text("\n".join(lines) if shown else "No recent media found.")

# ─── Entrypoint ─────────────────────────────────────
def main():
    keep_alive()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("forward", forward_history))
    application.add_handler(CommandHandler("increasepound", increasepound))
    application.add_handler(CommandHandler("increasecart", increasecart))
    application.add_handler(CommandHandler("post", post))
    application.add_handler(CommandHandler("postadj", postadj))
    application.add_handler(CommandHandler("targets", targets))           
    application.add_handler(MessageHandler(filters.ALL, forward_handler), group=1)
    application.add_handler(CommandHandler("diag_soldout", diag_soldout))
    logger.info("Bot up and entering polling loop.")
    application.run_polling()

if __name__ == "__main__":
    main()
