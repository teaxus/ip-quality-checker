# ============================================================================
# Windows bulk build — produces 2 of the 4 cross-platform targets:
#
#   dist\IPQualityChecker-windows-x64.exe       (x86_64 / AMD64)
#   dist\IPQualityChecker-windows-arm64.exe     (ARM64, Surface Pro X etc.)
#
# Plus matching ipqc CLI binaries.
#
# Zero-environment-prep design
# ----------------------------
# Designed to run on a FRESH Windows machine with nothing installed:
# the script detects missing Python and auto-installs it via:
#   1. winget (preferred, ships with Windows 10 1809+ / Windows 11)
#   2. Direct python.org .exe installer (fallback when winget unavailable)
#
# Both x64 and arm64 Python are installed when running on Windows ARM,
# letting you produce BOTH .exe targets from one machine.
#
# Usage
# -----
#   powershell -ExecutionPolicy Bypass -File scripts\build-all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build-all.ps1 -SkipAutoInstall
# ============================================================================
param(
    [switch]$SkipAutoInstall = $false,    # 跳过自动安装 Python
    [string]$PyVersion = "3.13.1"          # 直链回退用的具体小版本号
)
$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT

Write-Host ">>> Project root: $ROOT"
Write-Host ">>> Host architecture: $env:PROCESSOR_ARCHITECTURE"

# ── Refresh PATH from registry — needed after winget / installer dropped
#    new files into Program Files but our current shell doesn't see them yet.
function Refresh-Path {
    $env:PATH = ([System.Environment]::GetEnvironmentVariable("Path","Machine") +
                 ";" +
                 [System.Environment]::GetEnvironmentVariable("Path","User"))
}

