import asyncio
import logging
import os
import re
import sqlite3
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


# =========================================================
# FREE / PREMIUM SETTINGS
# =========================================================

FREE_DAILY_IMAGE_LIMIT = 10

MONTHLY_PRICE_ETB = 100
MONTHLY_DAYS = 30

YEARLY_PRICE_ETB = 1200
YEARLY_DAYS = 365


# =========================================================
# TELEBIRR RECEIVER
# =========================================================

TELEBIRR_NAME = "Amir Ali"
TELEBIRR_PHONE = "0967767646"


# =========================================================
# PAYMENT SECURITY
# =========================================================

MAX_PAYMENT_ATTEMPTS = 5
PAYMENT_TIMEOUT_MINUTES = 15


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
# GEMINI
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


def column_exists(cursor, table_name, column_name):
    cursor.execute(
        f"PRAGMA table_info({table_name})"
    )

    rows = cursor.fetchall()

    return any(
        row["name"] == column_name
        for row in rows
    )


def add_column_if_missing(
    cursor,
    table_name,
    column_name,
    definition,
):
    if not column_exists(
        cursor,
        table_name,
        column_name,
    ):
        cursor.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name} {definition}
            """
        )


def init_database():
    conn = get_db()
    cursor = conn.cursor()

    # USERS
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

    # IMAGE USAGE
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

    # PAYMENT REQUESTS
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount_etb INTEGER NOT NULL,
            screenshot_file_id TEXT,
            transaction_text TEXT,
            transaction_id TEXT,
            status TEXT DEFAULT 'pending',
            attempts_used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER,
            rejection_reason TEXT
        )
        """
    )

    # DATABASE MIGRATION
    add_column_if_missing(
        cursor,
        "payment_requests",
        "transaction_text",
        "TEXT",
    )

    add_column_if_missing(
        cursor,
        "payment_requests",
        "transaction_id",
        "TEXT",
    )

    add_column_if_missing(
        cursor,
        "payment_requests",
        "attempts_used",
        "INTEGER DEFAULT 0",
    )

    # Remove/ignore old screenshot records.
    # New system does not create screenshot payment requests.

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_payment_transaction_id
        ON payment_requests(transaction_id)
        WHERE transaction_id IS NOT NULL
        AND transaction_id != ''
        """
    )

    conn.commit()
    conn.close()

    logger.info("Database initialized.")


# =========================================================
# USERS
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
        """
        SELECT blocked
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )

    row = cursor.fetchone()

    conn.close()

    return bool(row["blocked"]) if row else False


# =========================================================
# PREMIUM
# =========================================================

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

    if (
        row["status"] != "premium"
        or not row["premium_until"]
    ):
        conn.close()
        return False

    try:
        expiry = datetime.fromisoformat(
            row["premium_until"]
        )

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

    old_expiry = None

    if row and row["premium_until"]:
        try:
            old_expiry = datetime.fromisoformat(
                row["premium_until"]
            )
        except Exception:
            old_expiry = None

    if old_expiry and old_expiry > now:
        expiry = old_expiry + timedelta(
            days=days
        )
    else:
        expiry = now + timedelta(
            days=days
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
# IMAGE LIMIT
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
        (
            user_id,
            today,
        ),
    )

    row = cursor.fetchone()

    conn.close()

    return (
        int(row["image_count"])
        if row
        else 0
    )


def record_image_usage(user_id):
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
        (
            user_id,
            today,
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# PAYMENT HELPERS
# =========================================================

def expected_amount(plan):
    if plan == "monthly":
        return MONTHLY_PRICE_ETB

    return YEARLY_PRICE_ETB


def plan_days(plan):
    if plan == "monthly":
        return MONTHLY_DAYS

    return YEARLY_DAYS


def plan_name(plan):
    if plan == "monthly":
        return "Monthly Premium"

    return "Yearly Premium"


def create_payment_request(
    user_id,
    plan,
    amount_etb,
    transaction_text,
    transaction_id,
    attempts_used,
):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO payment_requests (
            user_id,
            plan,
            amount_etb,
            screenshot_file_id,
            transaction_text,
            transaction_id,
            status,
            attempts_used,
            created_at
        )
        VALUES (?, ?, ?, NULL, ?, ?, 'pending', ?, ?)
        """,
        (
            user_id,
            plan,
            amount_etb,
            transaction_text,
            transaction_id,
            attempts_used,
            now_iso(),
        ),
    )

    request_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return request_id


def get_payment_request(request_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM payment_requests
        WHERE id = ?
        """,
        (request_id,),
    )

    row = cursor.fetchone()

    conn.close()

    return row


def update_payment_request(
    request_id,
    status,
    reviewer_id,
    rejection_reason=None,
):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE payment_requests
        SET status = ?,
            reviewed_at = ?,
            reviewed_by = ?,
            rejection_reason = ?
        WHERE id = ?
        AND status = 'pending'
        """,
        (
            status,
            now_iso(),
            reviewer_id,
            rejection_reason,
            request_id,
        ),
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


def has_pending_payment(user_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM payment_requests
        WHERE user_id = ?
        AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )

    row = cursor.fetchone()

    conn.close()

    return row is not None


def transaction_already_used(transaction_id):
    if not transaction_id:
        return None

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, user_id, status
        FROM payment_requests
        WHERE transaction_id = ?
        LIMIT 1
        """,
        (transaction_id,),
    )

    row = cursor.fetchone()

    conn.close()

    return row


