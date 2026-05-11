#!/usr/bin/env bash
# ============================================================================
# macOS bulk build — produces 2 of the 4 cross-platform targets:
#
#   dist/IPQualityChecker-macos-arm64.app   (Apple Silicon)
#   dist/IPQualityChecker-macos-x86_64.app  (Intel)
#
# Plus matching ipqc CLI binaries.
#
# Zero-environment-prep design
# ----------------------------
# Designed to run on a FRESH macOS machine with nothing installed:
#   1. Detects missing Python and auto-installs
#   2. Tier 1 (always tried first): python.org universal2 .pkg installer
#                                   — required to emit x86_64 binaries
#                                   from an Apple Silicon Mac
#   3. Tier 2 (fallback): Homebrew Python — arm64-only, can only build
#                         arm64 .app, but works fine for that
#
# Skip auto-install:
#   BUILD_NO_AUTO_INSTALL=1 ./scripts/build-all.sh
#
# Override Python:
#   PYTHON=/path/to/python3 ./scripts/build-all.sh
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

NO_AUTO="${BUILD_NO_AUTO_INSTALL:-}"
PY_VERSION="${PY_VERSION:-3.13.1}"   # for python.org direct download

# ──────────────────────────────────────────────────────────────────────────
# Detect Python that satisfies the universal2 requirement
# ──────────────────────────────────────────────────────────────────────────
find_python() {
    local candidates=(
        "$PYTHON"
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
        python3.13 python3.12 python3.11 python3
    )
    for cand in "${candidates[@]}"; do
        if [ -z "$cand" ]; then continue; fi
        if command -v "$cand" >/dev/null 2>&1; then
            echo "$(command -v "$cand")"
            return 0
        fi
    done
    return 1
}

python_arches() {
    # Echo "arm64,x86_64" / "arm64" / "x86_64" / "" based on what slices
    # the given interpreter binary supports.
    local py="$1"
    "$py" -c "
import subprocess, sys
out = subprocess.run(['file', sys.executable], capture_output=True, text=True).stdout
a = []
if 'arm64' in out:  a.append('arm64')
if 'x86_64' in out: a.append('x86_64')
print(','.join(a))
" 2>/dev/null
}

# ──────────────────────────────────────────────────────────────────────────
# Auto-install: prefer python.org universal2 .pkg (so we get both archs);
# fallback to Homebrew (arm64-only). Returns 0 on success.
# ──────────────────────────────────────────────────────────────────────────
install_python_org_pkg() {
    # python.org filename convention: python-3.13.1-macos11.pkg (universal2)
    local pkg_url="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"
    local tmp="/tmp/python-${PY_VERSION}-macos11.pkg"
    echo "  >> Downloading $pkg_url ..."
    if ! curl -fL --progress-bar "$pkg_url" -o "$tmp"; then
        echo "  ❌ Download failed (URL may have changed for this version)"
        return 1
    fi
    echo "  >> Installing (sudo required — system pkg) ..."
    if ! sudo installer -pkg "$tmp" -target /; then
        echo "  ❌ Installer failed"
        return 1
    fi
    rm -f "$tmp"
    # python.org installer drops files at /Library/Frameworks/Python.framework/Versions/3.13/
    return 0
}

install_python_brew() {
    local py_ver="3.13"
    if ! command -v brew >/dev/null 2>&1; then
        echo "  >> Homebrew not present; installing it first ..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            </dev/null || return 1
        # add brew to PATH for this script run
        if [ -x /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -x /usr/local/bin/brew ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi
    echo "  >> brew install python@$py_ver python-tk@$py_ver ..."
    brew install "python@$py_ver" "python-tk@$py_ver" >/dev/null
}

auto_install_python() {
    if [ -n "$NO_AUTO" ]; then
        echo "  BUILD_NO_AUTO_INSTALL set; not installing." >&2
        return 1
    fi
    echo ""
    echo "⚠ No suitable Python found; attempting auto-install ..."
    echo ""
    echo "Tier 1: python.org universal2 .pkg (gives both arm64 + x86_64)"
    if install_python_org_pkg; then
        echo "✓ python.org universal2 installed."
        return 0
    fi
    echo ""
    echo "Tier 2: Homebrew (arm64-only — Intel target won't be possible)"
    if install_python_brew; then
        echo "✓ Homebrew Python installed."
        return 0
    fi
    return 1
}

# ──────────────────────────────────────────────────────────────────────────
# Resolve Python; auto-install if needed
# ──────────────────────────────────────────────────────────────────────────
PY="$(find_python || true)"
if [ -z "$PY" ]; then
    auto_install_python || {
        echo "❌ Python auto-install failed. Install manually from"
        echo "   https://www.python.org/downloads/macos/ and re-run."
        exit 1
    }
    PY="$(find_python || true)"
fi
[ -n "$PY" ] || { echo "❌ Python still not found after install attempt." >&2; exit 1; }

ARCHES="$(python_arches "$PY")"
echo ">>> Using Python: $PY"
echo ">>> Python supports: ${ARCHES:-unknown}"

# If only single-arch and user is on Apple Silicon, offer to upgrade to
# universal2 (gives both targets).
if [[ "$ARCHES" != *"x86_64"* ]] && [[ "$ARCHES" == *"arm64"* ]] && [ -z "$NO_AUTO" ]; then
    cat <<EOF

ℹ Current Python is arm64-only — we can only build the macOS-arm64 target.

  To also build macOS-x86_64 from this machine, install the universal2
  Python from python.org. Run:

    sudo installer -pkg <(curl -L https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg) -target /
    PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 ./scripts/build-all.sh

  Or skip and rely on GitHub Actions for x86_64 builds.

EOF
fi

# ── venv setup ──
if [ ! -d ".venv-build" ]; then
    echo ">>> Creating .venv-build ..."
    "$PY" -m venv .venv-build
fi
# shellcheck disable=SC1091
source .venv-build/bin/activate
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python -m pip install pyinstaller --quiet

# ── Build each arch the Python supports ──
build_one() {
    local arch="$1"
    echo ""
    echo "============================================================"
    echo "  Building for macOS-$arch"
    echo "============================================================"
    python build.py --clean --cli \
        --arch "$arch" \
        --out-suffix "macos-$arch"
}

[[ "$ARCHES" == *"arm64"* ]]  && build_one arm64
[[ "$ARCHES" == *"x86_64"* ]] && build_one x86_64

echo ""
echo "============================================================"
echo "  ✓ macOS bulk build complete."
echo "============================================================"
ls -1 dist/ | grep IPQualityChecker || true
echo ""
echo "Windows builds: copy this folder to a Windows machine and double-click"
echo "  scripts\\build-all-windows.bat"
echo "(or push a tag → .github/workflows/build.yml does all 4 in CI)"
