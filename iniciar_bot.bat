@echo off
title Bot Citas Consulares
echo ============================================
echo   Bot de Citas Consulares - Montevideo
echo ============================================
echo.
cd /d "%~dp0"
python cita_bot_playwright.py
pause
