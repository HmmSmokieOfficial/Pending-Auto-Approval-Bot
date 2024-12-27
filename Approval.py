import logging
import sys
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message, ChatJoinRequest
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageDeleteForbidden, ChannelInvalid, InviteRequestSent
from pyrogram.errors import ChatAdminRequired, UserAlreadyParticipant, FloodWait
import motor.motor_asyncio
from datetime import datetime, UTC, timedelta
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

API_ID = "26490815"
API_HASH = "b99d8504b8812f9ec395ec61c010ac32"
BOT_TOKEN = "7776443697:AAE-TzaAT7l8H8UsamPR3o-k3K_0H79KGCE"
SESSION_STRING = "BQGfjM8AcnBHOyj-eKRTbttTseAGRhWmxKbljsd-IDYKyATeRWDDMAqK1IezrjwcFejDFstCZaFvxXIHL_frIXTQb-2qS3QOMHswYE7uABfZ04imELXj9i1PrmsSAEz3-2X8OYPyQav998AyGxJDRhoO8-D64WBPaFrJdjzwms7dV4kKGzZhujkhF3wh6AiqS7yXIkK5RxXxz57HomWfbQpKJ-qxam08Yoy2bdWh92qo3saMwNAPNAT60dQu8znRoUWJDA7cv94NCMz9KwA9w689-L1hYqtOceushyDN0JnhbSEhvdO738xBv1P0ZfBW0rlO6uryx-TSnXh6A_rN1wqbIJi2PAAAAAE9CjtJAA"
LOG_GROUP_ID = "-1001806351030"
# MongoDB configuration
MONGO_URL = "mongodb+srv://hmmSmokie:Saurabh0001@smokie.ibkld.mongodb.net/ApprovalDatabase?retryWrites=true&w=majority&appName=Smokie"
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = client.ApprovalDatabase
queue_collection = db.approve_queue
active_jobs_collection = db.active_jobs
user_collection = db.users
assistant_data_collection = db.assistant_data

DELETE_DELAY = 10  # Delay in seconds before deleting messages
APPROVE_DELAY = 3  # Delay in seconds between approving requests

bot = Client(
    "approvebot", 
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user = Client(
    "user_session",
    session_string=SESSION_STRING
)

async def send_log(message: str):
    """Send log message to the designated logging group"""
    try:
        await bot.send_message(LOG_GROUP_ID, message)
    except Exception as e:
        logger.error(f"Failed to send log message: {e}")

# Database utility functions
async def is_chat_authorized(chat_id):
    chat = await db.authorized_chats.find_one({"chat_id": chat_id})
    return bool(chat and chat.get("is_authorized", False))

async def authorize_chat(chat_id, chat_title, authorized_by):
    try:
        await db.authorized_chats.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "is_authorized": True,
                    "authorized_by": authorized_by,
                    "authorized_at": datetime.now(UTC)
                }
            },
            upsert=True
        )
        
        # Send log message
        auth_info = f"@{authorized_by['username']}" if authorized_by['username'] else authorized_by['full_name']
        await send_log(
            f"✅ Chat Authorized\n"
            f"📢 Chat: {chat_title}\n"
            f"🆔 Chat ID: `{chat_id}`\n"
            f"👤 Authorized By: {auth_info}"
        )
    except Exception as e:
        logger.error(f"Error in authorize_chat: {e}")


async def unauthorize_chat(chat_id):
    try:
        chat = await bot.get_chat(chat_id)
        await db.authorized_chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_authorized": False}}
        )
        
        # Send log message
        await send_log(
            f"❌ Chat Unauthorized\n"
            f"📢 Chat: {chat.title}\n"
            f"🆔 Chat ID: `{chat_id}`"
        )
    except Exception as e:
        logger.error(f"Error in unauthorize_chat: {e}")

async def log_approved_user(user_id, user_name, chat_id, chat_title, approved_by):
    """
    Log or update approved user information in the database.
    If user already exists in the chat, update their entry instead of creating a new one.
    """
    try:
        # Check if user already exists in this chat
        existing_entry = await db.approved_users.find_one({
            "user_id": user_id,
            "chat_id": chat_id
        })

        if existing_entry:
            # Update existing entry
            await db.approved_users.update_one(
                {
                    "user_id": user_id,
                    "chat_id": chat_id
                },
                {
                    "$set": {
                        "user_name": user_name,  # Update in case username changed
                        "chat_title": chat_title,  # Update in case chat title changed
                        "approved_by": approved_by,
                        "approved_at": datetime.now(UTC),
                        "approval_count": (existing_entry.get("approval_count", 1) + 1)
                    }
                }
            )
        else:
            # Create new entry
            await db.approved_users.insert_one({
                "user_id": user_id,
                "user_name": user_name,
                "chat_id": chat_id,
                "chat_title": chat_title,
                "approved_by": approved_by,
                "approved_at": datetime.now(UTC),
                "approval_count": 1
            })

    except Exception as e:
        logger.error(f"Error in log_approved_user: {str(e)}")

