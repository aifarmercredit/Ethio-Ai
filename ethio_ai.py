import asyncio
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
from google.genai import types


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# CONFIGURATION
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ADMIN_ID_RAW = os.getenv("ADMIN_ID")
MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.6-flash"
)

DATABASE = "ethio_ai_users.db"


# =========================================================
# PREMIUM SETTINGS
# =========================================================

PREMIUM_PRICE_STARS = 100
PREMIUM_DAYS = 30
PREMIUM_PAYLOAD = "ethio_ai_premium_30_days"


# =========================================================
# VALIDATE ENVIRONMENT
# =========================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "TELEGRAM_TOKEN is missing."
    )

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is missing."
    )

if not ADMIN_ID_RAW:
    raise RuntimeError(
        "ADMIN_ID is missing."
    )

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise RuntimeError(
        "ADMIN_ID must be a number."
    )


# =========================================================
# GEMINI CLIENT
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# DATABASE
# =========================================================

def get_db():

    conn = sqlite3.connect(
        DATABASE,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
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
        """
    )

    conn.commit()
    conn.close()


# =========================================================
# USER FUNCTIONS
# =========================================================

def save_user(user):

    if not user:
        return

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO users (
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

        ON CONFLICT(user_id)

        DO UPDATE SET

            first_name = excluded.first_name,

            last_name = excluded.last_name,

            username = excluded.username,

            last_active = excluded.last_active
        """,
        (
            user.id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            now,
            now,
        )
    )

    conn.commit()
    conn.close()


