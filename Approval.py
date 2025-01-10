import logging
import os
import traceback
from datetime import datetime, timedelta
from pyrogram import Client, filters, enums
from pyrogram.types import Message, ChatJoinRequest
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import (
    ChatAdminRequired,
    UserNotParticipant,
    ChannelPrivate,
    ChatWriteForbidden,
    FloodWait,
    RPCError
)
import asyncio
from functools import wraps
from pymongo import MongoClient
from datetime import datetime, timezone 
from enum import Enum
from typing import Tuple, List, Dict
from pyrogram.errors import (
    InputUserDeactivated,
    UserIsBlocked,
    PeerIdInvalid,
    UserDeactivated,
    FloodWait
)

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID"))
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client.ApproveBot
chats_collection = db.authorized_chats
assistant_collection = db.assistant_data
users_collection = db.users
approved_users_collection = db.approved_users
queue_collection = db.approval_queue

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'bot_logs_{datetime.now().strftime("%Y%m%d")}.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

user = None

def get_current_session_string():
    """Get the current session string from MongoDB or environment variable"""
    session_doc = assistant_collection.find_one({"type": "current_session"})
    return session_doc.get("session_string") if session_doc else SESSION_STRING

# Initialize clients with error handling
try:
    bot = Client(
        "approvebot", 
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )
    current_session = get_current_session_string()
    # Now initialize the global user variable
    user = Client(
        "user_session",
        session_string=current_session
    )
    logger.info("Clients initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize clients: {str(e)}")
    raise

@bot.on_message(filters.command('changestring') & filters.private)
async def change_string_session(client: Client, message: Message):
    """Handle changing the assistant's session string"""
    try:
        # Check if user is authorized (bot owner)
        if message.from_user.id != 1949883614:  # Your user ID
            await message.reply_text("⛔️ This command is only for the bot owner.")
            return

        # Check command format
        args = message.text.split(None, 1)
        if len(args) != 2:
            await message.reply_text(
                "❗️ Please provide the new session string.\n\n"
                "Usage: /changestring <new_session_string>"
            )
            return

        new_session = args[1].strip()

        # Delete the command message for security
        try:
            await message.delete()
        except Exception as e:
            logger.warning(f"Could not delete session string message: {e}")

        status_msg = await message.reply_text("🔄 Changing assistant session...")

        try:
            global user  # Now the global declaration is before any use
            # Stop the current user client
            await user.stop()

            # Create new client with new session
            user = Client(
                "user_session",
                session_string=new_session
            )

            # Test the new session
            await user.start()
            assistant_info = await user.get_me()

            # Update success message
            await status_msg.edit_text(
                f"✅ Successfully changed assistant account!\n\n"
                f"New Assistant Details:\n"
                f"• Username: @{assistant_info.username}\n"
                f"• ID: `{assistant_info.id}`\n\n"
                "ℹ️ The bot will now use this account for all assistant operations."
            )

            # Log the change
            logger.info(f"Assistant session changed to account: {assistant_info.id}")
            
            # Store new session info in MongoDB for persistence
            assistant_collection.update_one(
                {"type": "current_session"},
                {
                    "$set": {
                        "session_string": new_session,
                        "assistant_id": assistant_info.id,
                        "assistant_username": assistant_info.username,
                        "updated_at": datetime.now(timezone.utc)
                    }
                },
                upsert=True
            )

        except Exception as e:
            # If anything goes wrong, try to restore the old session
            logger.error(f"Error changing session: {e}")
            try:
                if user:
                    await user.stop()
                user = Client(
                    "user_session",
                    session_string=SESSION_STRING
                )
                await user.start()
            except Exception as restore_error:
                logger.error(f"Error restoring original session: {restore_error}")

            await status_msg.edit_text(
                f"❌ Failed to change assistant session: {str(e)}\n"
                "The previous session has been restored."
            )

    except Exception as e:
        logger.error(f"Error in change_string_session: {str(e)}")
        await message.reply_text("❌ An unexpected error occurred while changing the session.")

class QueueStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class QueueManager:
    def __init__(self):
        self.processing_lock = asyncio.Lock()
        
    async def add_to_queue(self, chat_id: int, admin_id: int, num_requests: int = None):
        """Add an approval request to the queue"""
        try:
            # Get current queue position
            queue_position = queue_collection.count_documents({
                "status": {"$in": [QueueStatus.PENDING.value, QueueStatus.PROCESSING.value]}
            }) + 1
            
            # Create queue item document
            queue_item = {
                "chat_id": chat_id,
                "admin_id": admin_id,
                "num_requests": num_requests,
                "status": QueueStatus.PENDING.value,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "queue_position": queue_position,
                "attempts": 0,
                "error_message": None
            }
            
            # Insert into collection
            result = queue_collection.insert_one(queue_item)
            logger.info(f"Added request to queue: Position {queue_position}, ID {result.inserted_id}")
            
            return queue_position, result.inserted_id
            
        except Exception as e:
            logger.error(f"Error adding to queue: {str(e)}")
            raise

    async def get_next_pending_request(self):
        """Get the next pending request from the queue"""
        return queue_collection.find_one_and_update(
            {
                "status": QueueStatus.PENDING.value,
                "attempts": {"$lt": 3}  # Limit retry attempts
            },
            {
                "$set": {
                    "status": QueueStatus.PROCESSING.value,
                    "updated_at": datetime.now(timezone.utc)
                },
                "$inc": {"attempts": 1}  # Correctly increment attempts
            },
            sort=[("queue_position", 1)]
        )

    async def update_request_status(self, request_id, status: QueueStatus, error_message=None):
        """Update the status of a queued request"""
        update_data = {
            "status": status.value,
            "updated_at": datetime.now(timezone.utc)
        }
        if error_message:
            update_data["error_message"] = error_message
            
        queue_collection.update_one(
            {"_id": request_id},
            {"$set": update_data}
        )

    async def get_queue_position(self, request_id):
        """Get current position in queue for a request"""
        request = queue_collection.find_one({"_id": request_id})
        if not request:
            return None
            
        ahead_in_queue = queue_collection.count_documents({
            "status": {"$in": [QueueStatus.PENDING.value, QueueStatus.PROCESSING.value]},
            "queue_position": {"$lt": request["queue_position"]}
        })
        return ahead_in_queue + 1

    async def process_queue(self):
        """Process pending requests in the queue"""
        while True:
            async with self.processing_lock:
                try:
                    request = await self.get_next_pending_request()
                    if not request:
                        await asyncio.sleep(10)  # Wait before checking again
                        continue

                    # First check if assistant is admin
                    try:
                        assistant_info = await user.get_me()
                        assistant_member = await user.get_chat_member(request["chat_id"], assistant_info.id)
                        
                        if not assistant_member.privileges or not assistant_member.privileges.can_invite_users:
                            error_msg = (
                                "Assistant requires admin rights with 'Invite Users' permission.\n"
                                "Please add the assistant as admin first using /addassistant command."
                            )
                            await bot.send_message(chat_id=request["admin_id"], text=error_msg)
                            await self.update_request_status(
                                request["_id"], 
                                QueueStatus.FAILED,
                                error_message="Assistant lacks required admin permissions"
                            )
                            continue

                    except UserNotParticipant:
                        error_msg = (
                            "Assistant is not a member of this chat.\n"
                            "Please add the assistant first using /addassistant command."
                        )
                        await bot.send_message(chat_id=request["admin_id"], text=error_msg)
                        await self.update_request_status(
                            request["_id"],
                            QueueStatus.FAILED,
                            error_message="Assistant not in chat"
                        )
                        continue
                        
                    except Exception as e:
                        logger.error(f"Error checking assistant status: {str(e)}")
                        await self.update_request_status(
                            request["_id"],
                            QueueStatus.FAILED,
                            error_message=f"Error checking assistant status: {str(e)}"
                        )
                        continue

                    # If we get here, assistant is admin with proper permissions
                    # Process the request
                    try:
                        stats = await approve_requests_internal(
                            chat_id=request["chat_id"],
                            admin_id=request["admin_id"],
                            num_requests=request["num_requests"]
                        )
                        
                        await self.update_request_status(
                            request["_id"],
                            QueueStatus.COMPLETED
                        )
                        
                    except Exception as e:
                        logger.error(f"Error processing queue request: {str(e)}")
                        await self.update_request_status(
                            request["_id"],
                            QueueStatus.FAILED,
                            error_message=str(e)
                        )
                        
                        await bot.send_message(
                            chat_id=request["admin_id"],
                            text=f"❌ Error processing approval request: {str(e)}"
                        )
                        
                except Exception as e:
                    logger.error(f"Error in queue processing: {str(e)}")
                    await asyncio.sleep(5)

# Initialize queue manager
queue_manager = QueueManager()

