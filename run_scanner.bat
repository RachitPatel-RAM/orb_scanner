@echo off
cd /d "%~dp0"
echo ========================================================
echo             Launching Live ORB Scanner
echo ========================================================
.venv\Scripts\python.exe main.py live
pause
