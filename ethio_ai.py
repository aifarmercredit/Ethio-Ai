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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DATABASE = "ethio_ai_users.db"

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

client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

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
                last_active
            )
            VALUES (?, ?, ?, ?, 'free', 0, ?, ?)
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

    return bool(row and row["blocked"])


# =========================
# ADMIN
# =========================

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


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
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


# =========================
# HELP
# =========================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user or not update.message:
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
        "You can simply type your question."
    )


# =========================
# ADMIN DASHBOARD
# =========================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await require_admin(update):
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

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=24)
    ).isoformat()

    active = conn.execute(
        "SELECT COUNT(*) FROM users WHERE last_active >= ?",
        (cutoff,)
    ).fetchone()[0]

    conn.close()

    await update.message.reply_text(
        "🤖 ETHIO AI — ADMIN\n\n"
        f"👥 Total Users: {total}\n"
        f"🟢 Active Users: {active}\n"
        f"⭐ Premium Users: {premium}\n"
        f"🆓 Free Users: {free}\n\n"
        "📊 Admin Commands\n"
        "────────────────────\n"
        "/users - View all users\n"
        "/stats - Show statistics\n"
        "/premium ID - Make user premium\n"
        "/free ID - Make user free\n"
        "/block ID - Block user\n"
        "/unblock ID - Unblock user\n"
        "────────────────────"
    )


# =========================
# USERS
# =========================

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await require_admin(update):
        return

    conn = get_db()

    users = conn.execute(
        "SELECT * FROM users ORDER BY last_active DESC"
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
            f"{user['first_name']} {user['last_name']}"
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
        elif user["status"] == "premium":
            status = "⭐ Premium"
        else:
            status = "🆓 Free"

        try:
            dt = datetime.fromisoformat(
                user["last_active"]
            )

            last_active = dt.astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )

        except Exception:
            last_active = "Unknown"

        text += (
            "────────────────────\n"
            f"👤 {name}\n"
            f"{username}\n"
            f"🆔 ID: {user['user_id']}\n"
            f"{status}\n"
            f"🕐 Last active: {last_active}\n"
        )

    for i in range(0, len(text), 4000):
        await update.message.reply_text(
            text[i:i + 4000]
        )


# =========================
# STATS
# =========================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await require_admin(update):
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


# =========================
# PREMIUM / FREE
# =========================

async def set_status(update, context, status):

    if not await require_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            f"Usage:\n/{status} USER_ID"
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
        "UPDATE users SET status=? WHERE user_id=?",
        (status, user_id)
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


async def premium_command(update, context):
    await set_status(
        update,
        context,
        "premium"
    )


async def free_command(update, context):
    await set_status(
        update,
        context,
        "free"
    )


# =========================
# BLOCK / UNBLOCK
# =========================

async def set_block(update, context, blocked):

    if not await require_admin(update):
        return

    if not context.args:
        command = "block" if blocked else "unblock"

        await update.message.reply_text(
            f"Usage:\n/{command} USER_ID"
        )
        return

    try:
        user_id = int(context.args[0])

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
        "UPDATE users SET blocked=? WHERE user_id=?",
        (1 if blocked else 0, user_id)
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


async def block_command(update, context):
    await set_block(
        update,
        context,
        True
    )


async def unblock_command(update, context):
    await set_block(
        update,
        context,
        False
    )


# =========================
# GEMINI CHAT
# =========================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    if not update.effective_user:
        return

    save_user(update.effective_user)

    if is_blocked(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    question = update.message.text.strip()

    if not question:
        return

    try:

        await update.effective_chat.send_action(
            "typing"
        )

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Identity rules:
- Your assistant name is "Ethio AI".
- If asked "What is your name?", answer:
  "My name is Ethio AI."
- If asked what technology powers you, answer:
  "I am powered by Google Gemini."
- Do not say your name is Gemini.
- Do not invent a creator, company, person, or organization.
- Be helpful, polite, clear, and concise.
- You can communicate in English and Afaan Oromoo.
- If the user writes in Afaan Oromoo,
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

        for i in range(0, len(answer), 4000):

            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception as e:
    logger.exception("GEMINI ERROR")
    await update.message.reply_text(
        f"⚠️ Ethio AI error:\n{type(e).__name__}: {e}"
    )

# =========================
# ERROR
# =========================

async def error_handler(update, context):

    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error
    )


# =========================
# MAIN
# =========================

def main():

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

    # AI chat
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat
        )
    )

    app.add_error_handler(
        error_handler
    )

    print("✅ Ethio AI is running!")

    app.run_polling()


if __name__ == "__main__":
    main()