async def send_log(client, message, action_type=None, extra_info=None):
    """
    Send formatted logs to the logging group
    
    Args:
        client: Bot client instance
        message: Original message that triggered the action
        action_type: Type of action (e.g., 'start', 'auth', 'unauth', 'approve', 'addassistant')
        extra_info: Additional information to include in log
    """
    try:
        user = message.from_user
        user_mention = f"[{user.first_name}](tg://user?id={user.id})"
        
        log_text = f"📝 **New Bot Activity**\n"
        log_text += f"👤 **User:** {user_mention}\n"
        log_text += f"🆔 **User ID:** `{user.id}`\n"
        
        if action_type == "start":
            log_text += f"📱 **Action:** Started the bot\n"
            
        elif action_type in ["auth", "unauth"]:
            chat_id = message.text.split()[1]
            try:
                chat = await client.get_chat(int(chat_id))
                chat_title = chat.title
                log_text += f"📱 **Action:** {'Authorized' if action_type == 'auth' else 'Unauthorized'} chat\n"
                log_text += f"💭 **Chat:** {chat_title}\n"
                log_text += f"🆔 **Chat ID:** `{chat_id}`\n"
            except Exception as e:
                log_text += f"📱 **Action:** Failed {'authorization' if action_type == 'auth' else 'unauthorized'}\n"
                log_text += f"❌ **Error:** {str(e)}\n"
                
        elif action_type == "approve":
            chat_id = message.text.split()[1]
            try:
                chat = await client.get_chat(int(chat_id))
                chat_title = chat.title
                log_text += f"📱 **Action:** Approval request\n"
                log_text += f"💭 **Chat:** {chat_title}\n"
                log_text += f"🆔 **Chat ID:** `{chat_id}`\n"
                
                # Add number of requests if specified
                if len(message.text.split()) == 3:
                    num_requests = message.text.split()[2]
                    log_text += f"📊 **Requests:** {num_requests}\n"
                
                # Add approval statistics if provided
                if extra_info and isinstance(extra_info, dict):
                    log_text += "\n📊 **Approval Statistics:**\n"
                    log_text += f"✅ Approved: {extra_info.get('approved_count', 0)}\n"
                    log_text += f"ℹ️ Already Members: {extra_info.get('already_member_count', 0)}\n"
                    log_text += f"⚠️ Too Many Channels: {extra_info.get('too_many_channels_count', 0)}\n"
                    log_text += f"❗️ Deactivated: {extra_info.get('deactivated_count', 0)}\n"
                    log_text += f"❌ Failed: {extra_info.get('skipped_count', 0)}\n"
            except Exception as e:
                log_text += f"📱 **Action:** Failed approval\n"
                log_text += f"❌ **Error:** {str(e)}\n"

        elif action_type == "addassistant":
            chat_id = message.text.split()[1]
            try:
                chat = await client.get_chat(int(chat_id))
                chat_title = chat.title
                log_text += f"📱 **Action:** Add Assistant\n"
                log_text += f"💭 **Chat:** {chat_title}\n"
                log_text += f"🆔 **Chat ID:** `{chat_id}`\n"
                
                # Add status and additional info if provided
                if extra_info and isinstance(extra_info, dict):
                    status = extra_info.get('status', '')
                    if status == 'success':
                        log_text += f"✅ **Status:** Successfully added assistant\n"
                        log_text += f"📌 **Chat Type:** {extra_info.get('chat_type', 'Unknown')}\n"
                        log_text += f"🤖 **Assistant:** @{extra_info.get('assistant_username', 'Unknown')}\n"
                    elif status == 'already_participant':
                        log_text += f"ℹ️ **Status:** Assistant already in chat\n"
                        log_text += f"🤖 **Assistant:** @{extra_info.get('assistant_username', 'Unknown')}\n"
                    elif status == 'flood_wait':
                        log_text += f"⚠️ **Status:** Rate limited\n"
                        log_text += f"⏳ **Wait Time:** {extra_info.get('wait_time', 'Unknown')} seconds\n"
                    elif status == 'rpc_error' or status == 'error':
                        log_text += f"❌ **Status:** Failed\n"
                        log_text += f"❌ **Error:** {extra_info.get('error', 'Unknown error')}\n"
                        
            except Exception as e:
                log_text += f"📱 **Action:** Failed to add assistant\n"
                log_text += f"❌ **Error:** {str(e)}\n"
        
        # Add timestamp
        log_text += f"\n⏰ **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        
        # Send log message to group
        await client.send_message(
            chat_id=LOG_GROUP_ID,
            text=log_text,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error sending log to group: {str(e)}")

async def is_chat_authorized(chat_id: int) -> bool:
    """Check if a chat is authorized in MongoDB"""
    chat_doc = chats_collection.find_one({"chat_id": chat_id})
    return chat_doc.get("is_authorized", False) if chat_doc else False

async def store_chat_authorization(chat_id: int, chat_title: str, authorized_by: dict, is_authorized: bool):
    """Store chat authorization status in MongoDB"""
    chat_doc = {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "authorized_at": datetime.now(timezone.utc),
        "authorized_by": authorized_by,
        "is_authorized": is_authorized
    }
    
    # Update or insert the document
    chats_collection.update_one(
        {"chat_id": chat_id},
        {"$set": chat_doc},
        upsert=True
    )

async def store_assistant_data(chat_id: int, chat_title: str, added_by: dict, assistant_info: dict, is_active: bool):
    """Store assistant data in MongoDB"""
    assistant_doc = {
        "chat_id": chat_id,
        "added_by": {
            "user_id": added_by.id,
            "username": added_by.username,
            "first_name": added_by.first_name
        },
        "assistant_id": assistant_info.id,
        "assistant_username": assistant_info.username,
        "chat_title": chat_title,
        "is_active": is_active,
        "added_at": datetime.now(timezone.utc)
    }
    
    # Update or insert the document
    assistant_collection.update_one(
        {"chat_id": chat_id},
        {"$set": assistant_doc},
        upsert=True
    )

async def store_user_data(user):
    """Store user information in MongoDB"""
    user_doc = {
        "user_id": user.id,
        "first_name": user.first_name,
        "username": user.username,
        "joined_at": datetime.now(timezone.utc),
        "last_active": datetime.now(timezone.utc)
    }
    
    # Update or insert the document
    users_collection.update_one(
        {"user_id": user.id},
        {
            "$set": user_doc,
            "$setOnInsert": {"first_joined": datetime.now(timezone.utc)}
        },
        upsert=True
    )

async def store_approved_user(user, chat, approval_type):
    """Store approved user information in MongoDB
    
    Args:
        user: User object containing user details
        chat: Chat object containing chat details
        approval_type: String indicating 'auto_approval' or 'manual_approval'
    """
    approved_user_doc = {
        "user_id": user.id,
        "user_name": user.username or user.first_name,  # Fallback to first_name if username is None
        "chat_id": chat.id,
        "chat_title": chat.title,
        "approved_by": approval_type,
        "approved_at": datetime.now(timezone.utc)
    }
    
    # Insert the document
    approved_users_collection.insert_one(approved_user_doc)

def handle_flood_wait(func):
    """Decorator to handle FloodWait errors"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            logger.warning(f"FloodWait encountered: waiting for {e.value} seconds")
            await asyncio.sleep(e.value)
            return await func(*args, **kwargs)
    return wrapper

async def validate_chat(client, chat_id):
    """Validate chat existence and bot's admin status"""
    try:
        # First try to get chat info
        chat = await client.get_chat(chat_id)
        
        # Get bot's member info
        bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)
        
        # Check if bot has the required permissions
        if not bot_member.privileges or not bot_member.privileges.can_invite_users:
            return {
                "valid": False,
                "error": "Bot needs admin rights with 'Invite Users' permission in this chat."
            }
        
        # If we get here, bot has required permissions
        chat_type = "channel" if chat.type == enums.ChatType.CHANNEL else "group"
        
        return {
            "valid": True,
            "chat_type": chat_type,
            "title": chat.title,
            "chat": chat  # Return the chat object for creating invite link
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error validating chat: {str(e)}"
        }


