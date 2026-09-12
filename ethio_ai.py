import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai


# =========================================================
# CONFIGURATION
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Put your Telegram numeric ID in ADMIN_ID environment variable.
# Example:
# ADMIN_ID=123456789
ADMIN_ID = os.getenv("ADMIN_ID")

MODEL = "gemini-3.6-flash"
DATABASE = "ethio_ai_users.db"


# =========================================================
# CHECK ENVIRONMENT
# =========================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing.")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing.")

if not ADMIN_ID:
    raise RuntimeError(
        "ADMIN_ID is missing. Set ADMIN_ID to your Telegram numeric ID."
    )

try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram ID.")


# =========================================================
# GEMINI
# =========================================================

client = genai.Client(api_key=GEMINI_API_KEY)


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
            last_active TEXT
        )
    """)

    conn.commit()
    conn.close()


def save_user(user):
    if not user:
        return

    now = datetime.now(timezone.utc).isoformat()

    first_name = user.first_name or ""
    last_name = user.last_name or ""
    username = user.username or ""

    conn = get_db()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE users
            SET first_name = ?,
                last_name = ?,
                username = ?,
                last_active = ?
            WHERE user_id = ?
            """,
            (
                first_name,
                last_name,
                username,
                now,
                user.id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO users
            (
                user_id,
                first_name,
                last_name,
                username,
                status,
                blocked,
                created_at,
                last_active
            )
            VALUES (?, ?, ?, ?, 'free', 0, ?, ?)
            """,
            (
                user.id,
                first_name,
                last_name,
                username,
                now,
                now,
            ),
        )

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_db()

    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    conn.close()

    return row


def is_blocked(user_id):
    row = get_user(user_id)

    if not row:
        return False

    return bool(row["blocked"])


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(update: Update):
    if not update.effective_user:
        return False

    return update.effective_user.id == ADMIN_ID


async def admin_only(update: Update):
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    save_user(update.effective_user)

    if is_blocked(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    await update.message.reply_text(
        "👋 Hello!\n\n"
        "🤖 I am Ethio AI.\n"
        "Your intelligent AI assistant powered by Google Gemini.\n\n"
        "Ask me anything and I will try my best to help you.\n\n"
        "Use /help to see available commands."
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    save_user(update.effective_user)

    if is_blocked(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    await update.message.reply_text(
        "🤖 ETHIO AI\n\n"
        "I am your AI assistant powered by Google Gemini.\n\n"
        "Send me any question and I will try my best to help you.\n\n"
        "📌 Commands:\n"
        "/start - Start Ethio AI\n"
        "/help - Show this help message\n\n"
        "You can simply type your question without using a command."
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    conn = get_db()

    total_users = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    premium_users = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status = 'premium'"
    ).fetchone()[0]

    free_users = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status = 'free'"
    ).fetchone()[0]

    today = datetime.now(timezone.utc) - timedelta(hours=24)

    active_users = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE last_active >= ?
        """,
        (today.isoformat(),),
    ).fetchone()[0]

    conn.close()

    text = (
        "🤖 ETHIO AI — ADMIN\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"⭐ Premium Users: {premium_users}\n"
        f"🆓 Free Users: {free_users}\n\n"
        "📊 Admin Commands\n"
        "────────────────────\n"
        "/users - View all users\n"
        "/premium ID - Make user premium\n"
        "/free ID - Make user free\n"
        "/block ID - Block user\n"
        "/unblock ID - Unblock user\n"
        "/stats - Show statistics\n"
        "────────────────────"
    )

    await update.message.reply_text(text)


# =========================================================
# USERS LIST
# =========================================================

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    conn = get_db()

    users = conn.execute(
        """
        SELECT *
        FROM users
        ORDER BY last_active DESC
        """
    ).fetchall()

    conn.close()

    if not users:
        await update.message.reply_text(
            "👥 No users found."
        )
        return

    text = "👥 ETHIO AI — USERS\n\n"

    for user in users:

        first_name = user["first_name"] or "Unknown"
        last_name = user["last_name"] or ""

        full_name = f"{first_name} {last_name}".strip()

        username = user["username"]

        if username:
            username_text = f"@{username}"
        else:
            username_text = "No username"

        status = user["status"]

        if status == "premium":
            status_icon = "⭐ Premium"
        else:
            status_icon = "🆓 Free"

        if user["blocked"]:
            status_icon = "🚫 Blocked"

        last_active = user["last_active"]

        try:
            dt = datetime.fromisoformat(last_active)
            last_active_text = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            last_active_text = "Unknown"

        text += (
            "────────────────────\n"
            f"👤 {full_name}\n"
            f"{username_text}\n"
            f"🆔 ID: {user['user_id']}\n"
            f"{status_icon}\n"
            f"🕐 Last active: {last_active_text}\n"
        )

    await update.message.reply_text(text)


