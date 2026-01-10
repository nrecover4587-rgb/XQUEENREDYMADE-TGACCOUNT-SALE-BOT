"""
Account Management Module for OTP Bot
Handles Pyrogram login, OTP verification, and session management
"""

import logging
import re
import threading
import time
import asyncio
from datetime import datetime
from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
    FloodWait, PhoneCodeEmpty
)

logger = logging.getLogger(__name__)

# Global event loop for async operations
_global_event_loop = None

def get_event_loop():
    """Get or create a global event loop"""
    global _global_event_loop
    if _global_event_loop is None:
        try:
            _global_event_loop = asyncio.get_running_loop()
        except RuntimeError:
            _global_event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_global_event_loop)
    return _global_event_loop

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
            loop = get_event_loop()
            # Check if we're in the event loop thread
            if loop.is_running():
                # Run in a new thread with its own event loop
                return self._run_in_thread(coro)
            else:
                # Run in current event loop
                return loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f"Async operation failed: {e}")
            raise
    
    def _run_in_thread(self, coro):
        """Run coroutine in a separate thread with its own event loop"""
        result = None
        exception = None
        
        def run():
            nonlocal result, exception
            try:
                # Create new event loop for this thread
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result = new_loop.run_until_complete(coro)
                new_loop.close()
            except Exception as e:
                exception = e
        
        # Run in thread
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        
        if exception:
            raise exception
        return result

# -----------------------
# PYROGRAM CLIENT MANAGER (FIXED)
# -----------------------
class PyrogramClientManager:
    """Fixed Pyrogram client management without ping issues"""
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.lock = threading.Lock()
    
    async def create_client(self, session_string=None, name=None):
        """Create a Pyrogram client with proper settings"""
        if name is None:
            name = f"client_{int(time.time())}"
        
        # Create client with settings to avoid ping issues
        client = Client(
            name=name,
            session_string=session_string,
            api_id=self.api_id,
            api_hash=self.api_hash,
            in_memory=True,
            no_updates=True,  # Disable updates
            takeout=False,    # Disable takeout
            sleep_threshold=0 # Disable automatic sleeping
        )
        return client
    
    async def send_code(self, client, phone_number):
        """Send verification code"""
        try:
            # Disconnect first if already connected
            if hasattr(client, 'is_connected') and client.is_connected:
                await self.safe_disconnect(client)
            
            await client.connect()
            sent_code = await client.send_code(phone_number)
            return True, sent_code.phone_code_hash, None
        except FloodWait as e:
            return False, None, f"FloodWait: Please wait {e.value} seconds"
        except Exception as e:
            return False, None, str(e)
    
    async def sign_in_with_otp(self, client, phone_number, phone_code_hash, otp_code):
        """Sign in with OTP"""
        try:
            # Ensure client is connected
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            await client.sign_in(
                phone_number=phone_number,
                phone_code=otp_code,
                phone_code_hash=phone_code_hash
            )
            return True, None, None
        except SessionPasswordNeeded:
            return False, "password_required", None
        except Exception as e:
            return False, "error", str(e)
    
    async def sign_in_with_password(self, client, password):
        """Sign in with 2FA password"""
        try:
            # Ensure client is connected
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            await client.check_password(password)
            return True, None
        except Exception as e:
            return False, str(e)
    
    async def get_session_string(self, client):
        """Get session string from authorized client"""
        try:
            # Ensure client is connected
            if not hasattr(client, 'is_connected') or not client.is_connected:
                await client.connect()
            
            # In Pyrogram v2, check authorization by getting "me"
            try:
                me = await client.get_me()
                if me:
                    session_string = await client.export_session_string()
                    return session_string
                else:
                    return None
            except Exception as e:
                logger.error(f"User not authorized or error getting me: {e}")
                return None
        except Exception as e:
            logger.error(f"Error getting session string: {e}")
            return None
    
    async def safe_disconnect(self, client):
        """Safely disconnect client without ping errors"""
        try:
            if client and hasattr(client, 'is_connected') and client.is_connected:
                # Stop session first to prevent ping errors
                if hasattr(client, 'session') and client.session:
                    try:
                        await client.session.stop()
                    except:
                        pass
                await client.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting client: {e}")
            # Ignore disconnection errors

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
                "country": country,
                "api_id": api_id,
                "api_hash": api_hash
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
        api_id = state.get("api_id", 6435225)
        api_hash = state.get("api_hash", "4e984ea35f854762dcde906dce426c2d")
        manager = state.get("manager") or PyrogramClientManager(api_id, api_hash)
        
        # Try to sign in with OTP
        success, status, error = await manager.sign_in_with_otp(
            client, state["phone"], state["phone_code_hash"], otp_code
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
            "api_id": api_id,
            "api_hash": api_hash
        }
        
        # Insert account - FIXED: Check if accounts_col is not None
        if accounts_col is not None:
            result = accounts_col.insert_one(account_data)
            logger.info(f"Account saved to database with ID: {result.inserted_id}")
        else:
            logger.error("accounts_col is None, cannot save account")
        
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
        api_id = state.get("api_id", 6435225)
        api_hash = state.get("api_hash", "4e984ea35f854762dcde906dce426c2d")
        manager = state.get("manager") or PyrogramClientManager(api_id, api_hash)
        
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
            "api_id": api_id,
            "api_hash": api_hash
        }
        
        # Insert account - FIXED: Check if accounts_col is not None
        if accounts_col is not None:
            result = accounts_col.insert_one(account_data)
            logger.info(f"2FA Account saved to database with ID: {result.inserted_id}")
        else:
            logger.error("accounts_col is None, cannot save 2FA account")
        
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

