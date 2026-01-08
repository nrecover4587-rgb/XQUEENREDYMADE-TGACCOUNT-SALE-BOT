"""
Account Management Module for OTP Bot
Handles Pyrogram login, OTP verification, and session management
"""

import logging
import re
import threading
import time
import os
import asyncio
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
    FloodWait, PhoneCodeEmpty
)

logger = logging.getLogger(__name__)

# -----------------------
# ASYNC MANAGEMENT
# -----------------------
class AsyncManager:
    """Manages async operations in sync context"""
    def __init__(self):
        self.lock = threading.Lock()
        
    def run_async(self, coro):
        """Run async coroutine from sync context"""
        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Async operation failed: {e}")
            raise

# -----------------------
# PYROGRAM CLIENT MANAGER (FIXED)
# -----------------------
class PyrogramClientManager:
    """Fixed Pyrogram client management with proper session handling"""
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.lock = threading.Lock()
        
    async def create_client(self, session_string=None, name=None):
        """Create a Pyrogram client with proper settings"""
        if name is None:
            name = f"client_{int(time.time())}_{threading.get_ident()}"
            
        # Create client with settings to avoid ping issues
        client = Client(
            name=name,
            session_string=session_string,
            api_id=self.api_id,
            api_hash=self.api_hash,
            in_memory=True,
            no_updates=True,  # Disable updates
            takeout=False,    # Disable takeout
            sleep_threshold=0,  # Disable automatic sleeping
            workdir="./sessions"  # Set work directory for sessions
        )
        
        return client
    
    async def send_code(self, client, phone_number):
        """Send verification code"""
        try:
            await client.connect()
            sent_code = await client.send_code(phone_number)
            return True, sent_code.phone_code_hash, None
        except FloodWait as e:
            return False, None, f"FloodWait: Please wait {e.value} seconds"
        except Exception as e:
            logger.error(f"Send code error: {e}")
            return False, None, str(e)
    
    async def sign_in_with_otp(self, client, phone_number, phone_code_hash, otp_code):
        """Sign in with OTP"""
        try:
            await client.sign_in(
                phone_number=phone_number,
                phone_code=otp_code,
                phone_code_hash=phone_code_hash
            )
            return True, None, None
        except SessionPasswordNeeded:
            return False, "password_required", None
        except Exception as e:
            logger.error(f"Sign in error: {e}")
            return False, "error", str(e)
    
    async def sign_in_with_password(self, client, password):
        """Sign in with 2FA password"""
        try:
            await client.check_password(password)
            return True, None
        except Exception as e:
            logger.error(f"Password check error: {e}")
            return False, str(e)
    
    async def get_session_string(self, client):
        """Get session string from authorized client"""
        try:
            if await client.is_user_authorized():
                return await client.export_session_string()
            return None
        except Exception as e:
            logger.error(f"Error getting session string: {e}")
            return None
    
    async def safe_disconnect(self, client):
        """Safely disconnect client"""
        try:
            if client:
                await client.disconnect()
                await client.stop()
        except Exception as e:
            logger.error(f"Error disconnecting client: {e}")
        finally:
            # Clean up session files if they exist
            try:
                session_files = [
                    f"./{client.name}.session",
                    f"./{client.name}.session-journal"
                ]
                for file in session_files:
                    if os.path.exists(file):
                        os.remove(file)
            except:
                pass

# -----------------------
# ACCOUNT MANAGEMENT FUNCTIONS
# -----------------------
async def pyrogram_login_flow_async(login_states, accounts_col, user_id, phone_number, chat_id, message_id, country, api_id, api_hash):
    """Async Pyrogram login flow for adding accounts"""
    try:
        # Check if user is in login states
        if user_id not in login_states:
            return False, "Session expired"
        
        manager = PyrogramClientManager(api_id, api_hash)
        
        # Create client
        client = await manager.create_client()
        
        # Send code
        success, phone_code_hash, error = await manager.send_code(client, phone_number)
        
        if success:
            # Store client and state
            login_states[user_id].update({
                "client": client,
                "phone": phone_number,
                "phone_code_hash": phone_code_hash,
                "step": "waiting_otp",
                "manager": manager,
                "country": country
            })
            return True, "OTP sent successfully"
        else:
            await manager.safe_disconnect(client)
            return False, error or "Failed to send OTP"
            
    except Exception as e:
        logger.error(f"Pyrogram login error: {e}")
        return False, str(e)