# ── Locate a Python interpreter of the requested architecture ──
function Find-Python {
    param([string]$Arch)   # "AMD64" or "ARM64"
    Refresh-Path
    $candidates = @()
    # Try py launcher first
    foreach ($v in @("3.13","3.12","3.11")) {
        if ($Arch -eq "AMD64") { $candidates += "py -$v-64" }
        else                    { $candidates += "py -$v-arm" }
    }
    # Then concrete install paths
    $localApp = $env:LocalAppData
    if ($Arch -eq "AMD64") {
        $candidates += "$localApp\Programs\Python\Python313\python.exe"
        $candidates += "$localApp\Programs\Python\Python312\python.exe"
        $candidates += "$localApp\Programs\Python\Python311\python.exe"
        $candidates += "C:\Python313\python.exe"
        $candidates += "C:\Python312\python.exe"
    } else {
        $candidates += "$localApp\Programs\Python\Python313-arm64\python.exe"
        $candidates += "$localApp\Programs\Python\Python312-arm64\python.exe"
    }
    foreach ($c in $candidates) {
        try {
            $out = & cmd /c "$c -c `"import platform; print(platform.machine())`" 2>nul"
            if ($LASTEXITCODE -eq 0 -and $out) {
                $machine = $out.Trim()
                if ($machine -ieq $Arch) { return $c }
            }
        } catch { }
    }
    return $null
}

# ── Auto-install Python for a given arch ──
function Install-Python {
    param([string]$Arch)   # "AMD64" or "ARM64"
    if ($SkipAutoInstall) {
        Write-Host "  -SkipAutoInstall set; not installing $Arch Python." -ForegroundColor Yellow
        return $false
    }

    # ON x86_64 host, we cannot meaningfully install ARM64 Python — it would
    # not run (x86 Windows has no arm64-to-x86 emulation, only the reverse).
    if ($Arch -eq "ARM64" -and $env:PROCESSOR_ARCHITECTURE -ne "ARM64") {
        Write-Host "  Host is $env:PROCESSOR_ARCHITECTURE; cannot install ARM64 Python." -ForegroundColor Yellow
        Write-Host "  → ARM64 .exe must be produced from a Windows ARM64 host." -ForegroundColor Yellow
        return $false
    }

    # ── Attempt 1: winget (cleanest) ──
    $haveWinget = $false
    try {
        & winget --version *> $null
        $haveWinget = ($LASTEXITCODE -eq 0)
    } catch { }

    if ($haveWinget) {
        $archFlag = if ($Arch -eq "AMD64") { "x64" } else { "arm64" }
        Write-Host "  >> winget install Python.Python.3.13 --architecture $archFlag ..." -ForegroundColor Cyan
        & winget install --id Python.Python.3.13 `
            --architecture $archFlag `
            --scope user `
            --silent `
            --accept-package-agreements `
            --accept-source-agreements `
            --disable-interactivity 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) {
            Refresh-Path
            return $true
        }
        Write-Host "  ⚠ winget failed (exit $LASTEXITCODE); falling back to direct download." -ForegroundColor Yellow
    } else {
        Write-Host "  >> winget not found; using direct python.org download." -ForegroundColor Cyan
    }

    # ── Attempt 2: direct python.org installer ──
    $archSuffix = if ($Arch -eq "AMD64") { "amd64" } else { "arm64" }
    $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-$archSuffix.exe"
    $tmp = Join-Path $env:TEMP "python-$PyVersion-$archSuffix.exe"
    Write-Host "  >> Downloading $url ..." -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    } catch {
        Write-Host "  ❌ Download failed: $_" -ForegroundColor Red
        return $false
    }
    Write-Host "  >> Running installer (per-user, silent, adds to PATH) ..." -ForegroundColor Cyan
    # /passive = visible progress bar, no clicks needed
    # InstallAllUsers=0 = per-user (no UAC prompt)
    # PrependPath=1 = adds python.exe to PATH
    # Include_test=0 = skip test suite to save space
    & $tmp /passive InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0 | Out-Null
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1602) {
        Write-Host "  ❌ Installer exited with $LASTEXITCODE" -ForegroundColor Red
        return $false
    }
    Refresh-Path
    return $true
}

function Build-One {
    param([string]$Arch, [string]$PyCmd)
    Write-Host ""
    Write-Host "============================================================"
    Write-Host "  Building for windows-$Arch  (Python: $PyCmd)"
    Write-Host "============================================================"

    $venv = ".venv-build-$Arch"
    if (-not (Test-Path $venv)) {
        Write-Host ">>> Creating $venv ..."
        & cmd /c "$PyCmd -m venv $venv"
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed for $Arch" }
    }
    $vpy = "$venv\Scripts\python.exe"
    & $vpy -m pip install --upgrade pip --quiet
    & $vpy -m pip install -r requirements.txt --quiet
    & $vpy -m pip install pyinstaller --quiet

    $suffix = if ($Arch -eq "AMD64") { "windows-x64" } else { "windows-arm64" }
    & $vpy build.py --clean --cli --out-suffix $suffix
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Arch" }
}

# ── Ensure both Python archs are present (install if missing) ──
$pyAmd64 = Find-Python "AMD64"
if (-not $pyAmd64) {
    Write-Host ""
    Write-Host "⚠ x86_64 Python not found; attempting auto-install..." -ForegroundColor Yellow
    if (Install-Python "AMD64") {
        $pyAmd64 = Find-Python "AMD64"
    }
}

$pyArm64 = Find-Python "ARM64"
if (-not $pyArm64) {
    Write-Host ""
    Write-Host "⚠ ARM64 Python not found; attempting auto-install..." -ForegroundColor Yellow
    if (Install-Python "ARM64") {
        $pyArm64 = Find-Python "ARM64"
    }
}

if (-not $pyAmd64 -and -not $pyArm64) {
    Write-Host ""
    Write-Host "❌ No Python interpreters available even after auto-install attempt." -ForegroundColor Red
    Write-Host "   Manual install: https://www.python.org/downloads/windows/" -ForegroundColor Red
    exit 1
}

# ── Build each available arch ──
if ($pyAmd64) {
    Build-One -Arch "AMD64" -PyCmd $pyAmd64
} else {
    Write-Host "⚠ Skipping x86_64 build — no AMD64 Python available." -ForegroundColor Yellow
}

if ($pyArm64) {
    Build-One -Arch "ARM64" -PyCmd $pyArm64
} else {
    Write-Host "⚠ Skipping ARM64 build — no ARM64 Python available." -ForegroundColor Yellow
    if ($env:PROCESSOR_ARCHITECTURE -ne "ARM64") {
        Write-Host "  (Expected on x86_64 hosts — ARM64 .exe needs ARM64 Windows.)" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  ✓ Windows bulk build complete." -ForegroundColor Green
Write-Host "============================================================"
Get-ChildItem dist\ -Filter "IPQualityChecker*" -ErrorAction SilentlyContinue | Format-Table Name, Length -AutoSize
Write-Host ""
Write-Host "macOS arm64 / x86_64 builds: run scripts/build-all.sh on macOS,"
Write-Host "or push a tag and use .github/workflows/build.yml for all 4 at once."