async def add_to_queue(chat_id: int, chat_title: str, requested_by: Optional[int] = None):
    try:
        existing_entry = await queue_collection.find_one({"chat_id": chat_id})
        current_time = datetime.now(UTC)
        
        if existing_entry and existing_entry['status'] not in ['pending', 'processing']:
            await queue_collection.update_one(
                {"chat_id": chat_id},
                {
                    "$set": {
                        "chat_title": chat_title,
                        "requested_by": requested_by,
                        "status": "pending",
                        "created_at": current_time,
                        "started_at": None,
                        "completed_at": None,
                        "error": None,
                        "approved_count": 0,
                        "skipped_count": 0
                    }
                }
            )
        elif not existing_entry:
            await queue_collection.insert_one({
                "chat_id": chat_id,
                "chat_title": chat_title,
                "requested_by": requested_by,
                "status": "pending",
                "created_at": current_time,
                "started_at": None,
                "completed_at": None,
                "error": None,
                "approved_count": 0,
                "skipped_count": 0
            })
            
        # Get queue position
        position = await get_queue_position(chat_id)
        
        # Send log message
        await send_log(
            f"📝 New Queue Entry\n"
            f"📢 Chat: {chat_title}\n"
            f"🆔 Chat ID: `{chat_id}`\n"
            f"📊 Queue Position: {position}\n"
            f"👤 Requested By: `{requested_by if requested_by else 'Channel Admin'}`"
        )
    except Exception as e:
        logger.error(f"Error adding to queue: {e}")

async def get_next_in_queue() -> Optional[dict]:
    """Get the next pending request in queue"""
    return await queue_collection.find_one(
        {
            "status": "pending",
            # Optionally add a condition to exclude entries that were recently processed
            "completed_at": None
        },
        sort=[("created_at", 1)]  # Get oldest request first
    )

async def update_queue_status(queue_id, status: str, **kwargs):
    """Update the status of a queued request"""
    update_data = {"status": status}
    update_data.update(kwargs)
    
    if status == "processing":
        update_data["started_at"] = datetime.now(UTC)
    elif status in ["completed", "failed"]:
        update_data["completed_at"] = datetime.now(UTC)
    
    await queue_collection.update_one(
        {"_id": queue_id},
        {"$set": update_data}
    )

async def is_approval_in_progress():
    """Check if there's an active approval process running"""
    active_job = await active_jobs_collection.find_one(
        {"type": "approve", "active": True}
    )
    return bool(active_job)

async def set_approval_status(is_active: bool):
    """Set the status of approval process"""
    if is_active:
        await active_jobs_collection.update_one(
            {"type": "approve"},
            {
                "$set": {
                    "active": True,
                    "last_updated": datetime.now(UTC)
                }
            },
            upsert=True
        )
    else:
        await active_jobs_collection.delete_one({"type": "approve"})

async def get_queue_position(chat_id: int) -> int:
    """Get position in queue for a specific chat"""
    try:
        # Get the current chat's entry
        current_entry = await queue_collection.find_one({"chat_id": chat_id})
        if not current_entry:
            return 0
            
        # Count only pending entries that were created before this one
        position = await queue_collection.count_documents({
            "status": "pending",
            "created_at": {"$lt": current_entry["created_at"]}
        })
        return position + 1
    except Exception as e:
        logger.error(f"Error getting queue position: {e}")
        return 1 


async def send_notification(chat_id: int, message: str, delete_after: int = None):
    """Send notification message with optional auto-delete"""
    try:
        msg = await bot.send_message(chat_id=chat_id, text=message)
        if delete_after:
            asyncio.create_task(delete_messages_with_delay(None, msg))
        return msg
    except Exception as e:
        logger.error(f"Failed to send notification to {chat_id}: {e}")
        return None

async def process_queue():
    """Process pending requests in the queue"""
    while True:
        try:
            if await is_approval_in_progress():
                await asyncio.sleep(5)
                continue

            next_request = await get_next_in_queue()
            if not next_request:
                await asyncio.sleep(5)
                continue

            # Delete queue message for the chat that's about to be processed
            await delete_queue_message(next_request["chat_id"])

            # Update queue positions for remaining chats
            async for doc in queue_collection.find({"status": "pending"}):
                if doc["chat_id"] != next_request["chat_id"]:
                    position = await get_queue_position(doc["chat_id"])
                    try:
                        # Delete previous queue message if exists
                        await delete_queue_message(doc["chat_id"])
                        
                        queue_msg = await bot.send_message(
                            chat_id=doc["chat_id"],
                            text=f"⏳ Your request is in queue\n📊 Position: {position}\nPlease wait until current process is complete."
                        )
                        await save_queue_message(doc["chat_id"], queue_msg.id)
                    except Exception as e:
                        logger.error(f"Error updating queue message: {e}")

            # Verify chat exists and is accessible before processing
            try:
                chat = await user.get_chat(next_request["chat_id"])
                if not chat:
                    raise ChannelInvalid("Channel not found")
            except Exception as e:
                logger.error(f"Chat verification failed: {e}")
                await update_queue_status(
                    next_request["_id"],
                    "failed",
                    error=f"Chat verification failed: {str(e)}"
                )
                continue

            await set_approval_status(True)
            await update_queue_status(next_request["_id"], "processing")

            try:
                # Send processing notification
                notification_msg = None
                try:
                    notification_msg = await bot.send_message(
                        chat_id=next_request["chat_id"],
                        text="🔄 Processing join requests..."
                    )
                except Exception as e:
                    logger.error(f"Failed to send processing notification: {e}")

                approved_count, skipped_count = await approve_requests_with_delay(next_request["chat_id"])
                
                # Update queue entry with results
                await update_queue_status(
                    next_request["_id"],
                    "completed",
                    approved_count=approved_count,
                    skipped_count=skipped_count
                )

                # Send completion notification
                completion_msg = f"✅ Queue processed:\n• Approved: {approved_count} request(s)"
                if skipped_count > 0:
                    completion_msg += f"\n• Skipped: {skipped_count} request(s)"

                if approved_count > 0 or skipped_count > 0:
                    await send_log(
                        f"✅ Queue Processed\n"
                        f"📢 Chat: {next_request['chat_title']}\n"
                        f"🆔 Chat ID: `{next_request['chat_id']}`\n"
                        f"👥 Approved: {approved_count}\n"
                        f"⏭️ Skipped: {skipped_count}"
                    )
                
                try:
                    if notification_msg:
                        final_msg = await notification_msg.edit_text(completion_msg)
                    else:
                        final_msg = await bot.send_message(
                            chat_id=next_request["chat_id"],
                            text=completion_msg
                        )
                    
                    asyncio.create_task(delete_messages_with_delay(None, final_msg))
                        
                except Exception as e:
                    logger.error(f"Failed to send completion notification: {e}")

            except Exception as e:
                error_str = str(e)
                error_message = get_error_message(error_str, next_request["chat_title"])
                
                await update_queue_status(
                    next_request["_id"],
                    "failed",
                    error=error_message
                )
                
                try:
                    if notification_msg:
                        error_msg = await notification_msg.edit_text(f"❌ Error: {error_message}")
                    else:
                        error_msg = await bot.send_message(
                            chat_id=next_request["chat_id"],
                            text=f"❌ Error: {error_message}"
                        )
                    
                    asyncio.create_task(delete_messages_with_delay(None, error_msg))
                        
                except Exception as notify_error:
                    logger.error(f"Failed to send error notification: {notify_error}")

            finally:
                await set_approval_status(False)

        except Exception as e:
            logger.error(f"Error in queue processing: {e}")
            await asyncio.sleep(5)

