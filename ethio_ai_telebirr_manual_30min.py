import asyncio
import logging
import os
import sqlite3
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
    "gemini-3.6-flash",
)

DATABASE = os.getenv(
    "DATABASE",
    "ethio_ai_users.db",
)

BOT_USERNAME = "ethio_ai_gemini_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

# False = menu/button based bot; ordinary typed text is not sent to Gemini.
TEXT_CHAT_ENABLED = True


# =========================================================
# FREE / PREMIUM SETTINGS
# =========================================================

FREE_DAILY_IMAGE_LIMIT = 10

MONTHLY_PRICE_ETB = 100
MONTHLY_DAYS = 30

YEARLY_PRICE_ETB = 1200
YEARLY_DAYS = 365

TELEBIRR_NAME = "Amir Ali"
TELEBIRR_PHONE = "0967767646"

# Referral reward is configurable. Keep 0 until you decide the reward amount.
REFERRAL_REWARD_ETB = int(os.getenv("REFERRAL_REWARD_ETB", "1"))
MIN_WITHDRAW_ETB = int(os.getenv("MIN_WITHDRAW_ETB", "1"))


# =========================================================
# VALIDATE ENVIRONMENT
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
    raise RuntimeError("ADMIN_ID must be a number.")


# =========================================================
# GEMINI CLIENT
# =========================================================

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# TIME
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE,
        check_same_thread=False,
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

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS image_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            image_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount_etb INTEGER NOT NULL,
            screenshot_file_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            rejection_reason TEXT
        )
        """
    )

    # Persistent SMS-payment fields. These survive Railway restarts.
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN payment_proof_text TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE payment_requests ADD COLUMN expires_at TEXT")
    except Exception:
        pass

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_etb INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            rejection_reason TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS affiliate_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL,
            amount_etb INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # Referral columns are added safely to existing databases.
    try:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN balance_etb INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# =========================================================
# USER FUNCTIONS
# =========================================================

def save_user(user):
    if not user:
        return

    now = now_iso()

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
        ),
    )

    conn.commit()
    conn.close()


def is_admin(user_id):
    return user_id == ADMIN_ID


def is_blocked(user_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT blocked FROM users WHERE user_id = ?",
        (user_id,),
    )

    row = cursor.fetchone()
    conn.close()

    return bool(row["blocked"]) if row else False


def expire_premium_if_needed(user_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT status, premium_until
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )

    row = cursor.fetchone()

    if not row:
        conn.close()
        return False

    if row["status"] != "premium" or not row["premium_until"]:
        conn.close()
        return False

    try:
        expiry = datetime.fromisoformat(row["premium_until"])

        if expiry > now_utc():
            conn.close()
            return True

    except Exception:
        pass

    cursor.execute(
        """
        UPDATE users
        SET status = 'free',
            premium_until = NULL
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()

    return False


def is_premium(user_id):
    return expire_premium_if_needed(user_id)


def set_premium(user_id, days):
    now = now_utc()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT premium_until
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )

    row = cursor.fetchone()

    if row and row["premium_until"]:
        try:
            old_expiry = datetime.fromisoformat(
                row["premium_until"]
            )
        except Exception:
            old_expiry = None
    else:
        old_expiry = None

    if old_expiry and old_expiry > now:
        expiry = old_expiry + timedelta(days=days)
    else:
        expiry = now + timedelta(days=days)

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
        ),
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
        (user_id,),
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
        (user_id,),
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
        (user_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# FREE IMAGE LIMIT
# =========================================================

def get_today_image_count(user_id):
    today = now_utc().date().isoformat()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT image_count
        FROM image_usage
        WHERE user_id = ?
        AND usage_date = ?
        """,
        (user_id, today),
    )

    row = cursor.fetchone()
    conn.close()

    return int(row["image_count"]) if row else 0


def can_use_free_image(user_id):
    if is_premium(user_id):
        return True

    return get_today_image_count(user_id) < FREE_DAILY_IMAGE_LIMIT


def record_free_image(user_id):
    today = now_utc().date().isoformat()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO image_usage (
            user_id,
            usage_date,
            image_count
        )
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_date)
        DO UPDATE SET
            image_count = image_count + 1
        """,
        (user_id, today),
    )

    conn.commit()
    conn.close()


# =========================================================
# PAYMENT REQUESTS
# =========================================================

def create_sms_payment_request(user_id, plan, amount_etb):
    """Create a persistent 30-minute Telebirr SMS payment request."""
    expires = now_utc() + timedelta(minutes=30)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO payment_requests
            (user_id, plan, amount_etb, screenshot_file_id, status, created_at, payment_proof_text, expires_at)
        VALUES (?, ?, ?, '', 'waiting_sms', ?, '', ?)
        """,
        (user_id, plan, amount_etb, now_iso(), expires.isoformat()),
    )
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return request_id, expires


def get_payment_request(request_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_active_sms_payment(user_id):
    """Return the newest unexpired SMS request, or None."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM payment_requests
        WHERE user_id = ?
          AND status IN ('waiting_sms', 'pending')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if row and row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if now_utc() >= exp and row["status"] in ('waiting_sms', 'pending'):
                cursor.execute(
                    "UPDATE payment_requests SET status='expired', reviewed_at=? WHERE id=?",
                    (now_iso(), row["id"]),
                )
                conn.commit()
                row = None
        except Exception:
            logger.exception("Could not parse payment expiry")
    conn.close()
    return row


def has_pending_payment(user_id):
    return get_active_sms_payment(user_id) is not None


def save_payment_sms(request_id, sms_text):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE payment_requests
        SET payment_proof_text = ?, status = 'pending'
        WHERE id = ? AND status = 'waiting_sms'
        """,
        (sms_text, request_id),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def expire_old_payment_requests():
    """Mark old 30-minute requests expired. Safe to run at startup and periodically."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE payment_requests
        SET status='expired', reviewed_at=?
        WHERE status IN ('waiting_sms', 'pending')
          AND expires_at IS NOT NULL
          AND expires_at <= ?
        """,
        (now_iso(), now_iso()),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def update_payment_request(request_id, status, reviewer_id, rejection_reason=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE payment_requests
        SET status=?, reviewed_at=?, reviewed_by=?, rejection_reason=?
        WHERE id=? AND status IN ('waiting_sms','pending')
        """,
        (status, now_iso(), reviewer_id, rejection_reason, request_id),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed > 0


