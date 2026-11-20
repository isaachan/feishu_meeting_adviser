#!/usr/bin/env bash
# Temporary helper: restart only advisor and llm-gateway (prod systemd units).
set -euo pipefail
for name in llm-gateway advisor; do
  sudo systemctl restart "meeting-advisor@${name}.service"
done
for name in llm-gateway advisor; do
  systemctl is-active --quiet "meeting-advisor@${name}.service"
  echo "meeting-advisor@${name}.service: active"
done