def is_blocked(user_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT blocked
        FROM users
        WHERE user_id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    conn.close()

    if row:
        return bool(row["blocked"])

    return False


def is_premium(user_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT status, premium_until
        FROM users
        WHERE user_id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    conn.close()

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

        # Premium expired
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE users

            SET status = 'free',
                premium_until = NULL

            WHERE user_id = ?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()

        return False

    except Exception:

        return False


def set_premium(user_id, days=30):

    now = datetime.now(
        timezone.utc
    )

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT premium_until
        FROM users
        WHERE user_id = ?
        """,
        (user_id,)
    )

    row = cursor.fetchone()

    if row and row["premium_until"]:

        try:
            old_expiry = datetime.fromisoformat(
                row["premium_until"]
            )

            if old_expiry > now:
                expiry = (
                    old_expiry
                    + timedelta(days=days)
                )
            else:
                expiry = (
                    now
                    + timedelta(days=days)
                )

        except Exception:

            expiry = (
                now
                + timedelta(days=days)
            )

    else:

        expiry = (
            now
            + timedelta(days=days)
        )

    cursor.execute(
        """
        UPDATE users

        SET status = 'premium',
            premium_until = ?

        WHERE user_id = ?
        """,
        (
            expiry.isoformat(),
            user_id,
        )
    )

    conn.commit()
    conn.close()

    return expiry


def set_free(user_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET status = 'free',
            premium_until = NULL

        WHERE user_id = ?
        """,
        (user_id,)
    )

    conn.commit()
    conn.close()


def set_block(user_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET blocked = 1

        WHERE user_id = ?
        """,
        (user_id,)
    )

    conn.commit()
    conn.close()


def set_unblock(user_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users

        SET blocked = 0

        WHERE user_id = ?
        """,
        (user_id,)
    )

    conn.commit()
    conn.close()


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user = update.effective_user

    save_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

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
        ],

    ]

    reply_markup = InlineKeyboardMarkup(
        keyboard
    )

    await update.message.reply_text(

        f"👋 Hello {user.first_name}!\n\n"

        "🤖 Welcome to Ethio AI.\n\n"

        "I am an intelligent AI assistant.\n\n"

        "You can:\n"
        "💬 Ask questions\n"
        "🖼️ Send images\n"
        "🌍 Use Afaan Oromoo or English\n"
        "⭐ Get Premium\n\n"

        "How can I help you?",

        reply_markup=reply_markup
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

    save_user(
        update.effective_user
    )

    await update.message.reply_text(

        "🤖 ETHIO AI — HELP\n\n"

        "You can talk to me naturally.\n\n"

        "💬 Send a message\n"
        "🖼️ Send an image and ask a question\n"
        "🇪🇹 Afaan Oromoo supported\n"
        "🇬🇧 English supported\n\n"

        "Commands:\n"
        "/start - Start Ethio AI\n"
        "/help - Help\n"
        "/premium - Premium\n\n"

        "👑 Admin commands are available only "
        "to the administrator."
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

    user_id = update.effective_user.id

    save_user(
        update.effective_user
    )

    if is_blocked(user_id):

        await update.message.reply_text(
            "🚫 Your access has been blocked."
        )

        return

    if is_premium(user_id):

        await update.message.reply_text(

            "⭐ ETHIO AI PREMIUM\n\n"

            "✅ Your Premium membership is active.\n\n"

            "Enjoy Ethio AI Premium!"
        )

        return

    keyboard = [

        [
            InlineKeyboardButton(
                f"⭐ Buy Premium — {PREMIUM_PRICE_STARS} Stars",
                callback_data="get_premium"
            )
        ]

    ]

    await update.message.reply_text(

        "⭐ ETHIO AI PREMIUM\n\n"

        f"Price: {PREMIUM_PRICE_STARS} Telegram Stars\n"
        f"Duration: {PREMIUM_DAYS} days\n\n"

        "Premium gives you access to "
        "Ethio AI Premium services.\n\n"

        "Click the button below to continue.",

        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# SEND PREMIUM INVOICE
# =========================================================

async def get_premium(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    save_user(user)

    if is_blocked(user.id):

        await query.message.reply_text(
            "🚫 Your access has been blocked."
        )

        return

    if is_premium(user.id):

        await query.message.reply_text(
            "⭐ You already have an active Premium membership."
        )

        return

    prices = [

        LabeledPrice(
            label="Ethio AI Premium - 30 Days",
            amount=PREMIUM_PRICE_STARS
        )

    ]

    try:

        await context.bot.send_invoice(

            chat_id=user.id,

            title="Ethio AI Premium",

            description=(
                "Ethio AI Premium access "
                "for 30 days."
            ),

            payload=PREMIUM_PAYLOAD,

            currency="XTR",

            prices=prices,

            provider_token=""

        )

    except Exception:

        logger.exception(
            "PREMIUM INVOICE ERROR"
        )

        await query.message.reply_text(
            "⚠️ I could not create the payment invoice."
        )


# =========================================================
# PRE-CHECKOUT
# =========================================================

async def precheckout_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.pre_checkout_query

    if not query:
        return

    try:

        if query.invoice_payload != PREMIUM_PAYLOAD:

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

    except Exception:

        logger.exception(
            "PRECHECKOUT ERROR"
        )

        try:

            await query.answer(
                ok=False,
                error_message=(
                    "Payment verification failed."
                )
            )

        except Exception:
            pass


# =========================================================
# SUCCESSFUL PAYMENT
# =========================================================

async def successful_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    save_user(user)

    payment = (
        update.message.successful_payment
    )

    if not payment:
        return

    if payment.invoice_payload != PREMIUM_PAYLOAD:
        return

    try:

        expiry = set_premium(
            user.id,
            PREMIUM_DAYS
        )

        expiry_text = expiry.strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        await update.message.reply_text(

            "🎉 PAYMENT SUCCESSFUL!\n\n"

            "⭐ Your Ethio AI Premium is now ACTIVE.\n\n"

            f"📅 Premium expires:\n"
            f"{expiry_text}\n\n"

            "Thank you for supporting Ethio AI! 🤖❤️"
        )

    except Exception:

        logger.exception(
            "PREMIUM ACTIVATION ERROR"
        )

        await update.message.reply_text(

            "⚠️ Payment was successful, "
            "but Premium activation encountered "
            "an error.\n\n"

            "Please contact the administrator."
        )


# =========================================================
# TEXT CHAT
# =========================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    user = update.effective_user

    save_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    user_message = (
        update.message.text or ""
    ).strip()

    if not user_message:
        return

    try:

        await update.effective_chat.send_action(
            action="typing"
        )

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Your name is Ethio AI.

If the user asks:
"What is your name?"
Answer:
"My name is Ethio AI."

If the user asks what technology you use,
answer:
"I am powered by Google Gemini."

Language rules:
- If the user writes Afaan Oromoo,
  respond in Afaan Oromoo.
- If the user writes English,
  respond in English.
- If the user mixes Afaan Oromoo and English,
  respond naturally using the same style.

Be helpful, accurate and clear.

User message:
{user_message}
"""

        # Run synchronous Gemini request
        # outside the Telegram event loop.
        response = await asyncio.to_thread(

            client.models.generate_content,

            model=MODEL,

            contents=prompt
        )

        answer = (

            response.text

            if response and response.text

            else None
        )

        if not answer:

            await update.message.reply_text(
                "⚠️ I could not generate a response."
            )

            return

        # Telegram message limit
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
            "TEXT CHAT ERROR"
        )

        await update.message.reply_text(

            "⚠️ Ethio AI error:\n\n"
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

    user = update.effective_user

    save_user(user)

    user_id = user.id

    if is_blocked(user_id):

        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )

        return

    question = (

        update.message.caption

        or
        "Please analyze this image carefully "
        "and explain what you see."

    ).strip()

    try:

        # -------------------------------------------------
        # TYPING
        # -------------------------------------------------

        await update.effective_chat.send_action(
            action="typing"
        )

        # -------------------------------------------------
        # CHECK PHOTO
        # -------------------------------------------------

        if not update.message.photo:

            await update.message.reply_text(
                "❌ I could not receive the image."
            )

            return

        # -------------------------------------------------
        # HIGHEST QUALITY PHOTO
        # -------------------------------------------------

        photo = update.message.photo[-1]

        # -------------------------------------------------
        # DOWNLOAD PHOTO
        # -------------------------------------------------

        telegram_file = await context.bot.get_file(
            photo.file_id
        )

        image_bytes = (
            await telegram_file.download_as_bytearray()
        )

        if not image_bytes:

            await update.message.reply_text(
                "❌ The image could not be downloaded."
            )

            return

        # -------------------------------------------------
        # GEMINI PROMPT
        # -------------------------------------------------

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Analyze the image carefully.

Important rules:

- Only describe things that can actually be seen.
- Do not invent information.
- Answer the user's question directly.
- If the user writes in Afaan Oromoo,
  respond in Afaan Oromoo.
- If the user writes in English,
  respond in English.
- Be clear and helpful.

User's question:

{question}
"""

        # -------------------------------------------------
        # CREATE GEMINI IMAGE PART
        # -------------------------------------------------

        image_part = types.Part.from_bytes(

            data=bytes(image_bytes),

            mime_type="image/jpeg"
        )

        # -------------------------------------------------
        # SEND IMAGE TO GEMINI
        # -------------------------------------------------

        response = await asyncio.to_thread(

            client.models.generate_content,

            model=MODEL,

            contents=[
                prompt,
                image_part
            ]
        )

        # -------------------------------------------------
        # RESPONSE
        # -------------------------------------------------

        answer = (

            response.text

            if response and response.text

            else None
        )

        if not answer:

            await update.message.reply_text(

                "⚠️ I received the image, "
                "but I could not generate an answer."
            )

            return

        # -------------------------------------------------
        # SEND ANSWER
        # -------------------------------------------------

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
            "IMAGE ANALYSIS ERROR"
        )

        await update.message.reply_text(

            "⚠️ Ethio AI could not analyze the image.\n\n"

            f"Error: {type(e).__name__}: {e}"
        )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):

    return user_id == ADMIN_ID


# =========================================================
# ADMIN DASHBOARD
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) AS count FROM users"
    )

    total_users = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE status = 'premium'
        AND premium_until > ?
        """,
        (
            datetime.now(
                timezone.utc
            ).isoformat(),
        )
    )

    premium_users = (
        cursor.fetchone()["count"]
    )

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE status = 'free'
        AND blocked = 0
        """
    )

    free_users = (
        cursor.fetchone()["count"]
    )

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE last_active > ?
        AND blocked = 0
        """,
        (
            (
                datetime.now(
                    timezone.utc
                )
                - timedelta(days=1)
            ).isoformat(),
        )
    )

    active_users = (
        cursor.fetchone()["count"]
    )

    conn.close()

    await update.message.reply_text(

        "🤖 ETHIO AI — ADMIN\n\n"

        f"👥 Total Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"⭐ Premium Users: {premium_users}\n"
        f"🆓 Free Users: {free_users}\n\n"

        "Admin Commands:\n"
        "/users\n"
        "/stats\n"
        "/premiumuser USER_ID\n"
        "/free USER_ID\n"
        "/block USER_ID\n"
        "/unblock USER_ID"
    )


# =========================================================
# USERS LIST
# =========================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            user_id,
            first_name,
            last_name,
            username,
            status,
            blocked,
            last_active,
            premium_until

        FROM users

        ORDER BY last_active DESC
        """
    )

    rows = cursor.fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "No users yet."
        )

        return

    text = "👥 ETHIO AI — USERS\n\n"

    for row in rows:

        name = (

            row["first_name"]

            or "Unknown"
        )

        if row["last_name"]:

            name += (
                " "
                + row["last_name"]
            )

        username = (

            "@"
            + row["username"]

            if row["username"]

            else "No username"
        )

        status = row["status"]

        if row["blocked"]:

            status = "🚫 BLOCKED"

        elif status == "premium":

            status = "⭐ PREMIUM"

        else:

            status = "🆓 FREE"

        text += (

            f"👤 {name}\n"
            f"{username}\n"
            f"🆔 ID: {row['user_id']}\n"
            f"{status}\n"
            f"🕐 Last active: "
            f"{row['last_active']}\n\n"
        )

        # Telegram message limit
        if len(text) > 3500:

            await update.message.reply_text(
                text
            )

            text = ""

    if text:

        await update.message.reply_text(
            text
        )


