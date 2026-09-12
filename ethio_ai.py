import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from google import genai


# =========================================================
# CONFIGURATION
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

DATABASE = "ethio_ai_users.db"

# Telegram Stars
PREMIUM_PRICE_STARS = 100
PREMIUM_DAYS = 30
PREMIUM_PAYLOAD = "ethio_ai_premium_30_days"


# =========================================================
# CHECK ENVIRONMENT
# =========================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing.")

if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is missing.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram ID.")


# =========================================================
# GEMINI
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            status TEXT DEFAULT 'free',
            blocked INTEGER DEFAULT 0,
            created_at TEXT,
            last_active TEXT,
            premium_until TEXT
        )
    """)

    conn.commit()
    conn.close()


def add_premium_column():

    conn = get_db()

    columns = conn.execute(
        "PRAGMA table_info(users)"
    ).fetchall()

    names = [
        column["name"]
        for column in columns
    ]

    if "premium_until" not in names:

        conn.execute(
            "ALTER TABLE users ADD COLUMN premium_until TEXT"
        )

        conn.commit()

    conn.close()


# =========================================================
# USER MANAGEMENT
# =========================================================

def save_user(user):

    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()

    if existing:

        conn.execute("""
            UPDATE users
            SET first_name = ?,
                last_name = ?,
                username = ?,
                last_active = ?
            WHERE user_id = ?
        """, (
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            now,
            user.id
        ))

    else:

        conn.execute("""
            INSERT INTO users
            (
                user_id,
                first_name,
                last_name,
                username,
                status,
                blocked,
                created_at,
                last_active,
                premium_until
            )
            VALUES (?, ?, ?, ?, 'free', 0, ?, ?, NULL)
        """, (
            user.id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            now,
            now
        ))

    conn.commit()
    conn.close()


def get_user(user_id):

    conn = get_db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    conn.close()

    return row


def is_blocked(user_id):

    row = get_user(user_id)

    return bool(
        row and row["blocked"]
    )


def is_premium(user_id):

    row = get_user(user_id)

    if not row:
        return False

    if row["status"] != "premium":
        return False

    premium_until = row["premium_until"]

    if not premium_until:
        return False

    try:

        expiry = datetime.fromisoformat(
            premium_until
        )

        if expiry > datetime.now(timezone.utc):

            return True

    except Exception:

        return False

    # Expired
    conn = get_db()

    conn.execute("""
        UPDATE users
        SET status = 'free',
            premium_until = NULL
        WHERE user_id = ?
    """, (user_id,))

    conn.commit()
    conn.close()

    return False


# =========================================================
# ADMIN
# =========================================================

def is_admin(update):

    return (
        update.effective_user
        and update.effective_user.id == ADMIN_ID
    )


async def require_admin(update):

    if not is_admin(update):

        if update.message:

            await update.message.reply_text(
                "⛔ You are not authorized to use this command."
            )

        return False

    return True


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    save_user(
        update.effective_user
    )

    if is_blocked(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    if is_premium(
        update.effective_user.id
    ):

        premium_text = (
            "⭐ PREMIUM USER\n\n"
            "Your Ethio AI Premium is active."
        )

    else:

        premium_text = (
            "🆓 FREE USER\n\n"
            "Upgrade to Premium to unlock "
            "more features."
        )

    keyboard = [

        [
            InlineKeyboardButton(
                "⭐ Get Premium",
                callback_data="get_premium"
            )
        ],

        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help"
            )
        ]

    ]

    await update.message.reply_text(

        "👋 Hello!\n\n"

        "🤖 I am Ethio AI.\n"

        "Your intelligent AI assistant "
        "powered by Google Gemini.\n\n"

        f"{premium_text}\n\n"

        "💬 Send me a question.\n"
        "🖼️ You can also send me an image "
        "with a question.",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    save_user(
        update.effective_user
    )

    if is_blocked(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    await update.message.reply_text(

        "🤖 ETHIO AI\n\n"

        "I am your AI assistant powered by "
        "Google Gemini.\n\n"

        "💬 Send a normal question.\n\n"

        "🖼️ Send an image and ask a question "
        "about it.\n\n"

        "⭐ Use Get Premium for Premium access."
    )


# =========================================================
# PREMIUM MENU
# =========================================================

async def premium_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    save_user(
        update.effective_user
    )

    if is_premium(
        update.effective_user.id
    ):

        row = get_user(
            update.effective_user.id
        )

        await update.message.reply_text(
            "⭐ You already have Premium.\n\n"
            f"📅 Premium until:\n"
            f"{row['premium_until']}"
        )

        return

    keyboard = [

        [
            InlineKeyboardButton(
                f"💎 Buy Premium — "
                f"{PREMIUM_PRICE_STARS} ⭐",
                callback_data="get_premium"
            )
        ]

    ]

    await update.message.reply_text(

        "⭐ ETHIO AI PREMIUM\n\n"

        "Premium includes:\n\n"

        "✅ AI chat\n"
        "✅ Image questions\n"
        "✅ Advanced AI assistance\n"
        "✅ Premium access\n"
        "✅ 30 days access\n\n"

        f"💎 Price: "
        f"{PREMIUM_PRICE_STARS} Telegram Stars\n\n"

        "Tap the button below to continue.",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# GET PREMIUM BUTTON
# =========================================================

async def get_premium(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if is_premium(user_id):

        await query.message.reply_text(
            "⭐ You already have an active Premium subscription."
        )

        return

    await context.bot.send_invoice(

        chat_id=user_id,

        title="Ethio AI Premium",

        description=(
            "Ethio AI Premium — "
            "30 days of Premium access."
        ),

        payload=PREMIUM_PAYLOAD,

        provider_token="",

        currency="XTR",

        prices=[
            LabeledPrice(
                "Ethio AI Premium — 30 Days",
                PREMIUM_PRICE_STARS
            )
        ],
    )


# =========================================================
# PRE-CHECKOUT
# =========================================================

async def precheckout_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.pre_checkout_query

    if (
        query.invoice_payload
        != PREMIUM_PAYLOAD
    ):

        await query.answer(
            ok=False,
            error_message=(
                "Invalid Premium payment."
            )
        )

        return

    await query.answer(
        ok=True
    )


# =========================================================
# SUCCESSFUL PAYMENT
# =========================================================

async def successful_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    payment = update.message.successful_payment

    if (
        payment.invoice_payload
        != PREMIUM_PAYLOAD
    ):

        return

    user_id = update.effective_user.id

    # 30 days from now
    premium_until = (
        datetime.now(timezone.utc)
        + timedelta(days=PREMIUM_DAYS)
    ).isoformat()

    conn = get_db()

    conn.execute("""
        UPDATE users
        SET status = 'premium',
            premium_until = ?
        WHERE user_id = ?
    """, (
        premium_until,
        user_id
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(

        "🎉 PAYMENT SUCCESSFUL!\n\n"

        "⭐ ETHIO AI PREMIUM ACTIVATED!\n\n"

        "✅ Premium access: ACTIVE\n"
        "📅 Duration: 30 days\n\n"

        f"⏰ Expires:\n"
        f"{premium_until}\n\n"

        "Thank you for supporting Ethio AI! 🤖"
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_admin(update):
        return

    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    premium = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE status='premium'"
    ).fetchone()[0]

    free = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE status='free'"
    ).fetchone()[0]

    blocked = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE blocked=1"
    ).fetchone()[0]

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=24)
    ).isoformat()

    active = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE last_active >= ?",
        (cutoff,)
    ).fetchone()[0]

    conn.close()

    await update.message.reply_text(

        "🤖 ETHIO AI — ADMIN\n\n"

        f"👥 Total Users: {total}\n"
        f"🟢 Active Users: {active}\n"
        f"⭐ Premium Users: {premium}\n"
        f"🆓 Free Users: {free}\n"
        f"🚫 Blocked Users: {blocked}\n\n"

        "📊 Commands\n"
        "────────────────────\n"
        "/users - All users\n"
        "/stats - Statistics\n"
        "/premium ID - Give Premium\n"
        "/free ID - Remove Premium\n"
        "/block ID - Block user\n"
        "/unblock ID - Unblock user"
    )


# =========================================================
# USERS
# =========================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_admin(update):
        return

    conn = get_db()

    users = conn.execute(
        "SELECT * FROM users "
        "ORDER BY last_active DESC"
    ).fetchall()

    conn.close()

    if not users:

        await update.message.reply_text(
            "👥 No users found."
        )

        return

    text = "👥 ETHIO AI — USERS\n\n"

    for user in users:

        name = (
            f"{user['first_name']} "
            f"{user['last_name']}"
        ).strip()

        if not name:
            name = "Unknown"

        username = (
            f"@{user['username']}"
            if user["username"]
            else "No username"
        )

        if user["blocked"]:

            status = "🚫 Blocked"

        elif is_premium(
            user["user_id"]
        ):

            status = "⭐ Premium"

        else:

            status = "🆓 Free"

        text += (
            "────────────────────\n"
            f"👤 {name}\n"
            f"{username}\n"
            f"🆔 ID: {user['user_id']}\n"
            f"{status}\n"
        )

    for i in range(
        0,
        len(text),
        4000
    ):

        await update.message.reply_text(
            text[i:i + 4000]
        )


# =========================================================
# STATS
# =========================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_admin(update):
        return

    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    premium = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE status='premium'"
    ).fetchone()[0]

    free = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE status='free'"
    ).fetchone()[0]

    blocked = conn.execute(
        "SELECT COUNT(*) FROM users "
        "WHERE blocked=1"
    ).fetchone()[0]

    conn.close()

    await update.message.reply_text(

        "📊 ETHIO AI — STATISTICS\n\n"

        f"👥 Total Users: {total}\n"
        f"⭐ Premium Users: {premium}\n"
        f"🆓 Free Users: {free}\n"
        f"🚫 Blocked Users: {blocked}"
    )


# =========================================================
# MANUAL PREMIUM
# =========================================================

async def set_status(
    update,
    context,
    status
):

    if not await require_admin(update):
        return

    if not context.args:

        await update.message.reply_text(
            f"Usage:\n/{status} USER_ID"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid user ID."
        )

        return

    if status == "premium":

        premium_until = (
            datetime.now(timezone.utc)
            + timedelta(days=PREMIUM_DAYS)
        ).isoformat()

    else:

        premium_until = None

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET status = ?,
            premium_until = ?
        WHERE user_id = ?
        """,
        (
            status,
            premium_until,
            user_id
        )
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    if status == "premium":

        await update.message.reply_text(
            f"⭐ User {user_id} is now PREMIUM."
        )

    else:

        await update.message.reply_text(
            f"🆓 User {user_id} is now FREE."
        )