async def is_chat_admin(client, chat_id, user_id):
    """Check if user is admin in chat"""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        is_admin = member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        logger.info(f"Admin status check - User: {user_id}, Chat: {chat_id}, Is Admin: {is_admin}")
        return is_admin
    except UserNotParticipant:
        logger.warning(f"User {user_id} is not a member of chat {chat_id}")
        return False
    except ChatAdminRequired:
        logger.error(f"Bot needs admin rights in chat {chat_id}")
        return False
    except Exception as e:
        logger.error(f"Error checking admin status - User: {user_id}, Chat: {chat_id}, Error: {str(e)}")
        return False
    
async def extract_chat_id(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            return None
        chat_id = int(parts[1].strip('@'))
        logger.info(f"Extracted chat ID: {chat_id} from message: {message.text}")
        return chat_id
    except ValueError as e:
        logger.error(f"Invalid chat ID format in message: {message.text}, Error: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error extracting chat ID from message: {message.text}, Error: {str(e)}")
        return None

@bot.on_message(filters.command('addassistant') & filters.private)
@handle_flood_wait
async def add_assistant(client: Client, message: Message):
    """Add assistant to a chat using invite link"""
    try:
        chat_id = await extract_chat_id(message)
        if not chat_id:
            await message.reply("Please use the format: /addassistant [channel_id or group_id]")
            return

        is_admin = await is_chat_admin(client, chat_id, message.from_user.id)
        if not is_admin:
            await message.reply("You don't have admin permissions in this chat.")
            return

        processing_message = await message.reply("Validating chat and adding assistant, please wait...")
        
        # Validate chat and bot permissions
        validation_result = await validate_chat(client, chat_id)
        if not validation_result["valid"]:
            await processing_message.edit_text(f"❌ Error: {validation_result['error']}")
            return

        try:
            # Get the user client's own info
            assistant_info = await user.get_me()
            
            # Create a temporary invite link with expiration
            chat = validation_result["chat"]
            invite_link = await client.create_chat_invite_link(
                chat_id,
                member_limit=1,
                expire_date=datetime.now() + timedelta(minutes=5)
            )
            
            # Try to join using the invite link
            try:
                await user.join_chat(invite_link.invite_link)
                
                # Store assistant data and send success log only if join was successful
                await store_assistant_data(
                    chat_id=chat_id,
                    chat_title=validation_result["title"],
                    added_by=message.from_user,
                    assistant_info=assistant_info,
                    is_active=True
                )
                
                chat_type = validation_result["chat_type"]
                chat_title = validation_result["title"]
                
                # Log successful addition
                await send_log(
                    client,
                    message,
                    action_type="addassistant",
                    extra_info={
                        "status": "success",
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "chat_type": chat_type,
                        "assistant_username": assistant_info.username
                    }
                )
                
                success_message = (
                    f"✅ Successfully added assistant to the {chat_type}!\n\n"
                    f"📌 Chat Details:\n"
                    f"• Title: {chat_title}\n"
                    f"• Type: {chat_type}\n"
                    f"• ID: `{chat_id}`\n\n"
                    f"🤖 Assistant Details:\n"
                    f"• Username: @{assistant_info.username}\n"
                    f"• ID: `{assistant_info.id}`\n\n"
                    "ℹ️ Please ensure the assistant account has admin rights with 'Invite Users' permission."
                )
                
                await processing_message.edit_text(success_message)
                
            except RPCError as e:
                error_message = str(e).lower()
                
                if "user_already_participant" in error_message:
                    # Store assistant data even if already a participant
                    await store_assistant_data(
                        chat_id=chat_id,
                        chat_title=validation_result["title"],
                        added_by=message.from_user,
                        assistant_info=assistant_info,
                        is_active=True
                    )
                    
                    # Log the already participant status only once
                    await send_log(
                        client, 
                        message, 
                        action_type="addassistant",
                        extra_info={
                            "status": "already_participant",
                            "chat_id": chat_id,
                            "chat_title": validation_result["title"],
                            "assistant_username": assistant_info.username
                        }
                    )
                    
                    await processing_message.edit_text(
                        "Assistant account is already in the chat.\n"
                        "⚠️ Please ensure the assistant has admin rights to approve join requests."
                    )
                    return
                elif "invite_hash_expired" in error_message:
                    await processing_message.edit_text(
                        "❌ Error: Unable to add assistant - The assistant account appears to be banned from this chat.\n\n"
                        "Please follow these steps:\n"
                        f"1. Check if the assistant account @{assistant_info.username} is banned\n"
                        "2. If banned, unban the account from your chat settings\n"
                        "3. Try the /addassistant command again\n\n"
                        "If the problem persists, you may need to:\n"
                        "• Remove any restrictions on the assistant account\n"
                        "• Wait a few hours before trying again\n"
                        "• Contact Telegram support if the issue continues"
                    )
                    return
                elif "privacy_restricted" in error_message:
                    await processing_message.edit_text(
                        "❌ Error: Unable to add assistant due to privacy restrictions.\n"
                        "Please ensure the chat's privacy settings allow new members to join."
                    )
                    return
                elif "user_banned_in_channel" in error_message:
                    await processing_message.edit_text(
                        "❌ Error: The assistant account is banned from this chat.\n"
                        "Please unban the account and try again."
                    )
                    return
                else:
                    raise
                    
        except FloodWait as e:
            error_msg = f"Rate limited. Please try again after {e.value} seconds."
            await processing_message.edit_text(error_msg)
            logger.warning(f"FloodWait in add_assistant: {e.value} seconds")
            
            # Log flood wait error
            await send_log(
                client,
                message,
                action_type="addassistant",
                extra_info={
                    "status": "flood_wait",
                    "chat_id": chat_id,
                    "wait_time": e.value
                }
            )
            
        except RPCError as e:
            error_msg = f"Failed to add assistant: {str(e)}"
            await processing_message.edit_text(error_msg)
            logger.error(error_msg)
            
            # Log RPC error
            await send_log(
                client,
                message,
                action_type="addassistant",
                extra_info={
                    "status": "rpc_error",
                    "chat_id": chat_id,
                    "error": str(e)
                }
            )
            
    except Exception as e:
        logger.error(f"Error in add_assistant: {str(e)}")
        logger.error(traceback.format_exc())
        await message.reply("An unexpected error occurred. Please try again later.")
        
        # Log unexpected error
        await send_log(
            client,
            message,
            action_type="addassistant",
            extra_info={
                "status": "error",
                "error": str(e)
            }
        )

@bot.on_message(filters.command('approve') & filters.private)
@handle_flood_wait
async def approve_requests(client, message):
    try:
        args = message.text.split()
        if len(args) not in [2, 3]:
            await message.reply("Please use either:\n/approve [chat_id]\nOR\n/approve [chat_id] [number_of_requests]")
            return

        try:
            chat_id = int(args[1].strip('@'))
        except ValueError:
            await message.reply("Invalid chat ID format.")
            return

        num_requests = None
        if len(args) == 3:
            try:
                num_requests = int(args[2])
                if num_requests <= 0:
                    await message.reply("Number of requests must be greater than 0.")
                    return
            except ValueError:
                await message.reply("Invalid number format for requests.")
                return

        if not await is_chat_admin(client, chat_id, message.from_user.id):
            await message.reply("You don't have admin permissions in this chat.")
            return

        # Log the initial approval request
        await send_log(client, message, action_type="approve")

        # Check assistant status before queuing
        try:
            assistant_info = await user.get_me()
            assistant_member = await user.get_chat_member(chat_id, assistant_info.id)
            
            if not assistant_member.privileges or not assistant_member.privileges.can_invite_users:
                await message.reply(
                    "⚠️ Assistant requires admin rights with 'Invite Users' permission.\n"
                    "Please add the assistant as admin first using /addassistant command."
                )
                return

        except UserNotParticipant:
            await message.reply(
                "⚠️ Assistant is not a member of this chat.\n"
                "Please add the assistant first using /addassistant command."
            )
            return
            
        except Exception as e:
            logger.error(f"Error checking assistant status: {str(e)}")
            await message.reply("An error occurred while checking assistant status. Please try again.")
            return

        # Add request to queue
        queue_position, request_id = await queue_manager.add_to_queue(
            chat_id=chat_id,
            admin_id=message.from_user.id,
            num_requests=num_requests
        )

        await message.reply(
            f"✅ Your approval request has been queued!\n\n"
            f"🔹 Queue Position: {queue_position}\n"
            f"🔹 Request ID: `{str(request_id)}`\n\n"
            "You will be notified when the processing is complete."
        )

    except Exception as e:
        logger.error(f"Error in approve_requests: {str(e)}")
        await message.reply("An unexpected error occurred. Please try again later.")

async def approve_requests_internal(chat_id: int, admin_id: int, num_requests: int = None):
    try:
        # First verify the chat exists and is accessible to the bot
        try:
            chat = await bot.get_chat(chat_id)
        except ChannelPrivate:
            raise Exception(
                "Unable to access the chat. Ensure the bot is added to the channel/group "
                "and has proper permissions."
            )
        except Exception as e:
            raise Exception(f"Unable to find the chat. Please verify the chat ID. Error: {str(e)}")

        # Check if assistant is in the chat
        try:
            assistant_info = await user.get_me()
            assistant_member = await user.get_chat_member(chat_id, assistant_info.id)
        except UserNotParticipant:
            raise Exception(
                "Assistant account is not a member of the chat. "
                "Please first add the assistant using /addassistant command."
            )
        except Exception as e:
            logger.error(f"Error checking assistant membership: {str(e)}")
            raise

        # Check assistant's admin rights
        if not assistant_member.privileges or not assistant_member.privileges.can_invite_users:
            raise Exception(
                "Assistant account needs admin rights with 'Invite Users' permission. "
                "Please ensure proper admin rights are granted."
            )

        # Initialize counters
        stats = {
            "approved_count": 0,
            "skipped_count": 0,
            "already_member_count": 0,
            "too_many_channels_count": 0,
            "deactivated_count": 0  # New counter for deactivated users
        }
        
        try:
            async for request in user.get_chat_join_requests(chat_id):
                if num_requests is not None and stats["approved_count"] >= num_requests:
                    break
                
                try:
                    # Try to approve the request
                    await user.approve_chat_join_request(
                        chat_id=chat_id,
                        user_id=request.user.id
                    )
                    
                    # Store approved user data
                    await store_approved_user(
                        user=request.user,
                        chat=chat,
                        approval_type="manual_approval"
                    )
                    
                    stats["approved_count"] += 1
                    await asyncio.sleep(0.5)  # Rate limiting prevention
                    
                except RPCError as e:
                    error_message = str(e).upper()
                    if "USER_ALREADY_PARTICIPANT" in error_message:
                        stats["already_member_count"] += 1
                        logger.info(f"User {request.user.id} is already a member of chat {chat_id}")
                        try:
                            # Try to hide the join request since user is already a member
                            await user.decline_chat_join_request(chat_id, request.user.id)
                        except Exception as hide_error:
                            logger.error(f"Error hiding join request: {str(hide_error)}")
                    elif "USER_CHANNELS_TOO_MUCH" in error_message or "CHANNELS_TOO_MUCH" in error_message:
                        stats["too_many_channels_count"] += 1
                        logger.info(f"User {request.user.id} is in too many channels/groups")
                    elif "INPUT_USER_DEACTIVATED" in error_message:
                        stats["deactivated_count"] += 1
                        logger.info(f"Skipping deactivated user {request.user.id}")
                        try:
                            # Try to hide the join request for deactivated user
                            await user.decline_chat_join_request(chat_id, request.user.id)
                        except Exception as hide_error:
                            logger.error(f"Error hiding join request for deactivated user: {str(hide_error)}")
                    else:
                        stats["skipped_count"] += 1
                        logger.error(f"Error approving request: {str(e)}")
                    continue

        except ChatAdminRequired:
            raise Exception(
                "Assistant account needs admin rights to view join requests. "
                "Please ensure proper admin rights are granted."
            )

        # Notify admin of completion
        try:
            # Prepare detailed status message
            status_message = []
            if stats["approved_count"] > 0:
                status_message.append(f"✅ Successfully approved {stats['approved_count']} request(s)")
            if stats["already_member_count"] > 0:
                status_message.append(f"ℹ️ Skipped {stats['already_member_count']} already member(s)")
            if stats["too_many_channels_count"] > 0:
                status_message.append(
                    f"⚠️ Skipped {stats['too_many_channels_count']} user(s) in too many channels"
                )
            if stats["deactivated_count"] > 0:
                status_message.append(f"❗️ Skipped {stats['deactivated_count']} deactivated user(s)")
            if stats["skipped_count"] > 0:
                status_message.append(f"❌ Failed to approve {stats['skipped_count']} request(s)")
            
            if status_message:
                await bot.send_message(
                    chat_id=admin_id,
                    text="\n".join(status_message)
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text="ℹ️ No pending join requests found."
                )
        except Exception as e:
            logger.error(f"Error sending completion notification to admin: {str(e)}")

        return stats

    except Exception as e:
        logger.error(f"Error in approve_requests_internal: {str(e)}")
        logger.error(traceback.format_exc())
        raise

@bot.on_message(filters.command("auth") & filters.private)
@handle_flood_wait
async def authorize_chat(client, message):
    try:
        chat_id = await extract_chat_id(message)
        if not chat_id:
            await message.reply("Please use the format: /auth [channel_id or group_id]")
            return

        if not await is_chat_admin(client, chat_id, message.from_user.id):
            await message.reply("You don't have admin permissions in this chat.")
            return

        # Get chat info
        chat = await client.get_chat(chat_id)
        
        # Store authorization in MongoDB
        authorized_by = {
            "user_id": message.from_user.id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name
        }
        
        await store_chat_authorization(
            chat_id=chat_id,
            chat_title=chat.title,
            authorized_by=authorized_by,
            is_authorized=True
        )
        
        await message.reply(f"Chat {chat_id} has been authorized for automatic approval.")
        logger.info(f"Chat {chat_id} authorized for automatic approval by user {message.from_user.id}")
        await send_log(client, message, action_type="auth")
        
    except Exception as e:
        logger.error(f"Error in authorize_chat: {str(e)}")
        logger.error(traceback.format_exc())
        await message.reply("An error occurred while authorizing the chat. Please try again later.")

@bot.on_message(filters.command("unauth") & filters.private)
@handle_flood_wait
async def unauthorize_chat(client, message):
    try:
        chat_id = await extract_chat_id(message)
        if not chat_id:
            await message.reply("Please use the format: /unauth [channel_id or group_id]")
            return

        if not await is_chat_admin(client, chat_id, message.from_user.id):
            await message.reply("You don't have admin permissions in this chat.")
            return

        # Check if chat is currently authorized
        if not await is_chat_authorized(chat_id):
            await message.reply("This chat is not authorized for automatic approval.")
            return

        # Get chat info
        chat = await client.get_chat(chat_id)
        
        # Store unauthorized status in MongoDB
        authorized_by = {
            "user_id": message.from_user.id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name
        }
        
        await store_chat_authorization(
            chat_id=chat_id,
            chat_title=chat.title,
            authorized_by=authorized_by,
            is_authorized=False
        )
        
        await message.reply(f"Chat {chat_id} has been unauthorized for automatic approval.")
        logger.info(f"Chat {chat_id} unauthorized by user {message.from_user.id}")
        await send_log(client, message, action_type="unauth")
        
    except Exception as e:
        logger.error(f"Error in unauthorize_chat: {str(e)}")
        logger.error(traceback.format_exc())
        await message.reply("An error occurred while unauthorizing the chat. Please try again later.")

@bot.on_chat_join_request()
@handle_flood_wait
async def handle_join_request(client, request):
    try:
        chat_id = request.chat.id
        user_id = request.from_user.id

        # Check authorization status in MongoDB
        if await is_chat_authorized(chat_id):
            user_full_name = request.from_user.full_name
            chat_name = request.chat.title

            welcome_message = f"𝐇𝐞𝐲 {user_full_name} ✨\n\n" \
                            "𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 𝗼𝘂𝗿 𝗰𝗼𝗺𝗺𝘂𝗻𝗶𝘁𝘆 🎉\n\n" \
                            f"●︎ ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴘᴘʀᴏᴠᴇᴅ ᴛᴏ ᴊᴏɪɴ {chat_name}!\n\n" \
                            "ᴘʟᴇᴀꜱᴇ ᴄᴏɴꜱɪᴅᴇʀ ᴊᴏɪɴɪɴɢ ᴏᴜʀ ꜱᴜᴘᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ᴀꜱ ᴡᴇʟʟ."
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Support Channel", url="https://t.me/SmokieOfficial")]
            ])

            try:
                gif_url = "https://cdn.glitch.global/04a38d5f-8c30-452e-b709-33da5c74b12d/175446-853577055.mp4?v=1732257487908"
                await bot.send_animation(
                    chat_id=user_id,
                    animation=gif_url,
                    caption=welcome_message,
                    reply_markup=keyboard
                )
                await request.approve()
                # Store approved user data
                await store_approved_user(
                    user=request.from_user,
                    chat=request.chat,
                    approval_type="auto_approval"
                )
                logger.info(f"Approved join request for user {user_id} in chat {chat_id}")
            except ChatWriteForbidden:
                logger.warning(f"Unable to send welcome message to user {user_id} - User has blocked the bot")
            except Exception as e:
                logger.error(f"Error sending welcome message to user {user_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error in handle_join_request: {str(e)}")
        logger.error(traceback.format_exc())

