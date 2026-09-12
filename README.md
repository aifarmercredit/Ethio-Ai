# 🤖 Ethio AI Telegram Bot

Ethio AI is a Telegram AI chatbot powered by Google Gemini.

## Features

- 🤖 AI chat
- 🇬🇧 English
- 🇪🇹 Afaan Oromoo
- 👥 User tracking
- 📊 Admin dashboard
- ⭐ Premium users
- 🆓 Free users
- 🚫 Block / unblock users
- 📈 Statistics
- 🗃️ SQLite database

## Admin Commands

Only the Telegram account specified in `ADMIN_ID`
can use these commands:

/admin

/users

/stats

/premium USER_ID

/free USER_ID

/block USER_ID

/unblock USER_ID

## Railway Deployment

Create a Railway project from this GitHub repository.

Add these Railway Variables:

TELEGRAM_TOKEN

GEMINI_API_KEY

ADMIN_ID

Optional:

GEMINI_MODEL

Use this Start Command:

python ethio_ai.py

Then deploy.

## Security

Never put real Telegram bot tokens or Gemini API keys
inside GitHub files.

Use Railway Variables for secret values.

## Local Windows

Install dependencies:

py -m pip install -r requirements.txt

Set variables:

$env:TELEGRAM_TOKEN="YOUR_TOKEN"
$env:GEMINI_API_KEY="YOUR_GEMINI_KEY"
$env:ADMIN_ID="YOUR_TELEGRAM_ID"

Run:

py ethio_ai.py
