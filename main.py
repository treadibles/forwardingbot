import os
import asyncio
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, InputMediaPhoto, InputMediaDocument
from telethon.errors import SessionPasswordNeededError
import logging

# Try to load dotenv if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("python-dotenv not installed. Reading from system environment variables.")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration from .env
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
SOURCE_CHANNEL = os.getenv('SOURCE_CHANNEL')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')

# Storage for registered target channels
target_channels = set()
target_channels_file = 'target_channels.txt'

# Session directory (for Docker volumes)
session_dir = os.getenv('TELETHON_SESSION_DIR', '.')
user_session_path = os.path.join(session_dir, 'user_session')
bot_session_path = os.path.join(session_dir, 'bot_session')

# Initialize clients
user_client = TelegramClient(user_session_path, API_ID, API_HASH)
bot_client = TelegramClient(bot_session_path, API_ID, API_HASH)

async def load_target_channels():
    """Load target channels from file"""
    try:
        if os.path.exists(target_channels_file):
            with open(target_channels_file, 'r') as f:
                for line in f:
                    channel = line.strip()
                    if channel:
                        target_channels.add(channel)
            logger.info(f"Loaded {len(target_channels)} target channels")
    except Exception as e:
        logger.error(f"Error loading target channels: {e}")

async def save_target_channels():
    """Save target channels to file"""
    try:
        with open(target_channels_file, 'w') as f:
            for channel in target_channels:
                f.write(f"{channel}\n")
        logger.info("Target channels saved")
    except Exception as e:
        logger.error(f"Error saving target channels: {e}")

async def start_user_client():
    """Start the user client for forwarding"""
    await user_client.start(phone=PHONE_NUMBER)
    logger.info("User client started successfully")

async def start_bot_client():
    """Start the bot client for commands"""
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started successfully")

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Handle /start command"""
    await event.reply(
        "Welcome to the Channel Forwarding Bot!\n\n"
        "Commands:\n"
        "/register <channel_id> - Register a target channel\n"
        "/unregister <channel_id> - Unregister a target channel\n"
        "/list - List all registered target channels\n"
        "/help - Show this help message\n\n"
        "Channel ID can be:\n"
        "- Username (e.g., @channel_name)\n"
        "- Numeric ID (e.g., -1001234567890)"
    )

@bot_client.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    """Handle /help command"""
    await start_command(event)

@bot_client.on(events.NewMessage(pattern='/register'))
async def register_command(event):
    """Handle /register command"""
    try:
        # Extract channel ID from command
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.reply("Usage: /register <channel_id>")
            return
        
        channel_id = parts[1]
        
        # Verify the channel exists and bot has access
        try:
            # Try to get channel entity using user client
            channel = await user_client.get_entity(channel_id)
            
            # Add to target channels
            target_channels.add(channel_id)
            await save_target_channels()
            
            await event.reply(f"✅ Successfully registered channel: {channel_id}")
            logger.info(f"Registered new target channel: {channel_id}")
            
        except Exception as e:
            await event.reply(f"❌ Error: Could not access channel {channel_id}. Make sure the bot has access to it.")
            logger.error(f"Error registering channel {channel_id}: {e}")
            
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        logger.error(f"Error in register command: {e}")

@bot_client.on(events.NewMessage(pattern='/unregister'))
async def unregister_command(event):
    """Handle /unregister command"""
    try:
        parts = event.message.text.split()
        if len(parts) < 2:
            await event.reply("Usage: /unregister <channel_id>")
            return
        
        channel_id = parts[1]
        
        if channel_id in target_channels:
            target_channels.remove(channel_id)
            await save_target_channels()
            await event.reply(f"✅ Successfully unregistered channel: {channel_id}")
            logger.info(f"Unregistered target channel: {channel_id}")
        else:
            await event.reply(f"❌ Channel {channel_id} is not registered")
            
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")
        logger.error(f"Error in unregister command: {e}")

@bot_client.on(events.NewMessage(pattern='/list'))
async def list_command(event):
    """Handle /list command"""
    if target_channels:
        channels_list = "\n".join(f"• {channel}" for channel in target_channels)
        await event.reply(f"📋 Registered target channels:\n\n{channels_list}")
    else:
        await event.reply("No target channels registered yet. Use /register to add channels.")

@user_client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def forward_message(event):
    """Forward messages from source channel to all target channels"""
    if not target_channels:
        logger.warning("No target channels registered")
        return
    
    message = event.message
    
    # Forward to each target channel
    for target in target_channels:
        try:
            # Check if it's a grouped media message
            if message.grouped_id:
                # Handle grouped media
                album_messages = []
                
                # Get all messages in the group
                async for msg in user_client.iter_messages(
                    SOURCE_CHANNEL, 
                    limit=10,
                    min_id=message.id - 10,
                    max_id=message.id + 10
                ):
                    if msg.grouped_id == message.grouped_id:
                        album_messages.append(msg)
                
                # Sort by ID to maintain order
                album_messages.sort(key=lambda x: x.id)
                
                # Prepare media list
                media_list = []
                caption = None
                
                for msg in album_messages:
                    if msg.media:
                        if isinstance(msg.media, MessageMediaPhoto):
                            media = InputMediaPhoto(
                                id=msg.media.photo.id,
                                access_hash=msg.media.photo.access_hash,
                                file_reference=msg.media.photo.file_reference
                            )
                        elif isinstance(msg.media, MessageMediaDocument):
                            media = InputMediaDocument(
                                id=msg.media.document.id,
                                access_hash=msg.media.document.access_hash,
                                file_reference=msg.media.document.file_reference
                            )
                        else:
                            continue
                        
                        media_list.append(media)
                        
                        # Use the first available caption
                        if msg.text and not caption:
                            caption = msg.text
                
                # Send album to target channel
                if media_list:
                    await user_client.send_file(
                        target,
                        media_list,
                        caption=caption
                    )
                    logger.info(f"Forwarded album to {target}")
            
            else:
                # Handle single message (text or media)
                if message.media:
                    # Forward media with caption
                    await user_client.send_file(
                        target,
                        message.media,
                        caption=message.text
                    )
                    logger.info(f"Forwarded media message to {target}")
                else:
                    # Forward text only
                    await user_client.send_message(
                        target,
                        message.text,
                        formatting_entities=message.entities
                    )
                    logger.info(f"Forwarded text message to {target}")
                    
        except Exception as e:
            logger.error(f"Error forwarding to {target}: {e}")

async def main():
    """Main function to run both clients"""
    # Load saved target channels
    await load_target_channels()
    
    # Start both clients
    await start_user_client()
    await start_bot_client()
    
    logger.info(f"Bot is running. Monitoring source channel: {SOURCE_CHANNEL}")
    logger.info(f"Registered target channels: {len(target_channels)}")
    
    # Keep the clients running
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())