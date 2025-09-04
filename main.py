import os
import re
import json
import asyncio
import threading
import logging
import string
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
from telethon.sessions import StringSession

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
    inc_pound    = _config.get("inc_pound", {})
    inc_cart     = _config.get("inc_cart", {})
    text_targets = _config.get("text_targets", [])
    album_index  = _config.get("album_index", {})
except:
    _config = {}

def _save_config():
    _config["target_chats"] = target_chats
    _config["inc_pound"]    = inc_pound
    _config["inc_cart"]     = inc_cart
    _config["text_targets"] = text_targets
    _config["album_index"]  = album_index
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)

# ─── Constants and regex ───────────────────────────
THRESHOLD = 200
_pattern = re.compile(r"(\$?)(\d+(?:\.\d+)?)(?=/\s*(?:[Pp]\s*for|[Ee][Aa]))", re.IGNORECASE)
# matches: "TAKE FOR 500", "take for 500", "Take   for   500", optional "$"
_pattern_takefor = re.compile(r'(?i)(\btake\s*for\s*)(\$?)(\d+(?:\.\d+)?)\b')

import unicodedata
_WS = re.compile(r"\s+")

def _chatid(x):
    """int for numeric ids (e.g. '-100…'), else untouched (e.g. '@publicname')."""
    s = str(x).strip()
    return int(s) if s.lstrip("-").isdigit() else s

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = _WS.sub(" ", s).strip()
    # strip common trailing punctuation like . , ! ? :
    return s.strip(string.punctuation + " ")

def _first_non_empty_caption(msgs):
    """Albums often store the caption on a later item; pick the first non-empty."""
    for m in msgs:
        cap = (getattr(m, "message", None) or getattr(m, "caption", None) or "").strip()
        if cap:
            return cap
    return ""

def _hard_reason(exc: Exception) -> str:
    """Return a short reason string for hard/perm errors from Bot API."""
    s = str(exc).lower()
    if "bot was kicked" in s:
        return "bot_kicked"
    if "chat_restricted" in s or "not enough rights" in s or "can't be deleted" in s:
        return "bot_no_delete"
    if "chat not found" in s or "channel_private" in s:
        return "bot_invisible"
    if "forbidden" in s and "send" in s:
        return "bot_forbidden_send"
    return ""

SOURCE_CHAT_ID = _chatid(SOURCE_CHAT)

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

# ─── Caption adjustment utility ────────────────────
def adjust_caption(text: str, chat: str) -> str:
    # Adjust things like "$30/ea" or "975/P for 20"
    def repl_slashprice(m):
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

    # Adjust things like "TAKE FOR 500" (case-insensitive)
    def repl_takefor(m):
        lead, prefix, orig = m.group(1), m.group(2), m.group(3)
        val = float(orig)
        inc = inc_pound.get(chat, THRESHOLD) if val > THRESHOLD else inc_cart.get(chat, 15)
        new_val = val + inc
        if '.' in orig:
            dec_len = len(orig.split('.')[-1])
            new = f"{new_val:.{dec_len}f}"
        else:
            new = str(int(new_val))
        # keep original "take for" casing/spaces (lead) and any "$" (prefix)
        return f"{lead}{prefix}{new}"

    # Run both substitutions
    out = _pattern.sub(repl_slashprice, text)
    out = _pattern_takefor.sub(repl_takefor, out)
    return out

def _add_album_record(chat: str, caption: str, message_ids: list[int]):
    cid = str(chat)
    album_index.setdefault(cid, [])
    album_index[cid].append({"caption": caption or "", "message_ids": message_ids})
    # keep only the last N records per channel (e.g., 500)
    if len(album_index[cid]) > 500:
        album_index[cid] = album_index[cid][-500:]
    _save_config()

def _extract_phrase_before_sold_out(text: str) -> str:
    i = text.lower().find("sold out")
    if i == -1:
        return ""
    return text[:i].strip()

async def _delete_matching_album(ctx: ContextTypes.DEFAULT_TYPE, chat: str, phrase: str) -> bool:
    """
    Use our local index to find the most-recent album whose caption starts with `phrase`.
    Delete all messages in that album via the bot and remove from index.
    """
    cid = str(chat)
    if cid not in album_index or not album_index[cid]:
        return False

    phrase_norm = phrase.lower().strip()
    for idx in range(len(album_index[cid]) - 1, -1, -1):
        rec = album_index[cid][idx]
        cap = (rec.get("caption") or "").strip()
        if _norm(phrase) in _norm(cap) and rec.get("message_ids"):            
            deleted_any = False
            for mid in rec["message_ids"]:
                try:
                    await ctx.bot.delete_message(chat_id=_chatid(chat), message_id=mid)
                    deleted_any = True
                except Exception as e:
                    logger.exception(f"Index delete failed for {chat} mid={mid}: {e}")
            album_index[cid].pop(idx)
            _save_config()
            if deleted_any:
                logger.info(f"Indexed delete OK in {chat}: {rec['message_ids']}")
            return deleted_any
    return False

