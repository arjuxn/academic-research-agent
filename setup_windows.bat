@echo off
cd /d "%~dp0"
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
echo.
echo Setup complete. Start the desktop app with run_ui.bat
pause
