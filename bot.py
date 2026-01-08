import re
import time
import asyncio
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import SessionPasswordNeeded, FloodWait
from pymongo import MongoClient

# ============ CONFIG (DIRECT ENV INSIDE BOT) ============
BOT_TOKEN = "7802838001:AAHhK3IohejoIOmOI5Skf2C5JrvmKGYfnFs"
API_ID = 6435225
API_HASH = "4e984ea35f854762dcde906dce426c2d"

MONGO_URI = "mongodb+srv://teamdaxx123:teamdaxx123@cluster0.ysbpgcp.mongodb.net/?retryWrites=true&w=majority"
# ======================================================

mongo = MongoClient(MONGO_URI)
db = mongo.otp_panel
accounts_col = db.accounts

bot = Client(
    "otp_panel_bot",
    bot_token=BOT_TOKEN,
    api_id=API_ID,
    api_hash=API_HASH
)

user_state = {}
temp_clients = {}

# ================= START =================
@bot.on_message(filters.command("start"))
async def start(_, m):
    kb = [
        [InlineKeyboardButton("➕ Add Account", callback_data="add")],
        [InlineKeyboardButton("📂 Accounts", callback_data="accounts")]
    ]
    await m.reply(
        "👋 **OTP Panel Bot**\n\nChoose option 👇",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ================= CALLBACK =================
@bot.on_callback_query()
async def callbacks(_, q):
    uid = q.from_user.id

    # ADD ACCOUNT
    if q.data == "add":
        user_state[uid] = {"step": "phone"}
        await q.message.reply("📱 Send phone number\nExample: +919xxxxxxxxx")

    # LIST ACCOUNTS
    if q.data == "accounts":
        accounts = list(accounts_col.find({"user_id": uid}))
        if not accounts:
            await q.message.reply("❌ No accounts found")
            return

        buttons = []
        for acc in accounts:
            buttons.append([
                InlineKeyboardButton(
                    f"📱 {acc['phone']}",
                    callback_data=f"open_{acc['phone']}"
                )
            ])

        await q.message.reply(
            f"📂 **Total Accounts:** {len(accounts)}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # OPEN ACCOUNT
    if q.data.startswith("open_"):
        phone = q.data.split("_", 1)[1]
        kb = [
            [InlineKeyboardButton("📩 Get OTP", callback_data=f"otp_{phone}")],
            [InlineKeyboardButton("🚪 Logout", callback_data=f"logout_{phone}")]
        ]
        await q.message.reply(
            f"📱 **Account:** `{phone}`",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # LOGOUT
    if q.data.startswith("logout_"):
        phone = q.data.split("_", 1)[1]
        acc = accounts_col.find_one({"user_id": uid, "phone": phone})

        if not acc:
            await q.message.reply("❌ Account not found")
            return

        client = Client(
            ":memory:",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=acc["session"]
        )

        try:
            await client.start()
            await client.log_out()
            await client.stop()
        except:
            pass

        accounts_col.delete_one({"_id": acc["_id"]})
        await q.message.reply(f"🚪 Logged out `{phone}`")

    # GET OTP
    if q.data.startswith("otp_"):
        phone = q.data.split("_", 1)[1]
        acc = accounts_col.find_one({"user_id": uid, "phone": phone})

        if not acc:
            await q.message.reply("❌ Account not found")
            return

        client = Client(
            ":memory:",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=acc["session"]
        )

        await client.start()
        otp = await fetch_latest_otp(client)
        await client.stop()

        if not otp:
            await q.message.reply("❌ No OTP found")
            return

        kb = [[InlineKeyboardButton("🚪 Logout", callback_data=f"logout_{phone}")]]
        await q.message.reply(
            f"✅ **Latest OTP**\n\n"
            f"📱 `{phone}`\n"
            f"🔢 OTP: `{otp}`\n"
            f"🔐 2-Step: `{acc['two_step']}`",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ================= TEXT HANDLER =================
@bot.on_message(filters.text & ~filters.command("start"))
async def text_handler(_, m):
    uid = m.from_user.id
    if uid not in user_state:
        return

    step = user_state[uid]["step"]

    # PHONE
    if step == "phone":
        phone = m.text.strip()
        client = Client(":memory:", api_id=API_ID, api_hash=API_HASH)
        await client.connect()
        sent = await client.send_code(phone)

        temp_clients[uid] = client
        user_state[uid] = {
            "step": "otp",
            "phone": phone,
            "hash": sent.phone_code_hash
        }
        await m.reply("📩 OTP sent, send OTP")

    # OTP
    elif step == "otp":
        data = user_state[uid]
        client = temp_clients[uid]

        try:
            await client.sign_in(
                phone_number=data["phone"],
                phone_code=m.text,
                phone_code_hash=data["hash"]
            )
            two_step = False

        except SessionPasswordNeeded:
            user_state[uid]["step"] = "password"
            await m.reply("🔐 Send 2-step password")
            return

        await save_account(uid, data["phone"], client, two_step)
        await m.reply("✅ Account added")
        cleanup(uid)

    # PASSWORD
    elif step == "password":
        data = user_state[uid]
        client = temp_clients[uid]
        await client.check_password(m.text)

        await save_account(uid, data["phone"], client, True)
        await m.reply("✅ Account added")
        cleanup(uid)

# ================= HELPERS =================
async def save_account(uid, phone, client, two_step):
    session = await client.export_session_string()
    accounts_col.insert_one({
        "user_id": uid,
        "phone": phone,
        "session": session,
        "two_step": two_step,
        "added_at": datetime.utcnow()
    })
    await client.disconnect()

def cleanup(uid):
    user_state.pop(uid, None)
    temp_clients.pop(uid, None)

async def fetch_latest_otp(client):
    pattern = r"\b\d{5}\b"
    async for msg in client.get_chat_history("Telegram", limit=15):
        if msg.text:
            match = re.search(pattern, msg.text)
            if match:
                return match.group()
    return None

# ================= RUN =================
bot.run()
