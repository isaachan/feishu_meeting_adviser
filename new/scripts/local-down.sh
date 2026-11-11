#!/bin/zsh
# Stop the local stack started by local-up.sh (pids file + orphan sweep).
DATA="$(cd "$(dirname "$0")/.." && pwd)/data/local"
if [ -f "$DATA/pids" ]; then
  while read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$DATA/pids"
  sleep 1
  while read -r pid; do
    kill -9 "$pid" 2>/dev/null || true
  done < "$DATA/pids"
  : > "$DATA/pids"
fi
# orphan sweep: anything from the local venv still serving
pkill -f "new/.venv-local/bin/(auth|stt-server|llm-gateway|advisor|bot) serve" 2>/dev/null || true
sleep 0.5
pkill -9 -f "new/.venv-local/bin/(auth|stt-server|llm-gateway|advisor|bot) serve" 2>/dev/null || true
echo "本地栈已停止"
