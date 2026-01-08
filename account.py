"""
Simple Account Management Module for OTP Bot
Avoids complex async issues
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

# -----------------------
# SIMPLE CLIENT MANAGER
# -----------------------
class SimpleClientManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
    
    def send_code_sync(self, phone_number):
        """Send verification code synchronously"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._send_code_async(phone_number))
        finally:
            loop.close()
    
    async def _send_code_async(self, phone_number):
        """Async version of send code"""
        try:
            async with Client(
                "temp_session", 
                self.api_id, 
                self.api_hash, 
                in_memory=True
            ) as app:
                sent_code = await app.send_code(phone_number)
                return {
                    "success": True,
                    "phone_code_hash": sent_code.phone_code_hash,
                    "phone": phone_number
                }
        except FloodWait as e:
            return {
                "success": False, 
                "error": f"Please wait {e.value} seconds before trying again"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def verify_otp_sync(self, phone, phone_code_hash, otp_code):
        """Verify OTP synchronously"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self._verify_otp_async(phone, phone_code_hash, otp_code)
            )
        finally:
            loop.close()
    
    async def _verify_otp_async(self, phone, phone_code_hash, otp_code):
        """Async version of verify OTP"""
        try:
            async with Client(
                "temp_session", 
                self.api_id, 
                self.api_hash, 
                in_memory=True
            ) as app:
                try:
                    # Try to sign in
                    await app.sign_in(phone, phone_code_hash, otp_code)
                    session_string = await app.export_session_string()
                    
                    return {
                        "success": True,
                        "session_string": session_string,
                        "has_2fa": False,
                        "password": None
                    }
                    
                except SessionPasswordNeeded:
                    return {
                        "success": False,
                        "password_required": True,
                        "error": "2FA password required"
                    }
                    
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def verify_password_sync(self, phone, phone_code_hash, otp_code, password):
        """Verify 2FA password synchronously"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self._verify_password_async(phone, phone_code_hash, otp_code, password)
            )
        finally:
            loop.close()
    
    async def _verify_password_async(self, phone, phone_code_hash, otp_code, password):
        """Async version of verify password"""
        try:
            async with Client(
                "temp_session", 
                self.api_id, 
                self.api_hash, 
                in_memory=True
            ) as app:
                # First sign in with OTP
                await app.sign_in(phone, phone_code_hash, otp_code)
                
                # Then check password
                await app.check_password(password)
                
                session_string = await app.export_session_string()
                
                return {
                    "success": True,
                    "session_string": session_string,
                    "has_2fa": True,
                    "password": password
                }
                
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def search_otp_sync(self, session_string):
        """Search for OTP synchronously"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self._search_otp_async(session_string)
            )
        finally:
            loop.close()
    
    async def _search_otp_async(self, session_string):
        """Search for OTP in messages"""
        try:
            async with Client(
                "otp_searcher",
                self.api_id,
                self.api_hash,
                session_string=session_string,
                in_memory=True
            ) as app:
                otp_codes = []
                
                try:
                    # Get recent messages from Telegram
                    async for message in app.get_chat_history("Telegram", limit=5):
                        if message.text:
                            # Look for 5-digit codes
                            matches = re.findall(r'\b\d{5}\b', message.text)
                            for match in matches:
                                if match not in otp_codes:
                                    otp_codes.append(match)
                                    logger.info(f"Found OTP: {match}")
                                    break  # Just need one
                        
                        if otp_codes:
                            break
                            
                except Exception as e:
                    logger.error(f"Error searching messages: {e}")
                
                return otp_codes
                
        except Exception as e:
            logger.error(f"Error in OTP search: {e}")
            return []

# -----------------------
# ACCOUNT MANAGER (MAIN CLASS)
# -----------------------
class AccountManager:
    def __init__(self, api_id=6435225, api_hash="4e984ea35f854762dcde906dce426c2d"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client_manager = SimpleClientManager(api_id, api_hash)
    
    def send_otp(self, phone_number):
        """Send OTP to phone number"""
        logger.info(f"Sending OTP to {phone_number}")
        
        # Validate phone number format
        if not re.match(r'^\+\d{10,15}$', phone_number):
            return False, "Invalid phone number format. Use: +919876543210"
        
        # Send code
        result = self.client_manager.send_code_sync(phone_number)
        
        if result["success"]:
            return True, {
                "phone": result["phone"],
                "phone_code_hash": result["phone_code_hash"]
            }
        else:
            return False, result.get("error", "Failed to send OTP")
    
    def verify_otp(self, phone, phone_code_hash, otp_code):
        """Verify OTP"""
        logger.info(f"Verifying OTP for {phone}")
        
        if not otp_code.isdigit() or len(otp_code) != 5:
            return False, "Invalid OTP format. Must be 5 digits"
        
        result = self.client_manager.verify_otp_sync(phone, phone_code_hash, otp_code)
        
        if result["success"]:
            return True, {
                "session_string": result["session_string"],
                "has_2fa": result["has_2fa"],
                "password": result["password"]
            }
        elif result.get("password_required"):
            return False, "password_required"
        else:
            return False, result.get("error", "OTP verification failed")
    
    def verify_password(self, phone, phone_code_hash, otp_code, password):
        """Verify 2FA password"""
        logger.info(f"Verifying 2FA password for {phone}")
        
        result = self.client_manager.verify_password_sync(
            phone, phone_code_hash, otp_code, password
        )
        
        if result["success"]:
            return True, {
                "session_string": result["session_string"],
                "has_2fa": result["has_2fa"],
                "password": result["password"]
            }
        else:
            return False, result.get("error", "Password verification failed")
    
    def search_otp(self, session_string):
        """Search for OTP in messages"""
        return self.client_manager.search_otp_sync(session_string)
    
    def start_otp_monitoring(self, session_string, user_id, phone, session_id, bot, otp_sessions_col, max_time=1800):
        """Start monitoring for OTP"""
        def monitor():
            start_time = time.time()
            found_otps = []
            
            while time.time() - start_time < max_time:
                try:
                    # Check if session is still active
                    if otp_sessions_col:
                        session_data = otp_sessions_col.find_one({"session_id": session_id})
                        if not session_data or session_data.get("status") == "completed":
                            logger.info(f"Monitoring stopped for {phone}")
                            break
                    
                    # Search for OTP
                    otp_codes = self.search_otp(session_string)
                    
                    # Check for new OTPs
                    for otp in otp_codes:
                        if otp not in found_otps:
                            found_otps.append(otp)
                            logger.info(f"New OTP found for {phone}: {otp}")
                            
                            # Send OTP to user
                            try:
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
                                    f"🔢 OTP Code: `{otp}`\n\n"
                                    f"Enter this code in Telegram X app.\n"
                                    f"Click 'Complete Order' when done.",
                                    parse_mode="Markdown",
                                    reply_markup=markup
                                )
                                
                                # Update database
                                if otp_sessions_col:
                                    otp_sessions_col.update_one(
                                        {"session_id": session_id},
                                        {"$set": {
                                            "status": "otp_delivered",
                                            "otp_code": otp,
                                            "latest_otp_at": datetime.utcnow(),
                                            "total_otps_received": len(found_otps)
                                        }}
                                    )
                                    
                            except Exception as e:
                                logger.error(f"Failed to send OTP message: {e}")
                    
                    # Wait before checking again
                    time.sleep(15)
                    
                except Exception as e:
                    logger.error(f"Monitoring error: {e}")
                    time.sleep(15)
        
        # Start monitoring thread
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        return thread

# Export the main class
__all__ = ['AccountManager']