# =========================================================
# BALANCE + AFFILIATE + WITHDRAWALS
# =========================================================

def get_account_financials(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COALESCE(balance_etb,0) AS balance, COALESCE(referral_count,0) AS invites FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    cursor.execute(
        "SELECT COALESCE(SUM(amount_etb),0) AS lifetime FROM affiliate_earnings WHERE user_id = ?",
        (user_id,),
    )
    lifetime = cursor.fetchone()["lifetime"]
    cursor.execute(
        "SELECT COALESCE(SUM(amount_etb),0) AS withdrawn FROM withdrawals WHERE user_id = ? AND status = 'approved'",
        (user_id,),
    )
    withdrawn = cursor.fetchone()["withdrawn"]
    conn.close()
    if not row:
        return 0, 0, int(lifetime or 0), int(withdrawn or 0)
    return int(row["balance"]), int(row["invites"]), int(lifetime or 0), int(withdrawn or 0)


def get_balance(user_id):
    return get_account_financials(user_id)[0]


def get_referral_count(user_id):
    return get_account_financials(user_id)[1]


def get_referral_link(user_id):
    return f"{BOT_LINK}?start=ref_{user_id}"


def credit_balance(user_id, amount):
    if amount <= 0:
        return
    conn = get_db()
    conn.execute(
        "UPDATE users SET balance_etb = COALESCE(balance_etb,0) + ? WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def process_referral(referrer_id, new_user_id):
    """Register a referral exactly once and credit the configured reward."""
    if not referrer_id or referrer_id == new_user_id:
        return False
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT referred_by FROM users WHERE user_id = ?", (new_user_id,))
        new_user = cur.fetchone()
        if not new_user or new_user["referred_by"] is not None:
            conn.close()
            return False
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,))
        if not cur.fetchone():
            conn.close()
            return False
        cur.execute(
            "UPDATE users SET referred_by = ?, referral_count = COALESCE(referral_count,0) WHERE user_id = ? AND referred_by IS NULL",
            (referrer_id, new_user_id),
        )
        if cur.rowcount != 1:
            conn.rollback(); conn.close(); return False
        cur.execute(
            "UPDATE users SET referral_count = COALESCE(referral_count,0) + 1 WHERE user_id = ?",
            (referrer_id,),
        )
        if REFERRAL_REWARD_ETB > 0:
            cur.execute(
                "UPDATE users SET balance_etb = COALESCE(balance_etb,0) + ? WHERE user_id = ?",
                (REFERRAL_REWARD_ETB, referrer_id),
            )
            cur.execute(
                "INSERT INTO affiliate_earnings (user_id, referred_user_id, amount_etb, created_at) VALUES (?,?,?,?)",
                (referrer_id, new_user_id, REFERRAL_REWARD_ETB, now_iso()),
            )
        conn.commit(); conn.close(); return True
    except Exception:
        conn.rollback(); conn.close()
        logger.exception("REFERRAL ERROR")
        return False


def build_invite_text():
    return (
        "💎 ETHIO AI PRO 🇪🇹\n\n"
        "ሰላም! 👋 የETHIO AI PREMIUMን ይሞክሩ!\n\n"
        "🤖 ፈጣንና ብልህ የAI ረዳት!\n\n"
        "✨ PREMIUM ጥቅሞች:\n"
        "✅ Unlimited AI Chat\n"
        "🖼️ Unlimited Image Analysis\n"
        "⚡ Fast AI Responses\n"
        "💎 Premium Access\n\n"
        "💰 MONTHLY — 100 ETB / 30 Days\n"
        "👑 YEARLY — 1,200 ETB / 365 Days\n\n"
        "🇬🇧 English:\n\n"
        "🚀 Upgrade to ETHIO AI PREMIUM!\n\n"
        "Enjoy:\n"
        "✅ Unlimited AI Chat\n"
        "🖼️ Unlimited Image Analysis\n"
        "⚡ Fast AI Responses\n"
        "💎 Premium Access\n\n"
        "💚 Monthly — 100 ETB / 30 Days\n"
        "👑 Yearly — 1,200 ETB / 365 Days\n\n"
        "💳 Telebirr payment available\n"
        "🔜 Other payment methods are coming soon!\n\n"
        "👇 ለመጀመር ከታች ያለውን ይጫኑ።"
    )


def account_info_text(user):
    username = f"@{user.username}" if user.username else "Not set"
    balance, invites, lifetime, withdrawn = get_account_financials(user.id)
    return (
        "👤 Account Info\n"
        f"Name: {user.first_name or 'User'}\n"
        f"User ID: {user.id}\n"
        f"Username: {username}\n\n"
        "---\n\n"
        "💰 My Balance\n"
        "Current Balance:\n"
        f"💵 {balance:.2f} ETB\n\n"
        "---\n\n"
        "📈 Affiliate Stats\n"
        f"Total Invites: {invites}\n"
        f"Lifetime Earnings: {lifetime:.2f} ETB\n"
        f"Total Withdrawn: {withdrawn:.2f} ETB"
    )