async def premium_command(
    update,
    context
):

    await set_status(
        update,
        context,
        "premium"
    )


async def free_command(
    update,
    context
):

    await set_status(
        update,
        context,
        "free"
    )


# =========================================================
# BLOCK / UNBLOCK
# =========================================================

async def set_block(
    update,
    context,
    blocked
):

    if not await require_admin(update):
        return

    if not context.args:

        command = (
            "block"
            if blocked
            else "unblock"
        )

        await update.message.reply_text(
            f"Usage:\n/{command} USER_ID"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid user ID."
        )

        return

    if user_id == ADMIN_ID:

        await update.message.reply_text(
            "❌ You cannot block the admin."
        )

        return

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET blocked = ?
        WHERE user_id = ?
        """,
        (
            1 if blocked else 0,
            user_id
        )
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    if blocked:

        await update.message.reply_text(
            f"🚫 User {user_id} has been BLOCKED."
        )

    else:

        await update.message.reply_text(
            f"✅ User {user_id} has been UNBLOCKED."
        )


async def block_command(
    update,
    context
):

    await set_block(
        update,
        context,
        True
    )


async def unblock_command(
    update,
    context
):

    await set_block(
        update,
        context,
        False
    )


# =========================================================
# GEMINI TEXT CHAT
# =========================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    save_user(
        update.effective_user
    )

    if is_blocked(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    question = (
        update.message.text or ""
    ).strip()

    if not question:
        return

    try:

        await update.effective_chat.send_action(
            "typing"
        )

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Identity rules:
- Your name is Ethio AI.
- If asked your name, say:
  "My name is Ethio AI."
- If asked what technology powers you, say:
  "I am powered by Google Gemini."
- Never say your name is Gemini.
- Be helpful and polite.
- You can communicate in English and Afaan Oromoo.
- If the user writes Afaan Oromoo,
  respond in Afaan Oromoo when possible.

User question:
{question}
"""

        response = client.models.generate_content(
            model=MODEL,
            contents=prompt
        )

        answer = (
            response.text
            or "Sorry, I could not generate an answer."
        )

        for i in range(
            0,
            len(answer),
            4000
        ):

            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception as e:

        logger.exception(
            "GEMINI ERROR"
        )

        await update.message.reply_text(
            "⚠️ Ethio AI error:\n"
            f"{type(e).__name__}: {e}"
        )


