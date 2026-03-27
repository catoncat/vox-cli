#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODEL="${VOX_LOCAL_LLM_MODEL:-mlx-community/Qwen2.5-1.5B-Instruct-4bit}"
HOST="${VOX_LOCAL_LLM_HOST:-127.0.0.1}"
PORT="${VOX_LOCAL_LLM_PORT:-18080}"
LOG_DIR="${VOX_LOCAL_LLM_LOG_DIR:-${HOME}/.vox/logs}"
RUN_DIR="${VOX_LOCAL_LLM_RUN_DIR:-${HOME}/.vox/run}"
LOG_FILE="${LOG_DIR}/mlx-local-${PORT}.log"
PID_FILE="${RUN_DIR}/mlx-local-${PORT}.pid"

FOREGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground)
      FOREGROUND=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      echo "usage: $0 [--foreground]" >&2
      exit 1
      ;;
  esac
done

mkdir -p "${LOG_DIR}" "${RUN_DIR}"

if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "local llm already listening on ${HOST}:${PORT}"
  lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN
  exit 0
fi

if [[ "${FOREGROUND}" == "1" ]]; then
  cd "${REPO_ROOT}"
  exec uv run mlx_lm.server \
    --model "${MODEL}" \
    --host "${HOST}" \
    --port "${PORT}"
fi

cd "${REPO_ROOT}"
nohup uv run mlx_lm.server \
  --model "${MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  >"${LOG_FILE}" 2>&1 &

PID=$!
echo "${PID}" > "${PID_FILE}"

for _ in {1..20}; do
  if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "local llm started"
    echo "pid: ${PID}"
    echo "url: http://${HOST}:${PORT}/v1"
    echo "log: ${LOG_FILE}"
    echo "pid file: ${PID_FILE}"
    exit 0
  fi
  sleep 0.5
done

echo "local llm did not start successfully; check log: ${LOG_FILE}" >&2
exit 1
