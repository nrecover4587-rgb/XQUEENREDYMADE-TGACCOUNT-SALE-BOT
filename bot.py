import logging
import re
import threading
import time
import random
from datetime import datetime, timedelta
from bson import ObjectId
import asyncio
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from pymongo import MongoClient
import os
import requests
from pyrogram import Client
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
    FloodWait, PhoneCodeEmpty
)

# -----------------------
# CONFIG
# -----------------------
BOT_TOKEN = os.getenv('BOT_TOKEN', '7802838001:AAHhK3IohejoIOmOI5Skf2C5JrvmKGYfnFs')
ADMIN_ID = int(os.getenv('ADMIN_ID', '7582601826'))
MONGO_URL = os.getenv('MONGO_URL', 'mongodb+srv://teamdaxx123:teamdaxx123@cluster0.ysbpgcp.mongodb.net/?retryWrites=true&w=majority')
API_ID = int(os.getenv('API_ID', '30038466'))
API_HASH = os.getenv('API_HASH', '5a492a0dfb22b1a0b7caacbf90cbf96e')

# IMB Gateway Config
IMB_API_URL = "https://pay.imb.org.in/api/create-order"
IMB_CHECK_STATUS_URL = "https://pay.imb.org.in/api/check-order-status"
IMB_API_TOKEN = ""

# Referral commission percentage
REFERRAL_COMMISSION = 1.5  # 1.5% per recharge

# Global API Credentials for Pyrogram Login
GLOBAL_API_ID = 6435225
GLOBAL_API_HASH = "4e984ea35f854762dcde906dce426c2d"

# -----------------------
# INIT
# -----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN)

# MongoDB Setup
try:
    client = MongoClient(MONGO_URL)
    db = client['otp_bot']
    users_col = db['users']
    accounts_col = db['accounts']
    orders_col = db['orders']
    wallets_col = db['wallets']
    recharges_col = db['recharges']
    otp_sessions_col = db['otp_sessions']
    referrals_col = db['referrals']
    countries_col = db['countries']
    banned_users_col = db['banned_users']
    transactions_col = db['transactions']
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")

# Store temporary data
user_states = {}
pending_messages = {}
active_chats = {}
user_stage = {}
user_last_message = {}
user_orders = {}
order_messages = {}
cancellation_trackers = {}
order_timers = {}
change_number_requests = {}
whatsapp_number_timers = {}
payment_orders = {}
admin_deduct_state = {}
referral_data = {}

# Pyrogram login states
login_states = {}  # Format: {user_id: {"step": "phone", "client": client_obj, ...}}

# Import account management
try:
    from account import AccountManager
    account_manager = AccountManager(GLOBAL_API_ID, GLOBAL_API_HASH)
    logger.info("✅ Account manager loaded successfully")
except ImportError as e:
    logger.error(f"❌ Failed to load account module: {e}")
    account_manager = None

# Async manager for background tasks
async_manager = None
if account_manager:
    async_manager = account_manager.async_manager

# -----------------------
# UTILITY FUNCTIONS
# -----------------------
def ensure_user_exists(user_id, user_name=None, username=None, referred_by=None):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user_data = {
            "user_id": user_id,
            "name": user_name or "Unknown",
            "username": username,
            "referred_by": referred_by,
            "referral_code": f"REF{user_id}",
            "total_commission_earned": 0.0,
            "total_referrals": 0,
            "created_at": datetime.utcnow()
        }
        users_col.insert_one(user_data)
        
        # If referred by someone, record the referral
        if referred_by:
            referral_record = {
                "referrer_id": referred_by,
                "referred_id": user_id,
                "referral_code": user_data['referral_code'],
                "status": "pending",
                "created_at": datetime.utcnow()
            }
            referrals_col.insert_one(referral_record)
            
            # Update referrer's total referrals count
            users_col.update_one(
                {"user_id": referred_by},
                {"$inc": {"total_referrals": 1}}
            )
            
            logger.info(f"Referral recorded: {referred_by} -> {user_id}")
    
    wallets_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "balance": 0.0}},
        upsert=True
    )

def get_balance(user_id):
    rec = wallets_col.find_one({"user_id": user_id})
    return float(rec.get("balance", 0.0)) if rec else 0.0

def add_balance(user_id, amount):
    wallets_col.update_one({"user_id": user_id}, {"$inc": {"balance": float(amount)}}, upsert=True)

def deduct_balance(user_id, amount):
    wallets_col.update_one({"user_id": user_id}, {"$inc": {"balance": -float(amount)}}, upsert=True)

def format_currency(x):
    try:
        x = float(x)
        if x.is_integer():
            return f"₹{int(x)}"
        return f"₹{x:.2f}"
    except:
        return "₹0"

def get_available_accounts_count(country):
    return accounts_col.count_documents({"country": country, "status": "active", "used": False})

def is_admin(user_id):
    """Check if user is admin"""
    try:
        return str(user_id) == str(ADMIN_ID)
    except:
        return False

def is_user_banned(user_id):
    """Check if user is banned"""
    banned = banned_users_col.find_one({"user_id": user_id, "status": "active"})
    return banned is not None

def get_all_countries():
    """Get all active countries"""
    return list(countries_col.find({"status": "active"}))

def get_country_by_name(country_name):
    return countries_col.find_one({
        "name": {"$regex": f"^{country_name}$", "$options": "i"},
        "status": "active"
    })

def add_referral_commission(referrer_id, recharge_amount, recharge_id):
    """Add commission to referrer when referred user recharges"""
    try:
        commission = (recharge_amount * REFERRAL_COMMISSION) / 100
        
        # Add commission to referrer's balance
        add_balance(referrer_id, commission)
        
        # Record transaction
        transaction_id = f"COM{referrer_id}{int(time.time())}"
        transaction_record = {
            "transaction_id": transaction_id,
            "user_id": referrer_id,
            "amount": commission,
            "type": "referral_commission",
            "description": f"Referral commission from recharge #{recharge_id}",
            "timestamp": datetime.utcnow(),
            "recharge_id": str(recharge_id)
        }
        transactions_col.insert_one(transaction_record)
        
        # Update user's total commission
        users_col.update_one(
            {"user_id": referrer_id},
            {"$inc": {"total_commission_earned": commission}}
        )
        
        # Update referral status
        referrals_col.update_one(
            {"referred_id": recharge_id.get("user_id"), "referrer_id": referrer_id},
            {"$set": {"status": "completed", "commission": commission, "completed_at": datetime.utcnow()}}
        )
        
        # Notify referrer
        try:
            bot.send_message(
                referrer_id,
                f"💰 **Referral Commission Earned!**\n\n"
                f"✅ You earned {format_currency(commission)} commission!\n"
                f"📊 From: {format_currency(recharge_amount)} recharge\n"
                f"📈 Commission Rate: {REFERRAL_COMMISSION}%\n"
                f"💳 New Balance: {format_currency(get_balance(referrer_id))}\n\n"
                f"Keep referring to earn more! 🎉"
            )
        except:
            pass
            
        logger.info(f"Referral commission added: {referrer_id} - {format_currency(commission)}")
        
    except Exception as e:
        logger.error(f"Error adding referral commission: {e}")

