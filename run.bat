@echo off
chcp 65001 >nul
title LDCompact
cd /d "%~dp0"
python app.py
if errorlevel 1 (
  echo.
  echo Khong chay duoc. Kiem tra: pip install pywebview
  pause
)
