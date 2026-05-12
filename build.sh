#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

show_help() {
  cat <<'EOF'
用法:
  ./build.sh [包装参数] -- [build.py 参数]
  ./build.sh [build.py 参数]

安全默认行为:
  - 不带参数: 仅显示帮助并退出（不会构建）
  - 带参数: 默认直接执行（不再二次确认）

包装参数:
  -h, --help        显示帮助并退出
  -n, --dry-run     仅打印将执行的命令，不实际执行
  -c, --confirm     执行前二次确认
  -y, --yes         与旧行为兼容（当前为可选，无需使用）

常用 build.py 参数:
  --remote                        在 GitHub Actions 远程构建并下载产物
  --platform windows|macos|all    配合 --remote 指定平台（默认: all）
  --clean                         构建前清理 build/dist
  --arch arm64|x86_64|universal2  macOS 目标架构
  --onedir                        目录输出模式
  --onefile                       单文件输出模式
  --cli                           同时构建 CLI
  --cli-only                      仅构建 CLI
  --out-suffix NAME               输出后缀
  --out-dir PATH                  输出目录（默认: dist）

示例:
  ./build.sh --remote --platform windows
  ./build.sh --remote --platform macos
  ./build.sh --clean --arch arm64 --out-suffix macos-arm64
  ./build.sh --out-dir dist/remote --remote --platform windows
  ./build.sh --dry-run --remote --platform all
  ./build.sh --confirm --remote --platform windows

说明:
  - 该脚本会将剩余参数透传给 build.py。
  - 仅使用当前项目目录下的 .venv/bin/python。
  - 远程构建需要先完成 gh 登录（gh auth login）。
EOF
}

find_python() {
  if [[ -x ".venv/bin/python" ]]; then
    echo ".venv/bin/python"
    return 0
  fi

  echo ""
  return 1
}

if [[ $# -eq 0 ]]; then
  show_help
  exit 0
fi

WRAP_DRY_RUN=0
WRAP_YES=0
WRAP_CONFIRM=0
BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    -n|--dry-run)
      WRAP_DRY_RUN=1
      shift
      ;;
    -c|--confirm)
      WRAP_CONFIRM=1
      shift
      ;;
    -y|--yes)
      WRAP_YES=1
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        BUILD_ARGS+=("$1")
        shift
      done
      ;;
    *)
      BUILD_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#BUILD_ARGS[@]} -eq 0 ]]; then
  echo "错误：未提供构建参数。" >&2
  echo "请运行 ./build.sh -h 查看用法。" >&2
  exit 2
fi

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "错误：未找到项目虚拟环境 Python: .venv/bin/python" >&2
  echo "请先在项目目录执行：python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

CMD=("$PYTHON_BIN" build.py "${BUILD_ARGS[@]}")

echo ">>> 使用 Python: $PYTHON_BIN"
echo ">>> 将执行命令: ${CMD[*]}"

if [[ $WRAP_DRY_RUN -eq 1 ]]; then
  echo ">>> 已启用 dry-run，未执行命令。"
  exit 0
fi

if [[ $WRAP_CONFIRM -eq 1 && $WRAP_YES -ne 1 ]]; then
  if [[ ! -t 0 ]]; then
    echo "错误：当前是非交互环境，使用 --confirm 时必须同时添加 --yes。" >&2
    exit 2
  fi
  read -r -p ">>> 是否继续执行？[y/N] " ans
  case "$ans" in
    y|Y|yes|YES)
      ;;
    *)
      echo ">>> 已取消。"
      exit 0
      ;;
  esac
fi

exec "${CMD[@]}"