# =========================================================
# PAYMENT SESSION
# =========================================================

def start_payment_session(context, plan):
    expires = now_utc() + timedelta(
        minutes=PAYMENT_TIMEOUT_MINUTES
    )

    context.user_data["payment_session"] = {
        "plan": plan,
        "attempts_used": 0,
        "expires_at": expires.isoformat(),
    }


def get_payment_session(context):
    session = context.user_data.get(
        "payment_session"
    )

    if not session:
        return None

    try:
        expiry = datetime.fromisoformat(
            session["expires_at"]
        )

        if now_utc() >= expiry:
            context.user_data.pop(
                "payment_session",
                None,
            )
            return None

    except Exception:
        context.user_data.pop(
            "payment_session",
            None,
        )
        return None

    return session


def clear_payment_session(context):
    context.user_data.pop(
        "payment_session",
        None,
    )


# =========================================================
# MULTILINGUAL PAYMENT MESSAGES
# =========================================================

def payment_received_message():
    return (
        "🔍 TRANSACTION RECEIVED — VERIFYING PAYMENT...\n\n"

        "🇬🇧 English:\n"
        "Your Telebirr transaction has been received. "
        "I am checking the payment amount, recipient and transaction reference.\n"
        "⚠️ This is an initial check only. Premium will NOT activate until an admin verifies the payment.\n\n"

        "🇪🇹 Afaan Oromoo:\n"
        "Transaction Telebirr kee nu gaheera. "
        "Amma hanga kaffaltii, nama itti ergametti fi transaction reference sakatta'aa jira.\n"
        "⚠️ Kun sakatta'iinsa jalqabaa qofa. Premium kan banamu admin erga kaffaltii mirkaneesse booda qofa.\n\n"

        "🇪🇹 አማርኛ:\n"
        "የTelebirr ግብይትዎ ደርሶናል። "
        "የክፍያ መጠኑን፣ ተቀባዩን እና የግብይት ማጣቀሻውን እያረጋገጥን ነው።\n"
        "⚠️ ይህ የመጀመሪያ ማጣሪያ ብቻ ነው። Premium የሚነቃው አስተዳዳሪው ክፍያውን ካረጋገጠ በኋላ ብቻ ነው።"
    )


def payment_failed_message(
    reason_en,
    reason_or,
    reason_am,
    attempts_left,
):
    return (
        "❌ PAYMENT FAILED\n\n"

        "🇬🇧 English:\n"
        f"Reason: {reason_en}\n"
        f"🔢 Attempts remaining: {attempts_left}/{MAX_PAYMENT_ATTEMPTS}\n"
        f"⏱️ Payment session timeout: {PAYMENT_TIMEOUT_MINUTES} minutes.\n\n"

        "🇪🇹 Afaan Oromoo:\n"
        f"Sababni: {reason_or}\n"
        f"🔢 Carraan hafe: {attempts_left}/{MAX_PAYMENT_ATTEMPTS}\n"
        f"⏱️ Yeroon kaffaltii: daqiiqaa {PAYMENT_TIMEOUT_MINUTES}.\n\n"

        "🇪🇹 አማርኛ:\n"
        f"ምክንያት፦ {reason_am}\n"
        f"🔢 የቀሩ ሙከራዎች፦ {attempts_left}/{MAX_PAYMENT_ATTEMPTS}\n"
        f"⏱️ የክፍያ ጊዜ፦ {PAYMENT_TIMEOUT_MINUTES} ደቂቃ።\n\n"

        "💡 🇬🇧 Copy and paste the FULL Telebirr SMS exactly as received.\n"
        "💡 🇪🇹 Afaan Oromoo: SMS Telebirr guutuu akkuma siif dhufeetti COPY godhiitii asitti PASTE godhi.\n"
        "💡 🇪🇹 አማርኛ፦ ሙሉውን የTelebirr SMS መልእክት እንደደረሰዎት በትክክል COPY እና PASTE ያድርጉ።"
    )


def payment_locked_message():
    return (
        "🔒 PAYMENT SESSION LOCKED\n\n"

        "🇬🇧 English:\n"
        "You used all 5 payment attempts.\n"
        "Please wait 15 minutes before starting a new payment session.\n\n"

        "🇪🇹 Afaan Oromoo:\n"
        "Carraa kaffaltii 5 hunda fayyadamteetta.\n"
        "Maaloo daqiiqaa 15 eegiitii booda session kaffaltii haaraa jalqabi.\n\n"

        "🇪🇹 አማርኛ:\n"
        "5ቱንም የክፍያ ሙከራዎች ተጠቅመዋል።\n"
        "እባክዎ 15 ደቂቃ ይጠብቁ ከዚያ አዲስ የክፍያ ሂደት ይጀምሩ።"
    )


# =========================================================
# DIGIT NORMALIZATION
# =========================================================