# -----------------------
# IMPROVED OTP SEARCHER FUNCTION
# -----------------------
async def otp_searcher(session_string, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d", last_message_id=None):
    """Search for LATEST OTP in Telegram messages - returns latest OTP only"""
    client = None
    try:
        # Create client with specific name
        client = Client(
            "otp_searcher_" + str(time.time()),
            session_string=session_string,
            api_id=int(api_id),
            api_hash=api_hash,
            in_memory=True,
            no_updates=True,
            sleep_threshold=0
        )
        
        await client.connect()
        latest_otp = None
        otp_time = None
        message_count = 0
        
        try:
            # Get last 30 messages from "Telegram" chat
            async for message in client.get_chat_history("Telegram", limit=30):
                message_count += 1
                if message.text and any(keyword in message.text.lower() for keyword in ["code", "login", "verification", "رمز", "تأكيد"]):
                    # Pattern for OTP codes
                    pattern = r'\b\d{5}\b'  # 5 digit codes
                    matches = re.findall(pattern, message.text)
                    for match in matches:
                        # Check if this is newer than previous OTP
                        if message.date:
                            current_time = message.date.timestamp()
                            if otp_time is None or current_time > otp_time:
                                otp_time = current_time
                                latest_otp = match
                                logger.info(f"Found OTP in message: {match} at {message.date}")
                        break  # First match is enough
                    if latest_otp:
                        break  # Found OTP, no need to continue
            
            # If not found in Telegram chat, check 777000
            if not latest_otp:
                async for message in client.get_chat_history(777000, limit=30):
                    if message.text and any(keyword in message.text.lower() for keyword in ["code", "login", "verification"]):
                        pattern = r'\b\d{5}\b'
                        matches = re.findall(pattern, message.text)
                        for match in matches:
                            if message.date:
                                current_time = message.date.timestamp()
                                if otp_time is None or current_time > otp_time:
                                    otp_time = current_time
                                    latest_otp = match
                                    logger.info(f"Found OTP from 777000: {match} at {message.date}")
                            break
                        if latest_otp:
                            break
        
        except Exception as e:
            logger.error(f"Error searching OTP in chat: {e}")
        
        # Safe disconnect
        if client:
            try:
                await client.disconnect()
            except:
                pass
        
        logger.info(f"OTP search completed. Messages checked: {message_count}, Found OTP: {latest_otp}")
        return latest_otp  # Return single latest OTP
    
    except Exception as e:
        logger.error(f"OTP searcher error: {e}")
        if client:
            try:
                await client.disconnect()
            except:
                pass
        return None

# -----------------------
# LOGOUT SESSION FUNCTION (FIXED)
# -----------------------
async def logout_session_async(session_id, user_id, otp_sessions_col, accounts_col, orders_col):
    """Logout from session and mark order as completed"""
    try:
        from bson import ObjectId
        
        # FIXED: Check if collections are not None
        if otp_sessions_col is None:
            return False, "otp_sessions_col is None"
        
        # Find session data
        session_data = otp_sessions_col.find_one({"session_id": session_id})
        if not session_data:
            return False, "Session not found"
        
        # Check if user owns this session
        if session_data.get("user_id") != user_id:
            return False, "Not authorized to logout this session"
        
        # Update session status
        otp_sessions_col.update_one(
            {"session_id": session_id},
            {"$set": {
                "status": "completed",
                "completed_at": datetime.utcnow(),
                "completed_by_user": True
            }}
        )
        
        # FIXED: Update order status only if orders_col is not None
        if orders_col is not None:
            orders_col.update_one(
                {"session_id": session_id},
                {"$set": {
                    "status": "completed",
                    "completed_at": datetime.utcnow(),
                    "user_completed": True
                }}
            )
        
        # FIXED: Mark account as used only if accounts_col is not None
        account_id = session_data.get("account_id")
        if account_id and accounts_col is not None:
            try:
                # mark account used
                accounts_col.update_one(
                    {"_id": ObjectId(account_id)},
                    {"$set": {"used": True, "used_at": datetime.utcnow()}}
                )
            except:
                pass
        
        # 🔥 REAL TELEGRAM LOGOUT (CPython / Telegram X remove)
        try:
            account = accounts_col.find_one({"_id": ObjectId(account_id)})
            if account and account.get("session_string"):
                tg_client = Client(
                    name=f"logout_{session_id}",
                    session_string=account["session_string"],
                    api_id=int(account.get("api_id", 6435225)),
                    api_hash=account.get("api_hash", "4e984ea35f854762dcde906dce426c2d"),
                    in_memory=True,
                    no_updates=True
                )
                await tg_client.connect()
                await tg_client.log_out()  # ✅ REAL LOGOUT
                await tg_client.disconnect()
                logger.info(f"Telegram account FORCE logged out for {account.get('phone')}")
        except Exception as e:
            logger.error(f"Telegram logout failed: {e}")
        
        logger.info(f"User {user_id} logged out from session {session_id}")
        return True, "Logged out successfully from Telegram"
    
    except Exception as e:
        logger.error(f"Logout error: {e}")
        return False, str(e)

# -----------------------
# GET LATEST OTP FUNCTION
# -----------------------
async def get_latest_otp_async(session_string, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
    """Get the latest OTP from session (for Get OTP button)"""
    try:
        logger.info(f"Getting latest OTP for session...")
        latest_otp = await otp_searcher(session_string, api_id, api_hash)
        return latest_otp
    except Exception as e:
        logger.error(f"Error getting latest OTP: {e}")
        return None

# -----------------------
# GET OTP FROM DATABASE FUNCTION (IMPORTANT)
# -----------------------
async def get_otp_from_database_async(session_id, otp_sessions_col):
    """Get OTP directly from database (fastest method for Get OTP button)"""
    try:
        if otp_sessions_col is None:
            logger.error("otp_sessions_col is None in get_otp_from_database_async")
            return None
        
        # Directly fetch from database
        session_data = otp_sessions_col.find_one({"session_id": session_id})
        if session_data and session_data.get("otp_code"):
            otp_code = session_data.get("otp_code")
            logger.info(f"OTP fetched from database for session {session_id}: {otp_code}")
            return otp_code
        else:
            logger.warning(f"No OTP found in database for session {session_id}")
            return None
    except Exception as e:
        logger.error(f"Error getting OTP from database: {e}")
        return None

# -----------------------
# SIMPLE OTP MONITORING (NON-AUTOMATIC)
# -----------------------
async def simple_otp_monitor(session_string, session_id, max_wait_time=1800, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
    """Simple OTP monitoring without automatic notifications"""
    start_time = time.time()
    
    logger.info(f"Simple OTP monitoring started for session {session_id}")
    while time.time() - start_time < max_wait_time:
        try:
            # Just keep the session alive, don't search for OTP automatically
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Simple monitor error: {e}")
            await asyncio.sleep(10)
    
    logger.info(f"Simple OTP monitoring ended for session {session_id}")
    return None

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
                    login_states, accounts_col, user_id, phone_number, chat_id, message_id, country, self.api_id, self.api_hash
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
    
    def get_latest_otp_sync(self, session_string):
        """Sync wrapper to get latest OTP from session"""
        try:
            return self.async_manager.run_async(
                get_latest_otp_async(session_string, self.api_id, self.api_hash)
            )
        except Exception as e:
            logger.error(f"Error getting latest OTP: {e}")
            return None
    
    def get_otp_from_database_sync(self, session_id, otp_sessions_col):
        """Sync wrapper to get OTP from database"""
        try:
            return self.async_manager.run_async(
                get_otp_from_database_async(session_id, otp_sessions_col)
            )
        except Exception as e:
            logger.error(f"Error getting OTP from database: {e}")
            return None
    
    def logout_session_sync(self, session_id, user_id, otp_sessions_col, accounts_col, orders_col):
        """Sync wrapper to logout session"""
        try:
            return self.async_manager.run_async(
                logout_session_async(session_id, user_id, otp_sessions_col, accounts_col, orders_col)
            )
        except Exception as e:
            logger.error(f"Logout error: {e}")
            return False, str(e)
    
    def start_simple_monitoring_sync(self, session_string, session_id, max_wait_time=1800):
        """Start simple monitoring (session keep-alive only)"""
        try:
            return self.async_manager.run_async(
                simple_otp_monitor(session_string, session_id, max_wait_time, self.api_id, self.api_hash)
            )
        except Exception as e:
            logger.error(f"Simple monitoring error: {e}")
            return None

# -----------------------
# EXPORT EVERYTHING
# -----------------------
__all__ = [
    'AsyncManager',
    'PyrogramClientManager',
    'AccountManager',
    'otp_searcher',
    'get_latest_otp_async',
    'get_otp_from_database_async',
    'logout_session_async',
    'simple_otp_monitor'
]
