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
            sleep_threshold=0  # Disable automatic sleeping
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
            "api_id": api_id,
            "api_hash": api_hash
        }
        
        # Insert account
        if accounts_col:
            result = accounts_col.insert_one(account_data)
            logger.info(f"Account saved to database with ID: {result.inserted_id}")
        
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
        
        # Insert account
        if accounts_col:
            result = accounts_col.insert_one(account_data)
            logger.info(f"2FA Account saved to database with ID: {result.inserted_id}")
        
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

# Export everything
__all__ = [
    'AsyncManager',
    'PyrogramClientManager',
    'AccountManager'
]