async def save_queue_message(chat_id: int, message_id: int):
    """Save the queue notification message ID for a chat"""
    await db.queue_messages.update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "message_id": message_id,
                "updated_at": datetime.now(UTC)
            }
        },
        upsert=True
    )

async def delete_queue_message(chat_id: int):
    """Delete the saved queue message for a chat"""
    queue_message = await db.queue_messages.find_one_and_delete({"chat_id": chat_id})
    if queue_message:
        try:
            await bot.delete_messages(chat_id, queue_message["message_id"])
        except Exception as e:
            logger.error(f"Error deleting queue message: {e}")

# Add these new collections after the existing database setup
user_collection = db.users
assistant_data_collection = db.assistant_data

async def store_user_data(user):
    try:
        await user_collection.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "last_active": datetime.now(UTC),
                    "is_bot": user.is_bot
                },
                "$setOnInsert": {
                    "joined_date": datetime.now(UTC)
                }
            },
            upsert=True
        )
        
        # Send log message
        user_info = f"@{user.username}" if user.username else f"{user.first_name}"
        await send_log(f"🆕 New User Started Bot\n👤 User: {user_info}\n🆔 ID: `{user.id}`")
    except Exception as e:
        logger.error(f"Error storing user data: {e}")

async def store_assistant_data(chat_id: int, chat_title: str, added_by_id: Optional[int] = None):
    try:
        assistant_info = await user.get_me()
        
        success = await assistant_data_collection.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_title": chat_title,
                    "assistant_id": assistant_info.id,
                    "assistant_username": assistant_info.username,
                    "last_updated": datetime.now(UTC),
                    "added_by": added_by_id,
                    "is_active": True
                },
                "$setOnInsert": {
                    "first_added": datetime.now(UTC)
                }
            },
            upsert=True
        )
        
        # Send log message
        await send_log(
            f"➕ Assistant Added to Chat\n"
            f"📢 Chat: {chat_title}\n"
            f"🆔 Chat ID: `{chat_id}`\n"
            f"👤 Added By: `{added_by_id if added_by_id else 'Channel Admin'}`"
        )
        return True
    except Exception as e:
        logger.error(f"Error storing assistant data: {e}")
        return False
    
async def update_assistant_status(chat_id: int, is_active: bool):
    """
    Update the active status of assistant in a chat
    """
    try:
        await assistant_data_collection.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "is_active": is_active,
                    "last_updated": datetime.now(UTC)
                }
            }
        )
    except Exception as e:
        logger.error(f"Error updating assistant status: {e}")

async def is_chat_admin(client, chat_id, user_id):
    try:
        member = await client.get_chat_member(chat_id, user_id)
        is_admin = member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        print(f"Checked admin status for user {user_id} in chat {chat_id}: {is_admin}")
        return is_admin
    except Exception as e:
        print(f"Error checking admin status for user {user_id} in chat {chat_id}: {e}")
        return False

async def delete_messages_with_delay(command_msg, response_msg):
    """Delete messages after a delay with proper error handling"""
    try:
        await asyncio.sleep(DELETE_DELAY)
        
        if command_msg:
            try:
                await command_msg.delete()
            except Exception as e:
                pass
            except Exception as e:
                # Log other unexpected errors
                if "MESSAGE_DELETE_FORBIDDEN" not in str(e):
                    logger.error(f"Unexpected error deleting command message: {e}")
        
        if response_msg:
            try:
                await response_msg.delete()
            except Exception as e:
                logger.error(f"Error deleting response message: {e}")

    except Exception as e:
        logger.error(f"Error in delete_messages_with_delay: {e}")

