import os
import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters
)
from telegram.ext import _updater as ptb_updater_module
from tornado.web import RequestHandler

class HealthHandler(RequestHandler):
    """Simple public health endpoint for Render/UptimeRobot."""

    def get(self):
        self.set_header("Content-Type", "text/plain; charset=UTF-8")
        self.set_status(200)
        self.finish("OK")

    def head(self):
        self.set_status(200)
        self.finish()


def install_health_endpoint():
    """
    Add / and /health routes to the same Tornado server used by PTB's webhook.
    The Telegram webhook URL and its secret path remain unchanged.
    """
    original_webhook_app = ptb_updater_module.WebhookAppClass

    def webhook_app_with_health(*args, **kwargs):
        tornado_app = original_webhook_app(*args, **kwargs)
        tornado_app.add_handlers(
            r".*$",
            [
                (r"/", HealthHandler),
                (r"/health/?", HealthHandler),
            ],
        )
        return tornado_app

    ptb_updater_module.WebhookAppClass = webhook_app_with_health


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO")
)
log = logging.getLogger("force-sub-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.getenv("DB_NAME", "force_sub_bot")
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-this-secret")
PORT = int(os.getenv("PORT", "10000"))

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
db = mongo[DB_NAME]
users = db.users
admins = db.admins
force_channels = db.force_channels
target_channels = db.target_channels
banned_users = db.banned_users
invite_logs = db.invite_logs
settings = db.settings

# A pending input is kept in memory for the short admin setup flow.
# Persistent configuration and user data are stored in MongoDB.
PENDING = {}

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_admin(uid: int) -> bool:
    return is_owner(uid) or admins.find_one({"user_id": uid}) is not None

def is_banned(uid: int) -> bool:
    return banned_users.find_one({"user_id": uid}) is not None

def nice_title(doc):
    return doc.get("title") or str(doc.get("chat_id"))

def owner_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Force Sub", callback_data="add_force"),
         InlineKeyboardButton("➖ Remove Force Sub", callback_data="remove_force")],
        [InlineKeyboardButton("➕ Add Target Channel", callback_data="add_target"),
         InlineKeyboardButton("➖ Remove Target", callback_data="remove_target")],
        [InlineKeyboardButton("👮 Add Admin", callback_data="add_admin"),
         InlineKeyboardButton("🗑 Remove Admin", callback_data="remove_admin")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="ban_user"),
         InlineKeyboardButton("✅ Unban User", callback_data="unban_user")],
        [InlineKeyboardButton("📊 Statistics", callback_data="stats"),
         InlineKeyboardButton("📣 Broadcast", callback_data="broadcast")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    users.update_one(
        {"user_id": uid},
        {"$set": {"user_id": uid, "username": update.effective_user.username,
                  "first_name": update.effective_user.first_name,
                  "last_name": update.effective_user.last_name,
                  "last_seen": datetime.now(timezone.utc)},
         "$setOnInsert": {"first_seen": datetime.now(timezone.utc)}},
        upsert=True
    )
    if is_banned(uid):
        await update.effective_message.reply_text("Aapko is bot ka use karne ki permission nahi hai.")
        return
    if is_owner(uid):
        await update.effective_message.reply_text(
            "👑 Owner Control Panel\nNeeche diye gaye buttons se bot manage karein.",
            reply_markup=owner_keyboard()
        )
        return
    user_doc = users.find_one({"user_id": uid}) or {}
    if not user_doc.get("human_verified"):
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🤖 I am not a robot", callback_data="human_verify"
        )]])
        await update.effective_message.reply_text(
            "Pehle confirm karein ki aap robot nahi hain.", reply_markup=keyboard
        )
        return
    await show_force_panel(update.effective_message, context, uid)

async def human_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    uid = user.id
    if is_banned(uid):
        await q.answer("Aapko is bot ka use karne ki permission nahi hai.", show_alert=True)
        return
    doc = users.find_one({"user_id": uid}) or {}
    if doc.get("human_verified"):
        await q.answer("Aap pehle se verify ho chuke hain.")
        try:
            await q.message.delete()
        except TelegramError:
            pass
        await show_force_panel(q.message, context, uid)
        return
    users.update_one({"user_id": uid}, {"$set": {
        "human_verified": True,
        "verified_at": datetime.now(timezone.utc),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username
    }}, upsert=True)
    await q.answer("Verification successful!")
    display_name = " ".join(x for x in [user.first_name, user.last_name] if x) or "Unknown"
    username = f"@{user.username}" if user.username else "Not set"
    notice = (
        "New User Verified ✅\n\n"
        f"Name: {display_name}\n"
        f"Username: {username}\n"
        f"User ID: {uid}\n"
        "Status: Verified"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=notice)
    except TelegramError as exc:
        log.warning("Could not notify owner about verified user %s: %s", uid, exc)
    try:
        await q.message.delete()
    except TelegramError:
        pass
    await show_force_panel(q.message, context, uid)

