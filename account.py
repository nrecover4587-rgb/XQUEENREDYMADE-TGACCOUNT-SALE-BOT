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
    PhoneCodeExpired, SessionPasswordNeeded,
    PasswordHashInvalid, FloodWait, PhoneCodeEmpty
)

logger = logging.getLogger(__name__)

# -----------------------
# GLOBAL EVENT LOOP
# -----------------------
_global_event_loop = None

def get_event_loop():
    global _global_event_loop
    if _global_event_loop is None:
        _global_event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_global_event_loop)
    return _global_event_loop


# -----------------------
# ASYNC MANAGER
# -----------------------
class AsyncManager:
    def run_async(self, coro):
        try:
            loop = get_event_loop()
            if loop.is_running():
                return asyncio.run_coroutine_threadsafe(coro, loop).result()
            return loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f"Async error: {e}")
            raise


# -----------------------
# PYROGRAM CLIENT MANAGER
# -----------------------
class PyrogramClientManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash

    async def create_client(self):
        return Client(
            name=f"client_{int(time.time())}",
            api_id=self.api_id,
            api_hash=self.api_hash,
            in_memory=True,
            no_updates=True
        )

    async def send_code(self, client, phone):
        try:
            await client.connect()
            sent = await client.send_code(phone)
            return True, sent.phone_code_hash, None
        except FloodWait as e:
            return False, None, f"FloodWait {e.value}s"
        except Exception as e:
            return False, None, str(e)

    async def sign_in_otp(self, client, phone, hash_, otp):
        try:
            await client.sign_in(phone, hash_, otp)
            return True, None
        except SessionPasswordNeeded:
            return False, "password"
        except Exception as e:
            return False, str(e)

    async def sign_in_password(self, client, password):
        try:
            await client.check_password(password)
            return True, None
        except Exception as e:
            return False, str(e)

    async def export_session(self, client):
        try:
            await client.get_me()
            return await client.export_session_string()
        except Exception:
            return None

    async def disconnect(self, client):
        try:
            await client.disconnect()
        except:
            pass


# -----------------------
# ASYNC LOGIN FLOW
# -----------------------
async def pyrogram_login_flow_async(login_states, accounts_col, user_id, phone, country, api_id, api_hash):
    manager = PyrogramClientManager(api_id, api_hash)
    client = await manager.create_client()

    ok, code_hash, err = await manager.send_code(client, phone)
    if not ok:
        return False, err

    login_states[user_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": code_hash,
        "country": country.title().strip(),  # 🔥 FIXED
        "step": "otp",
        "manager": manager
    }
    return True, "OTP_SENT"


async def verify_otp_and_save_async(login_states, accounts_col, user_id, otp):
    state = login_states.get(user_id)
    if not state:
        return False, "SESSION_EXPIRED"

    client = state["client"]
    manager = state["manager"]

    ok, err = await manager.sign_in_otp(
        client, state["phone"], state["phone_code_hash"], otp
    )
    if err == "password":
        state["step"] = "password"
        return False, "PASSWORD_REQUIRED"
    if not ok:
        await manager.disconnect(client)
        login_states.pop(user_id, None)
        return False, err

    session = await manager.export_session(client)
    if not session:
        await manager.disconnect(client)
        return False, "SESSION_FAIL"

    accounts_col.insert_one({
        "country": state["country"],          # 🔥 MATCHED
        "phone": state["phone"],
        "session_string": session,
        "has_2fa": False,
        "two_step_password": None,
        "status": "active",
        "used": False,
        "created_at": datetime.utcnow(),
        "created_by": user_id
    })

    await manager.disconnect(client)
    login_states.pop(user_id, None)
    return True, "ACCOUNT_ADDED"


async def verify_2fa_password_async(login_states, accounts_col, user_id, password):
    state = login_states.get(user_id)
    if not state:
        return False, "SESSION_EXPIRED"

    client = state["client"]
    manager = state["manager"]

    ok, err = await manager.sign_in_password(client, password)
    if not ok:
        return False, err

    session = await manager.export_session(client)
    if not session:
        return False, "SESSION_FAIL"

    accounts_col.insert_one({
        "country": state["country"],          # 🔥 MATCHED
        "phone": state["phone"],
        "session_string": session,
        "has_2fa": True,
        "two_step_password": password,
        "status": "active",
        "used": False,
        "created_at": datetime.utcnow(),
        "created_by": user_id
    })

    await manager.disconnect(client)
    login_states.pop(user_id, None)
    return True, "ACCOUNT_ADDED"


# -----------------------
# SYNC WRAPPER
# -----------------------
class AccountManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.async_manager = AsyncManager()

    def pyrogram_login_flow_sync(self, login_states, accounts_col, user_id, phone, country):
        return self.async_manager.run_async(
            pyrogram_login_flow_async(
                login_states, accounts_col, user_id,
                phone, country, self.api_id, self.api_hash
            )
        )

    def verify_otp_and_save_sync(self, login_states, accounts_col, user_id, otp):
        return self.async_manager.run_async(
            verify_otp_and_save_async(login_states, accounts_col, user_id, otp)
        )

    def verify_2fa_password_sync(self, login_states, accounts_col, user_id, password):
        return self.async_manager.run_async(
            verify_2fa_password_async(login_states, accounts_col, user_id, password)
        )


__all__ = ["AccountManager"]