# =========================================================
# STATS
# =========================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) AS count FROM users"
    )

    total = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE status = 'premium'
        AND premium_until > ?
        """,
        (
            datetime.now(
                timezone.utc
            ).isoformat(),
        )
    )

    premium = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE status = 'free'
        AND blocked = 0
        """
    )

    free = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE blocked = 1
        """
    )

    blocked = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE last_active > ?
        AND blocked = 0
        """,
        (
            (
                datetime.now(
                    timezone.utc
                )
                - timedelta(days=1)
            ).isoformat(),
        )
    )

    active = cursor.fetchone()["count"]

    conn.close()

    await update.message.reply_text(

        "📊 ETHIO AI — STATISTICS\n\n"

        f"👥 Total Users: {total}\n"
        f"🟢 Active (24h): {active}\n"
        f"⭐ Premium: {premium}\n"
        f"🆓 Free: {free}\n"
        f"🚫 Blocked: {blocked}"
    )


# =========================================================
# ADMIN PREMIUM USER
# =========================================================

async def premium_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    if not context.args:

        await update.message.reply_text(

            "Usage:\n"
            "/premiumuser USER_ID"
        )

        return

    try:

        target_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID must be a number."
        )

        return

    expiry = set_premium(
        target_id,
        PREMIUM_DAYS
    )

    await update.message.reply_text(

        "⭐ Premium activated.\n\n"

        f"User ID: {target_id}\n"
        f"Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}"
    )


