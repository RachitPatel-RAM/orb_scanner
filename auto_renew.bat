@echo off
cd /d "%~dp0"
echo ========================================================
echo        DhanHQ Token Daily Auto-Renewal Script
echo ========================================================
.venv\Scripts\python.exe main.py renew
echo ========================================================
pause
