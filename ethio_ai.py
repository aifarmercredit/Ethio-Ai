import logging
import os

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
MODEL = "gemini-3.6-flash"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing.")

client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello!\n\n"
        "🤖 I am Ethio AI.\n"
        "Your intelligent AI assistant powered by Google Gemini.\n\n"
        "Ask me anything and I will try my best to help you.\n\n"
        "Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 ETHIO AI\n\n"
        "I am your AI assistant powered by Google Gemini.\n\n"
        "Send me any question and I will try my best to help you.\n\n"
        "📌 Commands:\n"
        "/start - Start Ethio AI\n"
        "/help - Show this help message\n\n"
        "You can simply type your question without using a command."
    )

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
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
- If the user asks "What is your name?", answer: "My name is Ethio AI."
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

        answer = response.text or "Sorry, I could not generate an answer."

        # Telegram messages have a maximum length.
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i:i + 4000])

    except Exception:
        logger.exception("Gemini request failed")
        await update.message.reply_text(
            "⚠️ Sorry, Ethio AI could not process your request right now.\n\n"
            "Please try again in a moment."
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram error", exc_info=context.error)

def main():
    print("=" * 55)
    print("                    ETHIO AI")
    print("=" * 55)
    print("Starting Ethio AI Telegram Bot...")
    print(f"Gemini model: {MODEL}")
    print()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, chat)
    )
    app.add_error_handler(error_handler)

    print("✅ Ethio AI is running!")
    print("Open Telegram and send /start")
    print()

    app.run_polling()

if __name__ == "__main__":
    main()