@bot.on_message(filters.command('start') & filters.private)
@handle_flood_wait
async def start_command(client: Client, message: Message):
    """Handle the /start command"""
    try:
        # Store user data
        await store_user_data(message.from_user)
        
        
        user_name = message.from_user.first_name
        start_message = (
            f"👋 **Hello {user_name}!**\n\n"
            "🤖 I am **Auto Approve Bot**, your automated assistant for managing join requests in your channels and groups.\n\n"
            "**Key Features:**\n"
            "• Auto-approve new join requests\n"
            "• Manual approval management\n"
            "• Detailed approval logs\n\n"
            "**Main Commands:**\n"
            "• /addassistant [ChatId] - To add assistant to your chat\n"
            "• /approve [ChatId] - Manually approve pending requests\n"
            "• /auth [ChatId] - Enable auto-approval for a chat\n"
            "• /unauth [ChatId] - Disable auto-approval\n\n"
            "To get started, add me to your channel/group as an admin with 'Invite Users' permission! 🚀"
        )

        bot_username = (await client.get_me()).username

        # Create inline keyboard with support channel button
        keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add me to your channel", url=f"https://t.me/{bot_username}?startchannel=true")],
        [InlineKeyboardButton("➕ Add me to your Group", url=f"https://t.me/{bot_username}?startgroup=true")],
        [
            InlineKeyboardButton("👥 Support", url="https://t.me/BotCodeVerse"),
            InlineKeyboardButton("👨‍💻 Owner", url="https://t.me/Hmm_Smokie")
        ]
    ])
        # Send welcome video with caption
        await client.send_video(
            chat_id=message.chat.id,
            video="https://cdn.glitch.global/94c1411f-81f7-4957-8c81-6e2e5285e45c/201734-916310639_medium.mp4?v=1736459660788",
            caption=start_message,
            reply_markup=keyboard
        )
        logger.info(f"Start command handled and user data stored for user {message.from_user.id}")
        await send_log(client, message, action_type="start")
    except Exception as e:
        error_msg = f"Error in start command: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        await message.reply("An error occurred while processing your request. Please try again later.")