# =========================================================
# FREE USER
# =========================================================

async def free_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Usage:\n/free USER_ID"
        )

        return

    try:

        target_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID must be a number."
        )

        return

    set_free(target_id)

    await update.message.reply_text(

        "🆓 User changed to Free.\n\n"

        f"User ID: {target_id}"
    )


# =========================================================
# BLOCK USER
# =========================================================

async def block_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Usage:\n/block USER_ID"
        )

        return

    try:

        target_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID must be a number."
        )

        return

    set_block(target_id)

    await update.message.reply_text(

        "🚫 User blocked.\n\n"

        f"User ID: {target_id}"
    )


# =========================================================
# UNBLOCK USER
# =========================================================

async def unblock_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "🚫 Admin only."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "Usage:\n/unblock USER_ID"
        )

        return

    try:

        target_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID must be a number."
        )

        return

    set_unblock(target_id)

    await update.message.reply_text(

        "🟢 User unblocked.\n\n"

        f"User ID: {target_id}"
    )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    save_user(user)

    if is_blocked(user.id):

        await query.message.reply_text(
            "🚫 Your access has been blocked."
        )

        return

    if query.data == "get_premium":

        # Send Premium invoice
        prices = [

            LabeledPrice(
                label="Ethio AI Premium - 30 Days",
                amount=PREMIUM_PRICE_STARS
            )

        ]

        try:

            await context.bot.send_invoice(

                chat_id=user.id,

                title="Ethio AI Premium",

                description=(
                    "Ethio AI Premium access "
                    "for 30 days."
                ),

                payload=PREMIUM_PAYLOAD,

                currency="XTR",

                prices=prices,

                provider_token=""
            )

        except Exception:

            logger.exception(
                "BUTTON PREMIUM ERROR"
            )

            await query.message.reply_text(
                "⚠️ Payment invoice could not be created."
            )

        return

    if query.data == "help":

        await query.message.reply_text(

            "ℹ️ ETHIO AI HELP\n\n"

            "💬 Send me any question.\n"
            "🖼️ Send an image and ask me about it.\n"
            "🇪🇹 Afaan Oromoo supported.\n"
            "🇬🇧 English supported.\n\n"

            "Commands:\n"
            "/start\n"
            "/help\n"
            "/premium"
        )

        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "UNHANDLED ERROR",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    # Initialize database
    init_database()

    logger.info(
        "Starting Ethio AI..."
    )

    logger.info(
        f"Gemini model: {MODEL}"
    )

    logger.info(
        f"Admin ID: {ADMIN_ID}"
    )

    # Create Telegram application
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ADMIN COMMANDS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # PAYMENT
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # BUTTONS
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # -----------------------------------------------------
    # IMAGE
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            image_chat
        )
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat
        )
    )

    # -----------------------------------------------------
    # ERROR HANDLER
    # -----------------------------------------------------

    app.add_error_handler(
        error_handler
    )

    # -----------------------------------------------------
    # START BOT
    # -----------------------------------------------------

    logger.info(
        "Ethio AI is running..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