async def approve_requests_with_delay(chat_id):
    """Approve join requests with a delay between each approval"""
    try:
        pending_requests = []
        approved_count = 0
        skipped_count = 0
        
        try:
            # First verify if the user account can access the chat
            chat = await user.get_chat(chat_id)
            chat_title = chat.title
            
            async for request in user.get_chat_join_requests(chat_id):
                pending_requests.append(request)
                
        except ChannelInvalid:
            raise ChannelInvalid("CHANNEL_INVALID")
        except ChatAdminRequired:
            raise ChatAdminRequired("CHAT_ADMIN_REQUIRED")
        except Exception as e:
            error_str = str(e)
            if "CHANNEL_PRIVATE" in error_str:
                raise
            logger.error(f"Error getting join requests: {e}")
            raise
        
        total_requests = len(pending_requests)
        if total_requests == 0:
            return 0, 0
            
        for request in pending_requests:
            try:
                await user.approve_chat_join_request(
                    chat_id=chat_id,
                    user_id=request.user.id
                )
                
                # Log the manually approved user
                await log_approved_user(
                    user_id=request.user.id,
                    user_name=request.user.username or request.user.first_name,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    approved_by="manual_approval"
                )
                
                approved_count += 1
                if approved_count < total_requests:  # Don't delay after the last request
                    await asyncio.sleep(APPROVE_DELAY)
            except Exception as e:
                error_str = str(e)
                if "USER_CHANNELS_TOO_MUCH" in error_str:
                    skipped_count += 1
                    continue
                elif "CHANNEL_PRIVATE" in error_str:
                    raise
                else:
                    logger.error(f"Error approving request: {e}")
                    raise
                
        return approved_count, skipped_count
    except Exception as e:
        error_str = str(e)
        if not any(err in error_str for err in ["CHANNEL_PRIVATE", "CHANNEL_INVALID", "CHAT_ADMIN_REQUIRED"]):
            logger.error(f"Error in approve_requests_with_delay: {e}")
        raise
    
async def add_assistant_to_chat(chat_id):
    try:
        # Get the assistant account's info
        assistant_info = await user.get_me()
        
        try:
            # Try to get chat info using bot first
            try:
                chat = await bot.get_chat(chat_id)
            except ChannelInvalid:
                chat = await user.get_chat(chat_id)
            
            # For channels/groups, try to join using invite link
            if hasattr(chat, 'invite_link') and chat.invite_link:
                await user.join_chat(chat.invite_link)
            else:
                # Try to create and use a new invite link
                try:
                    invite_link = await bot.create_chat_invite_link(chat_id)
                    await user.join_chat(invite_link.invite_link)
                except Exception:
                    # If creating invite link fails, try joining directly
                    await user.join_chat(chat_id)
            
            # Store assistant data after successful join
            await store_assistant_data(chat_id, chat.title)
            return True, assistant_info.id, "Successfully added assistant account"
            
        except UserAlreadyParticipant:
            # Update assistant data even if already participant
            await store_assistant_data(chat_id, chat.title)
            return True, assistant_info.id, "Assistant is already in the chat"
            
        except ChatAdminRequired:
            return False, assistant_info.id, "Bot needs admin rights to add assistant"
            
        except Exception as e:
            return False, assistant_info.id, f"Error adding assistant: {str(e)}"
            
    except Exception as e:
        return False, None, f"Error getting assistant info: {str(e)}"

async def get_user_stats():
    """Get statistics about bot users"""
    try:
        total_users = await user_collection.count_documents({})
        active_today = await user_collection.count_documents({
            "last_active": {
                "$gte": datetime.now(UTC) - timedelta(days=1)
            }
        })
        return {
            "total_users": total_users,
            "active_today": active_today
        }
    except Exception as e:
        logger.error(f"Error getting user stats: {e}")
        return None

async def get_assistant_stats():
    """Get statistics about assistant usage"""
    try:
        total_chats = await assistant_data_collection.count_documents({})
        active_chats = await assistant_data_collection.count_documents({
            "is_active": True
        })
        return {
            "total_chats": total_chats,
            "active_chats": active_chats
        }
    except Exception as e:
        logger.error(f"Error getting assistant stats: {e}")
        return None
    
@bot.on_message(filters.command("start"))
async def start_command(client, message):
    # Store user data when they start the bot
    await store_user_data(message.from_user)
    
    # Get bot username for dynamic button URLs
    bot_username = (await client.get_me()).username
    
    # Rest of your existing start command code...
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add me to your channel", url=f"https://t.me/{bot_username}?startchannel=true")],
        [InlineKeyboardButton("➕ Add me to your Group", url=f"https://t.me/{bot_username}?startgroup=true")],
        [
            InlineKeyboardButton("👥 Support", url="https://t.me/SmokieOfficial"),
            InlineKeyboardButton("👨‍💻 Owner", url="https://t.me/Hmm_Smokie")
        ]
    ])
    
    description = (
        "👋 ɪ'ᴍ ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ ʏᴏᴜ ᴍᴀɴᴀɢᴇ ʏᴏᴜʀ ᴄʜᴀɴɴᴇʟ ᴀɴᴅ ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀꜱ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ.\n\n"
        "Here are some commands you can use:\n\n"
        "1️⃣ `/addassistant` - Adds an assistant account to your chat to help with join requests.\n"
        "2️⃣ `/approve` - Approves all pending join requests in the group or channel.\n"
        "3️⃣ `/auth` - Authorizes the chat for automatic approval of all join requests.\n"
        "4️⃣ `/unauth` - Removes the authorization for automatic approval of join requests.\n\n"
        "⚙️ Please ensure that the assistant account is granted admin rights to approve join requests.\n\n"
        "💬 If you need help, feel free to contact the bot admin!"
    )
    
    await message.reply_video(
        video="https://cdn.glitch.global/04a38d5f-8c30-452e-b709-33da5c74b12d/175446-853577055.mp4?v=1732257487908",
        caption=description,
        reply_markup=keyboard
    )