def normalize_digits(text):
    arabic = str.maketrans(
        "٠١٢٣٤٥٦٧٨٩",
        "0123456789",
    )

    ethiopic = str.maketrans(
        "፩፪፫፬፭፮፯፰፱",
        "123456789",
    )

    return text.translate(arabic).translate(ethiopic)


# =========================================================
# AMOUNT PARSER
# =========================================================

def extract_amounts(text):
    text = normalize_digits(text)

    patterns = [
        r"(?:amount|paid|payment|sent|transferred|debited)"
        r"[^\d]{0,40}"
        r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"
        r"\s*(?:ETB|Birr|birr|ብር)?",

        r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"
        r"\s*(?:ETB|Birr|birr|ብር)",
    ]

    values = []

    for pattern in patterns:
        matches = re.findall(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        for item in matches:
            try:
                values.append(
                    float(
                        item.replace(",", "")
                    )
                )
            except ValueError:
                pass

    return values


def amount_matches(text, expected):
    amounts = extract_amounts(text)

    return any(
        abs(amount - expected) < 0.01
        for amount in amounts
    )


# =========================================================
# RECIPIENT CHECK
# =========================================================

def recipient_matches(text):
    normalized = " ".join(
        text.lower().split()
    )

    name_normalized = " ".join(
        TELEBIRR_NAME.lower().split()
    )

    phone_digits = re.sub(
        r"\D",
        "",
        TELEBIRR_PHONE,
    )

    text_digits = re.sub(
        r"\D",
        "",
        text,
    )

    name_ok = (
        name_normalized in normalized
    )

    phone_ok = (
        phone_digits in text_digits
    )

    return name_ok or phone_ok


# =========================================================
# TRANSACTION ID
# =========================================================

def extract_transaction_id(text):
    patterns = [
        r"(?:transaction\s*(?:id|no|number|ref(?:erence)?))"
        r"\s*[:#=\-]?\s*([A-Za-z0-9\-]{5,})",

        r"(?:tx\s*(?:id|no))"
        r"\s*[:#=\-]?\s*([A-Za-z0-9\-]{5,})",

        r"(?:reference\s*(?:id|no|number))"
        r"\s*[:#=\-]?\s*([A-Za-z0-9\-]{5,})",

        r"(?:ref)"
        r"\s*[:#=\-]\s*([A-Za-z0-9\-]{5,})",

        r"(?:receipt\s*(?:no|number))"
        r"\s*[:#=\-]?\s*([A-Za-z0-9\-]{5,})",

        r"(?:የግብይት\s*(?:ቁጥር|መለያ))"
        r"\s*[:#=\-]?\s*([A-Za-z0-9\-]{5,})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1).strip()

    return None


# =========================================================
# PAYMENT VALIDATION
# =========================================================

def validate_payment_sms(text, expected):
    text = text.strip()

    if len(text) < 10:
        return {
            "ok": False,
            "reason": "The SMS is too short.",
            "reason_or": "SMS'n baay'ee gabaabaa dha.",
            "reason_am": "የSMS መልእክቱ በጣም አጭር ነው።",
            "transaction_id": None,
        }

    # AMOUNT
    if not amount_matches(text, expected):
        return {
            "ok": False,
            "reason": (
                f"The payment amount does not match "
                f"the required {expected} ETB."
            ),
            "reason_or": (
                f"Hangi kaffaltii {expected} ETB "
                "barbaadame waliin hin gitu."
            ),
            "reason_am": (
                f"የክፍያው መጠን {expected} ETB "
                "ከሚፈለገው መጠን ጋር አይመጣጠንም።"
            ),
            "transaction_id": None,
        }

    # RECEIVER
    if not recipient_matches(text):
        return {
            "ok": False,
            "reason": (
                "The payment was sent to the wrong recipient. "
                f"Please pay to {TELEBIRR_NAME} / {TELEBIRR_PHONE}."
            ),
            "reason_or": (
                "Kaffaltiin kee nama sirrii hin taaneef ergame. "
                f"Maqaa {TELEBIRR_NAME} / {TELEBIRR_PHONE} ilaali."
            ),
            "reason_am": (
                "ክፍያው ወደ ትክክለኛው ተቀባይ አልተላከም። "
                f"እባክዎ {TELEBIRR_NAME} / {TELEBIRR_PHONE} ያረጋግጡ።"
            ),
            "transaction_id": None,
        }

    # TRANSACTION ID
    transaction_id = extract_transaction_id(text)

    if transaction_id:
        old = transaction_already_used(
            transaction_id
        )

        if old:
            return {
                "ok": False,
                "reason": (
                    "This transaction/reference ID has already been submitted."
                ),
                "reason_or": (
                    "Transaction/reference ID kun duraan submit ta'eera."
                ),
                "reason_am": (
                    "ይህ የግብይት/ማጣቀሻ መለያ ቀደም ሲል ተልኳል።"
                ),
                "transaction_id": transaction_id,
            }

    return {
        "ok": True,
        "reason": "",
        "reason_or": "",
        "reason_am": "",
        "transaction_id": transaction_id,
    }


# =========================================================
# START
# =========================================================

async def start(update, context):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access to Ethio AI has been blocked."
        )
        return

    if is_premium(user.id):

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT premium_until
            FROM users
            WHERE user_id = ?
            """,
            (user.id,),
        )

        row = cursor.fetchone()
        conn.close()

        expiry_text = "unknown"

        if row and row["premium_until"]:
            expiry = datetime.fromisoformat(
                row["premium_until"]
            )

            expiry_text = expiry.strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        text = (
            f"👋 Hello {user.first_name}!\n\n"
            "🤖 Welcome to Ethio AI.\n\n"
            "💎 Your Premium is ACTIVE.\n"
            f"📅 Expires: {expiry_text}\n\n"
            "💬 Send me a question or 🖼️ send an image."
        )

    else:

        used = get_today_image_count(
            user.id
        )

        text = (
            f"👋 Hello {user.first_name}!\n\n"
            "🤖 Welcome to Ethio AI.\n\n"
            "💬 You can chat with me.\n"
            f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
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
                "ℹ️ Help",
                callback_data="help",
            ),
        ],
        [
            InlineKeyboardButton(
                "👥 Invite Friend",
                url=(
                    "https://t.me/share/url"
                    f"?url={BOT_LINK}"
                    "&text=🤖 Try Ethio AI - your intelligent AI assistant!"
                ),
            )
        ],
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# =========================================================
# HELP
# =========================================================

async def help_command(update, context):
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
        f"🖼️ Images today: {get_today_image_count(user.id)}/{FREE_DAILY_IMAGE_LIMIT}\n\n"
        "💎 Premium:\n"
        f"• Monthly: {MONTHLY_PRICE_ETB} ETB\n"
        f"• Yearly: {YEARLY_PRICE_ETB} ETB\n"
        "• Unlimited image analysis\n\n"
        "💳 Payment:\n"
        "Choose a Premium plan, send the exact amount to Telebirr, "
        "then copy and paste the FULL Telebirr SMS.\n\n"
        "Commands:\n"
        "/start - Start Ethio AI\n"
        "/help - Help\n"
        "/premium - Upgrade to Premium\n"
        "/status - Check Premium status"
    )


# =========================================================
# STATUS
# =========================================================

async def status_command(update, context):
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
            """
            SELECT premium_until
            FROM users
            WHERE user_id = ?
            """,
            (user.id,),
        )

        row = cursor.fetchone()
        conn.close()

        expiry = datetime.fromisoformat(
            row["premium_until"]
        )

        remaining = expiry - now_utc()

        days = max(
            0,
            remaining.days,
        )

        await update.message.reply_text(
            "💎 ETHIO AI PREMIUM\n\n"
            "✅ Status: ACTIVE\n"
            f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"⏳ Remaining: {days} days\n"
            "🖼️ Images: UNLIMITED"
        )

    else:

        used = get_today_image_count(
            user.id
        )

        await update.message.reply_text(
            "🆓 ETHIO AI FREE\n\n"
            "✅ AI Chat: AVAILABLE\n"
            f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
            "💎 Premium: Not active"
        )


# =========================================================
# PREMIUM MENU
# =========================================================

async def premium_menu(update, context):
    if not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):

        target = (
            update.message
            or (
                update.callback_query.message
                if update.callback_query
                else None
            )
        )

        if target:
            await target.reply_text(
                "🚫 Your access has been blocked."
            )

        return

    if is_premium(user.id):

        if update.message:
            await status_command(
                update,
                context,
            )

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
# PAYMENT INSTRUCTIONS
# =========================================================

async def show_payment_instructions(
    query,
    plan,
    amount,
):
    keyboard = [
        [
            InlineKeyboardButton(
                "📩 I Paid — Paste Full Telebirr SMS",
                callback_data=f"paste_payment:{plan}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="premium_menu",
            )
        ],
    ]

    await query.edit_message_text(
        "📱 TELEBIRR PAYMENT\n\n"

        f"💎 Plan: {plan_name(plan)}\n"
        f"💰 Amount: {amount} ETB\n\n"

        f"👤 Name: {TELEBIRR_NAME}\n"
        f"📞 Telebirr: {TELEBIRR_PHONE}\n\n"

        "⚠️ PLEASE READ\n\n"

        "🇬🇧 English:\n"
        f"Send exactly {amount} ETB to:\n"
        f"Name: {TELEBIRR_NAME}\n"
        f"Telebirr: {TELEBIRR_PHONE}\n\n"
        "After payment, check the Telebirr SMS you received.\n"
        "Copy the FULL SMS exactly as received and paste it here.\n\n"

        "🇪🇹 Afaan Oromoo:\n"
        f"Hanga {amount} ETB sirriitti:\n"
        f"Maqaa: {TELEBIRR_NAME}\n"
        f"Telebirr: {TELEBIRR_PHONE}\n"
        "Erga kaffaltee booda SMS Telebirr siif dhufe ilaali.\n"
        "SMS GUUTUU akkuma siif dhufeetti COPY godhiitii asitti PASTE godhi.\n\n"

        "🇪🇹 አማርኛ:\n"
        f"በትክክል {amount} ETB ወደ፦\n"
        f"ስም፦ {TELEBIRR_NAME}\n"
        f"Telebirr፦ {TELEBIRR_PHONE}\n"
        "ክፍያውን ከፈጸሙ በኋላ የTelebirr SMS ይመልከቱ።\n"
        "ሙሉውን SMS እንደደረሰዎት COPY እና PASTE ያድርጉ።\n\n"

        "🔐 Premium will activate ONLY after admin verification.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(update, context):
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

    # PREMIUM MENU
    if data == "premium_menu":
        await premium_menu(
            update,
            context,
        )
        return

    # STATUS
    if data == "my_status":

        if is_premium(user.id):

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT premium_until
                FROM users
                WHERE user_id = ?
                """,
                (user.id,),
            )

            row = cursor.fetchone()
            conn.close()

            expiry = datetime.fromisoformat(
                row["premium_until"]
            )

            await query.message.reply_text(
                "💎 PREMIUM STATUS\n\n"
                "✅ ACTIVE\n"
                f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
                "🖼️ Images: UNLIMITED"
            )

        else:

            used = get_today_image_count(
                user.id
            )

            await query.message.reply_text(
                "🆓 FREE STATUS\n\n"
                "✅ AI Chat: AVAILABLE\n"
                f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
                "💎 Premium: NOT ACTIVE"
            )

        return

    # HELP
    if data == "help":

        used = get_today_image_count(
            user.id
        )

        await query.message.reply_text(
            "🤖 ETHIO AI — HELP\n\n"
            "💬 Chat is available.\n"
            f"🖼️ Images today: {used}/{FREE_DAILY_IMAGE_LIMIT}\n"
            "💎 Premium: unlimited images.\n\n"
            "/premium - Upgrade\n"
            "/status - Check status"
        )

        return

    # MONTHLY
    if data == "plan_monthly":

        if has_pending_payment(user.id):
            await query.message.reply_text(
                "⏳ You already have a payment waiting for admin verification."
            )
            return

        await show_payment_instructions(
            query,
            "monthly",
            MONTHLY_PRICE_ETB,
        )
        return

    # YEARLY
    if data == "plan_yearly":

        if has_pending_payment(user.id):
            await query.message.reply_text(
                "⏳ You already have a payment waiting for admin verification."
            )
            return

        await show_payment_instructions(
            query,
            "yearly",
            YEARLY_PRICE_ETB,
        )
        return

    # PASTE SMS
    if data.startswith("paste_payment:"):

        plan = data.split(":", 1)[1]

        if plan not in ("monthly", "yearly"):
            await query.message.reply_text(
                "❌ Invalid Premium plan."
            )
            return

        if has_pending_payment(user.id):
            await query.message.reply_text(
                "⏳ You already have a payment waiting for admin verification."
            )
            return

        start_payment_session(
            context,
            plan,
        )

        amount = expected_amount(plan)

        await query.message.reply_text(
            "📩 PASTE FULL TELEBIRR SMS\n\n"

            f"💎 Plan: {plan_name(plan)}\n"
            f"💰 Amount: {amount} ETB\n"
            f"👤 Receiver: {TELEBIRR_NAME}\n"
            f"📞 Number: {TELEBIRR_PHONE}\n\n"

            "🇬🇧 English:\n"
            "Copy the COMPLETE Telebirr SMS exactly as received and paste it here.\n"
            f"🔢 You have {MAX_PAYMENT_ATTEMPTS} attempts.\n"
            f"⏱️ Session expires in {PAYMENT_TIMEOUT_MINUTES} minutes.\n\n"

            "🇪🇹 Afaan Oromoo:\n"
            "SMS Telebirr GUUTUU akkuma siif dhufeetti COPY godhiitii asitti PASTE godhi.\n"
            f"🔢 Carraa {MAX_PAYMENT_ATTEMPTS} qabda.\n"
            f"⏱️ Yeroon session daqiiqaa {PAYMENT_TIMEOUT_MINUTES} qofa.\n\n"

            "🇪🇹 አማርኛ:\n"
            "ሙሉውን የTelebirr SMS እንደደረሰዎት COPY እና እዚህ PASTE ያድርጉ።\n"
            f"🔢 {MAX_PAYMENT_ATTEMPTS} ሙከራዎች አሉዎት።\n"
            f"⏱️ ጊዜው {PAYMENT_TIMEOUT_MINUTES} ደቂቃ ብቻ ነው።"
        )

        return

    # =====================================================
    # ADMIN APPROVE
    # =====================================================

    if data.startswith("approve_payment:"):

        if not is_admin(user.id):
            await query.message.reply_text(
                "🚫 Admin only."
            )
            return

        try:
            request_id = int(
                data.split(":", 1)[1]
            )
        except ValueError:
            await query.message.reply_text(
                "❌ Invalid payment request."
            )
            return

        request = get_payment_request(
            request_id
        )

        if not request:
            await query.message.reply_text(
                "❌ Payment request not found."
            )
            return

        if request["status"] != "pending":
            await query.message.reply_text(
                f"ℹ️ This request is already {request['status']}."
            )
            return

        changed = update_payment_request(
            request_id,
            "approved",
            user.id,
        )

        if not changed:
            await query.message.reply_text(
                "⚠️ This request was already processed."
            )
            return

        days = plan_days(
            request["plan"]
        )

        expiry = set_premium(
            request["user_id"],
            days,
        )

        caption = (
            "✅ PAYMENT VERIFIED & APPROVED\n\n"
            f"Request ID: #{request_id}\n"
            f"User ID: {request['user_id']}\n"
            f"Plan: {request['plan'].title()}\n"
            f"Amount: {request['amount_etb']} ETB\n"
            f"Transaction ID: {request['transaction_id'] or 'Not found'}\n"
            f"Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}"
        )

        try:
            await query.edit_message_text(
                caption,
                reply_markup=None,
            )
        except Exception:
            logger.exception(
                "Could not edit admin payment message."
            )

        try:
            await context.bot.send_message(
                chat_id=request["user_id"],
                text=(
                    "🎉 PAYMENT APPROVED!\n\n"

                    "🇬🇧 English:\n"
                    "Your payment has been verified by the admin.\n"
                    "💎 Ethio AI Premium is now ACTIVE.\n\n"

                    "🇪🇹 Afaan Oromoo:\n"
                    "Kaffaltiin kee admin'n mirkanaa'eera.\n"
                    "💎 Ethio AI Premium amma ACTIVE dha.\n\n"

                    "🇪🇹 አማርኛ:\n"
                    "ክፍያዎ በአስተዳዳሪው ተረጋግጧል።\n"
                    "💎 Ethio AI Premium አሁን ACTIVE ሆኗል።\n\n"

                    f"💰 Paid: {request['amount_etb']} ETB\n"
                    f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
                    "🖼️ Images: UNLIMITED"
                ),
            )

        except Exception:
            logger.exception(
                "Could not notify user after approval."
            )

        return

    # =====================================================
    # ADMIN REJECT
    # =====================================================

    if data.startswith("reject_payment:"):

        if not is_admin(user.id):
            await query.message.reply_text(
                "🚫 Admin only."
            )
            return

        try:
            request_id = int(
                data.split(":", 1)[1]
            )
        except ValueError:
            await query.message.reply_text(
                "❌ Invalid payment request."
            )
            return

        request = get_payment_request(
            request_id
        )

        if not request:
            await query.message.reply_text(
                "❌ Payment request not found."
            )
            return

        if request["status"] != "pending":
            await query.message.reply_text(
                f"ℹ️ This request is already {request['status']}."
            )
            return

        changed = update_payment_request(
            request_id,
            "rejected",
            user.id,
            "Payment rejected by admin.",
        )

        if not changed:
            await query.message.reply_text(
                "⚠️ This request was already processed."
            )
            return

        try:
            await query.edit_message_text(
                (
                    "❌ PAYMENT REJECTED\n\n"
                    f"Request ID: #{request_id}\n"
                    f"User ID: {request['user_id']}\n"
                    f"Plan: {request['plan'].title()}\n"
                    f"Amount: {request['amount_etb']} ETB"
                ),
                reply_markup=None,
            )
        except Exception:
            logger.exception(
                "Could not edit rejected payment message."
            )

        try:
            await context.bot.send_message(
                chat_id=request["user_id"],
                text=(
                    "❌ PAYMENT REJECTED\n\n"

                    "🇬🇧 English:\n"
                    "Your payment could not be approved by the admin. "
                    "Please check the amount, receiver and Telebirr SMS.\n\n"

                    "🇪🇹 Afaan Oromoo:\n"
                    "Kaffaltiin kee admin'n hin mirkanoofne. "
                    "Hanga kaffaltii, nama itti ergite fi SMS Telebirr sirriitti ilaali.\n\n"

                    "🇪🇹 አማርኛ:\n"
                    "ክፍያዎ በአስተዳዳሪው ሊረጋገጥ አልቻለም። "
                    "የክፍያውን መጠን፣ ተቀባዩን እና SMS ያረጋግጡ።"
                ),
            )
        except Exception:
            logger.exception(
                "Could not notify rejected user."
            )

        return


