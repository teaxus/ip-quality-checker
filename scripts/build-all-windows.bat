@echo off
REM ============================================================================
REM Windows: 干净机器双击即用
REM
REM 这个 .bat 只是 PowerShell 脚本的双击外壳——它绕过 ExecutionPolicy 约束，
REM 调用 scripts\build-all.ps1。后者会：
REM   1. 检测当前架构（AMD64 / ARM64）
REM   2. 缺 Python 就自动用 winget 或直链装 (per-user, no admin)
REM   3. 自动建 venv + 装依赖 + PyInstaller
REM   4. 打 x64 .exe（任何 Windows）+ ARM64 .exe（仅 ARM64 host）
REM ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo.
echo ============================================================
echo   IP Quality Checker - Windows Bulk Build
echo   零环境前置：脚本会自动装 Python（如果没有）
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\build-all.ps1" %*
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，详见上方输出
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ^✓ 完成！
echo ============================================================
echo.
echo   dist\IPQualityChecker-windows-x64.exe          ^(双击运行^)
echo   dist\ipqc-windows-x64.exe                       ^(CLI^)
echo.

REM Auto-open dist folder
if exist dist\ explorer.exe dist
pause