@bot.on_message(filters.command('addassistant'))
async def add_assistant_command(client, message):
    try:
        # For channels, skip admin check
        if message.chat.type != enums.ChatType.CHANNEL:
            if not await is_chat_admin(client, message.chat.id, message.from_user.id):
                try:
                    response_msg = await message.reply("You need to be an admin to use this command.")
                    asyncio.create_task(delete_messages_with_delay(message, response_msg))
                except ChatAdminRequired:
                    logger.warning(f"Bot lacks permission to send messages in chat {message.chat.id}")
                return

        try:
            status_message = await message.reply("Adding assistant account to the chat...")
        except ChatAdminRequired:
            logger.warning("Bot lacks admin rights to send messages")
            return
        
        # Try to add the assistant
        success, assistant_id, msg = await add_assistant_to_chat(message.chat.id)
        
        try:
            if success:
                if "already in the chat" in msg:
                    final_message = await status_message.edit(
                        "Assistant account is already in the chat.\n"
                        "⚠️ Please ensure the assistant has admin rights to approve join requests."
                    )
                else:
                    final_message = await status_message.edit(
                        "✅ Assistant account has been successfully added to the chat.\n"
                        "⚠️ Important: Please grant admin rights to the assistant account "
                        "with at least these permissions:\n"
                        "- Invite users\n"
                        "- Manage users\n\n"
                        "Once admin rights are granted, you can use /approve to handle join requests."
                    )
            else:
                # Simplified error message
                error_text = (
                    "❌ Unable to add assistant\n\n"
                    "Please make sure:\n"
                    "1️⃣ The bot is an admin in this chat\n"
                    "2️⃣ The bot has permission to invite members\n"
                    "3️⃣ Try removing and re-adding the bot as admin"
                )
                
                final_message = await status_message.edit(error_text)
            
            # Create task for auto-deletion
            asyncio.create_task(delete_messages_with_delay(message, final_message))
            
        except ChatAdminRequired:
            logger.warning("Bot lacks admin rights to edit messages")
        except Exception as e:
            logger.error(f"Error in message handling: {str(e)}")
            
    except Exception as e:
        logger.error(f"Error in add_assistant_command: {str(e)}")

@bot.on_message(filters.command('approve'))
async def approve_requests(client, message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    chat_name = message.chat.title
    processing_message = None
    
    try:
        processing_message = await message.reply("⏳ Checking request...")
        
        # Handle different chat types
        if chat_type == enums.ChatType.CHANNEL:
            # For channels, just verify bot permissions
            try:
                bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)
                if bot_member.status != enums.ChatMemberStatus.ADMINISTRATOR:
                    await processing_message.edit_text(
                        "❌ Bot needs admin rights to manage join requests!\n"
                        "Please grant admin permissions and try again."
                    )
                    asyncio.create_task(delete_messages_with_delay(message, processing_message))
                    return
            except ChatAdminRequired:
                await processing_message.edit_text("❌ Bot needs admin rights!")
                asyncio.create_task(delete_messages_with_delay(message, processing_message))
                return
                
        elif chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            # For groups and supergroups, verify both user and bot permissions
            if not await is_chat_admin(client, chat_id, message.from_user.id):
                await processing_message.edit_text("❌ You need admin rights to use this command.")
                asyncio.create_task(delete_messages_with_delay(message, processing_message))
                return
                
            try:
                bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)
                if bot_member.status != enums.ChatMemberStatus.ADMINISTRATOR:
                    await processing_message.edit_text(
                        "❌ Bot needs admin rights to manage join requests!\n"
                        "Please grant admin permissions and try again."
                    )
                    asyncio.create_task(delete_messages_with_delay(message, processing_message))
                    return
            except ChatAdminRequired:
                await processing_message.edit_text("❌ Bot needs admin rights!")
                asyncio.create_task(delete_messages_with_delay(message, processing_message))
                return

        # Verify assistant account has access to the chat
        try:
            chat = await user.get_chat(chat_id)
            if not chat:
                raise ChannelInvalid("Chat not found")
                
            # Verify assistant account's admin rights
            assistant = await user.get_chat_member(chat_id, (await user.get_me()).id)
            if assistant.status != enums.ChatMemberStatus.ADMINISTRATOR:
                await processing_message.edit_text(
                    "⚠️ Assistant Account needs admin rights!\n\n"
                    "Please ensure the Assistant Account has these permissions:\n"
                    "1️⃣ Add New Members\n"
                    "2️⃣ Manage Chat\n"
                    "3️⃣ Invite Users via Link"
                )
                asyncio.create_task(delete_messages_with_delay(message, processing_message))
                return
                
        except Exception as e:
            error_message = get_error_message(str(e), chat_name)
            await processing_message.edit_text(f"❌ Error: {error_message}")
            asyncio.create_task(delete_messages_with_delay(message, processing_message))
            return

        # Check if there's already an active approval process
        is_active = await is_approval_in_progress()
        
        # Add request to queue
        await add_to_queue(
            chat_id=chat_id,
            chat_title=chat_name,
            requested_by=message.from_user.id if message.from_user else None
        )
        
        # Get queue position
        queue_position = await get_queue_position(chat_id)
        
        # Key change: Check if there's an active process OR if not first in queue
        if is_active or queue_position > 1:
            # Delete the initial checking message
            if processing_message:
                try:
                    await processing_message.delete()
                except:
                    pass
                    
            # Send queue position message
            queue_msg = await message.reply(
                f"⏳ Your request is in queue\n"
                f"📊 Position: {queue_position}\n"
                "Please wait until current process is complete."
            )
            # Save the queue message ID for later reference
            await save_queue_message(chat_id, queue_msg.id)
        else:
            # Only first in queue and no active process
            if processing_message:
                await processing_message.edit_text("🔄 Processing will begin shortly...")
                asyncio.create_task(delete_messages_with_delay(message, processing_message))

    except Exception as e:
        logger.error(f"Error in approve command: {e}")
        error_text = "❌ An unexpected error occurred."
        try:
            error_msg = await message.reply(error_text)
            asyncio.create_task(delete_messages_with_delay(message, error_msg))
        except:
            pass