# =========================================================
# STATS
# =========================================================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    premium = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status='premium'"
    ).fetchone()[0]

    free = conn.execute(
        "SELECT COUNT(*) FROM users WHERE status='free'"
    ).fetchone()[0]

    blocked = conn.execute(
        "SELECT COUNT(*) FROM users WHERE blocked=1"
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
# PREMIUM
# =========================================================

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/premium USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )
        return

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET status='premium'
        WHERE user_id=?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    await update.message.reply_text(
        f"⭐ User {user_id} is now PREMIUM."
    )


# =========================================================
# FREE
# =========================================================

async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/free USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )
        return

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET status='free'
        WHERE user_id=?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    await update.message.reply_text(
        f"🆓 User {user_id} is now FREE."
    )


# =========================================================
# BLOCK
# =========================================================

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/block USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )
        return

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET blocked=1
        WHERE user_id=?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    await update.message.reply_text(
        f"🚫 User {user_id} has been BLOCKED."
    )


# =========================================================
# UNBLOCK
# =========================================================

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await admin_only(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/unblock USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid user ID."
        )
        return

    conn = get_db()

    result = conn.execute(
        """
        UPDATE users
        SET blocked=0
        WHERE user_id=?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    if result.rowcount == 0:
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    await update.message.reply_text(
        f"✅ User {user_id} has been UNBLOCKED."
    )


# =========================================================
# CHAT WITH GEMINI
# =========================================================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    if not update.effective_user:
        return

    # Save/update user information
    save_user(update.effective_user)

    # Blocked users cannot use AI
    if is_blocked(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    question = update.message.text.strip()

    if not question:
        return

    try:

        await update.effective_chat.send_action("typing")

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Identity rules:
- Your assistant name is "Ethio AI".
- If the user asks "What is your name?", answer:
  "My name is Ethio AI."
- If the user asks what technology powers you, answer accurately:
  "I am powered by Google Gemini."
- Do not say your name is Gemini.
- Do not invent a creator, company, person, or organization.
- Be helpful, polite, clear, and concise.
- You can communicate in English and Afaan Oromoo.
- If the user writes in Afaan Oromoo, respond in Afaan Oromoo when possible.

User question:
{question}
"""

        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
        )

        answer = response.text or (
            "Sorry, I could not generate an answer."
        )

        # Telegram message maximum length
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception:

        logger.exception(
            "Gemini request failed"
        )

        await update.message.reply_text(
            "⚠️ Sorry, Ethio AI could not process "
            "your request right now.\n\n"
            "Please try again in a moment."
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    # Create database
    init_database()

    print("=" * 55)
    print("                    ETHIO AI")
    print("=" * 55)
    print("Starting Ethio AI Telegram Bot...")
    print(f"Gemini model: {MODEL}")
    print(f"Admin ID: {ADMIN_ID}")
    print()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # User commands
    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_command)
    )

    # Admin commands
    app.add_handler(
        CommandHandler("admin", admin_command)
    )

    app.add_handler(
        CommandHandler("users", users_command)
    )

    app.add_handler(
        CommandHandler("stats", stats_command)
    )

    app.add_handler(
        CommandHandler("premium", premium_command)
    )

    app.add_handler(
        CommandHandler("free", free_command)
    )

    app.add_handler(
        CommandHandler("block", block_command)
    )

    app.add_handler(
        CommandHandler("unblock", unblock_command)
    )

    # Normal AI chat
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat
        )
    )

    app.add_error_handler(error_handler)

    print("✅ Ethio AI is running!")
    print("Open Telegram and send /start")
    print()

    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