async def start_queue_processor():
    """Start the queue processor"""
    while True:
        try:
            await queue_manager.process_queue()
        except Exception as e:
            logger.error(f"Queue processor error: {str(e)}")
            await asyncio.sleep(5)

class BroadcastStats:
    def __init__(self):
        self.total_users = 0
        self.done = 0
        self.success = 0
        self.failed = 0
        self.blocked = 0
        self.deleted = 0
        self.invalid = 0
        self.failed_users: List[Tuple[int, str]] = []

    def update_failed(self, user_id: int, error: str):
        self.failed += 1
        self.failed_users.append((user_id, error))
        if error == "blocked":
            self.blocked += 1
        elif error == "deactivated":
            self.deleted += 1
        elif error == "invalid_id":
            self.invalid += 1

async def broadcast_message(client: Client, user_id: int, message: Message) -> Tuple[bool, str]:
    """Broadcast a message to a user with error handling"""
    try:
        if message.text:
            await client.send_message(
                chat_id=user_id,
                text=message.text,
                entities=message.entities,
                reply_markup=message.reply_markup,
                disable_notification=True
            )
        elif message.photo:
            await client.send_photo(
                chat_id=user_id,
                photo=message.photo.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities,
                reply_markup=message.reply_markup,
                disable_notification=True
            )
        elif message.video:
            await client.send_video(
                chat_id=user_id,
                video=message.video.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities,
                reply_markup=message.reply_markup,
                disable_notification=True
            )
        elif message.document:
            await client.send_document(
                chat_id=user_id,
                document=message.document.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities,
                reply_markup=message.reply_markup,
                disable_notification=True
            )
        elif message.animation:
            await client.send_animation(
                chat_id=user_id,
                animation=message.animation.file_id,
                caption=message.caption,
                caption_entities=message.caption_entities,
                reply_markup=message.reply_markup,
                disable_notification=True
            )
        return True, ""

    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await broadcast_message(client, user_id, message)
    except (InputUserDeactivated, UserDeactivated):
        return False, "deactivated"
    except UserIsBlocked:
        return False, "blocked"
    except PeerIdInvalid:
        return False, "invalid_id"
    except Exception as e:
        logger.error(f"Broadcast error for user {user_id}: {str(e)}")
        return False, f"other:{str(e)}"