# =========================================================
# IMAGE AI
# =========================================================

async def image_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    save_user(
        update.effective_user
    )

    user_id = update.effective_user.id

    if is_blocked(user_id):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    # Image feature
    # Premium-only keessatti jijjiiruu yoo barbaadde
    # asitti is_premium check dabaluu dandeessa.

    question = (
        update.message.caption
        or
        "Please analyze this image "
        "and explain what you see."
    )

    try:

        await update.effective_chat.send_action(
            "typing"
        )

        photo = update.message.photo[-1]

        file = await context.bot.get_file(
            photo.file_id
        )

        image_bytes = await file.download_as_bytearray()

        prompt = f"""
You are Ethio AI.

Analyze the image carefully.

Answer the user's question clearly.

If the user writes in Afaan Oromoo,
respond in Afaan Oromoo when possible.

Do not invent details that cannot be
seen in the image.

User question:
{question}
"""

        response = client.models.generate_content(
            model=MODEL,
            contents=[
                prompt,
                {
                    "mime_type": "image/jpeg",
                    "data": bytes(image_bytes),
                }
            ]
        )

        answer = (
            response.text
            or
            "I could not analyze this image."
        )

        for i in range(
            0,
            len(answer),
            4000
        ):

            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception as e:

        logger.exception(
            "IMAGE ERROR"
        )

        await update.message.reply_text(
            "⚠️ Image analysis error:\n"
            f"{type(e).__name__}: {e}"
        )


