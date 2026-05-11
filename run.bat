@echo off
REM Windows launcher — creates venv on first run, then starts the GUI.
cd /d %~dp0
if not exist ".venv" (
    echo ^>^>^> Creating virtual environment ^(.venv^) ...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
python main.py
pause