HISTORY_SCAN_LIMIT = 800  # recent messages per target to search in fallback

async def _delete_matching_album_fallback(ctx: ContextTypes.DEFAULT_TYPE, chat: str, phrase: str) -> bool:
    """
    If index didn’t find an album, scan target channel history (Telethon user client)
    for the most-recent album whose first caption starts with `phrase` (case-insensitive).
    """
    # Ensure Telethon user client is ready
    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            logger.warning("Telethon history_client not authorized; cannot fallback search.")
            return False
    except Exception as e:
        logger.exception(f"Telethon connect/authorize failed: {e}")
        return False

    # Resolve entity
    try:
        tgt = await _get_entity_resolving_channels(chat)
    except Exception as e:
        logger.exception(f"Cannot resolve target entity {chat}: {e}")
        return False

    # Collect recent media albums
    groups = {}  # grouped_id -> [Message,...]
    async for m in history_client.iter_messages(tgt, limit=HISTORY_SCAN_LIMIT):
        if not (m.photo or m.video or m.document):
            continue
        gid = m.grouped_id or None
        if gid is None:
            continue  # not an album
        groups.setdefault(gid, []).append(m)

    phrase_norm = (phrase or "").strip().lower()
    if not phrase_norm or not groups:
        return False

    # Newest groups first; caption check on first item
    for gid, arr in sorted(groups.items(), key=lambda kv: max(x.date for x in kv[1]), reverse=True):
        arr.sort(key=lambda x: x.date)
        first_cap = _first_non_empty_caption(arr)
        if _norm(phrase) in _norm(first_cap):
            mids = [x.id for x in arr]
            deleted_any = False
            bot_failed = []

            # 1) Try Bot API first (fast path)
            for mid in mids:
                try:
                    await ctx.bot.delete_message(chat_id=_chatid(chat), message_id=mid)
                    deleted_any = True
                except Exception as e:
                    # Keep Bot API error for visibility; collect for Telethon fallback
                    logger.exception(f"Bot delete failed for {chat} mid={mid}: {e}")
                    bot_failed.append(mid)

            # 2) If Bot API refused, try Telethon (user account) in one shot
            if bot_failed:
                try:
                    tgt = await _get_entity_resolving_channels(chat)  # already resolved above; reuse if you kept it
                    await history_client.delete_messages(tgt, bot_failed, revoke=True)
                    deleted_any = True
                    logger.info(f"Telethon delete OK in {chat}: {bot_failed}")
                except Exception as e:
                    logger.exception(f"Telethon delete failed in {chat} mids={bot_failed}: {e}")

            return deleted_any
    return False

# ─── Robust resolver for channels (handles -100... ids) ───────────────────────────
async def _get_entity_resolving_channels(chat: str | int):
    """
    Resolve a target or source for Telethon.
    Works for:
      - BotAPI-style ids like -1001234567890
      - @usernames
    Requires that the Telethon user account is a member for private channels.
    """
    s = str(chat).strip()

    # Username or link -> resolve directly
    if not s.lstrip("-").isdigit():
        return await history_client.get_entity(s)

    # Numeric ids
    if s.startswith("-100") and s[4:].isdigit():
        abs_id = int(s[4:])  # Telethon's internal positive id
        try:
            return await history_client.get_entity(abs_id)
        except Exception:
            pass
        # try dialogs
        try:
            async for d in history_client.iter_dialogs(limit=2000):
                ent = getattr(d, "entity", None)
                if ent is not None and getattr(ent, "id", None) == abs_id:
                    return ent
        except Exception:
            pass
        # last resort
        try:
            return await history_client.get_entity(s)
        except Exception:
            return await history_client.get_entity(abs_id)

    # other numeric ids (basic groups/users)
    return await history_client.get_entity(int(s))

# ─── /targets: show currently registered forwarding targets ────────
async def targets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not target_chats:
        return await update.message.reply_text("Targets: (none)")
    await update.message.reply_text("Targets:\n" + "\n".join(map(str, target_chats)))