async def send_balance_page(update, user):
    text = account_info_text(user)
    keyboard = [[InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_menu")]]
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    save_user(user)
    if is_blocked(user.id):
        await update.message.reply_text("🚫 Your access has been blocked.")
        return
    await send_balance_page(update, user)


async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    save_user(user)
    if is_blocked(user.id):
        await update.message.reply_text("🚫 Your access has been blocked.")
        return
    balance = get_balance(user.id)
    if balance < MIN_WITHDRAW_ETB:
        await update.message.reply_text(
            f"💸 Withdraw\n\nYour current balance is {balance:.2f} ETB.\n"
            f"Minimum withdrawal: {MIN_WITHDRAW_ETB} ETB."
        )
        return
    await update.message.reply_text(
        "💸 Withdraw\n\n"
        f"Available: {balance:.2f} ETB\n"
        f"Minimum: {MIN_WITHDRAW_ETB} ETB\n\n"
        "Use /withdraw AMOUNT\n"
        "Example: /withdraw 50"
    )


async def create_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a withdrawal request without allowing the same balance to be spent twice."""
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    save_user(user)
    if is_blocked(user.id):
        await update.message.reply_text("🚫 Your access has been blocked.")
        return

    if not context.args:
        await withdraw_command(update, context)
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Enter a valid whole ETB amount. Example: /withdraw 5")
        return

    if amount < MIN_WITHDRAW_ETB:
        await update.message.reply_text(f"❌ Minimum withdrawal is {MIN_WITHDRAW_ETB} ETB.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(balance_etb,0) AS balance FROM users WHERE user_id=?", (user.id,))
    row = cur.fetchone()
    balance = int(row["balance"] if row else 0)

    if amount > balance:
        conn.close()
        await update.message.reply_text(f"❌ Insufficient balance. Available: {balance:.2f} ETB")
        return

    # Reserve the money immediately. It is returned automatically if admin rejects.
    cur.execute(
        "UPDATE users SET balance_etb = balance_etb - ? WHERE user_id=? AND COALESCE(balance_etb,0) >= ?",
        (amount, user.id, amount),
    )
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        await update.message.reply_text("❌ Your balance changed. Please try again.")
        return

    cur.execute(
        "INSERT INTO withdrawals (user_id, amount_etb, status, created_at) VALUES (?,?, 'pending', ?)",
        (user.id, amount, now_iso()),
    )
    wid = cur.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Withdrawal request #{wid} submitted.\n\n"
        f"💵 Amount: {amount:.2f} ETB\n"
        "⏳ Status: Pending admin review."
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "💸 NEW WITHDRAWAL REQUEST\n\n"
                f"Request ID: {wid}\n"
                f"User: {user.first_name or 'User'}\n"
                f"User ID: {user.id}\n"
                f"Username: @{user.username if user.username else 'none'}\n"
                f"Amount: {amount} ETB\n\n"
                f"Approve: /approvewithdraw {wid}\n"
                f"Reject: /rejectwithdraw {wid}"
            ),
        )
    except Exception:
        logger.exception("Could not notify admin about withdrawal")


async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    save_user(user)
    balance = get_balance(user.id)
    if balance < MIN_WITHDRAW_ETB:
        await query.message.reply_text(
            f"💸 Withdraw\n\nYour current balance is {balance:.2f} ETB.\n"
            f"Minimum withdrawal: {MIN_WITHDRAW_ETB} ETB.\n\n"
            "Invite friends using your personal referral link to earn balance."
        )
        return
    await query.message.reply_text(
        "💸 Withdraw\n\n"
        f"Available balance: {balance:.2f} ETB\n"
        f"Minimum withdrawal: {MIN_WITHDRAW_ETB} ETB\n\n"
        "Send:\n/withdraw AMOUNT\n\n"
        "Example:\n/withdraw 5"
    )


async def pending_withdrawals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    conn = get_db()
    rows = conn.execute(
        "SELECT id,user_id,amount_etb,created_at FROM withdrawals WHERE status='pending' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("💸 Pending withdrawals: 0")
        return
    lines = ["💸 PENDING WITHDRAWALS\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} — User {r['user_id']} — {r['amount_etb']:.2f} ETB\n"
            f"/approvewithdraw {r['id']}  |  /rejectwithdraw {r['id']}"
        )
    await update.message.reply_text("\n\n".join(lines))


async def approve_withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approvewithdraw REQUEST_ID")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid request ID.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM withdrawals WHERE id=?", (wid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("❌ Withdrawal request not found.")
        return
    if row["status"] != "pending":
        conn.close()
        await update.message.reply_text(f"⚠️ Request #{wid} is already {row['status']}.")
        return

    cur.execute("UPDATE withdrawals SET status='approved' WHERE id=? AND status='pending'", (wid,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Withdrawal #{wid} approved.\n\n"
        f"💵 Amount: {row['amount_etb']:.2f} ETB\n"
        f"👤 User ID: {row['user_id']}\n\n"
        "⚠️ Send the money to the user's registered payout account manually."
    )
    try:
        await context.bot.send_message(
            chat_id=row["user_id"],
            text=(
                "✅ Your withdrawal was approved!\n\n"
                f"💵 Amount: {row['amount_etb']:.2f} ETB\n"
                f"📋 Request: #{wid}\n\n"
                "Please wait for the payout confirmation."
            ),
        )
    except Exception:
        logger.exception("Could not notify withdrawal user")


async def reject_withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /rejectwithdraw REQUEST_ID")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid request ID.")
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM withdrawals WHERE id=?", (wid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("❌ Withdrawal request not found.")
        return
    if row["status"] != "pending":
        conn.close()
        await update.message.reply_text(f"⚠️ Request #{wid} is already {row['status']}.")
        return

    # Return the reserved amount to the user's balance.
    cur.execute("UPDATE users SET balance_etb = COALESCE(balance_etb,0) + ? WHERE user_id=?", (row["amount_etb"], row["user_id"]))
    cur.execute("UPDATE withdrawals SET status='rejected' WHERE id=? AND status='pending'", (wid,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"❌ Withdrawal #{wid} rejected. Balance returned to user.")
    try:
        await context.bot.send_message(
            chat_id=row["user_id"],
            text=(
                "❌ Your withdrawal was rejected.\n\n"
                f"💵 Amount returned: {row['amount_etb']:.2f} ETB\n"
                f"📋 Request: #{wid}"
            ),
        )
    except Exception:
        logger.exception("Could not notify rejected withdrawal user")

async def referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    save_user(user)
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
            if process_referral(referrer_id, user.id):
                try:
                    reward_text = f"\n💵 Reward: {REFERRAL_REWARD_ETB} ETB" if REFERRAL_REWARD_ETB > 0 else ""
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎉 Namni tokko link kee irraa Ethio AI jalqabe!\n\n"
                            f"👥 Total Invites: {get_referral_count(referrer_id)}"
                            f"{reward_text}"
                        ),
                    )
                except Exception:
                    pass
        except ValueError:
            pass
    if is_blocked(user.id):
        await update.message.reply_text("🚫 Your access has been blocked.")
        return
    count = get_referral_count(user.id)
    await update.message.reply_text(
        "📈 ETHIO AI AFFILIATE\n\n"
        f"👥 Total Invites: {count}\n"
        f"💵 Referral Reward: {REFERRAL_REWARD_ETB:.2f} ETB per successful invite\n\n"
        f"🔗 Link kee:\n<code>{get_referral_link(user.id)}</code>\n\n"
        "📤 Link kana hiriyoota kee waliin qoodi!",
        parse_mode="HTML",
    )


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    save_user(user)

    # Register referral immediately when the user opens /start?ref_...
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
            if process_referral(referrer_id, user.id):
                try:
                    reward_text = f"\n💵 Reward: {REFERRAL_REWARD_ETB} ETB" if REFERRAL_REWARD_ETB > 0 else ""
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎉 Namni tokko link kee irraa Ethio AI jalqabe!\n\n"
                            f"👥 Total Invites: {get_referral_count(referrer_id)}"
                            f"{reward_text}"
                        ),
                    )
                except Exception:
                    pass
        except ValueError:
            pass

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    if is_premium(user.id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT premium_until FROM users WHERE user_id = ?",
            (user.id,),
        )
        row = cursor.fetchone()
        conn.close()

        expiry_text = "unknown"
        if row and row["premium_until"]:
            expiry = datetime.fromisoformat(row["premium_until"])
            expiry_text = expiry.strftime("%Y-%m-%d %H:%M UTC")

        text = (
            f"👋 Hello {user.first_name}!\n\n"
            "🤖 Welcome to Ethio AI.\n\n"
            "💎 Your Premium is ACTIVE.\n"
            f"📅 Expires: {expiry_text}\n\n"
            "💬 Send me a question or 🖼️ send an image."
        )
    else:
        text = (
            f"👋 Hello {user.first_name}!\n\n"
            "🤖 Welcome to Ethio AI.\n\n"
            "💬 You can chat with me for free.\n"
            f"🖼️ Free users can send up to {FREE_DAILY_IMAGE_LIMIT} images per day.\n"
            "💎 Upgrade to Premium for unlimited image analysis.\n\n"
            "How can I help you?"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "💎 Upgrade to Premium",
                callback_data="premium_menu",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 My Status",
                callback_data="my_status",
            ),
            InlineKeyboardButton(
                "💰 My balance",
                callback_data="my_balance",
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Invite Friend",
                url=(
                    "https://t.me/share/url"
                    f"?url={quote(get_referral_link(user.id), safe='')}"
                    f"&text={quote(build_invite_text(), safe='')}"
                ),
            )
        ],
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access has been blocked."
        )
        return

    await update.message.reply_text(
        "🤖 ETHIO AI — HELP\n\n"
        "💬 Send a text message to chat with me.\n"
        "🖼️ Send an image and I will analyze it.\n"
        f"🆓 Free users: {FREE_DAILY_IMAGE_LIMIT} images per day.\n"
        "💎 Premium users: unlimited image analysis.\n\n"
        "Commands:\n"
        "/start - Start Ethio AI\n"
        "/help - Help\n"
        "/premium - Upgrade to Premium\n"
        "/status - Check your Premium status\n"
        "/referrals - Your invite link and referral count"
    )


# =========================================================
# STATUS
# =========================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access has been blocked."
        )
        return

    if is_premium(user.id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT premium_until FROM users WHERE user_id = ?",
            (user.id,),
        )
        row = cursor.fetchone()
        conn.close()

        expiry = datetime.fromisoformat(row["premium_until"])
        remaining = expiry - now_utc()

        days = max(0, remaining.days)

        await update.message.reply_text(
            "💎 ETHIO AI Upgrade\n\n"
            "✅ Status: ACTIVE\n"
            f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"⏳ Remaining: {days} days\n"
            "🖼️ Images: UNLIMITED"
        )
    else:
        used = get_today_image_count(user.id)

        await update.message.reply_text(
            "🆓 ETHIO AI FREE\n\n"
            "✅ AI Chat: AVAILABLE\n"
            f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
            "💎 Premium: Not active"
        )


# =========================================================
# PREMIUM MENU
# =========================================================

async def premium_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        if update.message:
            await update.message.reply_text(
                "🚫 Your access has been blocked."
            )
        return

    if is_premium(user.id):
        if update.message:
            await status_command(update, context)
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"📅 Monthly — {MONTHLY_PRICE_ETB} ETB",
                callback_data="plan_monthly",
            )
        ],
        [
            InlineKeyboardButton(
                f"📅 Yearly — {YEARLY_PRICE_ETB} ETB",
                callback_data="plan_yearly",
            )
        ],
    ]

    text = (
        "💎 ETHIO AI PREMIUM\n\n"
        f"📅 Monthly: {MONTHLY_PRICE_ETB} ETB / {MONTHLY_DAYS} days\n"
        f"📅 Yearly: {YEARLY_PRICE_ETB} ETB / {YEARLY_DAYS} days\n\n"
        "Premium benefits:\n"
        "✅ AI Chat\n"
        "✅ Unlimited image analysis\n"
        "✅ Premium access until expiry\n\n"
        "👇 Select your plan:"
    )

    markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=markup,
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=markup,
        )


# =========================================================
# TELEBIRR PAYMENT INSTRUCTIONS
# =========================================================

async def show_payment_instructions(query, plan, amount):
    plan_name = "Monthly Premium" if plan == "monthly" else "Yearly Premium"

    # Do not create another request if this user already has an active one.
    existing = get_active_sms_payment(query.from_user.id)
    if existing:
        await query.answer("You already have an active payment request.", show_alert=True)
        return

    request_id, expires = create_sms_payment_request(query.from_user.id, plan, amount)
    expires_text = expires.astimezone(timezone.utc).strftime("%H:%M UTC")

    keyboard = [[InlineKeyboardButton("💳 I Paid — Send SMS", callback_data=f"paid_sms:{request_id}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="premium_menu")]]

    text = (
        "✅ የትዕዛዝዎ ማጠቃለያ\n\n"
        "ምርት፡  ETHIO AI upgrade\n"
        f"የእቅድ አይነት፡ {plan_name}\n"
        f"ዋጋ፡ {amount:.2f} ETB\n"
        "የክፍያ ዘዴ፡ Telebirr\n\n"
        f"እባክዎ በትክክል `{amount:.2f}` ETB ወደዚህ የTelebirr አካውንት ይላኩ፦\n\n"
        "• ስም፡ `Amir Ali`\n"
        "• Telebirr፡ `0967767646`\n\n"
        "⚠️ የክፍያ መመሪያዎች፦\n\n"
        f"1️⃣ በትክክል `{amount:.2f}` ETB ወደዚህ አካውንት ይላኩ።\n\n"
        "2️⃣ ክፍያውን ከፈጸሙ በኋላ ከTelebirr (127) የሚደርስዎትን የSMS ማረጋገጫ ይመልከቱ።\n\n"
        "3️⃣ ከTelebirr የደረሰዎትን ሙሉ የክፍያ SMS ኮፒ ያድርጉ።\n\n"
        "4️⃣ `💳 I Paid — Send SMS` ይጫኑ፣ achiis SMS guutuu asitti paste godhaa.\n\n"
        "5️⃣ SMS keessan gara adminitti ni ergama; adminiin kaffaltii ilaalee ACTIVE ykn AN ACTIVE filata.\n\n"
        "⏳ የክፍያ ጥያቄው 30 ደቂቃ ብቻ ይቆያል።\n"
        f"⏰ Expiry: {expires_text}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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

    data = query.data or ""

    if data == "premium_menu":
        await premium_menu(update, context)
        return

    if data == "my_status":
        if is_premium(user.id):
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT premium_until FROM users WHERE user_id = ?",
                (user.id,),
            )
            row = cursor.fetchone()
            conn.close()

            expiry = datetime.fromisoformat(row["premium_until"])

            await query.message.reply_text(
                "💎 PREMIUM STATUS\n\n"
                "✅ ACTIVE\n"
                f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
                "🖼️ Images: UNLIMITED"
            )
        else:
            used = get_today_image_count(user.id)
            await query.message.reply_text(
                "🆓 FREE STATUS\n\n"
                "✅ AI Chat: AVAILABLE\n"
                f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
                "💎 Premium: NOT ACTIVE"
            )
        return

    if data == "my_balance":
        await send_balance_page(update, user)
        return

    if data == "withdraw_menu":
        await withdraw_callback(update, context)
        return

    if data == "help":
        await query.message.reply_text(
            "🤖 ETHIO AI — HELP\n\n"
            "💬 Chat is available for FREE users.\n"
            f"🖼️ Free users: {FREE_DAILY_IMAGE_LIMIT} images/day.\n"
            "💎 Premium users: unlimited images.\n\n"
            "/premium - Upgrade\n"
            "/status - Check status"
        )
        return

    if data == "plan_monthly":
        await show_payment_instructions(
            query,
            "monthly",
            MONTHLY_PRICE_ETB,
        )
        return

    if data == "plan_yearly":
        await show_payment_instructions(
            query,
            "yearly",
            YEARLY_PRICE_ETB,
        )
        return

    if data.startswith("paid_sms:"):
        try:
            request_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("❌ Invalid payment request.")
            return
        request = get_payment_request(request_id)
        if not request or request["user_id"] != user.id:
            await query.message.reply_text("❌ Payment request not found.")
            return
        if request["status"] not in ("waiting_sms", "pending"):
            await query.message.reply_text("⏰ This payment request has expired. Please start again.")
            return
        if request["expires_at"] and now_utc() >= datetime.fromisoformat(request["expires_at"]):
            expire_old_payment_requests()
            await query.message.reply_text(
                "⏰ Payment request expired.\n\n"
                "30 minutes have passed and no payment SMS was received.\n\n"
                "Please go back and start a new payment request."
            )
            return
        context.user_data["awaiting_payment_sms_request_id"] = request_id
        await query.message.reply_text(
            "📩 SEND TELEBIRR SMS\n\n"
            "🇬🇧 Please paste the complete Telebirr payment SMS here.\n\n"
            "🇦🇲 እባክዎ ሙሉ የTelebirr የክፍያ SMS እዚህ ይላኩ።\n\n"
            "🇪🇹 SMS kaffaltii Telebirr guutuu asitti paste godhaa."
        )
        return

    if data.startswith("active_payment:"):
        if not is_admin(user.id):
            await query.message.reply_text("🚫 Admin only.")
            return
        try:
            request_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("❌ Invalid payment request.")
            return
        request = get_payment_request(request_id)
        if not request or request["status"] != "pending":
            await query.message.reply_text("❌ Payment request is not pending.")
            return
        days = MONTHLY_DAYS if request["plan"] == "monthly" else YEARLY_DAYS
        expiry = set_premium(request["user_id"], days)
        if not update_payment_request(request_id, "approved", user.id):
            await query.message.reply_text("⚠️ This request was already processed.")
            return
        try:
            await query.edit_message_text(
                (query.message.text or "") + f"\n\n🟢 ACTIVE by admin.\n📅 Premium expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=request["user_id"],
                text=(
                    "✅ Payment confirmed!\n\n"
                    "🇬🇧 💎 Your ETHIO AI Premium is now ACTIVE.\n"
                    f"📅 Plan: {request['plan'].title()}\n"
                    f"💰 Paid: {request['amount_etb']} ETB\n"
                    f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    "🇦🇲 ክፍያዎ ተረጋግጧል። Premium አባልነትዎ ነቅቷል።\n\n"
                    "🇪🇹 Kaffaltiin keessan mirkanaa'eera. Premium keessan hojii irra ooleera."
                )
            )
        except Exception:
            logger.exception("Could not notify user after activation")
        return

    if data.startswith("inactive_payment:"):
        if not is_admin(user.id):
            await query.message.reply_text("🚫 Admin only.")
            return
        try:
            request_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.message.reply_text("❌ Invalid payment request.")
            return
        request = get_payment_request(request_id)
        if not request or request["status"] != "pending":
            await query.message.reply_text("❌ Payment request is not pending.")
            return
        if not update_payment_request(request_id, "inactive", user.id, "Payment not received/confirmed by admin"):
            await query.message.reply_text("⚠️ This request was already processed.")
            return
        try:
            await query.edit_message_text((query.message.text or "") + "\n\n🔴 AN ACTIVE by admin.")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=request["user_id"],
                text=(
                    "❌ Payment not confirmed.\n\n"
                    "🇬🇧 The payment has not been received. Please check your payment and send the correct SMS.\n\n"
                    "🇦🇲 ክፍያው አልተረጋገጠም። እባክዎ ክፍያዎን ያረጋግጡና ትክክለኛውን SMS ይላኩ።\n\n"
                    "🇪🇹 Kaffaltiin hin mirkanoofne. Birri kaffaltan nu bira hin geenye. Maaloo kaffaltii keessan ilaalaa, SMS sirrii irra deebi'aa ergaa."
                )
            )
        except Exception:
            logger.exception("Could not notify user after inactive decision")
        return


# =========================================================
# PAYMENT SCREENSHOT
# =========================================================

async def payment_screenshot_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access has been blocked."
        )
        return

    plan = context.user_data.get(
        "awaiting_payment_screenshot"
    )

    if not plan:
        return

    if not update.message.photo:
        await update.message.reply_text(
            "📸 Please send the Telebirr payment screenshot as an image."
        )
        return

    if has_pending_payment(user.id):
        context.user_data.pop(
            "awaiting_payment_screenshot",
            None,
        )
        await update.message.reply_text(
            "⏳ You already have a payment request pending review."
        )
        return

    amount = (
        MONTHLY_PRICE_ETB
        if plan == "monthly"
        else YEARLY_PRICE_ETB
    )

    photo = update.message.photo[-1]
    file_id = photo.file_id

    request_id = create_payment_request(
        user.id,
        plan,
        amount,
        file_id,
    )

    context.user_data.pop(
        "awaiting_payment_screenshot",
        None,
    )

    await update.message.reply_text(
        "✅ PAYMENT SCREENSHOT RECEIVED\n\n"
        f"Request ID: #{request_id}\n"
        f"Plan: {plan.title()}\n"
        f"Amount: {amount} ETB\n\n"
        "⏳ Your payment is waiting for admin verification.\n"
        "💎 Premium will activate only after approval."
    )

    name = (
        f"{user.first_name or ''} "
        f"{user.last_name or ''}"
    ).strip()

    username = (
        f"@{user.username}"
        if user.username
        else "No username"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=f"approve_payment:{request_id}",
            ),
            InlineKeyboardButton(
                "❌ REJECT",
                callback_data=f"reject_payment:{request_id}",
            ),
        ]
    ]

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=file_id,
            caption=(
                "💰 NEW TELEBIRR PAYMENT\n\n"
                f"🆔 Request: #{request_id}\n"
                f"👤 Name: {name or 'Unknown'}\n"
                f"🔹 Username: {username}\n"
                f"🆔 Telegram ID: {user.id}\n"
                f"💎 Plan: {plan.title()}\n"
                f"💰 Amount: {amount} ETB\n"
                f"🕐 Time: {now_iso()}\n\n"
                "👇 Verify the screenshot and choose:"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception(
            "Could not send payment request to admin."
        )

        await update.message.reply_text(
            "⚠️ Your screenshot was saved, but I could not notify the admin automatically.\n"
            "Please contact the administrator."
        )


# =========================================================
# MENU-ONLY TEXT HANDLER
# =========================================================

async def text_chat_disabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Do not process ordinary typed messages in menu-only mode."""
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    await update.message.reply_text(
        "ℹ️ Please use the buttons below to use Ethio AI.\n\n"
        "💎 Upgrade to Premium\n"
        "📊 My Status\n"
        "💰 My Balance\n"
        "ℹ️ Help\n"
        "👥 Invite Friend\n\n"
        "💬 Direct text chat is currently disabled."
    )


# =========================================================
# TEXT CHAT
# =========================================================

async def chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    # Payment confirmation SMS takes priority over normal AI chat.
    request_id = context.user_data.get("awaiting_payment_sms_request_id")
    if request_id:
        request = get_payment_request(request_id)
        context.user_data.pop("awaiting_payment_sms_request_id", None)
        if not request or request["user_id"] != user.id:
            await update.message.reply_text("❌ Payment request not found. Please start again.")
            return
        if request["status"] != "waiting_sms":
            await update.message.reply_text("⏰ This payment request is no longer active. Please start again.")
            return
        if request["expires_at"] and now_utc() >= datetime.fromisoformat(request["expires_at"]):
            expire_old_payment_requests()
            await update.message.reply_text(
                "⏰ Payment request expired.\n\n"
                "30 minutes have passed. Please go back and start a new payment request."
            )
            return
        sms_text = (update.message.text or "").strip()
        if not sms_text:
            await update.message.reply_text("📩 Please paste the complete Telebirr SMS.")
            return
        if not save_payment_sms(request_id, sms_text):
            await update.message.reply_text("⚠️ This payment request was already processed.")
            return

        name = (f"{user.first_name or ''} {user.last_name or ''}").strip() or "Unknown"
        username = f"@{user.username}" if user.username else "No username"
        admin_text = (
            "👤 Account Info\n"
            f"Name: {name}\n"
            f"User ID: {user.id}\n"
            f"Username: {username}\n\n"
            "━━━━━━━━━━━━\n"
            "📩 SMS\n"
            "━━━━━━━━━━━━\n\n"
            f"{sms_text}\n\n"
            "━━━━━━━━━━━━\n"
            f"💎 Plan: {request['plan'].title()}\n"
            f"💰 Amount: {request['amount_etb']} ETB\n"
            f"🆔 Request: #{request_id}"
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 ACTIVE", callback_data=f"active_payment:{request_id}"),
                InlineKeyboardButton("🔴 AN ACTIVE", callback_data=f"inactive_payment:{request_id}"),
            ]
        ])
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_keyboard)
        except Exception:
            logger.exception("Failed to notify admin about SMS payment")

        await update.message.reply_text(
            "✅ SMS received successfully.\n\n"
            "🇬🇧 Your payment SMS has been sent to the administrator for verification.\n"
            "⏳ Please wait for confirmation.\n\n"
            "🇦🇲 SMS ተቀብለናል። ለአስተዳዳሪው ተልኳል።\n\n"
            "🇪🇹 SMS keessan fudhanneerra; adminitti ergineerra. Mirkaneessa eeggadhaa."
        )
        return

    # Direct text chat is a Premium feature. Free users can still
    # type messages, but they must upgrade before Gemini processes them.
    if not is_premium(user.id):
        await update.message.reply_text(
            "💎 Premium nu barbaachisa.\n\n"
            "🤖 AI waliin direct chat gochuuf Premium barbaachisa.\n"
            "Premium keessan upgrade godhaa, achiis ergaa keessan deebitanii ergaa.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💎 Upgrade to Premium",
                            callback_data="premium_menu",
                        )
                    ]
                ]
            ),
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
- If the user writes Afaan Oromoo, respond in Afaan Oromoo.
- If the user writes English, respond in English.
- If the user writes Amharic, respond in Amharic.
- If the user mixes languages, respond naturally in the same style.

