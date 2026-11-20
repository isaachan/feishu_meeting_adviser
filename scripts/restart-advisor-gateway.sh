#!/usr/bin/env bash
# Temporary helper: reinstall + restart only advisor and llm-gateway (prod).
# prod services run from .venv-prod (installed by prod-up.sh), so code changes
# require a pip reinstall before restart takes effect.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for name in advisor llm-gateway; do
  "$ROOT/.venv-prod/$name/bin/pip" install --quiet --upgrade --force-reinstall "$ROOT/$name"
done
for name in llm-gateway advisor; do
  sudo systemctl restart "meeting-advisor@${name}.service"
done
for name in llm-gateway advisor; do
  systemctl is-active --quiet "meeting-advisor@${name}.service"
  echo "meeting-advisor@${name}.service: active"
done
