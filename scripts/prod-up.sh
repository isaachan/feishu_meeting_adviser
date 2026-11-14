#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/scripts/prod.env"
[[ -f "$ENV_FILE" ]] || { echo "Copy scripts/prod.env.example to $ENV_FILE and configure it" >&2; exit 1; }
source "$ENV_FILE"
: "${AUTH_FEISHU_APP_ID:?}" "${AUTH_FEISHU_APP_SECRET:?}" "${BOT_APP_ID:?}" "${BOT_APP_SECRET:?}" "${STT_MODEL_DIR:?}" "${NOAM_BIN:?}" "${OPENCODE_BIN:?}" "${PUBLIC_BASE_URL:?}" "${INGEST_URL:?}"
[[ -d "$STT_MODEL_DIR" && -x "$NOAM_BIN" && -x "$OPENCODE_BIN" ]] || { echo 'Check STT_MODEL_DIR, NOAM_BIN and OPENCODE_BIN' >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || { echo 'Production deployment requires Linux/systemd' >&2; exit 1; }
command -v nginx >/dev/null || { echo 'Install nginx first' >&2; exit 1; }
PUBLIC_HOST="$(python3 -c 'import sys,urllib.parse; u=urllib.parse.urlsplit(sys.argv[1]); assert u.scheme == "http" and u.hostname and not u.path and not u.query and not u.fragment; print(u.hostname)' "$PUBLIC_BASE_URL")"
git -C "$ROOT" diff --quiet && git -C "$ROOT" diff --cached --quiet || { echo 'Commit source changes before deploying' >&2; exit 1; }
services=(auth stt-server llm-gateway advisor bot admin-site)
for service in "${services[@]}"; do
  if systemctl is-active --quiet "meeting-advisor@$service.service"; then
    echo 'Services already running; use prod-down.sh before deployment' >&2; exit 1
  fi
done
chmod 600 "$ENV_FILE"
umask 077
mkdir -p "$ROOT/data/prod/"{auth,advisor,gateway,logs} "$ROOT/.venv-prod"
for service in "${services[@]}"; do
  [[ -x "$ROOT/.venv-prod/$service/bin/python" ]] || python3 -m venv "$ROOT/.venv-prod/$service"
  "$ROOT/.venv-prod/$service/bin/pip" install --upgrade --force-reinstall "$ROOT/$service"
done
user="$(id -un)"
home="$HOME"
unit="$(mktemp)"
nginx_config="$(mktemp)"
trap 'rm -f "$unit" "$nginx_config"' EXIT
cat >"$unit" <<EOF
[Unit]
Description=Meeting advisor %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
WorkingDirectory=$ROOT
Environment=HOME=$home
ExecStart=/bin/bash $ROOT/scripts/prod-run.sh %i
Restart=on-failure
RestartSec=5
TimeoutStopSec=60
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
cat >"$nginx_config" <<EOF
server {
    listen 80;
    server_name $PUBLIC_HOST;
    access_log off;
    location = /oauth/callback { proxy_pass http://127.0.0.1:8381; }
    location = /nsdp/submit { proxy_pass http://127.0.0.1:8381; }
    location = /jira/submit { proxy_pass http://127.0.0.1:8381; }
    location /gui/ { proxy_pass http://127.0.0.1:8380; }
    location / { return 404; }
}
EOF
sudo install -m 644 "$unit" /etc/systemd/system/meeting-advisor@.service
sudo install -m 644 "$nginx_config" /etc/nginx/sites-available/meeting-advisor
sudo ln -sfn /etc/nginx/sites-available/meeting-advisor /etc/nginx/sites-enabled/meeting-advisor
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo systemctl daemon-reload
for service in "${services[@]}"; do
  sudo systemctl enable --now "meeting-advisor@$service.service"
  case "$service" in
    auth) port=8381 ;; stt-server) port=8393 ;; llm-gateway) port=8377 ;;
    advisor) port=8380 ;; admin-site) port=8390 ;; bot) port= ;;
  esac
  if [[ -n "$port" ]]; then
    ready=0
    for ((i=0; i<120; i++)); do
      if curl --noproxy '*' -fsS "http://127.0.0.1:$port/healthz" >/dev/null; then ready=1; break; fi
      sleep 1
    done
    [[ "$ready" == 1 ]] || { echo "$service health check failed" >&2; exit 1; }
  else
    sleep 2
    systemctl is-active --quiet "meeting-advisor@$service.service"
  fi
  "$ROOT/.venv-prod/$service/bin/$service" --version
done
git -C "$ROOT" rev-parse HEAD > "$ROOT/data/prod/deployed-commit"
echo "Production services started; commit $(<"$ROOT/data/prod/deployed-commit")"
