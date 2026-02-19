#!/usr/bin/env bash
set -euo pipefail

STAGE=""
COMMAND=""
ERROR_MSG=""
RETRY_CMD=""
LOG_FILE="${VOX_HOME:-$HOME/.vox}/agent/failures.jsonl"
SANITIZE=1

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/log_failure.sh \
    --stage "<stage>" \
    --command "<command>" \
    --error "<error-message>" \
    [--retry "<retry-command>"] \
    [--log-file "<path>"] \
    [--no-sanitize]
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)
      [[ $# -ge 2 ]] || usage
      STAGE="$2"
      shift 2
      ;;
    --command)
      [[ $# -ge 2 ]] || usage
      COMMAND="$2"
      shift 2
      ;;
    --error)
      [[ $# -ge 2 ]] || usage
      ERROR_MSG="$2"
      shift 2
      ;;
    --retry)
      [[ $# -ge 2 ]] || usage
      RETRY_CMD="$2"
      shift 2
      ;;
    --log-file)
      [[ $# -ge 2 ]] || usage
      LOG_FILE="$2"
      shift 2
      ;;
    --no-sanitize)
      SANITIZE=0
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

if [[ -z "$STAGE" || -z "$COMMAND" || -z "$ERROR_MSG" ]]; then
  usage
fi

mkdir -p "$(dirname "$LOG_FILE")"

python3 - "$LOG_FILE" "$STAGE" "$COMMAND" "$ERROR_MSG" "$RETRY_CMD" "$SANITIZE" <<'PY'
import datetime as dt
import json
import platform
import re
import socket
import sys
from pathlib import Path

log_file, stage, command, error_msg, retry_cmd, sanitize = sys.argv[1:7]
sanitize_enabled = sanitize == "1"

def scrub(text: str) -> str:
    text = text.replace(str(Path.home()), "~")
    text = re.sub(r"(?i)(bearer\s+)[^\s'\";]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)\b(gh[opusr]_[A-Za-z0-9_]{20,})\b", "<redacted_token>", text)
    text = re.sub(r"(?i)\b(sk-[A-Za-z0-9]{20,})\b", "<redacted_key>", text)
    text = re.sub(r"(?i)\b(hf_[A-Za-z0-9]{20,})\b", "<redacted_key>", text)
    text = re.sub(
        r"(?i)\b(api[-_]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)",
        lambda m: f"{m.group(1)}=<redacted>",
        text,
    )
    return text

if sanitize_enabled:
    command = scrub(command)
    error_msg = scrub(error_msg)
    retry_cmd = scrub(retry_cmd) if retry_cmd else retry_cmd

payload = {
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "stage": stage,
    "command": command,
    "error": error_msg,
    "retry": retry_cmd or None,
    "host": socket.gethostname(),
    "platform": platform.platform(),
}

with Path(log_file).open("a", encoding="utf-8") as f:
    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
PY

echo "[vox-failure] logged to $LOG_FILE" >&2