# Modified get_error_message function
def get_error_message(error_str: str, chat_name: str) -> str:
    """Get user-friendly error message based on error string"""
    if "CHANNEL_INVALID" in error_str:
        return (
            f"⚠️ Assistant Account is not in {chat_name}!\n\n"
            f"Please add the Assistant account in {chat_name} "
            "with admin privileges using /addassistant command."
        )
    elif "CHANNEL_PRIVATE" in error_str:
        return (
            f"⚠️ {chat_name} is private!\n\n"
            "Please ensure:\n"
            f"1️⃣ Assistant Account is a member of {chat_name}\n"
            "2️⃣ Assistant Account has admin privileges\n"
            "3️⃣ The group/channel is accessible to the Assistant"
        )
    elif "CHAT_ADMIN_REQUIRED" in error_str:
        return (
            "⚠️ Admin Privileges Required!\n\n"
            f"Please ensure Assistant Account has these permissions in {chat_name}:\n"
            "1️⃣ Add New Members\n"
            "2️⃣ Manage Chat\n"
            "3️⃣ Invite Users via Link\n\n"
            "Contact the group owner to grant these permissions."
        )
    elif "USER_CHANNELS_TOO_MUCH" in error_str:
        return (
            "⚠️ Some users couldn't be approved because they've joined "
            "too many channels/groups. These requests were skipped."
        )
    else:
        return f"An unexpected error occurred: {str(error_str)}"

@bot.on_message(filters.command("auth") & (filters.group | filters.channel))
async def authorize_chat_command(client, message):
    try:
        # For non-channel chats, check admin privileges
        if message.chat.type != enums.ChatType.CHANNEL:
            if not await is_chat_admin(client, message.chat.id, message.from_user.id):
                response_msg = await message.reply("You don't have this permission")
                asyncio.create_task(delete_messages_with_delay(message, response_msg))
                return

        try:
            bot_member = await bot.get_chat_member(message.chat.id, (await bot.get_me()).id)
            if bot_member.status != enums.ChatMemberStatus.ADMINISTRATOR:
                response_msg = await message.reply("Bot needs to be an admin in this chat to approve join requests!")
                asyncio.create_task(delete_messages_with_delay(message, response_msg))
                return
        except ChatAdminRequired:
            response_msg = await message.reply("Bot needs admin rights to function properly!")
            asyncio.create_task(delete_messages_with_delay(message, response_msg))
            return

        # For channels, use channel info instead of user info
        if message.chat.type == enums.ChatType.CHANNEL:
            authorized_by = {
                "user_id": None,
                "username": None,
                "full_name": "Channel Authorization",
                "auth_type": "channel"
            }
        else:
            authorized_by = {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "full_name": message.from_user.first_name,
                "auth_type": "user"
            }

        await authorize_chat(
            chat_id=message.chat.id,
            chat_title=message.chat.title,
            authorized_by=authorized_by
        )
        
        response_msg = await message.reply("This chat has been authorized for automatic approval of all join requests.")
        asyncio.create_task(delete_messages_with_delay(message, response_msg))
        
    except Exception as e:
        logger.error(f"Error in authorize_chat: {str(e)}")
        error_msg = await message.reply("An error occurred while processing your request.")
        asyncio.create_task(delete_messages_with_delay(message, error_msg))

@bot.on_message(filters.command("unauth") & (filters.group | filters.channel))
async def unauthorize_chat_command(client, message):
    try:
        if message.chat.type != enums.ChatType.CHANNEL and not await is_chat_admin(client, message.chat.id, message.from_user.id):
            try:
                response_msg = await message.reply("You don't have this permission")
                asyncio.create_task(delete_messages_with_delay(message, response_msg))
            except ChatAdminRequired:
                logger.warning(f"Bot lacks permission to send messages in chat {message.chat.id}")
            return

        chat_id = message.chat.id
        if await is_chat_authorized(chat_id):
            await unauthorize_chat(chat_id)
            try:
                response_msg = await message.reply("This chat has been unauthorized for automatic approval of join requests.")
            except ChatAdminRequired:
                logger.info(f"Chat {chat_id} unauthorized successfully but bot couldn't send confirmation message")
        else:
            try:
                response_msg = await message.reply("This chat is not authorized for automatic approval.")
            except ChatAdminRequired:
                logger.info(f"Bot couldn't send 'not authorized' message in chat {chat_id}")
            
        if 'response_msg' in locals():
            asyncio.create_task(delete_messages_with_delay(message, response_msg))
            
    except Exception as e:
        logger.error(f"Error in unauthorize_chat: {str(e)}")
        try:
            error_msg = await message.reply("An error occurred while processing your request.")
            asyncio.create_task(delete_messages_with_delay(message, error_msg))
        except ChatAdminRequired:
            logger.warning(f"Bot lacks permission to send error message in chat {message.chat.id}")

@bot.on_message(filters.command("addstring"))
async def add_session_string(client, message):
    try:
        # Check if user is bot owner (add your user ID here)
        if message.from_user.id != 1949883614:  # Replace with your user ID
            await message.reply("❌ Only bot owner can use this command.")
            return

        # Get the session string from command
        if len(message.command) != 2:
            await message.reply("❌ Usage: /addstring <session_string>")
            return

        new_session_string = message.command[1]

        # Save to database
        await db.settings.update_one(
            {"setting": "session_string"},
            {"$set": {
                "value": new_session_string,
                "updated_at": datetime.now(UTC),
                "updated_by": message.from_user.id
            }},
            upsert=True
        )

        # Recreate user client with new session
        global user
        user = Client(
            "user_session",
            session_string=new_session_string
        )
        await user.start()

        # Delete the command message for security
        try:
            await message.delete()
        except Exception:
            pass

        # Send success message
        success_msg = await message.reply("✅ Session string updated successfully!")
        await asyncio.sleep(5)
        await success_msg.delete()

    except Exception as e:
        error_msg = await message.reply(f"❌ Error updating session string: {str(e)}")
        await asyncio.sleep(5)
        await error_msg.delete()


