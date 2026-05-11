@echo off
REM ============================================================
REM Windows 一键构建脚本
REM 双击运行即可：建 venv → 装依赖 → 跑 PyInstaller → 输出 .exe
REM ============================================================
setlocal enabledelayedexpansion
cd /d %~dp0

echo.
echo ============================================================
echo   IP Quality Checker - Windows Build
echo ============================================================
echo.

REM 1. 找一个能用的 Python 3.10+
set "PY="
for %%P in (python py "%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe") do (
    if not defined PY (
        %%P --version >nul 2>&1
        if not errorlevel 1 set "PY=%%P"
    )
)

if not defined PY (
    echo [错误] 找不到 Python。请先去 https://www.python.org/downloads/windows/ 装 Python 3.11+
    echo        装的时候勾上 "Add Python to PATH"
    pause
    exit /b 1
)
echo 使用 Python: !PY!
!PY! --version

REM 2. 建 venv
if not exist ".venv" (
    echo.
    echo ^>^>^> 创建虚拟环境 .venv ...
    !PY! -m venv .venv
    if errorlevel 1 (
        echo [错误] venv 创建失败
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

REM 3. 装依赖 + PyInstaller
echo.
echo ^>^>^> 升级 pip + 安装依赖 ...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

REM 4. 跑 PyInstaller
echo.
echo ^>^>^> 开始打包 (单文件, 控制台已隐藏) ...
echo     这一步可能要 1-3 分钟，期间会有大量 PyInstaller 输出，正常现象
echo.
python build.py --clean --cli
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ✓ 打包完成！
echo ============================================================
echo.
echo   GUI:  dist\IPQualityChecker.exe   (双击运行)
echo   CLI:  dist\ipqc.exe               (在 cmd / PowerShell 里跑)
echo.
echo   双击 dist\IPQualityChecker.exe 即可使用，无需 Python 环境
echo.

REM 5. 自动打开 dist 文件夹
explorer.exe dist

pause