# ─── /register handler ─────────────────────────────
async def register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /register <chat_id_or_username>")
    chat = _chatid(ctx.args[0])
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

from datetime import datetime

async def prunetargets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Scan all registered targets and prune ones that are clearly unusable:
      - Bot kicked / restricted / no delete rights (hard errors)
      - Telethon user cannot resolve (not a member / private)
    Usage:
      /prunetargets          -> dry run (reports only)
      /prunetargets apply    -> actually remove bad targets and save config
    """
    apply = (len(ctx.args) >= 1 and ctx.args[0].lower() == "apply")
    removed = []
    report = []
    keep = []

    # Ensure Telethon is ready for visibility checks
    tele_ok = True
    try:
        if not history_client.is_connected():
            await history_client.connect()
        if not await history_client.is_user_authorized():
            tele_ok = False
    except Exception as e:
        tele_ok = False
        logger.exception(f"Telethon connect error in prunetargets: {e}")

    # We’ll check bot perms by trying to fetch the bot's member record
    # (Does not send any messages)
    bot_id = None
    try:
        me = await ctx.bot.get_me()
        bot_id = me.id
    except Exception as e:
        logger.exception(f"get_me failed: {e}")

    # Work on a copy so we can safely mutate lists/dicts if apply=True
    for chat in list(target_chats):
        reason = []
        # 1) Bot-side checks (admin/delete ability / presence)
        try:
            if bot_id is not None:
                cm = await ctx.bot.get_chat_member(_chatid(chat), bot_id)
                status = getattr(cm, "status", None)
                # PTB v20+: ChatMemberAdministrator(.privileges.can_delete_messages)
                can_del = getattr(getattr(cm, "privileges", None), "can_delete_messages", None)
                # PTB v13 style fallback:
                if can_del is None:
                    can_del = getattr(cm, "can_delete_messages", None)
                if status not in ("administrator", "creator"):
                    reason.append(f"bot_status={status}")
                elif not bool(can_del):
                    reason.append("bot_no_delete")
        except Exception as e:
            hr = _hard_reason(e) or f"bot_error={type(e).__name__}"
            reason.append(hr)

        # 2) Telethon visibility (only if Telethon is authorized)
        if tele_ok:
            try:
                ent = await _get_entity_resolving_channels(chat)
                # Quick visibility probe (cheap): try to iterate 1 message
                got_one = False
                async for _m in history_client.iter_messages(ent, limit=1):
                    got_one = True
                    break
                if not got_one:
                    reason.append("telethon_no_history")
            except Exception as e:
                reason.append("telethon_invisible")

        # Decide keep/prune:
        if any(r in ("bot_kicked", "bot_invisible") for r in reason) \
           or ("bot_no_delete" in reason and "telethon_invisible" in reason):
            # definitely unusable
            if apply:
                # remove from target_chats
                target_chats[:] = [c for c in target_chats if c != chat]
                # drop per-chat configs if present
                inc_pound.pop(chat, None)
                inc_cart.pop(chat, None)
                album_index.pop(str(chat), None)
                _save_config()
                removed.append(f"{chat}  [{', '.join(reason)}]")
            else:
                removed.append(f"{chat}  [{', '.join(reason)}]")
        else:
            keep.append(f"{chat}  [{', '.join(reason) if reason else 'ok'}]")

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f"🧹 Prune report @ {ts}\nMode: {'APPLY' if apply else 'DRY-RUN'}"
    lines = [header, "", "Will remove:" if not apply else "Removed:"]
    lines += (removed or ["(none)"])
    lines += ["", "Kept:"]
    lines += (keep or ["(none)"])
    return await update.message.reply_text("\n".join(lines))

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
            await ctx.bot.send_message(chat_id=_chatid(chat), text=text)
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
            await ctx.bot.send_message(chat_id=_chatid(chat), text=adjust_caption(base, chat))
            ok += 1
        except Exception as e:
            fail += 1
            logger.exception(f"/postadj failed for {chat}: {e}")

    note = f"\n🗑 Deleted album in: {', '.join(deleted_in)}" if deleted_in else ""
    return await update.message.reply_text(f"📣 Sent (adjusted) to {ok} targets" + (f", {fail} failed" if fail else "") + note)

# ─── Initialize persistent Telethon user client for history ────
# Requires a pre-generated string session in the .env (e.g. via Telethon’s session.export())
SESSION_STRING = os.getenv("SESSION_STRING")
if not SESSION_STRING:
    raise RuntimeError("SESSION_STRING not set in .env. Please generate a Telethon string session.")
history_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── /forward handler (history) ─────────────────────────────────
import tempfile

async def forward_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Forward all historical media posts (skipping text-only) from the source into the specified target channel,
    grouping albums/media-groups correctly, applying per-channel pound/cart increments.
    """
    # Validate arguments
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /forward <chat_id_or_username>")
    chat = _chatid(ctx.args[0])
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

    # Fetch source channel entity (robust resolver)
    try:
        src = await _get_entity_resolving_channels(SOURCE_CHAT)
    except Exception:
        return await notify.edit_text("❌ Cannot access source channel: Telethon user cannot resolve it.")

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
            orig_cap = _first_non_empty_caption(group) or ''
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
                sent = await ctx.bot.send_media_group(chat_id=_chatid(chat), media=media)
                count += len(sent)
                msg_ids = [m.message_id for m in sent]
                _add_album_record(chat, new_cap or "", msg_ids)
            except Exception as e:
                logger.exception(f"/forward_history album send failed for {chat}: {e}")

        else:
            # Single media message: native forward
            m = group[0]
            try:
                sent = await ctx.bot.copy_message(chat_id=_chatid(chat), from_chat_id=SOURCE_CHAT_ID, message_id=m.id)
                orig_cap = m.caption or m.message or ''
                new_cap = adjust_caption(orig_cap, chat) if orig_cap else None
                if new_cap and new_cap != orig_cap:
                    await ctx.bot.edit_message_caption(chat_id=_chatid(sent.chat_id), message_id=sent.message_id, caption=new_cap)
                count += 1
            except Exception as e:
                logger.exception(f"/forward_history single send failed for {chat}: {e}")


    # Cleanup temporary files
    try:
        for f in os.listdir(temp_dir):
            os.remove(os.path.join(temp_dir, f))
        os.rmdir(temp_dir)
    except Exception as e:
        logger.exception(f"history temp cleanup failed: {e}")

    # One final status message
    await notify.edit_text(f"✅ History forwarded: {count} media items to {chat}.")
    
