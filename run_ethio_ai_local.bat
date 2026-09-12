@echo off
title Ethio AI Bot
cd /d "%~dp0"

echo ==========================================
echo              ETHIO AI BOT
echo ==========================================
echo.

py -m pip install -r requirements.txt

echo.
echo Enter your NEW Telegram bot token:
set /p TELEGRAM_TOKEN=

echo.
echo Enter your NEW Gemini API key:
set /p GEMINI_API_KEY=

echo.
echo Starting Ethio AI...
echo.

py ethio_ai.py

echo.
echo Bot stopped.
pause