Be helpful, accurate and clear.

User message:
{user_message}
"""

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=prompt,
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

        for i in range(0, len(answer), 4000):
            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception as e:
        logger.exception("TEXT CHAT ERROR")

        await update.message.reply_text(
            "⚠️ Ethio AI error.\n\n"
            f"{type(e).__name__}: {e}"
        )


# =========================================================
# IMAGE AI
# =========================================================

async def image_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    # Payment screenshot takes priority when the user is
    # currently in the payment-upload flow.
    if context.user_data.get("awaiting_payment_screenshot"):
        await payment_screenshot_handler(update, context)
        return

    premium = is_premium(user.id)

    if not premium:
        used = get_today_image_count(user.id)

        if used >= FREE_DAILY_IMAGE_LIMIT:
            await update.message.reply_text(
                "🆓 DAILY IMAGE LIMIT REACHED\n\n"
                f"You have used {FREE_DAILY_IMAGE_LIMIT}/{FREE_DAILY_IMAGE_LIMIT} "
                "free images today.\n\n"
                "💎 Upgrade to Premium for UNLIMITED image analysis.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "💎 Upgrade to Premium",
                                callback_data="premium_menu",
                            )
                        ]
                    ]
                ),
            )
            return

        record_free_image(user.id)

    question = (
        update.message.caption
        or "Please analyze this image carefully and explain what you see."
    ).strip()

    try:
        await update.effective_chat.send_action(
            action="typing"
        )

        if not update.message.photo:
            await update.message.reply_text(
                "❌ I could not receive the image."
            )
            return

        photo = update.message.photo[-1]

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

        prompt = f"""