async def show_force_panel(message, context, uid):
    channels = list(force_channels.find().sort("_id", 1))
    missing = await find_missing_channels(context, channels, uid)
    if not missing:
        await show_targets(message, context, uid)
        return
    rows = [[InlineKeyboardButton(f"📢 JOIN {nice_title(ch)}", url=ch["join_url"])]
            for ch in missing]
    rows.append([InlineKeyboardButton("✅ VERIFY ALL", callback_data="verify_force")])
    await message.reply_text(
        "🔒 Kripya pehle neeche diye gaye sabhi channels join karein.",
        reply_markup=InlineKeyboardMarkup(rows)
    )

async def show_targets(message, context, uid):
    targets = list(target_channels.find().sort("_id", 1))
    missing = await find_missing_channels(context, targets, uid)
    if not missing:
        await message.reply_text("✅ Aap sabhi channels mein pehle se joined hain.")
        return
    rows = [[InlineKeyboardButton(
        f"🎯 GET {nice_title(ch)} INVITE", callback_data=f"get_target:{ch['_id']}"
    )] for ch in missing]
    sent = await message.reply_text(
        "✅ Force Subscribe complete!\nNeeche sirf un channels ke buttons hain jinhe aapne abhi join nahi kiya.",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    users.update_one({"user_id": uid}, {"$set": {
        "target_panel_chat_id": sent.chat_id,
        "target_panel_message_id": sent.message_id
    }}, upsert=True)

async def user_is_member(context, chat_id, uid):
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=uid)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                  ChatMemberStatus.OWNER)
    except TelegramError as exc:
        log.warning("Membership check failed for %s / %s: %s", chat_id, uid, exc)
        return False


async def find_missing_channels(context, channels, uid):
    """Check channel membership concurrently to reduce response time."""
    if not channels:
        return []
    results = await asyncio.gather(*(
        user_is_member(context, ch["chat_id"], uid) for ch in channels
    ))
    return [ch for ch, joined in zip(channels, results) if not joined]

async def verify_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if is_banned(uid):
        await q.edit_message_text("Aapko is bot ka use karne ki permission nahi hai.")
        return
    if not is_owner(uid) and not (users.find_one({"user_id": uid}) or {}).get("human_verified"):
        await q.answer("Pehle human verification complete karein.", show_alert=True)
        return
    channels = list(force_channels.find())
    missing = await find_missing_channels(context, channels, uid)
    if missing:
        await q.answer("Aapne abhi sabhi Force Subscribe Channels join nahi kiye hain.", show_alert=True)
        return
    await q.answer()
    try:
        await q.delete_message()
    except TelegramError:
        pass
    await show_targets(q.message, context, uid)

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if is_banned(uid):
        await q.answer("Aapka access band kar diya gaya hai.", show_alert=True)
        return
    if not is_owner(uid) and not (users.find_one({"user_id": uid}) or {}).get("human_verified"):
        await q.answer("Pehle human verification complete karein.", show_alert=True)
        return
    force_missing = await find_missing_channels(context, list(force_channels.find()), uid)
    if force_missing:
        await q.answer("Pehle sabhi Force Subscribe Channels join karein.", show_alert=True)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        await show_force_panel(q.message, context, uid)
        return
    target_id = q.data.split(":", 1)[1]
    from bson import ObjectId
    try:
        target = target_channels.find_one({"_id": ObjectId(target_id)})
    except Exception:
        target = None
    if not target:
        await q.answer("Target Channel nahi mila.", show_alert=True)
        return
    if await user_is_member(context, target["chat_id"], uid):
        await q.answer("Aap is channel mein pehle se joined hain.", show_alert=True)
        await refresh_target_panel(context, uid)
        return
    existing = invite_logs.find_one({
        "user_id": uid, "target_chat_id": target["chat_id"], "status": "pending"
    })
    if existing:
        try:
            await context.bot.revoke_chat_invite_link(
                chat_id=target["chat_id"], invite_link=existing["invite_link"]
            )
        except TelegramError:
            pass
        try:
            await context.bot.delete_message(existing["message_chat_id"], existing["message_id"])
        except TelegramError:
            pass
        invite_logs.update_one({"_id": existing["_id"]}, {"$set": {"status": "replaced"}})
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=target["chat_id"], member_limit=1, name=f"user-{uid}"
        )
        sent = await q.message.reply_text(
            f"🎯 {nice_title(target)} join karne ke liye neeche button par click karein.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 JOIN TARGET CHANNEL", url=invite.invite_link)
            ]])
        )
        invite_logs.insert_one({
            "user_id": uid, "target_chat_id": target["chat_id"],
            "invite_link": invite.invite_link, "message_chat_id": sent.chat_id,
            "message_id": sent.message_id, "status": "pending",
            "created_at": datetime.now(timezone.utc)
        })
        await q.answer("Aapka One-Time Invite Link ready hai.")
    except TelegramError as exc:
        log.exception("Could not create invite: %s", exc)
        await q.answer("Invite link nahi ban saka. Bot ko Target Channel mein Admin banayein aur Invite Users permission dein.", show_alert=True)

