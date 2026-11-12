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
NOAM_BIN="${NOAM_BIN:-$HOME/.local/bin/noam}"   # NSDP 取数（--nsdp noam）
NSDP_MODE="${NSDP_MODE:-noam}"                   # noam | off（本地测 GUI/主链路可 off 跳过 NSDP 门与取数）

# 对外可达地址：默认自动探测本机 IP（en0 -> en1），可用 HOST_IP 覆盖。
# 服务全部绑定 0.0.0.0，录制器/调试可从局域网其它机器通过 ws://$HOST_IP:8400/ingest 连入。
HOST_IP="${HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)}"
INGEST_URL="${INGEST_URL:-ws://$HOST_IP:8400/ingest}"

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
# GUI 客户端（含 PySide6/segno，体积较大，首次安装慢）
if ! ls "$VENV/lib/"*"/site-packages/__editable__.macos_gui"*-*.pth >/dev/null 2>&1; then
  echo "[setup] 安装模块 macos-gui[ui] …"
  "$VENV/bin/pip" install --timeout 60 --retries 2 -e "$ROOT/new/macos-gui[ui]"
fi
echo "[setup] 依赖就绪"

DATA="$ROOT/new/data/local"
mkdir -p "$DATA"/{auth,advisor,logs}
chmod 700 "$DATA" "$DATA/auth" "$DATA/advisor"   # advisor/auth 数据目录安全基线(700)
PIDS="$DATA/pids"
: > "$PIDS"

healthz() { curl --noproxy '*' -sf "http://127.0.0.1:$1/healthz" >/dev/null; }
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
  "$VENV/bin/auth" serve --data-dir "$DATA/auth" --host 0.0.0.0 --port 8381 \
    --base-url "$AUTH_BASE_URL"

start stt     8393 "$VENV/bin/stt-server" serve --host 0.0.0.0 --port 8392 \
  --health-port 8393 --auth none --model-dir "$STT_MODEL_DIR"

start gateway 8377 "$VENV/bin/llm-gateway" serve --host 0.0.0.0 --port 8377 \
  --database "$DATA/gateway.sqlite3"

start advisor 8380 "$VENV/bin/advisor" serve --data-dir "$DATA/advisor" \
  --api-host 0.0.0.0 --api-port 8380 --ingest-port 8400 \
  --ingest-url "$INGEST_URL" \
  --stt-url ws://127.0.0.1:8392 --gateway-url http://127.0.0.1:8377 \
  --auth-url http://127.0.0.1:8381 --model remote --remote-backend "$REMOTE_BACKEND" \
  --calendar feishu --nsdp "$NSDP_MODE" $( [ "$NSDP_MODE" = noam ] && echo --noam-bin "$NOAM_BIN" )

start bot     -    env BOT_APP_ID="$BOT_APP_ID" BOT_APP_SECRET="$BOT_APP_SECRET" \
  "$VENV/bin/bot" serve --advisor-url http://127.0.0.1:8380

echo
echo "全部就绪（本机 IP: $HOST_IP）"
echo "  auth:    http://$HOST_IP:8381  (回调基址 $AUTH_BASE_URL)"
echo "  stt:     ws://$HOST_IP:8392   health http://$HOST_IP:8393"
echo "  gateway: http://$HOST_IP:8377"
echo "  advisor: http://$HOST_IP:8380  ingest $INGEST_URL"
echo "  bot: 飞书长连接（无监听端口）"
echo "  GUI:    $VENV/bin/macos-gui   （服务器地址填 http://127.0.0.1:8380）"
echo "日志：$DATA/logs/   停止：new/scripts/local-down.sh"
