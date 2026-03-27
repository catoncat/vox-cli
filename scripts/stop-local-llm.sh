#!/usr/bin/env bash
set -euo pipefail

PORT="${VOX_LOCAL_LLM_PORT:-18080}"
RUN_DIR="${VOX_LOCAL_LLM_RUN_DIR:-${HOME}/.vox/run}"
PID_FILE="${RUN_DIR}/mlx-local-${PORT}.pid"

PIDS=()

if [[ -f "${PID_FILE}" ]]; then
  PID="$(cat "${PID_FILE}")"
  if [[ -n "${PID}" ]]; then
    PIDS+=("${PID}")
  fi
fi

while IFS= read -r PID; do
  if [[ -n "${PID}" ]]; then
    PIDS+=("${PID}")
  fi
done < <(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)

if [[ ${#PIDS[@]} -eq 0 ]]; then
  rm -f "${PID_FILE}"
  echo "local llm is not running on port ${PORT}"
  exit 0
fi

declare -A SEEN=()
for PID in "${PIDS[@]}"; do
  if [[ -n "${SEEN[$PID]:-}" ]]; then
    continue
  fi
  SEEN["$PID"]=1
  kill "${PID}" 2>/dev/null || true
done

for _ in {1..20}; do
  if ! lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    rm -f "${PID_FILE}"
    echo "local llm stopped on port ${PORT}"
    exit 0
  fi
  sleep 0.25
done

echo "local llm still appears to be running on port ${PORT}" >&2
exit 1