# =========================================================
# PAYMENT SMS HANDLER
# =========================================================

async def payment_sms_handler(
    update,
    context,
):
    if not update.message:
        return

    if not update.effective_user:
        return

    user = update.effective_user
    save_user(user)

    if is_blocked(user.id):
        await update.message.reply_text(
            "🚫 Your access has been blocked."
        )
        return

    session = get_payment_session(context)

    if not session:
        return

    if has_pending_payment(user.id):
        clear_payment_session(context)

        await update.message.reply_text(
            "⏳ You already have a payment waiting for admin verification."
        )
        return

    text = (
        update.message.text or ""
    ).strip()

    if not text:
        return

    # ATTEMPT
    session["attempts_used"] += 1

    attempts_left = (
        MAX_PAYMENT_ATTEMPTS
        - session["attempts_used"]
    )

    plan = session["plan"]
    amount = expected_amount(plan)

    # RECEIVED MESSAGE
    await update.message.reply_text(
        payment_received_message()
    )

    # VALIDATE
    result = validate_payment_sms(
        text,
        amount,
    )

    if not result["ok"]:

        if attempts_left <= 0:

            clear_payment_session(context)

            await update.message.reply_text(
                payment_locked_message()
            )

            return

        await update.message.reply_text(
            payment_failed_message(
                result["reason"],
                result["reason_or"],
                result["reason_am"],
                attempts_left,
            )
        )

        return

    transaction_id = result["transaction_id"]

    # =====================================================
    # CREATE PENDING REQUEST
    # =====================================================

    try:

        request_id = create_payment_request(
            user.id,
            plan,
            amount,
            text,
            transaction_id,
            session["attempts_used"],
        )

    except sqlite3.IntegrityError:

        clear_payment_session(context)

        await update.message.reply_text(
            "🚫 DUPLICATE TRANSACTION\n\n"

            "🇬🇧 English:\n"
            "This transaction/reference has already been submitted.\n\n"

            "🇪🇹 Afaan Oromoo:\n"
            "Transaction/reference kun duraan submit ta'eera.\n\n"

            "🇪🇹 አማርኛ:\n"
            "ይህ የግብይት/ማጣቀሻ መለያ ቀደም ሲል ተልኳል።"
        )

        return

    clear_payment_session(context)

    # =====================================================
    # USER PENDING
    # =====================================================

    await update.message.reply_text(
        "⏳ PAYMENT PENDING ADMIN VERIFICATION\n\n"

        "🇬🇧 English:\n"
        f"Request ID: #{request_id}\n"
        f"💎 Plan: {plan_name(plan)}\n"
        f"💰 Amount: {amount} ETB\n"
        f"🔢 Transaction ID: {transaction_id or 'Not detected'}\n"
        "Your payment details passed the initial checks.\n"
        "An admin must independently verify the transaction.\n"
        "💎 Premium will activate ONLY after admin approval.\n\n"

        "🇪🇹 Afaan Oromoo:\n"
        f"Request ID: #{request_id}\n"
        f"💎 Karoora: {plan_name(plan)}\n"
        f"💰 Hanga: {amount} ETB\n"
        f"🔢 Transaction ID: {transaction_id or 'Hin argamne'}\n"
        "Odeeffannoon kaffaltii kee sakatta'iinsa jalqabaa darbeera.\n"
        "Admin transaction sana ofumaan mirkaneessuu qaba.\n"
        "💎 Premium kan banamu admin erga raggaasise booda qofa.\n\n"

        "🇪🇹 አማርኛ:\n"
        f"Request ID: #{request_id}\n"
        f"💎 ፕላን፦ {plan_name(plan)}\n"
        f"💰 መጠን፦ {amount} ETB\n"
        f"🔢 Transaction ID፦ {transaction_id or 'አልተገኘም'}\n"
        "የክፍያዎ መረጃ የመጀመሪያ ማጣሪያውን አልፏል።\n"
        "አስተዳዳሪው ግብይቱን በተናጠል ማረጋገጥ አለበት።\n"
        "💎 Premium የሚነቃው አስተዳዳሪው ካጸደቀ በኋላ ብቻ ነው።"
    )

    # =====================================================
    # ADMIN NOTIFICATION
    # =====================================================

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
                "✅ VERIFY & APPROVE",
                callback_data=f"approve_payment:{request_id}",
            ),
            InlineKeyboardButton(
                "❌ REJECT",
                callback_data=f"reject_payment:{request_id}",
            ),
        ]
    ]

    admin_text = (
        "💰 NEW TELEBIRR PAYMENT\n\n"

        f"🆔 Request: #{request_id}\n"
        f"👤 Name: {name or 'Unknown'}\n"
        f"🔹 Username: {username}\n"
        f"🆔 Telegram ID: {user.id}\n"
        f"💎 Plan: {plan_name(plan)}\n"
        f"💰 Expected Amount: {amount} ETB\n"
        f"🔢 Transaction ID: {transaction_id or 'Not detected'}\n"
        f"🕐 Time: {now_iso()}\n\n"

        "⚠️ INITIAL CHECK PASSED.\n"
        "This does NOT prove that the payment is real.\n"
        "👨‍💼 Admin MUST independently verify the transaction in Telebirr before approving.\n\n"

        "📩 FULL TELEBIRR SMS:\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{text}\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    try:

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

    except Exception:

        logger.exception(
            "Could not send payment request to admin."
        )

        await update.message.reply_text(
            "⚠️ Payment details were saved, "
            "but the admin could not be notified automatically."
        )


# =========================================================
# IMAGE AI
# =========================================================

async def image_chat(update, context):
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

    # PAYMENT SESSION HAS PRIORITY
    if get_payment_session(context):

        await update.message.reply_text(
            "📩 Please paste the FULL Telebirr SMS as text.\n\n"
            "📸 Screenshot payment is not used."
        )

        return

    premium = is_premium(user.id)

    # FREE LIMIT
    if not premium:

        used = get_today_image_count(
            user.id
        )

        if used >= FREE_DAILY_IMAGE_LIMIT:

            await update.message.reply_text(
                "🆓 DAILY IMAGE LIMIT REACHED\n\n"
                f"🖼️ Images today: "
                f"{FREE_DAILY_IMAGE_LIMIT}/"
                f"{FREE_DAILY_IMAGE_LIMIT}\n\n"
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

    question = (
        update.message.caption
        or
        "Please analyze this image carefully and explain what you see."
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
            if response
            and response.text
            else None
        )

        if not answer:

            await update.message.reply_text(
                "⚠️ I received the image, but I could not generate an answer."
            )

            return

        # Record only after successful AI response
        if not premium:
            record_image_usage(
                user.id
            )

        for i in range(
            0,
            len(answer),
            4000,
        ):

            await update.message.reply_text(
                answer[i:i + 4000]
            )

        if not premium:

            current = get_today_image_count(
                user.id
            )

            remaining = max(
                0,
                FREE_DAILY_IMAGE_LIMIT - current,
            )

            await update.message.reply_text(
                f"🖼️ Images today: "
                f"{current}/{FREE_DAILY_IMAGE_LIMIT}\n"
                f"Remaining today: {remaining}"
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
# TEXT CHAT
# =========================================================

async def chat(update, context):
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

    # PAYMENT SMS HAS PRIORITY
    if get_payment_session(context):

        await payment_sms_handler(
            update,
            context,
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
            if response
            and response.text
            else None
        )

        if not answer:

            await update.message.reply_text(
                "⚠️ I could not generate a response."
            )

            return

        for i in range(
            0,
            len(answer),
            4000,
        ):

            await update.message.reply_text(
                answer[i:i + 4000]
            )

    except Exception as e:

        logger.exception(
            "TEXT CHAT ERROR"
        )

        await update.message.reply_text(
            "⚠️ Ethio AI error.\n\n"
            f"{type(e).__name__}: {e}"
        )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

async def admin_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
# USERS
# =========================================================

async def users_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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

        name = row["first_name"] or "Unknown"

        if row["last_name"]:
            name += " " + row["last_name"]

        username = (
            "@" + row["username"]
            if row["username"]
            else "No username"
        )

        if row["blocked"]:
            status = "🚫 BLOCKED"

        elif row["status"] == "premium":

            if is_premium(row["user_id"]):
                status = "💎 PREMIUM"
            else:
                status = "🆓 FREE"

        else:
            status = "🆓 FREE"

        premium_until = (
            row["premium_until"]
            or "-"
        )

        text += (
            f"👤 {name}\n"
            f"{username}\n"
            f"🆔 ID: {row['user_id']}\n"
            f"{status}\n"
            f"📅 Premium until: {premium_until}\n"
            f"🕐 Last active: {row['last_active']}\n\n"
        )

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

async def stats_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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

async def pending_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
            p.id,
            p.user_id,
            p.plan,
            p.amount_etb,
            p.transaction_id,
            p.created_at,
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
        ).strip()

        name = name or "Unknown"

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
            f"🔢 Transaction: "
            f"{row['transaction_id'] or 'Not detected'}\n"
            f"🕐 {row['created_at']}\n\n"
        )

    await update.message.reply_text(
        text
    )


# =========================================================
# ADMIN PREMIUM
# =========================================================

async def premium_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
            "/premiumuser USER_ID [days]\n\n"
            "Example:\n"
            "/premiumuser 123456789 30"
        )

        return

    try:

        target_id = int(
            context.args[0]
        )

        days = (
            int(context.args[1])
            if len(context.args) > 1
            else MONTHLY_DAYS
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID and days must be numbers."
        )

        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT user_id
        FROM users
        WHERE user_id = ?
        """,
        (target_id,),
    )

    exists = cursor.fetchone()

    conn.close()

    if not exists:

        await update.message.reply_text(
            "❌ User not found in database."
        )

        return

    expiry = set_premium(
        target_id,
        days,
    )

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
# FREE
# =========================================================

async def free_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
# BLOCK
# =========================================================

async def block_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
# UNBLOCK
# =========================================================

async def unblock_command(update, context):
    if not update.effective_user:
        return

    if not update.message:
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
# ERROR
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "UNHANDLED ERROR",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

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

    logger.info(
        f"Bot: @{BOT_USERNAME}"
    )

    logger.info(
        f"Free image limit: {FREE_DAILY_IMAGE_LIMIT}"
    )

    logger.info(
        f"Monthly Premium: {MONTHLY_PRICE_ETB} ETB"
    )

    logger.info(
        f"Yearly Premium: {YEARLY_PRICE_ETB} ETB"
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # =====================================================
    # USER COMMANDS
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "premium",
            premium_menu,
        )
    )

    app.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    # =====================================================
    # ADMIN COMMANDS
    # =====================================================

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "users",
            users_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "pending",
            pending_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "premiumuser",
            premium_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "free",
            free_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "block",
            block_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "unblock",
            unblock_command,
        )
    )

    # =====================================================
    # CALLBACK BUTTONS
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # =====================================================
    # PHOTOS
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            image_chat,
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat,
        )
    )

    # =====================================================
    # ERROR
    # =====================================================

    app.add_error_handler(
        error_handler
    )

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