async def update_broadcast_status(status_msg: Message, stats: BroadcastStats):
    """Update the broadcast status message"""
    try:
        await status_msg.edit_text(
            f"🚀 Broadcast in Progress...\n\n"
            f"👥 Total Users: {stats.total_users}\n"
            f"✅ Completed: {stats.done} / {stats.total_users}\n"
            f"✨ Success: {stats.success}\n"
            f"❌ Failed: {stats.failed}\n\n"
            f"🚫 Blocked: {stats.blocked}\n"
            f"❗️ Deleted: {stats.deleted}\n"
            f"📛 Invalid: {stats.invalid}"
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception as e:
        logger.error(f"Error updating broadcast status: {str(e)}")

@bot.on_message(filters.command('broadcast') & filters.private)
async def broadcast_handler(client: Client, message: Message):
    """Handle the broadcast command"""
    try:
        # Check if user is authorized
        if message.from_user.id != 1949883614:  # Replace with your user ID
            await message.reply_text("⛔️ This command is only for the bot owner.")
            return

        # Check if the command is a reply to a message
        if not message.reply_to_message:
            await message.reply_text(
                "❗️ Please reply to a message to broadcast it to all users."
            )
            return

        # Initial broadcast status message
        status_msg = await message.reply_text("🔍 Gathering user data...")

        # Initialize broadcast stats
        stats = BroadcastStats()

        # Gather unique users from both collections using synchronous operations
        unique_users = set()
        
        # Get users from users_collection
        for user in users_collection.find({}, {'user_id': 1}):
            unique_users.add(user['user_id'])
            
        # Get users from approved_users_collection
        for user in approved_users_collection.find({}, {'user_id': 1}):
            unique_users.add(user['user_id'])

        stats.total_users = len(unique_users)
        
        await status_msg.edit_text(
            f"🚀 Starting broadcast to {stats.total_users} users..."
        )

        # Process broadcast in chunks to avoid overwhelming the system
        chunk_size = 20
        user_chunks = [list(unique_users)[i:i + chunk_size] for i in range(0, len(unique_users), chunk_size)]

        for chunk in user_chunks:
            broadcast_tasks = []
            for user_id in chunk:
                task = asyncio.create_task(broadcast_message(
                    client,
                    user_id,
                    message.reply_to_message
                ))
                broadcast_tasks.append((user_id, task))

            # Wait for current chunk to complete
            for user_id, task in broadcast_tasks:
                try:
                    success_status, error = await task
                    stats.done += 1
                    
                    if success_status:
                        stats.success += 1
                    else:
                        stats.update_failed(user_id, error)
                except Exception as e:
                    logger.error(f"Task error for user {user_id}: {str(e)}")
                    stats.update_failed(user_id, f"task_error:{str(e)}")

            # Update status after each chunk
            await update_broadcast_status(status_msg, stats)
            await asyncio.sleep(1)  # Small delay between chunks

        # Final broadcast status
        completion_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success_rate = (stats.success / stats.total_users) * 100 if stats.total_users > 0 else 0
        
        await status_msg.edit_text(
            f"✅ Broadcast Completed!\n"
            f"Completed at: {completion_time}\n\n"
            f"👥 Total Users: {stats.total_users}\n"
            f"✨ Success: {stats.success}\n"
            f"❌ Failed: {stats.failed}\n\n"
            f"Success Rate: {success_rate:.2f}%\n\n"
            f"🚫 Blocked: {stats.blocked}\n"
            f"❗️ Deleted: {stats.deleted}\n"
            f"📛 Invalid: {stats.invalid}"
        )

        # Clean up invalid users
        if stats.failed_users:
            clean_msg = await message.reply_text(
                "🧹 Cleaning database...\n"
                "Removing blocked and deleted users."
            )

            # Extract user IDs from failed_users list
            invalid_user_ids = [user_id for user_id, _ in stats.failed_users]

            try:
                # Delete invalid users from both collections
                deleted_users = users_collection.delete_many(
                    {"user_id": {"$in": invalid_user_ids}}
                )
                deleted_approved = approved_users_collection.delete_many(
                    {"user_id": {"$in": invalid_user_ids}}
                )

                total_deleted = deleted_users.deleted_count + deleted_approved.deleted_count
                
                await clean_msg.edit_text(
                    f"🧹 Database cleaned!\n"
                    f"Removed {total_deleted} invalid users from databases."
                )
            except Exception as e:
                logger.error(f"Error cleaning database: {str(e)}")
                await clean_msg.edit_text(
                    "❌ Error occurred while cleaning database."
                )

    except Exception as e:
        logger.error(f"Error in broadcast handler: {str(e)}")
        logger.error(traceback.format_exc())
        await message.reply_text(
            f"❌ An error occurred during broadcast: {str(e)}"
        )

if __name__ == "__main__":
    try:
        logger.info("Starting bot...")
        
        # Create the event loop
        loop = asyncio.get_event_loop()
        
        async def start_bot():
            """Async function to start both user client and bot"""
            try:
                # Start user client
                await user.start()
                logger.info("User client started successfully")
                
                # Start the bot
                await bot.start()
                logger.info("Bot started successfully")
                
                # Create queue processor task
                queue_processor_task = asyncio.create_task(start_queue_processor())
                
                # Keep the bot running using an infinite loop
                while True:
                    await asyncio.sleep(1)
                    
            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, stopping bot...")
            except Exception as e:
                logger.error(f"Error in bot operation: {str(e)}")
                logger.error(traceback.format_exc())
            finally:
                # Cleanup
                logger.info("Stopping services...")
                try:
                    await user.stop()
                    await bot.stop()
                except Exception as e:
                    logger.error(f"Error during cleanup: {str(e)}")
                
        # Run everything in the event loop
        try:
            loop.run_until_complete(start_bot())
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            loop.close()
            logger.info("Bot stopped successfully")
            
    except Exception as e:
        logger.critical(f"Critical error starting bot: {str(e)}")
        logger.critical(traceback.format_exc())