# buffer for live media-groups
media_buf = {}
FLUSH_DELAY = 1.0

async def flush_media_group(gid: str, ctx: ContextTypes.DEFAULT_TYPE):
    msgs = media_buf.pop(gid, [])
    if not msgs:
        return
    msgs.sort(key=lambda m: m.message_id)
    orig = _first_non_empty_caption(msgs)
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
            sent = await ctx.bot.send_media_group(chat_id=_chatid(chat), media=media)
            msg_ids = [m.message_id for m in sent]
            _add_album_record(chat, new_cap or "", msg_ids)
        except Exception as e:
            logger.exception(f"flush_media_group failed for {chat}: {e}")
            continue

# ─── Live forward handler ───────────────────────────
async def forward_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    # Only handle messages from the source channel
    if isinstance(SOURCE_CHAT_ID, int):
        if update.effective_chat.id != SOURCE_CHAT_ID or not target_chats:
            return
    else:
        # SOURCE_CHAT_ID is like '@name'
        if (not update.effective_chat.username) or update.effective_chat.username.lower() != str(SOURCE_CHAT_ID).lstrip('@').lower() or not target_chats:
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
                    chat_id=_chatid(chat),
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    caption=new_cap
                )
            except Exception as e:
                logger.exception(f"forward_handler copy_message failed for {chat}: {e}")
                continue

        return

    # Handle text-only pricing posts (cart or pound) (cart or pound)
    if msg.text:
        # Only forward if text contains a price slash pattern
        if _pattern.search(msg.text):
            for chat in target_chats:
                new_txt = adjust_caption(msg.text, chat)
                try:
                    await ctx.bot.send_message(chat_id=_chatid(chat), text=new_txt)
                except Exception as e:
                    logger.exception(f"forward_handler send_message failed for {chat}: {e}")
                    continue
        return


# ─── Entrypoint ─────────────────────────────────────
def main():
    keep_alive()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("forward", forward_history))
    application.add_handler(CommandHandler("increasepound", increasepound))
    application.add_handler(CommandHandler("increasecart", increasecart))
    application.add_handler(CommandHandler("targets", targets))        
    application.add_handler(CommandHandler("prunetargets", prunetargets))
    application.add_handler(CommandHandler("post", post))
    application.add_handler(CommandHandler("postadj", postadj))
    application.add_handler(MessageHandler(filters.ALL, forward_handler), group=1)
    logger.info("Bot up and entering polling loop.")
    application.run_polling()

if __name__ == "__main__":
    main()
