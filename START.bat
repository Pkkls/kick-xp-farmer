@echo off
title Kick Points Launcher
chcp 65001 >nul
cd /d "%~dp0"

REM --- Python present ? ---
where python >nul 2>&1
if errorlevel 1 (
  echo [X] Python introuvable. Installe Python 3.10+ depuis https://python.org
  echo     ^(coche "Add python.exe to PATH" pendant l'installation^)
  pause
  exit /b 1
)

REM --- Premiere fois : installe les dependances + Chromium ---
if not exist ".ready" (
  echo ============================================================
  echo  Premiere installation ^(une seule fois, ~1-2 min^)...
  echo ============================================================
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [X] Echec de l'installation des dependances.
    pause
    exit /b 1
  )
  REM Note: pas de "playwright install" -- on pilote le VRAI Chrome de la machine,
  REM pas le Chromium bundle. Il suffit que Google Chrome soit installe.
  echo ok> .ready
  echo.
  echo Installation terminee.
  echo.
)

REM --- config.json present ? ---
if not exist "config.json" (
  echo [i] config.json absent : creation depuis config.example.json
  copy /y config.example.json config.json >nul
  echo     -^> ouvre config.json et colle ton session_token, puis relance.
  notepad config.json
  pause
  exit /b 0
)

echo Lancement du dashboard -^> http://127.0.0.1:8780
echo ^(Ferme cette fenetre ou Ctrl+C pour tout arreter^)
python launcher.py
pause
