#!/usr/bin/env bash
# Mac/Linux launcher — picks a Python that has tkinter, sets up venv on first run.
set -e
cd "$(dirname "$0")"

# Find a Python with tkinter. Homebrew's python often lacks _tkinter unless
# `brew install python-tk@3.13` was installed; macOS system python has it.
PYTHON=""
for cand in python3.13 python3.12 python3.11 python3 /usr/bin/python3 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import tkinter" >/dev/null 2>&1; then
      PYTHON="$cand"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  cat <<'EOF' >&2
错误：找不到带 tkinter 的 Python。

macOS 解决方案（任选一个）:
  1. 使用系统 Python: /usr/bin/python3
  2. brew install python-tk@3.13       # 给 Homebrew Python 加 tk
  3. brew install --cask python        # 安装 python.org 官方版（自带 tk）

Linux:
  sudo apt install python3-tk          # Debian/Ubuntu
  sudo dnf install python3-tkinter     # Fedora
EOF
  exit 1
fi

echo ">>> 使用 Python: $($PYTHON --version) at $(command -v $PYTHON)"

if [ ! -d ".venv" ]; then
  echo ">>> 创建虚拟环境 (.venv) ..."
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Re-check tkinter inside the venv (some venvs lose it)
if ! python -c "import tkinter" >/dev/null 2>&1; then
  echo ">>> venv 缺少 tkinter，重建..." >&2
  deactivate || true
  rm -rf .venv
  "$PYTHON" -m venv .venv
  source .venv/bin/activate
fi

python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python main.py