You are Ethio AI, an intelligent AI assistant.

Analyze the image carefully.

Important rules:
- Only describe things that can actually be seen.
- Do not invent information.
- Answer the user's question directly.
- If the user writes Afaan Oromoo, respond in Afaan Oromoo.
- If the user writes English, respond in English.
- If the user writes Amharic, respond in Amharic.
- Be clear and helpful.

User's question:
{question}
"""

        image_part = types.Part.from_bytes(
            data=bytes(image_bytes),
            mime_type="image/jpeg",
        )

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL,
            contents=[
                prompt,
                image_part,
            ],
        )

        answer = (
            response.text
            if response and response.text
            else None
        )

        if not answer:
            await update.message.reply_text(
                "⚠️ I received the image, but I could not generate an answer."
            )
            return

        for i in range(0, len(answer), 4000):
            await update.message.reply_text(
                answer[i:i + 4000]
            )

        if not premium:
            remaining = max(
                0,
                FREE_DAILY_IMAGE_LIMIT
                - get_today_image_count(user.id),
            )

            await update.message.reply_text(
                f"🖼️ Images today: "
                f"{get_today_image_count(user.id)}/{FREE_DAILY_IMAGE_LIMIT}\n"
                f"Remaining today: {remaining}"
            )

    except Exception as e:
        logger.exception("IMAGE ANALYSIS ERROR")

        await update.message.reply_text(
            "⚠️ Ethio AI could not analyze the image.\n\n"
            f"Error: {type(e).__name__}: {e}"
        )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        (now_iso(),),
    )
    premium_users = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE status = 'free'
        AND blocked = 0
        """
    )
    free_users = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE last_active > ?
        AND blocked = 0
        """,
        (
            (
                now_utc()
                - timedelta(days=1)
            ).isoformat(),
        ),
    )
    active_24h = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM payment_requests
        WHERE status = 'pending'
        """
    )
    pending_payments = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE last_active > ?
        AND blocked = 0
        """,
        (
            (
                now_utc()
                - timedelta(days=30)
            ).isoformat(),
        ),
    )
    monthly_users = cursor.fetchone()["count"]

    conn.close()

    await update.message.reply_text(
        "🤖 ETHIO AI — ADMIN\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📅 Monthly Users: {monthly_users}\n"
        f"🟢 Active (24h): {active_24h}\n"
        f"💎 Premium Users: {premium_users}\n"
        f"🆓 Free Users: {free_users}\n"
        f"💰 Pending Payments: {pending_payments}\n\n"
        "Admin Commands:\n"
        "/users\n"
        "/stats\n"
        "/pending\n"
        "/premiumuser USER_ID [days]\n"
        "/free USER_ID\n"
        "/block USER_ID\n"
        "/unblock USER_ID"
    )


# =========================================================
# USERS LIST
# =========================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        name = row["first_name"] or "Unknown"

        if row["last_name"]:
            name += " " + row["last_name"]

        username = (
            "@" + row["username"]
            if row["username"]
            else "No username"
        )

        status = "🆓 FREE"

        if row["blocked"]:
            status = "🚫 BLOCKED"
        elif row["status"] == "premium":
            if is_premium(row["user_id"]):
                status = "💎 PREMIUM"
            else:
                status = "🆓 FREE"

        premium_until = row["premium_until"] or "-"

        text += (
            f"👤 {name}\n"
            f"{username}\n"
            f"🆔 ID: {row['user_id']}\n"
            f"{status}\n"
            f"📅 Premium until: {premium_until}\n"
            f"🕐 Last active: {row['last_active']}\n\n"
        )

        if len(text) > 3500:
            await update.message.reply_text(text)
            text = ""

    if text:
        await update.message.reply_text(text)


# =========================================================
# STATS
# =========================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        (now_iso(),),
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
                now_utc()
                - timedelta(days=1)
            ).isoformat(),
        ),
    )
    active = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE last_active > ?
        AND blocked = 0
        """,
        (
            (
                now_utc()
                - timedelta(days=30)
            ).isoformat(),
        ),
    )
    monthly = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM payment_requests
        WHERE status = 'pending'
        """
    )
    pending = cursor.fetchone()["count"]

    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM payment_requests
        WHERE status = 'approved'
        """
    )
    approved_payments = cursor.fetchone()["count"]

    conn.close()

    await update.message.reply_text(
        "📊 ETHIO AI — STATISTICS\n\n"
        f"👥 Total Users: {total}\n"
        f"📅 Monthly Users: {monthly}\n"
        f"🟢 Active (24h): {active}\n"
        f"💎 Premium: {premium}\n"
        f"🆓 Free: {free}\n"
        f"🚫 Blocked: {blocked}\n"
        f"⏳ Pending Payments: {pending}\n"
        f"✅ Approved Payments: {approved_payments}"
    )