async def verify_otp_and_save_async(login_states, accounts_col, user_id, otp_code):
    """Verify OTP and save account to database"""
    try:
        if user_id not in login_states:
            return False, "Session expired"
        
        state = login_states[user_id]
        
        if "client" not in state:
            return False, "Client not found"
        
        client = state["client"]
        manager = state.get("manager") or PyrogramClientManager(api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d")
        
        # Try to sign in with OTP
        success, status, error = await manager.sign_in_with_otp(
            client,
            state["phone"],
            state["phone_code_hash"],
            otp_code
        )
        
        if status == "password_required":
            # 2FA required
            login_states[user_id]["step"] = "waiting_password"
            return False, "password_required"
        
        if not success:
            await manager.safe_disconnect(client)
            login_states.pop(user_id, None)
            return False, error or "OTP verification failed"
        
        # Get session string
        session_string = await manager.get_session_string(client)
        
        if not session_string:
            await manager.safe_disconnect(client)
            login_states.pop(user_id, None)
            return False, "Failed to get session string"
        
        # Save account to database
        account_data = {
            "country": state["country"],
            "phone": state["phone"],
            "session_string": session_string,
            "has_2fa": False,
            "two_step_password": None,
            "status": "active",
            "used": False,
            "created_at": datetime.utcnow(),
            "created_by": user_id,
            "api_id": api_id if "api_id" in state else 6435225,
            "api_hash": api_hash if "api_hash" in state else "4e984ea35f854762dcde906dce426c2d"
        }
        
        # Insert account
        if accounts_col:
            accounts_col.insert_one(account_data)
        
        # Cleanup
        await manager.safe_disconnect(client)
        login_states.pop(user_id, None)
        
        return True, "Account added successfully"
            
    except Exception as e:
        logger.error(f"OTP verification error: {e}")
        if user_id in login_states and "client" in login_states[user_id]:
            manager = login_states[user_id].get("manager") or PyrogramClientManager(api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d")
            await manager.safe_disconnect(login_states[user_id]["client"])
        login_states.pop(user_id, None)
        return False, str(e)

async def verify_2fa_password_async(login_states, accounts_col, user_id, password):
    """Verify 2FA password and save account"""
    try:
        if user_id not in login_states:
            return False, "Session expired"
        
        state = login_states[user_id]
        
        if "client" not in state:
            return False, "Client not found"
        
        client = state["client"]
        manager = state.get("manager") or PyrogramClientManager(api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d")
        
        # Check password
        success, error = await manager.sign_in_with_password(client, password)
        
        if not success:
            await manager.safe_disconnect(client)
            return False, error
        
        # Get session string
        session_string = await manager.get_session_string(client)
        
        if not session_string:
            await manager.safe_disconnect(client)
            login_states.pop(user_id, None)
            return False, "Failed to get session string"
        
        # Save account to database
        account_data = {
            "country": state["country"],
            "phone": state["phone"],
            "session_string": session_string,
            "has_2fa": True,
            "two_step_password": password,
            "status": "active",
            "used": False,
            "created_at": datetime.utcnow(),
            "created_by": user_id,
            "api_id": api_id if "api_id" in state else 6435225,
            "api_hash": api_hash if "api_hash" in state else "4e984ea35f854762dcde906dce426c2d"
        }
        
        # Insert account
        if accounts_col:
            accounts_col.insert_one(account_data)
        
        # Cleanup
        await manager.safe_disconnect(client)
        login_states.pop(user_id, None)
        
        return True, "Account added successfully"
            
    except Exception as e:
        logger.error(f"2FA verification error: {e}")
        if user_id in login_states and "client" in login_states[user_id]:
            manager = login_states[user_id].get("manager") or PyrogramClientManager(api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d")
            await manager.safe_disconnect(login_states[user_id]["client"])
        login_states.pop(user_id, None)
        return False, str(e)

async def logout_session_async(session_id, user_id, otp_sessions_col, accounts_col, orders_col):
    """Logout from a specific Pyrogram session"""
    try:
        # Find the session
        session_data = otp_sessions_col.find_one({"session_id": session_id})
        if not session_data:
            return False, "Session not found"
        
        # Get session string from account
        account = accounts_col.find_one({"_id": session_data["account_id"]})
        if not account or not account.get("session_string"):
            return False, "Account not found"
        
        # Create client and logout
        manager = PyrogramClientManager(
            api_id=account.get("api_id", 6435225),
            api_hash=account.get("api_hash", "4e984ea35f854762dcde906dce426c2d")
        )
        client = await manager.create_client(account["session_string"])
        await client.connect()
        
        # Check if authorized
        if await client.is_user_authorized():
            await client.log_out()
            logger.info(f"User {user_id} logged out from session {session_id}")
        
        # Cleanup
        await manager.safe_disconnect(client)
        
        # Update session status
        otp_sessions_col.update_one(
            {"session_id": session_id},
            {"$set": {
                "status": "logged_out",
                "logged_out_at": datetime.utcnow(),
                "logged_out_by": user_id
            }}
        )
        
        # Update order status if exists
        orders_col.update_one(
            {"session_id": session_id},
            {"$set": {
                "status": "completed",
                "completed_at": datetime.utcnow(),
                "logged_out": True
            }}
        )
        
        return True, "Logged out successfully"
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return False, str(e)

# -----------------------
# OTP SEARCHER FUNCTIONS (SIMPLIFIED)
# -----------------------
async def otp_searcher(session_string, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
    """Search for OTP in Telegram messages - SIMPLIFIED VERSION"""
    try:
        manager = PyrogramClientManager(api_id, api_hash)
        client = await manager.create_client(session_string)
        
        otp_codes = []
        
        try:
            await client.connect()
            
            # SIMPLIFIED: Just try to get the last message
            try:
                # Get chat with Telegram
                async for message in client.get_chat_history("Telegram", limit=3):
                    if message.text:
                        # Look for OTP patterns
                        pattern = r'\b\d{5}\b'
                        matches = re.findall(pattern, message.text)
                        for match in matches:
                            if match not in otp_codes:
                                otp_codes.append(match)
                                logger.info(f"OTP found: {match}")
                                break  # Found one, good enough
                    if otp_codes:
                        break
            except Exception as e:
                logger.error(f"Error getting messages: {e}")
                
        except Exception as e:
            logger.error(f"Connection error: {e}")
        finally:
            await manager.safe_disconnect(client)
        
        return otp_codes
        
    except Exception as e:
        logger.error(f"OTP searcher error: {e}")
        return []

async def continuous_otp_monitor(session_string, user_id, phone, session_id, max_wait_time=1800, 
                                 api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d", 
                                 bot=None, otp_sessions_col=None):
    """Monitor for multiple OTPs for 30 minutes"""
    start_time = time.time()
    all_otps_found = []
    
    while time.time() - start_time < max_wait_time:
        try:
            # Check if session is still active
            if otp_sessions_col:
                session_data = otp_sessions_col.find_one({"session_id": session_id})
                if not session_data or session_data.get("status") == "completed":
                    logger.info(f"OTP monitoring stopped for {phone} - session completed")
                    break
                
            otp_codes = await otp_searcher(session_string, api_id, api_hash)
            
            # Send new OTPs to user
            new_otps = [otp for otp in otp_codes if otp not in all_otps_found]
            
            for otp_code in new_otps:
                all_otps_found.append(otp_code)
                logger.info(f"New OTP found for {phone}: {otp_code}")
                
                if bot:
                    try:
                        # Import inside function to avoid circular import
                        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
                        
                        markup = InlineKeyboardMarkup(row_width=2)
                        markup.add(
                            InlineKeyboardButton("✅ Complete Order", callback_data=f"complete_order_{session_id}"),
                            InlineKeyboardButton("🚪 Logout", callback_data=f"logout_session_{session_id}")
                        )
                        
                        bot.send_message(
                            user_id,
                            f"✅ **New OTP Received!**\n\n"
                            f"📱 Phone: `{phone}`\n"
                            f"🔢 OTP Code: `{otp_code}`\n\n"
                            f"Enter this code in Telegram X app.\n"
                            f"Click 'Complete Order' when done.",
                            parse_mode="Markdown",
                            reply_markup=markup
                        )
                        
                        # Update session with latest OTP
                        if otp_sessions_col:
                            otp_sessions_col.update_one(
                                {"session_id": session_id},
                                {"$set": {
                                    "status": "otp_delivered", 
                                    "otp_code": otp_code,
                                    "latest_otp_at": datetime.utcnow(),
                                    "total_otps_received": len(all_otps_found)
                                }}
                            )
                            
                    except Exception as e:
                        logger.error(f"Failed to send OTP message: {e}")
            
            # Wait 15 seconds before checking again (less frequent)
            await asyncio.sleep(15)
            
        except Exception as e:
            logger.error(f"OTP monitor error: {e}")
            await asyncio.sleep(15)
    
    return all_otps_found

# -----------------------
# SYNC WRAPPERS FOR ASYNC FUNCTIONS
# -----------------------
class AccountManager:
    """Main account manager class"""
    def __init__(self, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.async_manager = AsyncManager()
        self.pyrogram_manager = PyrogramClientManager(api_id, api_hash)
    
    def pyrogram_login_flow_sync(self, login_states, accounts_col, user_id, phone_number, chat_id, message_id, country):
        """Sync wrapper for async login flow"""
        try:
            return self.async_manager.run_async(
                pyrogram_login_flow_async(
                    login_states, accounts_col, user_id, phone_number, 
                    chat_id, message_id, country, self.api_id, self.api_hash
                )
            )
        except Exception as e:
            logger.error(f"Login flow error: {e}")
            return False, str(e)
    
    def verify_otp_and_save_sync(self, login_states, accounts_col, user_id, otp_code):
        """Sync wrapper for async OTP verification"""
        try:
            return self.async_manager.run_async(
                verify_otp_and_save_async(login_states, accounts_col, user_id, otp_code)
            )
        except Exception as e:
            logger.error(f"OTP verification error: {e}")
            return False, str(e)
    
    def verify_2fa_password_sync(self, login_states, accounts_col, user_id, password):
        """Sync wrapper for async 2FA verification"""
        try:
            return self.async_manager.run_async(
                verify_2fa_password_async(login_states, accounts_col, user_id, password)
            )
        except Exception as e:
            logger.error(f"2FA verification error: {e}")
            return False, str(e)
    
    def logout_session_sync(self, session_id, user_id, otp_sessions_col, accounts_col, orders_col):
        """Sync wrapper for async logout"""
        try:
            return self.async_manager.run_async(
                logout_session_async(session_id, user_id, otp_sessions_col, accounts_col, orders_col)
            )
        except Exception as e:
            logger.error(f"Logout error: {e}")
            return False, str(e)
    
    def start_otp_monitoring(self, session_string, user_id, phone, session_id, bot, otp_sessions_col, max_wait_time=1800):
        """Start OTP monitoring in background thread"""
        def monitor_wrapper():
            try:
                self.async_manager.run_async(
                    continuous_otp_monitor(
                        session_string, user_id, phone, session_id, max_wait_time,
                        self.api_id, self.api_hash, bot, otp_sessions_col
                    )
                )
            except Exception as e:
                logger.error(f"OTP monitoring thread error: {e}")
        
        # Start thread
        thread = threading.Thread(target=monitor_wrapper, daemon=True)
        thread.start()
        return thread

# Export everything
__all__ = [
    'AsyncManager',
    'PyrogramClientManager',
    'AccountManager',
    'otp_searcher',
    'continuous_otp_monitor'
]
