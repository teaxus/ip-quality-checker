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

python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python main.py
