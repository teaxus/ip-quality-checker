@echo off
REM Windows launcher — uses only project-local .venv for deterministic runtime.
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Missing project virtual environment: .venv\Scripts\python.exe
    echo Please run:
    echo   py -3 -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
python main.py
pause
