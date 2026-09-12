# Ethio AI Telegram Bot

Python Telegram AI bot using Google Gemini.

## IMPORTANT SECURITY

The old Telegram bot token and Gemini API key were exposed in the previous code. Revoke/regenerate both before using this project.

Never put real API keys in GitHub.

## Railway

1. Create a GitHub repository.
2. Upload these project files.
3. Create a Railway project from the GitHub repository.
4. Add Railway Variables:
   - TELEGRAM_TOKEN
   - GEMINI_API_KEY
5. Set Start Command:
   `python ethio_ai.py`
6. Deploy and check the logs.

## Local Windows test

PowerShell:

```powershell
py -m pip install -r requirements.txt
$env:TELEGRAM_TOKEN="YOUR_NEW_TELEGRAM_TOKEN"
$env:GEMINI_API_KEY="YOUR_NEW_GEMINI_API_KEY"
py ethio_ai.py
```

The bot identifies itself as Ethio AI and can respond in English and Afaan Oromoo.
