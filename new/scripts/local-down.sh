#!/bin/zsh
# Stop the local stack started by local-up.sh.
DATA="$(cd "$(dirname "$0")/.." && pwd)/data/local"
[ -f "$DATA/pids" ] || { echo "没有运行中的本地栈"; exit 0; }
while read -r pid; do
  kill "$pid" 2>/dev/null || true
done < "$DATA/pids"
sleep 1
while read -r pid; do
  kill -9 "$pid" 2>/dev/null || true
done < "$DATA/pids"
: > "$DATA/pids"
echo "本地栈已停止"
