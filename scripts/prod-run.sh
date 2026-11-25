#!/usr/bin/env bash
# Launched by systemd, one instance per service.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/prod.env"
VENV="$ROOT/.venv-prod/$1"
DATA="$ROOT/data/prod"
export PYTHONUNBUFFERED=1
export no_proxy="${no_proxy:-},127.0.0.1,localhost,feishu.cn,.feishu.cn"
export NO_PROXY="$no_proxy"
case "$1" in
  auth)
    exec >>"$DATA/logs/auth.log" 2>&1
    exec "$VENV/bin/auth" serve --host 127.0.0.1 --port 8381 --data-dir "$DATA/auth" --base-url "$PUBLIC_BASE_URL" ;;
  stt-server)
    exec >>"$DATA/logs/stt.log" 2>&1
    exec "$VENV/bin/stt-server" serve --host 127.0.0.1 --port 8392 --health-port 8393 --auth none --model-dir "$STT_MODEL_DIR" ;;
  llm-gateway)
    exec >>"$DATA/logs/gateway.log" 2>&1
    args=(--opencode-executable "$OPENCODE_BIN" --noam-bin "$NOAM_BIN" --database "$DATA/gateway/tasks.sqlite3")
    [[ -z "${OPENCODE_MODEL:-}" ]] || args+=(--opencode-model "$OPENCODE_MODEL")
    [[ -z "${OPENCODE_AUTH:-}" ]] || args+=(--opencode-auth "$OPENCODE_AUTH")
    [[ -z "${OPENCODE_PROVIDER_CONFIG:-}" ]] || args+=(--opencode-provider-config "$OPENCODE_PROVIDER_CONFIG")
    [[ -z "${SKILLS_DIR:-}" ]] || args+=(--skills-dir "$SKILLS_DIR")
    exec "$VENV/bin/llm-gateway" serve --host 127.0.0.1 --port 8377 "${args[@]}" ;;
  advisor)
    exec >>"$DATA/logs/advisor.log" 2>&1
    exec "$VENV/bin/advisor" serve --data-dir "$DATA/advisor" --api-host 127.0.0.1 --api-port 8380 --ingest-port 8400 --ingest-url "$INGEST_URL" --stt-url ws://127.0.0.1:8392 --gateway-url http://127.0.0.1:8377 --auth-url http://127.0.0.1:8381 --model remote --remote-backend "${REMOTE_BACKEND:-opencode}" --calendar feishu --nsdp noam ;;
  bot)
    exec >>"$DATA/logs/bot.log" 2>&1
    exec "$VENV/bin/bot" serve --advisor-url http://127.0.0.1:8380 ;;
  admin-site)
    exec >>"$DATA/logs/admin-site.log" 2>&1
    exec "$VENV/bin/admin-site" serve --advisor-url http://127.0.0.1:8380 --logs-dir "$DATA/logs" --host 127.0.0.1 --port 8390 ;;
  *) exit 2 ;;
esac
