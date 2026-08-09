#!/usr/bin/env bash
set -euo pipefail

# Open frequently used JourneyPilot project documents or local service URLs.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_menu() {
  cat <<'EOF'

  JourneyPilot quick open

  README       English README
  README-ZH    Chinese README
  FRONTEND     Frontend README
  CONFIG       Example configuration
  API          Local API docs (requires backend)
  APP          Local frontend app (requires frontend dev server)

Usage:
  ./open.sh --list
  ./open.sh README API

EOF
}

target_for_key() {
  case "$1" in
    README) echo "$SCRIPT_DIR/README.md" ;;
    README-ZH|README_CN|READMEZH) echo "$SCRIPT_DIR/README.zh-CN.md" ;;
    FRONTEND) echo "$SCRIPT_DIR/frontend/README.md" ;;
    CONFIG) echo "$SCRIPT_DIR/config.example.yaml" ;;
    API) echo "http://localhost:8001/docs" ;;
    APP) echo "http://localhost:8080" ;;
    *) return 1 ;;
  esac
}

open_target() {
  local target="$1"
  if [[ "$target" != http://* && "$target" != https://* && ! -e "$target" ]]; then
    echo "目标不存在: $target" >&2
    return 1
  fi

  if [[ "$(uname -s)" == "Darwin" ]]; then
    open "$target"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target" >/dev/null 2>&1 &
  elif command -v explorer.exe >/dev/null 2>&1; then
    if [[ "$target" == http://* || "$target" == https://* ]]; then
      explorer.exe "$target" >/dev/null 2>&1
    elif command -v wslpath >/dev/null 2>&1; then
      explorer.exe "$(wslpath -w "$target")" >/dev/null 2>&1
    else
      echo "WSL 打开本地文件需要 wslpath: $target" >&2
      return 1
    fi
  else
    echo "找不到可用的打开命令: open / xdg-open / explorer.exe" >&2
    return 1
  fi
}

if [[ "${1:-}" == "--list" || "${1:-}" == "-l" || $# -eq 0 ]]; then
  print_menu
  [[ $# -eq 0 ]] && exit 0
  exit 0
fi

status=0
for arg in "$@"; do
  key="$(echo "$arg" | tr '[:lower:]' '[:upper:]')"
  if ! target="$(target_for_key "$key")"; then
    echo "未知索引: $arg" >&2
    status=1
    continue
  fi
  echo "打开: $key -> $target"
  open_target "$target" || status=1
done

exit "$status"
