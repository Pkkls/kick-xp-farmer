@echo off
title Kick XP Farmer
cd /d "%~dp0"

:loop
echo.
echo [%time%] Demarrage...
python farmer.py
echo.
echo [%time%] Arrete. Relance dans 30 secondes... (Ctrl+C pour quitter)
timeout /t 30 /nobreak
goto loop
