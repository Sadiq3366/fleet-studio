@echo off
cd /d "%~dp0"
title Fleet Studio - Local Audio Bridge
where python >nul 2>nul || ( echo Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH"^) & pause & exit /b 1 )
python -m pip install -q -U yt-dlp
python audio_bridge.py
pause
