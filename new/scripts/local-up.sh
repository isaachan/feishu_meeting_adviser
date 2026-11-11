#!/bin/zsh
# Local all-on-Mac bring-up: auth + stt-server + llm-gateway + advisor + bot.
# Usage: ./local-up.sh   (env from new/scripts/local.env, see local.env.example)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${1:-$ROOT/new/scripts/local.env}"
[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE（复制 local.env.example 并填写）"; exit 1; }
source "$ENV_FILE"

: "${AUTH_FEISHU_APP_ID:?}" "${AUTH_FEISHU_APP_SECRET:?}"
: "${BOT_APP_ID:?}" "${BOT_APP_SECRET:?}"
: "${STT_MODEL_DIR:?请设置 STT_MODEL_DIR（模型下载见 new/docs/LOCAL_TESTING.md）}"
[ -d "$STT_MODEL_DIR" ] || { echo "STT_MODEL_DIR 不存在: $STT_MODEL_DIR"; exit 1; }
REMOTE_BACKEND="${REMOTE_BACKEND:-fake}"
AUTH_BASE_URL="${AUTH_BASE_URL:-http://127.0.0.1:8381}"

VENV="$ROOT/new/.venv-local"
if [ ! -x "$VENV/bin/python" ]; then
  echo "[setup] 创建虚拟环境（首次运行约 2-5 分钟，取决于网络）"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --timeout 60 --retries 2 --upgrade pip
fi
for m in auth stt-server llm-gateway advisor bot recorder; do
  marker="$VENV/lib/"*"/site-packages/__editable__.${m//-/_}"*-*.pth
  if ! ls $~marker >/dev/null 2>&1; then
    echo "[setup] 安装模块 $m …"
    "$VENV/bin/pip" install --timeout 60 --retries 2 -e "$ROOT/new/$m"
  fi
done
echo "[setup] 依赖就绪"

DATA="$ROOT/new/data/local"
mkdir -p "$DATA"/{auth,advisor,logs}
chmod 700 "$DATA" "$DATA/auth" "$DATA/advisor"   # advisor/auth 数据目录安全基线(700)
PIDS="$DATA/pids"
: > "$PIDS"

healthz() { curl -sf "http://127.0.0.1:$1/healthz" >/dev/null; }
wait_up() { for _ in {1..60}; do healthz "$1" && return 0; sleep 0.5; done; return 1; }

start() { # name port cmd...
  local name=$1 port=$2; shift 2
  ( "$@" >"$DATA/logs/$name.log" 2>&1 & echo $! >> "$PIDS" )
  if [ "$port" != "-" ]; then
    wait_up "$port" || { echo "$name 未就绪（见 $DATA/logs/$name.log）"; exit 1; }
  else
    sleep 1.5
    kill -0 $(tail -1 "$PIDS") 2>/dev/null || { echo "$name 启动失败（见日志）"; exit 1; }
  fi
  echo "✓ $name"
}

start auth    8381 env AUTH_FEISHU_APP_ID="$AUTH_FEISHU_APP_ID" \
                    AUTH_FEISHU_APP_SECRET="$AUTH_FEISHU_APP_SECRET" \
  "$VENV/bin/auth" serve --data-dir "$DATA/auth" --port 8381 --base-url "$AUTH_BASE_URL"

start stt     8393 "$VENV/bin/stt-server" serve --port 8392 --health-port 8393 --auth none \
  --model-dir "$STT_MODEL_DIR"

start gateway 8377 "$VENV/bin/llm-gateway" serve --port 8377 \
  --database "$DATA/gateway.sqlite3"

start advisor 8380 "$VENV/bin/advisor" serve --data-dir "$DATA/advisor" \
  --api-port 8380 --ingest-port 8400 \
  --ingest-url ws://127.0.0.1:8400/ingest \
  --stt-url ws://127.0.0.1:8392 --gateway-url http://127.0.0.1:8377 \
  --auth-url http://127.0.0.1:8381 --model remote --remote-backend "$REMOTE_BACKEND" \
  --calendar feishu

start bot     -    env BOT_APP_ID="$BOT_APP_ID" BOT_APP_SECRET="$BOT_APP_SECRET" \
  "$VENV/bin/bot" serve --advisor-url http://127.0.0.1:8380

echo
echo "全部就绪：auth:8381 stt:8392 gateway:8377 advisor:8380/8400 bot:长连接"
echo "日志：$DATA/logs/   停止：new/scripts/local-down.sh"