# =========================================================
# BUTTONS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "get_premium":

        user_id = query.from_user.id

        if is_premium(user_id):

            await query.message.reply_text(
                "⭐ You already have active Premium."
            )

            return

        await context.bot.send_invoice(

            chat_id=user_id,

            title="Ethio AI Premium",

            description=(
                "30 days of Ethio AI Premium."
            ),

            payload=PREMIUM_PAYLOAD,

            provider_token="",

            currency="XTR",

            prices=[
                LabeledPrice(
                    "Premium — 30 Days",
                    PREMIUM_PRICE_STARS
                )
            ],
        )

    elif query.data == "help":

        await query.message.reply_text(

            "🤖 ETHIO AI HELP\n\n"

            "💬 Send text to chat.\n"
            "🖼️ Send an image with a question.\n"
            "⭐ Get Premium for premium access."
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_database()

    add_premium_column()

    print("=" * 55)
    print("                    ETHIO AI")
    print("=" * 55)

    print(
        "Starting Ethio AI Telegram Bot..."
    )

    print(
        f"Gemini model: {MODEL}"
    )

    print(
        f"Admin ID: {ADMIN_ID}"
    )

    print(
        f"Premium: {PREMIUM_PRICE_STARS} Stars / "
        f"{PREMIUM_DAYS} days"
    )

    print()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # Commands

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    app.add_handler(
        CommandHandler(
            "premium",
            premium_menu
        )
    )

    # Admin

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    app.add_handler(
        CommandHandler(
            "users",
            users_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    app.add_handler(
        CommandHandler(
            "premiumuser",
            premium_command
        )
    )

    app.add_handler(
        CommandHandler(
            "free",
            free_command
        )
    )

    app.add_handler(
        CommandHandler(
            "block",
            block_command
        )
    )

    app.add_handler(
        CommandHandler(
            "unblock",
            unblock_command
        )
    )

    # Buttons

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # Payment

    app.add_handler(
        PreCheckoutQueryHandler(
            precheckout_callback
        )
    )

    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment_callback
        )
    )

    # Images

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            image_chat
        )
    )

    # Text

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            chat
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "✅ Ethio AI is running!"
    )

    app.run_polling()


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":
    main()