@bot.on_chat_join_request()
async def handle_join_request(_, request):
    chat_id = request.chat.id
    user_id = request.from_user.id

    if await is_chat_authorized(chat_id):
        user_full_name = request.from_user.full_name
        chat_name = request.chat.title

        # Welcome message
        welcome_message = f"𝐇𝐞𝐲 {user_full_name} ✨\n\n" \
                          f"𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗼𝘂𝗿 𝗰𝗼𝗺𝗺𝘂𝗻𝗶𝘁𝘆 🎉\n\n" \
                          f"●︎ ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴘᴘʀᴏᴠᴇᴅ ᴛᴏ **{chat_name}**!\n\n" \
                          "ᴘʟᴇᴀꜱᴇ ᴄᴏɴꜱɪᴅᴇʀ ᴊᴏɪɴɪɴɢ ᴏᴜʀ ꜱᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ᴀꜱ ᴡᴇʟʟ."
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Join Support Channel", url="https://t.me/SmokieOfficial")]
        ])        

        gif_url = "https://cdn.glitch.global/04a38d5f-8c30-452e-b709-33da5c74b12d/175446-853577055.mp4?v=1732257487908"
        await bot.send_animation(chat_id=user_id, animation=gif_url, caption=welcome_message, reply_markup=keyboard)
        await request.approve()

        # Log the approved user
        await log_approved_user(
            user_id=user_id,
            user_name=request.from_user.username or user_full_name,
            chat_id=chat_id,
            chat_title=chat_name,
            approved_by="auto_approval"
        )

