#!/usr/bin/env bash
# Mac/Linux launcher — uses only project-local .venv for deterministic runtime.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  cat <<'EOF' >&2
错误：未找到项目虚拟环境 Python: .venv/bin/python

请先在项目目录执行：
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
EOF
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
echo ">>> 使用 Python: $(python --version) at .venv/bin/python"

# 只在依赖缺失时才安装，避免每次启动都走网络
if ! python -c "import customtkinter" 2>/dev/null; then
  echo ">>> 首次运行，安装依赖..."
  python -m pip install -r requirements.txt
fi

# Redirect OS-level stderr (e.g. macOS IMK messages, C-extension warnings)
# to both the terminal and the log file so nothing is silently lost.
LOG_DIR="$HOME/.ip-quality-checker/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d).log"

python main.py 2> >(tee -a "$LOG_FILE" >&2)