# -----------------------
# IMB PAYMENT FUNCTIONS
# -----------------------
def create_imb_payment_order(user_id, amount):
    """
    Create payment order via IMB Gateway
    """
    try:
        # Unique order ID
        order_id = f"BOT{user_id}{int(time.time())}"
        
        # Default mobile number (required by IMB)
        customer_mobile = "9999999999"
        
        payload = {
            "customer_mobile": customer_mobile,
            "user_token": IMB_API_TOKEN,
            "amount": str(amount),
            "order_id": order_id,
            "redirect_url": "https://t.me/QUEEN_X_OTP_BOT",
            "remark1": f"user{user_id}@gmail.com",
            "remark2": f"XQueen Wallet recharge for user {user_id}"
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        response = requests.post(IMB_API_URL, data=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") == True:
                result = data.get("result", {})
                return {
                    "success": True,
                    "order_id": order_id,
                    "payment_url": result.get("payment_url"),
                    "message": "Payment order created successfully"
                }
            else:
                return {"success": False, "error": data.get("message", "Order creation failed")}
        else:
            return {"success": False, "error": f"API Error: {response.status_code}"}
            
    except Exception as e:
        return {"success": False, "error": f"Payment creation failed: {str(e)}"}

def check_imb_payment_status(order_id):
    """
    Check payment status via IMB Gateway
    """
    try:
        payload = {
            "user_token": IMB_API_TOKEN,
            "order_id": order_id
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        response = requests.post(IMB_CHECK_STATUS_URL, data=payload, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("status") == "COMPLETED":
                return {
                    "success": True,
                    "status": "completed",
                    "utr": data.get("result", {}).get("utr"),
                    "amount": data.get("result", {}).get("amount"),
                    "message": "Payment completed successfully"
                }
            elif data.get("status") == "ERROR":
                return {"success": False, "status": "failed", "error": data.get("message")}
            else:
                return {"success": False, "status": "pending", "error": "Payment still processing"}
        else:
            return {"success": False, "error": f"API Error: {response.status_code}"}
            
    except Exception as e:
        return {"success": False, "error": f"Status check failed: {str(e)}"}

# -----------------------
# BACKGROUND PAYMENT CHECKER
# -----------------------
def check_pending_payments():
    """
    Background thread to check pending payments
    """
    while True:
        try:
            for user_id, data in list(pending_messages.items()):
                if user_stage.get(user_id) == "waiting_payment":
                    order_id = data.get("order_id")
                    if order_id:
                        status_result = check_imb_payment_status(order_id)
                        
                        if status_result["success"] and status_result["status"] == "completed":
                            amount = data.get("recharge_amount", 0)
                            
                            if amount > 0:
                                add_balance(user_id, amount)
                                
                                # Save to database
                                recharge_doc = {
                                    "user_id": user_id,
                                    "amount": amount,
                                    "order_id": order_id,
                                    "utr": status_result.get("utr", ""),
                                    "status": "approved",
                                    "verified_at": datetime.utcnow(),
                                    "method": "imb_auto"
                                }
                                recharge_id = recharges_col.insert_one(recharge_doc).inserted_id
                                
                                # Check for referral and add commission
                                user_data = users_col.find_one({"user_id": user_id})
                                if user_data and user_data.get("referred_by"):
                                    add_referral_commission(user_data["referred_by"], amount, recharge_doc)
                                
                                # Delete payment message
                                payment_msg_id = data.get("payment_msg_id")
                                if payment_msg_id:
                                    try:
                                        bot.delete_message(user_id, payment_msg_id)
                                    except:
                                        pass
                                
                                # Success message with buy button
                                kb = InlineKeyboardMarkup()
                                kb.add(InlineKeyboardButton("🛒 Buy Account Now", callback_data="buy_account"))
                                
                                bot.send_message(
                                    user_id,
                                    f"✅ **Payment Successful!**\n\n"
                                    f"• Amount: {format_currency(amount)} added to wallet\n"
                                    f"• UTR: `{status_result.get('utr', 'N/A')}`\n"
                                    f"• New Balance: {format_currency(get_balance(user_id))}",
                                    parse_mode="Markdown",
                                    reply_markup=kb
                                )
                                
                                # Cleanup
                                user_stage[user_id] = "done"
                                pending_messages.pop(user_id, None)
            
            time.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            logger.error(f"Payment checker error: {e}")
            time.sleep(60)

# -----------------------
# BOT HANDLERS
# -----------------------
@bot.message_handler(commands=['start'])
def start(msg):
    user_id = msg.from_user.id
    logger.info(f"Start command from user {user_id}")
    
    # Check if user is banned
    if is_user_banned(user_id):
        bot.send_message(
            user_id,
            "🚫 **Account Banned**\n\n"
            "Your account has been banned from using this bot.\n"
            "Contact admin @anmol144 for assistance."
        )
        return
    
    # Check for referral parameter
    referred_by = None
    if len(msg.text.split()) > 1:
        referral_code = msg.text.split()[1]
        if referral_code.startswith('REF'):
            try:
                referrer_id = int(referral_code[3:])
                # Verify referrer exists
                referrer = users_col.find_one({"user_id": referrer_id})
                if referrer:
                    referred_by = referrer_id
                    logger.info(f"Referral detected: {referrer_id} -> {user_id}")
            except:
                pass
    
    ensure_user_exists(user_id, msg.from_user.first_name, msg.from_user.username, referred_by)
    
    # Send welcome message with image
    try:
        bot.send_photo(
            user_id,
            "https://files.catbox.moe/7s0nqh.jpg",
            caption=(
                "🥂 <b>Welcome To Otp Bot By Xqueen</b> 🥂\n"
                "<blockquote expandable>\n"
                "- Automatic OTPs 📍\n"
                "- Easy to Use 🥂🥂\n"
                "- 24/7 Support 👨‍🔧\n"
                "- Instant Payment Approvals 🧾\n"
                "</blockquote>\n"
                "<blockquote expandable>\n"
                "🚀 <b>How to use Bot :</b>\n"
                "1️⃣ Recharge\n"
                "2️⃣ Select Country\n"
                "3️⃣ Buy Account\n"
                "4️⃣ Get Number & Login through Telegram X\n"
                "5️⃣ Receive OTP & You're Done ✅\n"
                "</blockquote>\n"
                "🚀 <b>Enjoy Fast Account Buying Experience!</b>"
            ),
            parse_mode="HTML"
        )
    except:
        # If image fails, send text only
        bot.send_message(
            user_id,
            "🥂 <b>Welcome To Otp Bot By Xqueen</b> 🥂\n\n"
            "• Automatic OTPs 📍\n"
            "• Easy to Use 🥂🥂\n"
            "• 24/7 Support 👨‍🔧\n"
            "• Instant Payment Approvals 🧾\n\n"
            "🚀 <b>Enjoy Fast Account Buying Experience!</b>",
            parse_mode="HTML"
        )
    
    show_main_menu(msg.chat.id)

def show_main_menu(chat_id):
    user_id = chat_id
    
    # Check if user is banned
    if is_user_banned(user_id):
        bot.send_message(
            user_id,
            "🚫 **Account Banned**\n\n"
            "Your account has been banned from using this bot.\n"
            "Contact admin @anmol144 for assistance."
        )
        return
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🛒 Buy Account", callback_data="buy_account"),
        InlineKeyboardButton("💰 Balance", callback_data="balance")
    )
    markup.add(
        InlineKeyboardButton("💳 Recharge", callback_data="recharge"),
        InlineKeyboardButton("📦 Your Orders", callback_data="my_orders")
    )
    markup.add(
        InlineKeyboardButton("👥 Refer Friends", callback_data="refer_friends"),
        InlineKeyboardButton("🛠️ Support", callback_data="support")
    )
    
    if is_admin(user_id):
        markup.add(InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel"))
    
    bot.send_message(
        chat_id,
        "🤖 **Welcome to OTP Bot**\n\n"
        "• Buy Telegram accounts instantly\n"
        "• Auto OTP delivery\n"
        "• Multiple countries available\n"
        "• 24/7 Support\n"
        f"• Refer & Earn {REFERRAL_COMMISSION}% commission!\n\n"
        "Select an option:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    
    # Check if user is banned
    if is_user_banned(user_id):
        bot.answer_callback_query(call.id, "🚫 Your account is banned", show_alert=True)
        return
    
    logger.info(f"Callback received: {data} from user {user_id}")
    
    try:
        if data == "buy_account":
            show_countries(call.message.chat.id, call.message.message_id)
        
        elif data == "balance":
            balance = get_balance(user_id)
            user_data = users_col.find_one({"user_id": user_id}) or {}
            commission_earned = user_data.get("total_commission_earned", 0)
            
            message = f"💰 **Your Balance:** {format_currency(balance)}\n\n"
            message += f"📊 **Referral Stats:**\n"
            message += f"• Total Commission Earned: {format_currency(commission_earned)}\n"
            message += f"• Total Referrals: {user_data.get('total_referrals', 0)}\n"
            message += f"• Commission Rate: {REFERRAL_COMMISSION}%\n\n"
            message += f"Your Referral Code: `{user_data.get('referral_code', 'REF' + str(user_id))}`"
            
            bot.answer_callback_query(call.id, f"💰 Balance: {format_currency(balance)}", show_alert=False)
            bot.send_message(call.message.chat.id, message, parse_mode="Markdown")
        
        elif data == "recharge":
            show_recharge_options(call.message.chat.id, call.message.message_id)
        
        elif data == "my_orders":
            show_my_orders(user_id, call.message.chat.id)
        
        elif data == "refer_friends":
            show_referral_info(user_id, call.message.chat.id)
        
        elif data == "support":
            bot.send_message(call.message.chat.id, "🛠️ Support: @anmol144")
        
        elif data == "admin_panel":
            if is_admin(user_id):
                show_admin_panel(call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        # Direct account purchase when clicking country
        elif data.startswith("country_"):
            # Extract country name from callback
            country_part = data.replace("country_", "")
            # Remove "_accounts" if present
            if "_accounts" in country_part:
                country_name = country_part.replace("_accounts", "").replace("_", " ").title()
            else:
                country_name = country_part.replace("_", " ").title()
            
            # Directly process purchase for this country
            direct_purchase_from_country(user_id, country_name, call.message.chat.id, call.message.message_id, call.id)
        
        elif data.startswith("buy_"):
            account_id = data.split("_", 1)[1]
            process_purchase(user_id, account_id, call.message.chat.id, call.message.message_id, call.id)
        
        elif data.startswith("complete_order_"):
            session_id = data.split("_", 2)[2]
            complete_order(user_id, session_id, call.message.chat.id, call.id)
        
        elif data.startswith("logout_session_"):
            session_id = data.split("_", 2)[2]
            handle_logout_session(user_id, session_id, call.message.chat.id, call.id)
        
        elif data.startswith("get_otp_"):
            session_id = data.split("_", 2)[2]
            get_latest_otp(user_id, session_id, call.message.chat.id, call.id)
        
        elif data == "back_to_countries":
            show_countries(call.message.chat.id, call.message.message_id)
        
        elif data == "back_to_menu":
            show_main_menu(call.message.chat.id)
        
        elif data == "recharge_manual":
            bot.send_message(call.message.chat.id, "💳 Enter recharge amount (minimum ₹10):")
            bot.register_next_step_handler(call.message, process_recharge_amount_manual)
        
        elif data == "recharge_auto":
            # Automatic recharge option
            bot.send_message(call.message.chat.id, "💳 Enter recharge amount (minimum ₹10):")
            bot.register_next_step_handler(call.message, process_recharge_amount_auto)
        
        elif data.startswith("approve_rech_"):
            if is_admin(user_id):
                recharge_id = data.split("_", 2)[2]
                approve_recharge(recharge_id, call.message.chat.id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data.startswith("reject_rech_"):
            if is_admin(user_id):
                recharge_id = data.split("_", 2)[2]
                reject_recharge(recharge_id, call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        # NEW: Add account via Pyrogram login
        elif data == "add_account":
            logger.info(f"Add account button clicked by user {user_id}")
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
                return
            
            # Start new Pyrogram login flow
            login_states[user_id] = {
                "step": "select_country",
                "message_id": call.message.message_id,
                "chat_id": call.message.chat.id
            }
            
            # Show country selection
            countries = get_all_countries()
            
            if not countries:
                bot.answer_callback_query(call.id, "❌ No countries available. Add a country first.", show_alert=True)
                return
            
            markup = InlineKeyboardMarkup(row_width=2)
            for country in countries:
                markup.add(InlineKeyboardButton(
                    country['name'],
                    callback_data=f"login_country_{country['name']}"
                ))
            markup.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel_login"))
            
            bot.edit_message_text(
                "🌍 **Select Country for Account**\n\nChoose country:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        
        elif data.startswith("login_country_"):
            handle_login_country_selection(call)
        
        elif data == "cancel_login":
            handle_cancel_login(call)
        
        elif data == "view_recharges":
            if is_admin(user_id):
                show_pending_recharges(call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "out_of_stock":
            bot.answer_callback_query(call.id, "❌ Out of Stock! No accounts available.", show_alert=True)
        
        # ADMIN FEATURES
        elif data == "broadcast_menu":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                bot.send_message(call.message.chat.id, "📢 Reply to a message (or send one) to broadcast to all users. Then send /sendbroadcast")
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "refund_start":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                msg = bot.send_message(call.message.chat.id, "💸 Enter user ID for refund:")
                bot.register_next_step_handler(msg, ask_refund_user)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "ranking":
            if is_admin(user_id):
                bot.answer_callback_query(call.id, "📊 Generating ranking...")
                show_user_ranking(call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "message_user":
            if is_admin(user_id):
                bot.answer_callback_query(call.id, "👤 Enter user ID to send message:")
                msg = bot.send_message(call.message.chat.id, "👤 Enter user ID to send message:")
                bot.register_next_step_handler(msg, ask_message_content)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "admin_deduct_start":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                # Admin balance deduction process
                admin_deduct_state[user_id] = {"step": "ask_user_id"}
                bot.send_message(call.message.chat.id, "👤 Enter User ID whose balance you want to deduct:")
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "ban_user":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                msg = bot.send_message(call.message.chat.id, "🚫 Enter User ID to ban:")
                bot.register_next_step_handler(msg, ask_ban_user)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "unban_user":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                msg = bot.send_message(call.message.chat.id, "✅ Enter User ID to unban:")
                bot.register_next_step_handler(msg, ask_unban_user)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "manage_countries":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                show_country_management(call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "add_country":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                msg = bot.send_message(call.message.chat.id, "🌍 Enter country name to add:")
                bot.register_next_step_handler(msg, ask_country_name)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data == "remove_country":
            if is_admin(user_id):
                bot.answer_callback_query(call.id)
                show_country_removal(call.message.chat.id)
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data.startswith("remove_country_"):
            if is_admin(user_id):
                country_name = data.split("_", 2)[2]
                remove_country(country_name, call.message.chat.id)
                bot.answer_callback_query(call.id, f"Removing {country_name}...")
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        elif data.startswith("approve_rech|") or data.startswith("cancel_rech|"):
            # Manual recharge approval
            if is_admin(user_id):
                parts = data.split("|")
                action = parts[0]
                req_id = parts[1] if len(parts) > 1 else None
                req = recharges_col.find_one({"req_id": req_id}) if req_id else None
                if not req:
                    bot.answer_callback_query(call.id, "❌ Request not found", show_alert=True)
                    bot.send_message(call.message.chat.id, "⚠️ Recharge request not found or already processed.")
                    return

                user_target = req.get("user_id")
                amount = float(req.get("amount", 0))

                if action == "approve_rech":
                    add_balance(user_target, amount)
                    recharges_col.update_one({"req_id": req_id}, {"$set": {"status": "approved", "processed_at": datetime.utcnow(), "processed_by": ADMIN_ID}})
                    bot.answer_callback_query(call.id, "✅ Recharge approved", show_alert=True)
                    
                    # Check for referral commission
                    user_data = users_col.find_one({"user_id": user_target})
                    if user_data and user_data.get("referred_by"):
                        add_referral_commission(user_data["referred_by"], amount, req)
                    
                    kb = InlineKeyboardMarkup()
                    kb.add(InlineKeyboardButton("🛒 Buy Account Now", callback_data="buy_account"))
                    
                    bot.send_message(
                        user_target, 
                        f"✅ Your recharge of {format_currency(amount)} has been approved and added to your wallet.\n\n💰 <b>New Balance: {format_currency(get_balance(user_target))}</b>\n\nClick below to buy accounts:", 
                        parse_mode="HTML", 
                        reply_markup=kb
                    )
                    
                    bot.send_message(call.message.chat.id, f"✅ Recharge approved and {format_currency(amount)} added to user {user_target}.")
                    
                    try:
                        bot.delete_message(call.message.chat.id, call.message.message_id)
                    except Exception as e:
                        print(f"Could not delete message: {e}")
                        
                else:
                    recharges_col.update_one({"req_id": req_id}, {"$set": {"status": "cancelled", "processed_at": datetime.utcnow(), "processed_by": ADMIN_ID}})
                    bot.answer_callback_query(call.id, "❌ Recharge cancelled", show_alert=True)
                    bot.send_message(user_target, f"❌ Your recharge of {format_currency(amount)} was not received.")
                    bot.send_message(call.message.chat.id, f"❌ Recharge cancelled for user {user_target}.")
                    
                    try:
                        bot.delete_message(call.message.chat.id, call.message.message_id)
                    except Exception as e:
                        print(f"Could not delete message: {e}")
            else:
                bot.answer_callback_query(call.id, "❌ Unauthorized", show_alert=True)
        
        else:
            bot.answer_callback_query(call.id, "❌ Unknown action", show_alert=True)
                
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Error occurred", show_alert=True)
            if is_admin(user_id):
                bot.send_message(call.message.chat.id, f"Callback handler error:\n{e}")
        except:
            pass

def direct_purchase_from_country(user_id, country_name, chat_id, message_id, callback_id):
    """Directly purchase account when clicking country"""
    try:
        # Get country details
        country = get_country_by_name(country_name)
        if not country:
            bot.answer_callback_query(callback_id, "❌ Country not found", show_alert=True)
            return
        
        # Get all available accounts for this country
        accounts = list(accounts_col.find({
            "country": country_name,
            "status": "active", 
            "used": False
        }))
        
        if not accounts:
            bot.answer_callback_query(callback_id, "❌ No accounts available for this country", show_alert=True)
            return
        
        # Select a random account
        account = random.choice(accounts)
        
        # Check user balance
        balance = get_balance(user_id)
        price = country['price']
        
        if balance < price:
            needed = price - balance
            bot.answer_callback_query(
                callback_id, 
                f"❌ Insufficient balance!\nNeed: {format_currency(price)}\nHave: {format_currency(balance)}\nRequired: {format_currency(needed)} more", 
                show_alert=True
            )
            return
        
        # Process purchase
        process_purchase(user_id, str(account['_id']), chat_id, message_id, callback_id)
        
    except Exception as e:
        logger.error(f"Direct purchase error: {e}")
        bot.answer_callback_query(callback_id, "❌ Error processing purchase", show_alert=True)

def handle_login_country_selection(call):
    user_id = call.from_user.id
    
    if user_id not in login_states:
        bot.answer_callback_query(call.id, "❌ Session expired", show_alert=True)
        return
    
    country_name = call.data.replace("login_country_", "")
    login_states[user_id]["country"] = country_name
    login_states[user_id]["step"] = "phone"
    
    bot.edit_message_text(
        f"🌍 Country: {country_name}\n\n"
        "📱 Enter phone number with country code:\n"
        "Example: +919876543210",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_login")
        )
    )

def handle_cancel_login(call):
    user_id = call.from_user.id
    
    # Cleanup any active client
    if user_id in login_states:
        state = login_states[user_id]
        if "client" in state:
            try:
                # Cleanup client
                if account_manager and account_manager.pyrogram_manager:
                    import asyncio
                    asyncio.run(account_manager.pyrogram_manager.safe_disconnect(state["client"]))
            except:
                pass
    
    login_states.pop(user_id, None)
    
    bot.edit_message_text(
        "❌ Login cancelled.",
        call.message.chat.id,
        call.message.message_id
    )
    show_admin_panel(call.message.chat.id)

def handle_logout_session(user_id, session_id, chat_id, callback_id):
    """Handle user logout from session"""
    try:
        if not account_manager:
            bot.answer_callback_query(callback_id, "❌ Account module not loaded", show_alert=True)
            return
        
        success, message = account_manager.logout_session_sync(
            session_id, user_id, otp_sessions_col, accounts_col, orders_col
        )
        
        if success:
            bot.answer_callback_query(callback_id, "✅ Logged out successfully!", show_alert=True)
            bot.send_message(
                chat_id,
                "🚪 **Logged Out Successfully!**\n\n"
                "You have been logged out from this session.\n"
                "Order marked as completed.\n\n"
                "Thank you for using our service! 👋"
            )
        else:
            bot.answer_callback_query(callback_id, f"❌ {message}", show_alert=True)
            
    except Exception as e:
        logger.error(f"Logout handler error: {e}")
        bot.answer_callback_query(callback_id, "❌ Error logging out", show_alert=True)

def get_latest_otp(user_id, session_id, chat_id, callback_id):
    """Get the latest OTP for a session - SHOWS ONLY WHEN CLICKED"""
    try:
        # Find the session
        session_data = otp_sessions_col.find_one({"session_id": session_id})
        if not session_data:
            bot.answer_callback_query(callback_id, "❌ Session not found", show_alert=True)
            return
        
        # Check if OTP already exists in database
        existing_otp = session_data.get("last_otp")
        
        if existing_otp:
            # OTP already in database, show it
            otp_code = existing_otp
            logger.info(f"Using existing OTP from database: {otp_code}")
        else:
            # Try to get latest OTP from session
            bot.answer_callback_query(callback_id, "🔍 Searching for OTP...", show_alert=False)
            
            session_string = session_data.get("session_string")
            if not session_string:
                bot.answer_callback_query(callback_id, "❌ No session string found", show_alert=True)
                return
            
            otp_code = account_manager.get_latest_otp_sync(session_string)
            
            if not otp_code:
                bot.answer_callback_query(callback_id, "❌ No OTP received yet", show_alert=True)
                return
            
            # Save to database
            otp_sessions_col.update_one(
                {"session_id": session_id},
                {"$set": {
                    "has_otp": True,
                    "last_otp": otp_code,
                    "last_otp_time": datetime.utcnow(),
                    "status": "otp_received"
                }}
            )
        
        # Get account details for 2FA password
        account_id = session_data.get("account_id")
        account = None
        two_step_password = ""
        
        if account_id:
            try:
                account = accounts_col.find_one({"_id": ObjectId(account_id)})
                if account:
                    two_step_password = account.get("two_step_password", "")
            except:
                pass
        
        # Check if we should show "Get OTP Again" or "Get OTP"
        has_otp = session_data.get("has_otp", False)
        button_text = "🔄 Get OTP Again" if has_otp else "🔢 Get OTP"
        
        # Create message
        message = f"✅ **Latest OTP**\n\n"
        message += f"📱 Phone: `{session_data.get('phone', 'N/A')}`\n"
        message += f"🔢 OTP Code: `{otp_code}`\n"
        
        if two_step_password:
            message += f"🔐 2FA Password: `{two_step_password}`\n"
        elif account and account.get("two_step_password"):
            message += f"🔐 2FA Password: `{account.get('two_step_password')}`\n"
        
        message += f"\n⏰ Time: {datetime.utcnow().strftime('%H:%M:%S')}"
        message += f"\n\nEnter this code in Telegram X app."
        
        # Create inline keyboard
        markup = InlineKeyboardMarkup(row_width=2)
        
        if has_otp:
            # If OTP already received, show both buttons
            markup.add(
                InlineKeyboardButton("🔄 Get OTP Again", callback_data=f"get_otp_{session_id}"),
                InlineKeyboardButton("🚪 Logout", callback_data=f"logout_session_{session_id}")
            )
        else:
            # First time OTP, show only Get OTP button
            markup.add(InlineKeyboardButton("🔢 Get OTP", callback_data=f"get_otp_{session_id}"))
        
        bot.send_message(
            chat_id,
            message,
            parse_mode="Markdown",
            reply_markup=markup
        )
        
        bot.answer_callback_query(callback_id, "✅ OTP sent!", show_alert=False)
        
    except Exception as e:
        logger.error(f"Get OTP error: {e}")
        bot.answer_callback_query(callback_id, "❌ Error getting OTP", show_alert=True)

# -----------------------
# MESSAGE HANDLER FOR LOGIN FLOW
# -----------------------
@bot.message_handler(func=lambda m: login_states.get(m.from_user.id, {}).get("step") in ["phone", "waiting_otp", "waiting_password"])
def handle_login_flow_messages(msg):
    user_id = msg.from_user.id
    
    if user_id not in login_states:
        return
    
    state = login_states[user_id]
    step = state["step"]
    chat_id = state["chat_id"]
    message_id = state["message_id"]
    
    if step == "phone":
        # Process phone number
        phone = msg.text.strip()
        
        if not re.match(r'^\+\d{10,15}$', phone):
            bot.send_message(chat_id, "❌ Invalid phone number format. Please enter with country code:\nExample: +919876543210")
            return
        
        # Check if account manager is loaded
        if not account_manager:
            bot.edit_message_text(
                "❌ Account module not loaded. Please contact admin.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)
            return
        
        # Start Pyrogram login flow using account manager
        try:
            success, message = account_manager.pyrogram_login_flow_sync(
                login_states, accounts_col, user_id, phone, chat_id, message_id, state["country"]
            )
            
            if success:
                bot.edit_message_text(
                    f"📱 Phone: {phone}\n\n"
                    "📩 OTP sent! Enter the OTP you received:",
                    chat_id,
                    message_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel_login")
                    )
                )
            else:
                bot.edit_message_text(
                    f"❌ Failed to send OTP: {message}\n\nPlease try again.",
                    chat_id,
                    message_id
                )
                login_states.pop(user_id, None)
        except Exception as e:
            logger.error(f"Login flow error: {e}")
            bot.edit_message_text(
                f"❌ Error: {str(e)}\n\nPlease try again.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)
    
    elif step == "waiting_otp":
        # Process OTP
        otp = msg.text.strip()
        
        if not otp.isdigit() or len(otp) != 5:
            bot.send_message(chat_id, "❌ Invalid OTP format. Please enter 5-digit OTP:")
            return
        
        # Check if account manager is loaded
        if not account_manager:
            bot.edit_message_text(
                "❌ Account module not loaded. Please contact admin.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)
            return
        
        try:
            success, message = account_manager.verify_otp_and_save_sync(
                login_states, accounts_col, user_id, otp
            )
            
            if success:
                # Account added successfully
                country = state["country"]
                phone = state["phone"]
                
                bot.edit_message_text(
                    f"✅ **Account Added Successfully!**\n\n"
                    f"🌍 Country: {country}\n"
                    f"📱 Phone: {phone}\n"
                    f"🔐 Session: Generated\n\n"
                    f"Account is now available for purchase!",
                    chat_id,
                    message_id
                )
                
                # Cleanup
                login_states.pop(user_id, None)
                
            elif message == "password_required":
                # 2FA required
                bot.edit_message_text(
                    f"📱 Phone: {state['phone']}\n\n"
                    "🔐 2FA Password required!\n"
                    "Enter your 2-step verification password:",
                    chat_id,
                    message_id,
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel_login")
                    )
                )
            else:
                bot.edit_message_text(
                    f"❌ OTP verification failed: {message}\n\nPlease try again.",
                    chat_id,
                    message_id
                )
                login_states.pop(user_id, None)
        except Exception as e:
            logger.error(f"OTP verification error: {e}")
            bot.edit_message_text(
                f"❌ Error: {str(e)}\n\nPlease try again.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)
    
    elif step == "waiting_password":
        # Process 2FA password
        password = msg.text.strip()
        
        if not password:
            bot.send_message(chat_id, "❌ Password cannot be empty. Enter 2FA password:")
            return
        
        # Check if account manager is loaded
        if not account_manager:
            bot.edit_message_text(
                "❌ Account module not loaded. Please contact admin.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)
            return
        
        try:
            success, message = account_manager.verify_2fa_password_sync(
                login_states, accounts_col, user_id, password
            )
            
            if success:
                # Account added successfully with 2FA
                country = state["country"]
                phone = state["phone"]
                
                bot.edit_message_text(
                    f"✅ **Account Added Successfully!**\n\n"
                    f"🌍 Country: {country}\n"
                    f"📱 Phone: {phone}\n"
                    f"🔐 2FA: Enabled\n"
                    f"🔐 Session: Generated\n\n"
                    f"Account is now available for purchase!",
                    chat_id,
                    message_id
                )
                
                # Cleanup
                login_states.pop(user_id, None)
            else:
                bot.edit_message_text(
                    f"❌ 2FA password failed: {message}\n\nPlease try again.",
                    chat_id,
                    message_id
                )
                login_states.pop(user_id, None)
        except Exception as e:
            logger.error(f"2FA verification error: {e}")
            bot.edit_message_text(
                f"❌ Error: {str(e)}\n\nPlease try again.",
                chat_id,
                message_id
            )
            login_states.pop(user_id, None)

# -----------------------
# REFERRAL SYSTEM FUNCTIONS
# -----------------------
def show_referral_info(user_id, chat_id):
    """Show referral information and stats"""
    user_data = users_col.find_one({"user_id": user_id}) or {}
    referral_code = user_data.get('referral_code', f'REF{user_id}')
    total_commission = user_data.get('total_commission_earned', 0)
    total_referrals = user_data.get('total_referrals', 0)
    
    referral_link = f"https://t.me/{bot.get_me().username}?start={referral_code}"
    
    message = f"👥 **Refer & Earn {REFERRAL_COMMISSION}% Commission!**\n\n"
    message += f"📊 **Your Stats:**\n"
    message += f"• Total Referrals: {total_referrals}\n"
    message += f"• Total Commission Earned: {format_currency(total_commission)}\n"
    message += f"• Commission Rate: {REFERRAL_COMMISSION}% per recharge\n\n"
    message += f"🔗 **Your Referral Link:**\n`{referral_link}`\n\n"
    message += f"📝 **How it works:**\n"
    message += f"1. Share your referral link with friends\n"
    message += f"2. When they join using your link\n"
    message += f"3. You earn {REFERRAL_COMMISSION}% of EVERY recharge they make!\n"
    message += f"4. Commission credited instantly\n\n"
    message += f"💰 **Example:** If a friend recharges ₹1000, you earn ₹{1000 * REFERRAL_COMMISSION / 100}!\n\n"
    message += f"Start sharing and earning today! 🎉"
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={referral_link}&text=Join%20this%20awesome%20OTP%20bot%20to%20buy%20Telegram%20accounts!"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu"))
    
    bot.send_message(chat_id, message, parse_mode="Markdown", reply_markup=markup)

# -----------------------
# ADMIN MANAGEMENT FUNCTIONS
# -----------------------
def show_admin_panel(chat_id):
    user_id = chat_id
    
    if not is_admin(user_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
    
    total_accounts = accounts_col.count_documents({})
    active_accounts = accounts_col.count_documents({"status": "active", "used": False})
    total_users = users_col.count_documents({})
    total_orders = orders_col.count_documents({})
    pending_recharges = recharges_col.count_documents({"status": "pending"})
    banned_users = banned_users_col.count_documents({"status": "active"})
    active_countries = countries_col.count_documents({"status": "active"})
    
    text = (
        f"👑 **Admin Panel**\n\n"
        f"📊 **Statistics:**\n"
        f"• Total Accounts: {total_accounts}\n"
        f"• Active Accounts: {active_accounts}\n"
        f"• Total Users: {total_users}\n"
        f"• Total Orders: {total_orders}\n"
        f"• Pending Recharges: {pending_recharges}\n"
        f"• Banned Users: {banned_users}\n"
        f"• Active Countries: {active_countries}\n\n"
        f"🛠️ **Management Tools:**"
    )
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Account", callback_data="add_account"),
        InlineKeyboardButton("📊 View Recharges", callback_data="view_recharges")
    )
    markup.add(
        InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_menu"),
        InlineKeyboardButton("💸 Refund", callback_data="refund_start")
    )
    markup.add(
        InlineKeyboardButton("📊 Ranking", callback_data="ranking"),
        InlineKeyboardButton("💬 Message User", callback_data="message_user")
    )
    markup.add(
        InlineKeyboardButton("💳 Deduct Balance", callback_data="admin_deduct_start"),
        InlineKeyboardButton("🚫 Ban User", callback_data="ban_user")
    )
    markup.add(
        InlineKeyboardButton("✅ Unban User", callback_data="unban_user"),
        InlineKeyboardButton("🌍 Manage Countries", callback_data="manage_countries")
    )
    
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

def show_country_management(chat_id):
    """Show country management options"""
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
    
    countries = get_all_countries()
    
    if not countries:
        text = "🌍 **Country Management**\n\nNo countries available. Add a country first."
    else:
        text = "🌍 **Country Management**\n\n**Available Countries:**\n"
        for country in countries:
            accounts_count = get_available_accounts_count(country['name'])
            text += f"• {country['name']} - Price: {format_currency(country['price'])} - Accounts: {accounts_count}\n"
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Country", callback_data="add_country"),
        InlineKeyboardButton("➖ Remove Country", callback_data="remove_country")
    )
    markup.add(InlineKeyboardButton("⬅️ Back to Admin", callback_data="admin_panel"))
    
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

def ask_country_name(message):
    """Ask for country name to add"""
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Unauthorized access")
        return
    
    country_name = message.text.strip()
    user_states[message.chat.id] = {
        "step": "ask_country_price",
        "country_name": country_name
    }
    
    bot.send_message(message.chat.id, f"💰 Enter price for {country_name}:")

@bot.message_handler(func=lambda message: user_states.get(message.chat.id, {}).get("step") == "ask_country_price")
def ask_country_price(message):
    """Ask for country price"""
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Unauthorized access")
        return
    
    try:
        price = float(message.text.strip())
        user_data = user_states.get(message.chat.id)
        country_name = user_data.get("country_name")
        
        # Add country to database
        country_data = {
            "name": country_name,
            "price": price,
            "status": "active",
            "created_at": datetime.utcnow(),
            "created_by": message.from_user.id
        }
        countries_col.insert_one(country_data)
        
        del user_states[message.chat.id]
        
        bot.send_message(
            message.chat.id,
            f"✅ **Country Added Successfully!**\n\n"
            f"🌍 Country: {country_name}\n"
            f"💰 Price: {format_currency(price)}\n\n"
            f"Country is now available for users to purchase accounts."
        )
        
        show_country_management(message.chat.id)
        
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid price. Please enter a number:")

def show_country_removal(chat_id):
    """Show countries for removal"""
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
    
    countries = get_all_countries()
    
    if not countries:
        bot.send_message(chat_id, "❌ No countries available to remove.")
        return
    
    markup = InlineKeyboardMarkup(row_width=2)
    for country in countries:
        markup.add(InlineKeyboardButton(
            f"❌ {country['name']}",
            callback_data=f"remove_country_{country['name']}"
        ))
    
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="manage_countries"))
    
    bot.send_message(
        chat_id,
        "🗑️ **Remove Country**\n\nSelect a country to remove:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

def remove_country(country_name, chat_id):
    """Remove a country from the system"""
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
    
    # Mark country as inactive
    countries_col.update_one(
        {"name": country_name},
        {"$set": {"status": "inactive", "removed_at": datetime.utcnow()}}
    )
    
    bot.send_message(chat_id, f"✅ Country '{country_name}' has been removed.")
    show_country_management(chat_id)

def ask_ban_user(message):
    """Ask for user ID to ban"""
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Unauthorized access")
        return
    
    try:
        user_id_to_ban = int(message.text.strip())
        
        # Check if user exists
        user = users_col.find_one({"user_id": user_id_to_ban})
        if not user:
            bot.send_message(message.chat.id, "❌ User not found in database.")
            return
        
        # Check if already banned
        already_banned = banned_users_col.find_one({"user_id": user_id_to_ban, "status": "active"})
        if already_banned:
            bot.send_message(message.chat.id, "⚠️ User is already banned.")
            return
        
        # Ban the user
        ban_record = {
            "user_id": user_id_to_ban,
            "banned_by": message.from_user.id,
            "reason": "Admin banned",
            "status": "active",
            "banned_at": datetime.utcnow()
        }
        banned_users_col.insert_one(ban_record)
        
        bot.send_message(message.chat.id, f"✅ User {user_id_to_ban} has been banned.")
        
        # Notify user
        try:
            bot.send_message(
                user_id_to_ban,
                "🚫 **Your Account Has Been Banned**\n\n"
                "You have been banned from using this bot.\n"
                "Contact admin @anmol144 if you believe this is a mistake."
            )
        except:
            pass
        
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid user ID. Please enter numeric ID only.")

def ask_unban_user(message):
    """Ask for user ID to unban"""
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Unauthorized access")
        return
    
    try:
        user_id_to_unban = int(message.text.strip())
        
        # Check if user is banned
        ban_record = banned_users_col.find_one({"user_id": user_id_to_unban, "status": "active"})
        if not ban_record:
            bot.send_message(message.chat.id, "⚠️ User is not banned.")
            return
        
        # Unban the user
        banned_users_col.update_one(
            {"user_id": user_id_to_unban, "status": "active"},
            {"$set": {"status": "unbanned", "unbanned_at": datetime.utcnow(), "unbanned_by": message.from_user.id}}
        )
        
        bot.send_message(message.chat.id, f"✅ User {user_id_to_unban} has been unbanned.")
        
        # Notify user
        try:
            bot.send_message(
                user_id_to_unban,
                "✅ **Your Account Has Been Unbanned**\n\n"
                "Your account access has been restored.\n"
                "You can now use the bot normally."
            )
        except:
            pass
        
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid user ID. Please enter numeric ID only.")

def show_user_ranking(chat_id):
    """Show user ranking by balance"""
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
    
    try:
        # Get all wallet records and join with users
        users_ranking = []
        all_wallets = wallets_col.find()
        
        for wallet in all_wallets:
            user_id_rank = wallet.get("user_id")
            balance = float(wallet.get("balance", 0))
            
            # Only include users with balance > 0
            if balance > 0:
                # Get user details
                user = users_col.find_one({"user_id": user_id_rank}) or {}
                name = user.get("name", "Unknown")
                username_db = user.get("username")
                
                users_ranking.append({
                    "user_id": user_id_rank,
                    "balance": balance,
                    "name": name,
                    "username": username_db
                })
        
        # Sort by balance (highest first)
        users_ranking.sort(key=lambda x: x["balance"], reverse=True)
        
        # Create ranking message
        ranking_text = "📊 **User Ranking by Wallet Balance**\n\n"
        
        if not users_ranking:
            ranking_text = "📊 No users found with balance greater than zero."
        else:
            for index, user_data in enumerate(users_ranking[:20], 1):  # Show top 20
                user_link = f"<a href='tg://user?id={user_data['user_id']}'>{user_data['user_id']}</a>"
                username_display = f"@{user_data['username']}" if user_data['username'] else "No Username"
                ranking_text += f"{index}. {user_link} - {username_display}\n"
                ranking_text += f"   💰 Balance: {format_currency(user_data['balance'])}\n\n"
        
        # Send ranking message
        bot.send_message(chat_id, ranking_text, parse_mode="HTML")
        
    except Exception as e:
        logger.exception("Error in ranking:")
        bot.send_message(chat_id, f"❌ Error generating ranking: {str(e)}")

# -----------------------
# FUNCTIONS FROM FIRST CODE
# -----------------------
def ask_refund_user(message):
    try:
        refund_user_id = int(message.text)
        msg = bot.send_message(message.chat.id, "💰 Enter refund amount:")
        bot.register_next_step_handler(msg, process_refund, refund_user_id)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid user ID. Please enter numeric ID only.")

def process_refund(message, refund_user_id):
    try:
        amount = float(message.text)
        user = users_col.find_one({"user_id": refund_user_id})

        if not user:
            bot.send_message(message.chat.id, "⚠️ User not found in database.")
            return

        add_balance(refund_user_id, amount)
        new_balance = get_balance(refund_user_id)

        bot.send_message(message.chat.id, f"✅ Refunded {format_currency(amount)} to user {refund_user_id}\n💰 New Balance: {format_currency(new_balance)}")

        try:
            bot.send_message(refund_user_id, f"💸 {format_currency(amount)} refunded to your wallet!\n💰 New Balance: {format_currency(new_balance)} ✅")
        except Exception:
            bot.send_message(message.chat.id, "⚠️ Could not DM the user (maybe blocked).")

    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid amount entered. Please enter a number.")
    except Exception as e:
        logger.exception("Error in process_refund:")
        bot.send_message(message.chat.id, f"Error processing refund: {e}")

def ask_message_content(msg):
    try:
        target_user_id = int(msg.text)
        # Check if user exists
        user_exists = users_col.find_one({"user_id": target_user_id})
        if not user_exists:
            bot.send_message(msg.chat.id, "❌ User not found in database.")
            return
        
        bot.send_message(msg.chat.id, f"💬 Now send the message (text, photo, video, or document) for user {target_user_id}:")
        bot.register_next_step_handler(msg, process_user_message, target_user_id)
    except ValueError:
        bot.send_message(msg.chat.id, "❌ Invalid user ID. Please enter numeric ID only.")

def process_user_message(msg, target_user_id):
    try:
        # Get message content
        text = getattr(msg, "text", None) or getattr(msg, "caption", "") or ""
        is_photo = bool(getattr(msg, "photo", None))
        is_video = getattr(msg, "video", None) is not None
        is_document = getattr(msg, "document", None) is not None
        
        # Send message to target user
        try:
            if is_photo and getattr(msg, "photo", None):
                bot.send_photo(target_user_id, photo=msg.photo[-1].file_id, caption=text or "")
            elif is_video and getattr(msg, "video", None):
                bot.send_video(target_user_id, video=msg.video.file_id, caption=text or "")
            elif is_document and getattr(msg, "document", None):
                bot.send_document(target_user_id, document=msg.document.file_id, caption=text or "")
            else:
                bot.send_message(target_user_id, f"💌 Message from Admin:\n{text}")
            
            bot.send_message(msg.chat.id, f"✅ Message sent successfully to user {target_user_id}")
        except Exception as e:
            bot.send_message(msg.chat.id, f"❌ Failed to send message to user {target_user_id}. User may have blocked the bot.")
            
    except Exception as e:
        logger.exception("Error in process_user_message:")
        bot.send_message(msg.chat.id, f"Error sending message: {e}")

def process_broadcast(msg):
    if not is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "❌ Unauthorized.")
        return
    source = msg.reply_to_message if msg.reply_to_message else msg
    text = getattr(source, "text", None) or getattr(source, "caption", "") or ""
    is_photo = bool(getattr(source, "photo", None))
    is_video = getattr(source, "video", None) is not None
    is_document = getattr(source, "document", None) is not None
    bot.send_message(msg.chat.id, "📡 Broadcasting started... Please wait.")
    threading.Thread(target=broadcast_thread, args=(source, text, is_photo, is_video, is_document)).start()

def broadcast_thread(source_msg, text, is_photo, is_video, is_document):
    users = list(users_col.find())
    total = len(users)
    sent = 0
    failed = 0
    progress_interval = 25
    for user in users:
        uid = user.get("user_id")
        if not uid or uid == ADMIN_ID:
            continue
        try:
            if is_photo and getattr(source_msg, "photo", None):
                bot.send_photo(uid, photo=source_msg.photo[-1].file_id, caption=text or "")
            elif is_video and getattr(source_msg, "video", None):
                bot.send_video(uid, video=source_msg.video.file_id, caption=text or "")
            elif is_document and getattr(source_msg, "document", None):
                bot.send_document(uid, document=source_msg.document.file_id, caption=text or "")
            else:
                bot.send_message(uid, f"📢 Broadcast:\n{text}")
            sent += 1
            if sent % progress_interval == 0:
                try:
                    bot.send_message(ADMIN_ID, f"✅ Sent {sent}/{total} users...")
                except Exception:
                    pass
            time.sleep(0.06)
        except Exception as e:
            failed += 1
            print(f"❌ Broadcast failed for {uid}: {e}")
    try:
        bot.send_message(ADMIN_ID, f"🎯 Broadcast completed!\n✅ Sent: {sent}\n❌ Failed: {failed}\n👥 Total: {total}")
    except Exception:
        pass

# -----------------------
# COUNTRY SELECTION FUNCTIONS
# -----------------------
def show_countries(chat_id, message_id=None):
    countries = get_all_countries()
    
    if not countries:
        text = "🌍 **Select Country**\n\n❌ No countries available right now. Please check back later."
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu"))
        
        if message_id:
            try:
                bot.edit_message_text(
                    text,
                    chat_id,
                    message_id,
                    reply_markup=markup,
                    parse_mode="Markdown"
                )
            except Exception:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        return
    
    text = "🌍 **Select Country**\n\nChoose your country:"
    markup = InlineKeyboardMarkup(row_width=2)
    
    for country in countries:
        count = get_available_accounts_count(country['name'])
        markup.add(InlineKeyboardButton(
            f"{country['name']} ({count}) - {format_currency(country['price'])}",
            callback_data=f"country_{country['name'].lower().replace(' ', '_')}"
        ))
    
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu"))
    
    if message_id:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error editing message: {e}")
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

# -----------------------
# RECHARGE FUNCTIONS
# -----------------------
def show_recharge_options(chat_id, message_id):
    text = "💳 **Recharge Options**\n\nChoose payment method:"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🤖 Automatic", callback_data="recharge_auto"),
        InlineKeyboardButton("👨‍💻 Manual", callback_data="recharge_manual")
    )
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu"))
    
    if message_id:
        try:
            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        except Exception:
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

def process_recharge_amount_auto(msg):
    """Process automatic recharge amount"""
    try:
        amount = float(msg.text)
        if amount < 10:
            bot.send_message(msg.chat.id, "❌ Minimum recharge is ₹10. Enter amount again:")
            bot.register_next_step_handler(msg, process_recharge_amount_auto)
            return
        
        user_id = msg.from_user.id
        
        # IMB payment order create
        creating_msg = bot.send_message(msg.chat.id, "🔄 Creating payment link, please wait...")
        
        payment_result = create_imb_payment_order(user_id, amount)

        # Delete creating message
        try:
            bot.delete_message(msg.chat.id, creating_msg.message_id)
        except:
            pass
        
        if payment_result["success"]:
            # Store payment details
            pending_messages[user_id] = {
                "recharge_amount": amount,
                "order_id": payment_result["order_id"],
                "payment_url": payment_result["payment_url"]
            }
            user_stage[user_id] = "waiting_payment"
            
            # Send payment link to user - ONLY PAY BUTTON
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("💳 Pay Now", url=payment_result["payment_url"]))
            
            payment_msg = bot.send_message(
                msg.chat.id,
                f"💰 **Payment Details:**\n"
                f"• Amount: {format_currency(amount)}\n"
                f"• Order ID: `{payment_result['order_id']}`\n\n"
                f"**Instructions:**\n"
                f"1. Click 'Pay Now' button to complete payment\n"
                f"2. Payment will auto verify within 2 minutes\n"
                f"3. Wallet will update automatically\n\n"
                f"Click below to complete payment:",
                parse_mode="Markdown",
                reply_markup=kb
            )
            
            # Store payment message ID for deletion
            pending_messages[user_id]["payment_msg_id"] = payment_msg.message_id
            
        else:
            bot.send_message(msg.chat.id, f"❌ {payment_result['error']}\n\nPlease try manual payment method.")
            user_stage[user_id] = "done"
        
    except ValueError:
        bot.send_message(msg.chat.id, "❌ Invalid amount. Enter numbers only:")
        bot.register_next_step_handler(msg, process_recharge_amount_auto)

def process_recharge_amount_manual(msg):
    """Process manual recharge amount"""
    try:
        amount = float(msg.text)
        if amount < 10:
            bot.send_message(msg.chat.id, "❌ Minimum recharge is ₹10. Enter amount again:")
            bot.register_next_step_handler(msg, process_recharge_amount_manual)
            return
        
        user_id = msg.from_user.id
        recharge_data = {
            "user_id": user_id,
            "amount": amount,
            "status": "pending",
            "created_at": datetime.utcnow(),
            "method": "manual"
        }
        recharge_id = recharges_col.insert_one(recharge_data).inserted_id
        
        bot.send_message(
            msg.chat.id,
            f"💳 **Payment Details**\n\n"
            f"Amount: {format_currency(amount)}\n"
            f"UPI ID: `amit.singh903@paytm`\n"
            f"Name: Amit Singh\n\n"
            f"**Instructions:**\n"
            f"1. Send {format_currency(amount)} to above UPI\n"
            f"2. Take screenshot of payment\n"
            f"3. Send screenshot here\n\n"
            f"Reference ID: `{recharge_id}`",
            parse_mode="Markdown"
        )
        
        bot.send_message(
            ADMIN_ID,
            f"🔄 New Recharge Request\n"
            f"User: {user_id}\n"
            f"Amount: {format_currency(amount)}\n"
            f"ID: `{recharge_id}`\n\n"
            f"Waiting for payment proof..."
        )
        
    except ValueError:
        bot.send_message(msg.chat.id, "❌ Invalid amount. Enter numbers only:")
        bot.register_next_step_handler(msg, process_recharge_amount_manual)

@bot.message_handler(content_types=['photo'])
def handle_payment_screenshot(msg):
    user_id = msg.from_user.id
    pending_recharge = recharges_col.find_one({"user_id": user_id, "status": "pending", "method": "manual"})
    
    if pending_recharge:
        recharge_id = pending_recharge['_id']
        amount = pending_recharge['amount']
        
        recharges_col.update_one(
            {"_id": recharge_id},
            {"$set": {"screenshot": msg.photo[-1].file_id, "submitted_at": datetime.utcnow()}}
        )
        
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_rech_{recharge_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_rech_{recharge_id}")
        )
        
        bot.send_photo(
            ADMIN_ID,
            msg.photo[-1].file_id,
            caption=f"📸 Payment Proof Received\n\n"
                   f"User: {user_id}\n"
                   f"Amount: {format_currency(amount)}\n"
                   f"Recharge ID: `{recharge_id}`",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        
        bot.send_message(msg.chat.id, "✅ Payment proof received! Waiting for admin approval...")
    else:
        bot.send_message(msg.chat.id, "❌ No pending recharge found. Use /start to recharge.")

# -----------------------
# PROCESS PURCHASE FUNCTION (UPDATED)
# -----------------------
def process_purchase(user_id, account_id, chat_id, message_id, callback_id):
    try:
        try:
            account = accounts_col.find_one({"_id": ObjectId(account_id)})
        except Exception:
            account = accounts_col.find_one({"_id": account_id})
        if not account:
            bot.answer_callback_query(callback_id, "❌ Account not available", show_alert=True)
            return
        
        if account.get('used', False):
            bot.answer_callback_query(callback_id, "❌ Account already sold out", show_alert=True)
            # Go back to country selection
            show_countries(chat_id, message_id)
            return
        
        # Get country price
        country = get_country_by_name(account['country'])
        if not country:
            bot.answer_callback_query(callback_id, "❌ Country not found", show_alert=True)
            return
        
        price = country['price']
        
        balance = get_balance(user_id)
        
        if balance < price:
            needed = price - balance
            bot.answer_callback_query(
                callback_id, 
                f"❌ Insufficient balance!\nNeed: {format_currency(price)}\nHave: {format_currency(balance)}\nRequired: {format_currency(needed)} more", 
                show_alert=True
            )
            return
        
        deduct_balance(user_id, price)
        
        # Create OTP session for this purchase
        session_id = f"otp_{user_id}_{int(time.time())}"
        
        otp_session = {
            "session_id": session_id,
            "user_id": user_id,
            "phone": account['phone'],
            "session_string": account.get('session_string', ''),
            "status": "active",
            "created_at": datetime.utcnow(),
            "account_id": str(account['_id']),
            "has_otp": False,  # Start with False, becomes True when OTP received
            "last_otp": None,
            "last_otp_time": None
        }
        
        otp_sessions_col.insert_one(otp_session)
        
        # Create order
        order = {
            "user_id": user_id,
            "account_id": str(account.get('_id')),
            "country": account['country'],
            "price": price,
            "phone_number": account.get('phone', 'N/A'),
            "session_id": session_id,
            "status": "waiting_otp",
            "created_at": datetime.utcnow(),
            "monitoring_duration": 1800
        }
        order_id = orders_col.insert_one(order).inserted_id
        
        # Mark account as used
        try:
            accounts_col.update_one({"_id": account.get('_id')}, {"$set": {"used": True, "used_at": datetime.utcnow()}})
        except Exception:
            accounts_col.update_one({"_id": ObjectId(account_id)}, {"$set": {"used": True, "used_at": datetime.utcnow()}})
        
        # Start simple background monitoring (session keep-alive only, no auto OTP search)
        def start_simple_monitoring():
            try:
                account_manager.start_simple_monitoring_sync(
                    account.get('session_string', ''),
                    session_id,
                    1800
                )
            except Exception as e:
                logger.error(f"Simple monitoring error: {e}")
        
        # Start monitoring thread
        thread = threading.Thread(target=start_simple_monitoring, daemon=True)
        thread.start()
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 Buy Another", callback_data="buy_account"))
        
        # USER KO SIRF PHONE NUMBER DIKHAO - NO API ID/HASH
        account_details = f"""✅ **Purchase Successful!**

🌍 Country: {account['country']}
💸 Price: {format_currency(price)}
📱 Phone Number: `{account.get('phone', 'N/A')}`"""

        if account.get('two_step_password'):
            account_details += f"\n🔒 2FA Password: `{account.get('two_step_password', 'N/A')}`"

        account_details += f"\n\n📲 **Instructions:**\n"
        account_details += f"1. Open Telegram X app\n"
        account_details += f"2. Enter phone number: `{account.get('phone', 'N/A')}`\n"
        account_details += f"3. Click 'Next'\n"
        account_details += f"4. **Click 'Get OTP' button below when you need OTP**\n\n"
        account_details += f"⏳ OTP available for 30 minutes"
        
        # Add ONLY Get OTP button (no logout)
        get_otp_markup = InlineKeyboardMarkup()
        get_otp_markup.add(InlineKeyboardButton("🔢 Get OTP", callback_data=f"get_otp_{session_id}"))
        
        account_details += f"\n💰 Remaining Balance: {format_currency(get_balance(user_id))}"
        
        bot.send_message(
            chat_id,
            account_details,
            parse_mode="Markdown",
            reply_markup=get_otp_markup
        )
        
        bot.answer_callback_query(callback_id, "✅ Purchase successful! Click Get OTP when needed.", show_alert=True)
        
        # Update the account display to show out of stock
        show_countries(chat_id, message_id)
        
    except Exception as e:
        logger.error(f"Purchase error: {e}")
        try:
            bot.answer_callback_query(callback_id, "❌ Purchase failed", show_alert=True)
        except:
            pass

# -----------------------
# MESSAGE HANDLER FOR ADMIN DEDUCT AND OTHER STATES
# -----------------------
@bot.message_handler(func=lambda m: True, content_types=['text','photo','video','document'])
def chat_handler(msg):
    user_id = msg.from_user.id
    
    # Check if user is banned
    if is_user_banned(user_id):
        return
    
    ensure_user_exists(user_id, msg.from_user.first_name or "Unknown", msg.from_user.username)
    
    # ADMIN DEDUCT PROCESS HANDLER
    if user_id == ADMIN_ID and user_id in admin_deduct_state:
        # Check which step admin is on
        state = admin_deduct_state[user_id]
        
        if state["step"] == "ask_user_id":
            try:
                target_user_id = int(msg.text.strip())
                # Check if user exists
                user_exists = users_col.find_one({"user_id": target_user_id})
                if not user_exists:
                    bot.send_message(ADMIN_ID, "❌ User not found in database. Please enter valid User ID:")
                    return
                
                # Get current balance of target user
                current_balance = get_balance(target_user_id)
                
                # Store target user ID and move to next step
                admin_deduct_state[user_id] = {
                    "step": "ask_amount",
                    "target_user_id": target_user_id,
                    "current_balance": current_balance
                }
                
                bot.send_message(
                    ADMIN_ID,
                    f"👤 User ID: {target_user_id}\n"
                    f"💰 Current Balance: {format_currency(current_balance)}\n\n"
                    f"💸 Enter amount to deduct (max {format_currency(current_balance)}):"
                )
            except ValueError:
                bot.send_message(ADMIN_ID, "❌ Invalid User ID. Please enter numeric ID only:")
            return
            
        elif state["step"] == "ask_amount":
            try:
                amount = float(msg.text.strip())
                target_user_id = state["target_user_id"]
                current_balance = state["current_balance"]
                
                if amount <= 0:
                    bot.send_message(ADMIN_ID, "❌ Amount must be greater than 0. Please enter valid amount:")
                    return
                
                if amount > current_balance:
                    bot.send_message(ADMIN_ID, f"❌ Amount exceeds user's balance. Maximum: {format_currency(current_balance)}\nPlease enter valid amount:")
                    return
                
                # Store amount and move to next step
                admin_deduct_state[user_id] = {
                    "step": "ask_reason",
                    "target_user_id": target_user_id,
                    "amount": amount,
                    "current_balance": current_balance
                }
                
                bot.send_message(ADMIN_ID, "📝 Enter reason for balance deduction:")
            except ValueError:
                bot.send_message(ADMIN_ID, "❌ Invalid amount. Please enter numeric value only:")
            return
            
        elif state["step"] == "ask_reason":
            reason = msg.text.strip()
            target_user_id = admin_deduct_state[user_id]["target_user_id"]
            amount = admin_deduct_state[user_id]["amount"]
            current_balance = admin_deduct_state[user_id]["current_balance"]
            
            if not reason:
                bot.send_message(ADMIN_ID, "❌ Reason cannot be empty. Please enter reason:")
                return
            
            # Now deduct the balance
            try:
                # Deduct balance from user
                deduct_balance(target_user_id, amount)
                new_balance = get_balance(target_user_id)
                
                # Record transaction in database
                transaction_id = f"DEDUCT{target_user_id}{int(time.time())}"
                deduction_record = {
                    "transaction_id": transaction_id,
                    "user_id": target_user_id,
                    "amount": amount,
                    "type": "deduction",
                    "reason": reason,
                    "admin_id": user_id,
                    "timestamp": datetime.utcnow(),
                    "old_balance": current_balance,
                    "new_balance": new_balance
                }
                transactions_col.insert_one(deduction_record)
                
                # Send confirmation to admin
                bot.send_message(
                    ADMIN_ID,
                    f"✅ **Balance Deducted Successfully!**\n\n"
                    f"👤 User ID: {target_user_id}\n"
                    f"💰 Amount Deducted: {format_currency(amount)}\n"
                    f"📝 Reason: {reason}\n"
                    f"📊 Old Balance: {format_currency(current_balance)}\n"
                    f"📊 New Balance: {format_currency(new_balance)}\n"
                    f"🆔 Transaction ID: {transaction_id}",
                    parse_mode="Markdown"
                )
                
                # Send notification to user
                try:
                    bot.send_message(
                        target_user_id,
                        f"⚠️ **Balance Deducted by Admin**\n\n"
                        f"💰 Amount: {format_currency(amount)}\n"
                        f"📝 Reason: {reason}\n"
                        f"📊 Your New Balance: {format_currency(new_balance)}\n"
                        f"🆔 Transaction ID: {transaction_id}\n\n"
                        f"Contact admin if this was a mistake.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    bot.send_message(ADMIN_ID, f"⚠️ Could not notify user {target_user_id} (maybe blocked)")
                
                # Cleanup state
                del admin_deduct_state[user_id]
                
            except Exception as e:
                logger.exception("Error in balance deduction:")
                bot.send_message(ADMIN_ID, f"❌ Error deducting balance: {str(e)}")
                del admin_deduct_state[user_id]
            return
    
    # Original admin commands
    if user_id == ADMIN_ID:
        if msg.text and msg.text.strip().lower() == "/sendbroadcast":
            process_broadcast(msg)
        return
    
    # Manual payment proof handler
    if user_stage.get(user_id) == "waiting_recharge_proof":
        pending_messages.setdefault(user_id, {})
        amount = pending_messages[user_id].get("recharge_amount", 0)
        if msg.content_type == 'text':
            text = msg.text.strip()
            if not text.isdigit() or len(text) != 12:
                bot.send_message(user_id, "⚠️ Please enter a valid 12-digit UTR or send a screenshot.")
                return
            pending_messages[user_id]['utr'] = text
            proof_text = f"UTR: {text}"
        elif msg.content_type == 'photo':
            pending_messages[user_id]['screenshot'] = msg.photo[-1].file_id
            proof_text = "📸 Screenshot provided"
        else:
            bot.send_message(user_id, "⚠️ Please send 12-digit UTR or a screenshot photo.")
            return

        req_id = f"R{int(time.time())}{user_id}"
        recharge_doc = {
            "req_id": req_id,
            "user_id": user_id,
            "amount": amount,
            "utr": pending_messages[user_id].get('utr'),
            "screenshot": pending_messages[user_id].get('screenshot'),
            "status": "pending",
            "requested_at": datetime.utcnow(),
            "method": "manual_utr"
        }
        recharges_col.insert_one(recharge_doc)

        bot.send_message(user_id, "🔄 Your recharge request has been sent for verification. Please wait for approval.", parse_mode="HTML")

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Approve", callback_data=f"approve_rech|{req_id}"),
               InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_rech|{req_id}"))
        admin_text = (f"💳 <b>Recharge Request</b>\n"
                      f"User: <a href='tg://user?id={user_id}'>{user_id}</a>\n"
                      f"Amount: {format_currency(amount)}\n"
                      f"Req ID: <code>{req_id}</code>\n")
        if 'utr' in pending_messages[user_id]:
            admin_text += f"UTR: {pending_messages[user_id]['utr']}\n"
            bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=kb)
        else:
            bot.send_photo(ADMIN_ID, pending_messages[user_id]['screenshot'], caption=admin_text, parse_mode="HTML", reply_markup=kb)

        user_stage[user_id] = "done"
        pending_messages.pop(user_id, None)
        return
    
    bot.send_message(user_id, "⚠️ Please use /start to begin or press buttons from the menu.")

# -----------------------
# OTHER FUNCTIONS
# -----------------------
def complete_order(user_id, session_id, chat_id, callback_id):
    """Mark order as completed when user clicks Complete button"""
    try:
        # Check if at least one OTP was received
        session_data = otp_sessions_col.find_one({"session_id": session_id})
        
        if not session_data:
            bot.answer_callback_query(callback_id, "❌ Order session not found", show_alert=True)
            return
            
        if not session_data.get("otp_code"):
            bot.answer_callback_query(
                callback_id, 
                "❌ No OTP received yet!\n\nPlease wait for at least one OTP before completing the order.", 
                show_alert=True
            )
            return
        
        # Update OTP session
        otp_sessions_col.update_one(
            {"session_id": session_id},
            {"$set": {
                "status": "completed",
                "completed_at": datetime.utcnow(),
                "completed_by_user": True
            }}
        )
        
        # Update order status
        orders_col.update_one(
            {"session_id": session_id},
            {"$set": {
                "status": "completed", 
                "completed_at": datetime.utcnow(),
                "user_completed": True
            }}
        )
        
        bot.answer_callback_query(callback_id, "✅ Order marked as completed!", show_alert=True)
        
        # Send confirmation message
        bot.send_message(
            chat_id,
            "🎉 **Order Completed Successfully!**\n\n"
            "✅ Your account has been successfully activated!\n"
            "📦 Order marked as completed.\n\n"
            "Thank you for your purchase! 🎊"
        )
        
    except Exception as e:
        logger.error(f"Complete order error: {e}")
        bot.answer_callback_query(callback_id, "❌ Error completing order", show_alert=True)

def show_my_orders(user_id, chat_id):
    orders = list(orders_col.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    
    if not orders:
        bot.send_message(chat_id, "📦 No orders found")
        return
    
    text = "📦 **Your Recent Orders**\n\n"
    for order in orders:
        status_icon = "✅" if order['status'] == 'completed' else "🔍" if order['status'] == 'waiting_otp' else "⏳" if order['status'] == 'monitoring' else "❌"
        text += f"{status_icon} {order['country']} - {format_currency(order['price'])} - {order['status']}\n"
        text += f"  📱 {order.get('phone_number', 'N/A')}\n\n"
    
    bot.send_message(chat_id, text, parse_mode="Markdown")

def show_pending_recharges(chat_id):
    if not is_admin(chat_id):
        bot.send_message(chat_id, "❌ Unauthorized access")
        return
        
    recharges = list(recharges_col.find({"status": "pending"}))
    
    if not recharges:
        bot.send_message(chat_id, "✅ No pending recharges")
        return
    
    text = "📋 **Pending Recharges**\n\n"
    for recharge in recharges:
        text += f"User: {recharge['user_id']}\n"
        text += f"Amount: {format_currency(recharge['amount'])}\n"
        text += f"ID: `{recharge['_id']}`\n\n"
    
    bot.send_message(chat_id, text, parse_mode="Markdown")

def approve_recharge(recharge_id, admin_chat_id, message_id):
    try:
        recharge = recharges_col.find_one({"_id": ObjectId(recharge_id)})
        if not recharge:
            bot.send_message(admin_chat_id, "❌ Recharge not found")
            return
        
        user_id = recharge['user_id']
        amount = recharge['amount']
        
        add_balance(user_id, amount)
        
        recharges_col.update_one(
            {"_id": ObjectId(recharge_id)},
            {"$set": {"status": "approved", "approved_at": datetime.utcnow(), "approved_by": ADMIN_ID}}
        )
        
        # Check for referral commission
        user_data = users_col.find_one({"user_id": user_id})
        if user_data and user_data.get("referred_by"):
            add_referral_commission(user_data["referred_by"], amount, recharge)
        
        bot.send_message(
            user_id,
            f"✅ Recharge Approved!\n\n"
            f"Amount: {format_currency(amount)}\n"
            f"New Balance: {format_currency(get_balance(user_id))}\n\n"
            f"Thank you for your payment! 🎉"
        )
        
        try:
            bot.edit_message_caption(
                chat_id=admin_chat_id,
                message_id=message_id,
                caption=f"✅ Recharge Approved\n\n"
                       f"User: {user_id}\n"
                       f"Amount: {format_currency(amount)}\n"
                       f"Balance Added: {format_currency(get_balance(user_id))}"
            )
        except:
            pass
        
    except Exception as e:
        logger.error(f"Approve recharge error: {e}")
        try:
            bot.send_message(admin_chat_id, f"❌ Error: {e}")
        except:
            pass

def reject_recharge(recharge_id, admin_chat_id):
    try:
        recharge = recharges_col.find_one({"_id": ObjectId(recharge_id)})
        if recharge:
            recharges_col.update_one(
                {"_id": ObjectId(recharge_id)},
                {"$set": {"status": "rejected", "rejected_at": datetime.utcnow()}}
            )
            
            bot.send_message(
                recharge['user_id'],
                f"❌ Recharge Rejected\n\n"
                f"Amount: {format_currency(recharge['amount'])}\n"
                f"Contact support if this is a mistake."
            )
            
            bot.send_message(admin_chat_id, f"❌ Recharge {recharge_id} rejected")
    except Exception as e:
        logger.error(f"Reject recharge error: {e}")

# -----------------------
# RUN BOT
# -----------------------
if __name__ == "__main__":
    logger.info(f"🤖 Fixed OTP Bot Starting...")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"Bot Token: {BOT_TOKEN[:10]}...")
    logger.info(f"Global API ID: {GLOBAL_API_ID}")
    logger.info(f"Global API Hash: {GLOBAL_API_HASH[:10]}...")
    logger.info(f"Referral Commission: {REFERRAL_COMMISSION}%")
    
    # Start background payment checker (ONLY ONCE)
    payment_checker_started = False
    if not payment_checker_started:
        payment_checker_thread = threading.Thread(
            target=check_pending_payments,
            daemon=True
        )
        payment_checker_thread.start()
        payment_checker_started = True
        logger.info("✅ Payment checker thread started")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"Bot error: {e}")
        time.sleep(30)
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