@bot.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client, message: Message):
    """Handle the broadcast command"""
    if str(message.from_user.id) != "1949883614":  # Your user ID
        await message.reply_text("⛔️ This command is only for the bot owner.")
        return

    if not message.reply_to_message:
        await message.reply_text(
            "❗️ Please reply to a message to broadcast it to all users."
        )
        return

    # Initial broadcast status message
    status_msg = await message.reply_text("🔍 Gathering user data...")

    # Get unique users from both collections
    unique_users = set()
    
    # Get users from user_collection
    async for user in user_collection.find({}, {'user_id': 1}):
        if 'user_id' in user:
            unique_users.add(user['user_id'])
    
    # Get users from approved_users collection
    async for user in db.approved_users.find({}, {'user_id': 1}):
        if 'user_id' in user:
            unique_users.add(user['user_id'])

    total_users = len(unique_users)
    await status_msg.edit_text(f"🚀 Starting broadcast to {total_users} unique users...")

    done = 0
    success = 0
    failed = 0
    blocked = 0
    deleted = 0
    invalid = 0
    failed_users = []
    
    async def broadcast_message(user_id):
        """Helper function to broadcast a message to a single user"""
        try:
            await message.reply_to_message.copy(user_id)
            return True, None
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await broadcast_message(user_id)
        except UserIsBlocked:
            return False, "blocked"
        except InputUserDeactivated:
            return False, "deactivated"
        except PeerIdInvalid:
            return False, "invalid_id"
        except Exception as e:
            return False, str(e)
    
    for user_id in unique_users:
        done += 1
        success_status, error = await broadcast_message(user_id)
        
        if success_status:
            success += 1
        else:
            failed += 1
            failed_users.append((user_id, error))
            if error == "blocked":
                blocked += 1
            elif error == "deactivated":
                deleted += 1
            elif error == "invalid_id":
                invalid += 1

        if done % 20 == 0:
            try:
                await status_msg.edit_text(
                    f"🚀 Broadcast in Progress...\n\n"
                    f"👥 Total Unique Users: {total_users}\n"
                    f"✅ Completed: {done} / {total_users}\n"
                    f"✨ Success: {success}\n"
                    f"❌ Failed: {failed}\n\n"
                    f"🚫 Blocked: {blocked}\n"
                    f"❗️ Deleted: {deleted}\n"
                    f"📛 Invalid: {invalid}"
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

    # Final broadcast status
    completion_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    
    await status_msg.edit_text(
        f"✅ Broadcast Completed!\n"
        f"Completed at: {completion_time}\n\n"
        f"👥 Total Unique Users: {total_users}\n"
        f"✨ Success: {success}\n"
        f"❌ Failed: {failed}\n\n"
        f"Success Rate: {(success/total_users)*100:.2f}%\n\n"
        f"🚫 Blocked: {blocked}\n"
        f"❗️ Deleted: {deleted}\n"
        f"📛 Invalid: {invalid}"
    )

    # Clean up invalid users from both databases
    if failed_users:
        clean_msg = await message.reply_text(
            "🧹 Cleaning databases...\n"
            "Removing blocked and deleted users."
        )
        
        # Extract user IDs from failed_users list
        invalid_user_ids = [user_id for user_id, _ in failed_users]
        
        # Delete invalid users from both collections
        delete_result1 = await user_collection.delete_many(
            {"user_id": {"$in": invalid_user_ids}}
        )
        delete_result2 = await db.approved_users.delete_many(
            {"user_id": {"$in": invalid_user_ids}}
        )
        
        total_deleted = delete_result1.deleted_count + delete_result2.deleted_count
        await clean_msg.edit_text(
            f"🧹 Databases cleaned!\n"
            f"Removed {total_deleted} invalid user entries."
        )

@bot.on_message(filters.command("users"))
async def users_status_command(client, message):
    """Handle the users command to show bot usage statistics"""
    # Check if user is authorized (bot owner)
    if str(message.from_user.id) != "1949883614":
        await message.reply_text("⛔️ This command is only for the bot owner.")
        return

    try:
        status_msg = await message.reply_text("📊 Gathering statistics...")

        # Get current time in UTC
        now = datetime.now(UTC)

        # Calculate time thresholds
        one_day_ago = now - timedelta(days=1)
        one_week_ago = now - timedelta(days=7)
        one_month_ago = now - timedelta(days=30)
        one_year_ago = now - timedelta(days=365)

        # Get activity counts for different periods
        day_active = await user_collection.count_documents({
            "last_active": {"$gte": one_day_ago}
        })
        
        week_active = await user_collection.count_documents({
            "last_active": {"$gte": one_week_ago}
        })
        
        month_active = await user_collection.count_documents({
            "last_active": {"$gte": one_month_ago}
        })
        
        year_active = await user_collection.count_documents({
            "last_active": {"$gte": one_year_ago}
        })

        # Get total users
        total_users = await user_collection.count_documents({})

        # Get active chats where bot/assistant is present
        active_chats = await assistant_data_collection.count_documents({
            "is_active": True
        })

        # Format the status message
        status_text = (
            "📊 Approval Bot Status ⇾ Report ✅\n"
            "━━━━━━━━━━━━━━━━\n"
            f"1 Day: {day_active:,} users were active\n"
            f"1 Week: {week_active:,} users were active\n"
            f"1 Month: {month_active:,} users were active\n"
            f"1 Year: {year_active:,} users were active\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Total Smart Tools Users: {total_users:,}\n"
            f"Active in Chats: {active_chats:,}"
        )

        await status_msg.edit_text(status_text)

    except Exception as e:
        logger.error(f"Error in users status command: {e}")
        await message.reply_text("❌ An error occurred while fetching user statistics.")

@bot.on_message(filters.command("broadcastgroup") & filters.private)
async def broadcast_group_handler(client, message: Message):
    if str(message.from_user.id) != "1949883614":
        await message.reply_text("⛔️ This command is only for the bot owner.")
        return

    if not message.reply_to_message:
        await message.reply_text("❗️ Please reply to a message to broadcast it to all groups.")
        return

    status_msg = await message.reply_text("🔍 Gathering group data...")

    # Get unique group IDs from both collections
    unique_groups = set()
    
    # Get groups from authorized_chats
    async for chat in db.authorized_chats.find({"chat_id": {"$lt": 0}}):
        unique_groups.add(chat['chat_id'])
    
    # Get groups from assistant_data
    async for chat in assistant_data_collection.find({"chat_id": {"$lt": 0}}):
        unique_groups.add(chat['chat_id'])

    total_groups = len(unique_groups)
    await status_msg.edit_text(f"🚀 Starting broadcast to {total_groups} groups...")

    done = 0
    success = 0
    failed = 0
    not_found = 0
    no_access = 0
    failed_groups = []
    
    async def broadcast_to_group(group_id):
        try:
            await message.reply_to_message.copy(group_id)
            return True, None
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await broadcast_to_group(group_id)
        except ChannelInvalid:
            return False, "not_found"
        except ChatAdminRequired:
            return False, "no_access"
        except Exception as e:
            return False, str(e)

    for group_id in unique_groups:
        done += 1
        success_status, error = await broadcast_to_group(group_id)
        
        if success_status:
            success += 1
        else:
            failed += 1
            failed_groups.append((group_id, error))
            if error == "not_found":
                not_found += 1
            elif error == "no_access":
                no_access += 1

        if done % 5 == 0:
            try:
                await status_msg.edit_text(
                    f"🚀 Group Broadcast Progress...\n\n"
                    f"👥 Total Groups: {total_groups}\n"
                    f"✅ Completed: {done} / {total_groups}\n"
                    f"✨ Success: {success}\n"
                    f"❌ Failed: {failed}\n\n"
                    f"🚫 Not Found: {not_found}\n"
                    f"⛔️ No Access: {no_access}"
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

    completion_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    await status_msg.edit_text(
        f"✅ Group Broadcast Completed!\n"
        f"Completed at: {completion_time}\n\n"
        f"👥 Total Groups: {total_groups}\n"
        f"✨ Success: {success}\n"
        f"❌ Failed: {failed}\n\n"
        f"Success Rate: {(success/total_groups)*100:.2f}%\n\n"
        f"🚫 Not Found: {not_found}\n"
        f"⛔️ No Access: {no_access}"
    )

    if failed_groups:
        clean_msg = await message.reply_text("🧹 Cleaning databases...")
        invalid_group_ids = [group_id for group_id, _ in failed_groups]
        
        delete_result1 = await db.authorized_chats.delete_many(
            {"chat_id": {"$in": invalid_group_ids}}
        )
        delete_result2 = await assistant_data_collection.delete_many(
            {"chat_id": {"$in": invalid_group_ids}}
        )
        
        total_deleted = delete_result1.deleted_count + delete_result2.deleted_count
        await clean_msg.edit_text(
            f"🧹 Databases cleaned!\n"
            f"Removed {total_deleted} invalid group entries."
        )

if __name__ == "__main__":
    logger.info("Starting bot...")
    
    async def start_bot():
        try:
            await user.start()
            logger.info("User client started successfully")
            
            # Create the queue processor task
            queue_processor = asyncio.create_task(process_queue())
            
            # Start the bot
            await bot.start()
            
            # Keep the bot running
            await asyncio.gather(queue_processor)
            
        except Exception as e:
            logger.critical(f"Failed to start bot: {str(e)}")
            sys.exit(1)
    
    # Create and run the event loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        # Clean up
        loop.run_until_complete(asyncio.gather(
            user.stop(),
            bot.stop()
        ))
        loop.close()