async def refresh_target_panel(context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Keep only not-yet-joined target buttons; remove the panel when all are joined."""
    user_doc = users.find_one({"user_id": uid}) or {}
    chat_id = user_doc.get("target_panel_chat_id")
    message_id = user_doc.get("target_panel_message_id")
    missing = await find_missing_channels(
        context, list(target_channels.find().sort("_id", 1)), uid
    )
    if not missing:
        if chat_id and message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramError:
                pass
        users.update_one({"user_id": uid}, {"$unset": {
            "target_panel_chat_id": "", "target_panel_message_id": ""
        }})
        try:
            await context.bot.send_message(uid, "✅ Aap sabhi channels mein pehle se joined hain.")
        except TelegramError:
            pass
        return
    if chat_id and message_id:
        rows = [[InlineKeyboardButton(
            f"🎯 GET {nice_title(ch)} INVITE", callback_data=f"get_target:{ch['_id']}"
        )] for ch in missing]
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id,
                reply_markup=InlineKeyboardMarkup(rows)
            )
        except TelegramError:
            pass

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.chat_member
    if not event:
        return
    old, new = event.old_chat_member, event.new_chat_member
    joined = new.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.OWNER) and old.status in (
                                ChatMemberStatus.LEFT, ChatMemberStatus.BANNED
                            )
    if not joined or new.user.is_bot:
        return
    uid = new.user.id
    # If the user joined through this bot's personal one-time link, delete that link message
    # and revoke the link immediately after successful join.
    invite = event.invite_link.invite_link if event.invite_link else None
    log_doc = invite_logs.find_one({
        "user_id": uid, "target_chat_id": event.chat.id, "status": "pending"
    })
    if log_doc and (not invite or log_doc.get("invite_link") == invite):
        try:
            await context.bot.delete_message(
                chat_id=log_doc["message_chat_id"], message_id=log_doc["message_id"]
            )
        except TelegramError:
            pass
        try:
            await context.bot.revoke_chat_invite_link(
                chat_id=event.chat.id, invite_link=log_doc["invite_link"]
            )
        except TelegramError:
            pass
        invite_logs.update_one({"_id": log_doc["_id"]}, {"$set": {
            "status": "used", "joined_at": datetime.now(timezone.utc)
        }})
    # Update the user's target list whether they used the invite or joined another way.
    if target_channels.find_one({"chat_id": event.chat.id}):
        await refresh_target_panel(context, uid)

async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    if data == "human_verify":
        await human_verify(update, context)
        return
    if data == "verify_force":
        await verify_force(update, context)
        return
    if data.startswith("get_target:"):
        await get_target(update, context)
        return
    if not is_admin(uid):
        await q.answer("Yeh control sirf Admins ke liye hai.", show_alert=True)
        return
    await q.answer()
    if data == "stats":
        await q.edit_message_text(
            f"📊 Statistics\nUsers: {users.count_documents({})}\n"
            f"Force Channels: {force_channels.count_documents({})}\n"
            f"Target Channels: {target_channels.count_documents({})}\n"
            f"Admins: {admins.count_documents({})}"
        )
        return
    if data in ("add_admin", "remove_admin", "ban_user", "unban_user",
                "add_force", "remove_force", "add_target", "remove_target", "broadcast"):
        if data in ("add_admin", "remove_admin", "ban_user", "unban_user") and not is_owner(uid):
            await q.answer("Yeh kaam sirf Owner kar sakta hai.", show_alert=True)
            return
        PENDING[uid] = data
        prompts = {
            "add_force": "Force Channel add karne ke liye is format mein bhejein:\nchat_id | title | public_join_url_or_invite_link\nExample: -1001234567890 | News | https://t.me/news",
            "remove_force": "Jis Force Channel ko remove karna hai, uska Chat ID bhejein.",
            "add_target": "Target Channel add karne ke liye is format mein bhejein:\nchat_id | title\nExample: -1001234567890 | Premium",
            "remove_target": "Jis Target Channel ko remove karna hai, uska Chat ID bhejein.",
            "add_admin": "Naye Admin ka Telegram numeric User ID bhejein.",
            "remove_admin": "Jis Admin ko remove karna hai, uska Telegram numeric User ID bhejein.",
            "ban_user": "Jis User ko Ban karna hai, uska Telegram numeric User ID bhejein.",
            "unban_user": "Jis User ko Unban karna hai, uska Telegram numeric User ID bhejein.",
            "broadcast": "Sabhi Users ko bhejne ke liye Broadcast Message likhein."
        }
        await q.message.reply_text(prompts[data])

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    action = PENDING.get(uid)
    if not action:
        return
    if not is_admin(uid):
        PENDING.pop(uid, None)
        return
    raw = update.effective_message.text.strip()
    try:
        if action == "add_force":
            parts = [p.strip() for p in raw.split("|", 2)]
            if len(parts) != 3:
                raise ValueError("Teen parts ko | se alag karke bhejein.")
            chat_id, title, join_url = int(parts[0]), parts[1], parts[2]
            if not join_url.startswith(("https://t.me/", "http://t.me/")):
                raise ValueError("Join URL Telegram ka valid URL hona chahiye.")
            chat = await context.bot.get_chat(chat_id)
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                raise ValueError("Bot ko us Channel mein Admin banayein.")
            force_channels.update_one({"chat_id": chat_id},
                {"$set": {"chat_id": chat_id, "title": title or chat.title, "join_url": join_url}},
                upsert=True)
            await update.effective_message.reply_text("✅ Force Subscribe Channel add ho gaya.")
        elif action == "add_target":
            parts = [p.strip() for p in raw.split("|", 1)]
            if len(parts) != 2:
                raise ValueError("chat_id | title format mein bhejein.")
            chat_id, title = int(parts[0]), parts[1]
            chat = await context.bot.get_chat(chat_id)
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                raise ValueError("Bot ko Target Channel mein Admin banayein.")
            target_channels.update_one({"chat_id": chat_id},
                {"$set": {"chat_id": chat_id, "title": title or chat.title}},
                upsert=True)
            await update.effective_message.reply_text("✅ Target Channel add ho gaya.")
        elif action == "remove_force":
            force_channels.delete_one({"chat_id": int(raw)})
            await update.effective_message.reply_text("✅ Force Channel remove ho gaya.")
        elif action == "remove_target":
            target_channels.delete_one({"chat_id": int(raw)})
            await update.effective_message.reply_text("✅ Target Channel remove ho gaya.")
        elif action == "add_admin":
            target_uid = int(raw)
            if target_uid == OWNER_ID:
                raise ValueError("Owner ko alag se Admin add karne ki zaroorat nahi hai.")
            admins.update_one({"user_id": target_uid},
                {"$set": {"user_id": target_uid, "added_by": uid}}, upsert=True)
            await update.effective_message.reply_text("✅ Admin add ho gaya.")
        elif action == "remove_admin":
            admins.delete_one({"user_id": int(raw)})
            await update.effective_message.reply_text("✅ Admin remove ho gaya.")
        elif action == "ban_user":
            banned_users.update_one({"user_id": int(raw)},
                {"$set": {"user_id": int(raw), "banned_by": uid}}, upsert=True)
            await update.effective_message.reply_text("🚫 User ko Ban kar diya gaya hai.")
        elif action == "unban_user":
            banned_users.delete_one({"user_id": int(raw)})
            await update.effective_message.reply_text("✅ User ko Unban kar diya gaya hai.")
        elif action == "broadcast":
            sent = failed = 0
            for user in users.find({}, {"user_id": 1}):
                try:
                    await context.bot.send_message(user["user_id"], raw)
                    sent += 1
                except TelegramError:
                    failed += 1
            await update.effective_message.reply_text(
                f"Broadcast complete. Successful: {sent}, Failed: {failed}"
            )
    except (ValueError, PyMongoError, TelegramError) as exc:
        await update.effective_message.reply_text(f"❌ Kaam complete nahi ho saka: {exc}")
    finally:
        PENDING.pop(uid, None)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Aapke paas Admin permission nahi hai.")
        return
    await update.effective_message.reply_text("⚙️ Admin Control Panel", reply_markup=owner_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    PENDING.pop(update.effective_user.id, None)
    await update.effective_message.reply_text("Current action cancel kar diya gaya hai.")

async def post_init(application: Application):
    # Validate MongoDB connectivity. run_webhook configures the webhook.
    mongo.admin.command("ping")
    log.info("MongoDB connection successful.")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(8)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(panel_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    install_health_endpoint()
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_SECRET,
        webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_SECRET}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
