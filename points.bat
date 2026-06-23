@echo off
title Kick Points Farmer
chcp 65001 >nul
cd /d "%~dp0"
python points_ui.py --menu
pause