# =========================================================
# PENDING PAYMENTS
# =========================================================

async def pending_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Admin only."
        )
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            p.id,
            p.user_id,
            p.plan,
            p.amount_etb,
            p.created_at,
            p.payment_proof_text,
            u.first_name,
            u.last_name,
            u.username
        FROM payment_requests p
        LEFT JOIN users u
            ON u.user_id = p.user_id
        WHERE p.status = 'pending'
        ORDER BY p.id DESC
        """
    )

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "💰 No pending payment requests."
        )
        return

    text = "💰 PENDING PAYMENTS\n\n"

    for row in rows:
        name = (
            f"{row['first_name'] or ''} "
            f"{row['last_name'] or ''}"
        ).strip() or "Unknown"

        username = (
            f"@{row['username']}"
            if row["username"]
            else "No username"
        )

        text += (
            f"🆔 Request: #{row['id']}\n"
            f"👤 {name}\n"
            f"{username}\n"
            f"Telegram ID: {row['user_id']}\n"
            f"💎 Plan: {row['plan'].title()}\n"
            f"💰 Amount: {row['amount_etb']} ETB\n"
            f"🕐 {row['created_at']}\n"
            + (f"📩 SMS: {row['payment_proof_text']}\n" if row["payment_proof_text"] else "")
            + "\n"
        )

    await update.message.reply_text(text)


# =========================================================
# ADMIN PREMIUM
# =========================================================

async def premium_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🚫 Admin only."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/premiumuser USER_ID [days]\n\n"
            "Example:\n"
            "/premiumuser 123456789 30"
        )
        return

    try:
        target_id = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else MONTHLY_DAYS
    except ValueError:
        await update.message.reply_text(
            "❌ USER_ID and days must be numbers."
        )
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (target_id,),
    )
    exists = cursor.fetchone()
    conn.close()

    if not exists:
        await update.message.reply_text(
            "❌ User not found in database."
        )
        return

    expiry = set_premium(target_id, days)

    await update.message.reply_text(
        "💎 Premium activated.\n\n"
        f"User ID: {target_id}\n"
        f"Duration: {days} days\n"
        f"Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "💎 PREMIUM ACTIVATED BY ADMIN\n\n"
                f"📅 Duration: {days} days\n"
                f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                "🖼️ Unlimited image analysis is now available."
            ),
        )
    except Exception:
        logger.exception(
            "Could not notify manually upgraded user."
        )


# =========================================================
# FREE COMMAND
# =========================================================

async def free_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        target_id = int(context.args[0])
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
# BLOCK / UNBLOCK
# =========================================================

async def block_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        target_id = int(context.args[0])
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


async def unblock_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not is_admin(update.effective_user.id):
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
        target_id = int(context.args[0])
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
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "UNHANDLED ERROR",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    init_database()

    logger.info("Starting Ethio AI...")
    logger.info(f"Gemini model: {MODEL}")
    logger.info(f"Admin ID: {ADMIN_ID}")
    logger.info(f"Bot: @{BOT_USERNAME}")

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

    app.add_handler(
        CommandHandler("premium", premium_menu)
    )

    app.add_handler(
        CommandHandler("status", status_command)
    )

    app.add_handler(
        CommandHandler("referrals", referrals_command)
    )

    app.add_handler(
        CommandHandler("balance", balance_command)
    )

    app.add_handler(
        CommandHandler("withdraw", create_withdrawal)
    )
    app.add_handler(
        CommandHandler("pendingwithdrawals", pending_withdrawals_command)
    )
    app.add_handler(
        CommandHandler("approvewithdraw", approve_withdraw_command)
    )
    app.add_handler(
        CommandHandler("rejectwithdraw", reject_withdraw_command)
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
        CommandHandler("pending", pending_command)
    )

    app.add_handler(
        CommandHandler("premiumuser", premium_command)
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

    # Callback buttons
    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Images
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            image_chat,
        )
    )

    # Text messages
    # Users can type normally. The chat() handler checks Premium status
    # before sending the message to Gemini.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat,
        )
    )

    app.add_error_handler(error_handler)

    expire_old_payment_requests()
    logger.info("Ethio AI is running...")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
