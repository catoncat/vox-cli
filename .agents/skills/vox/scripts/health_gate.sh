#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SELF_CHECK="$SCRIPT_DIR/self_check.sh"
STATE_DIR="${VOX_HOME:-$HOME/.vox}/agent/state"
STATE_FILE="$STATE_DIR/health.json"
TTL_HOURS="${VOX_HEALTH_TTL_HOURS:-24}"
FORCE=0

declare -a REQUIRED_MODELS=()
declare -a REQUIRED_FILES=()

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/health_gate.sh [options]

Options:
  --require-model <id|asr-auto|tts-default>   Required model (repeatable)
  --require-file <path>                       Required output/input file (repeatable)
  --ttl-hours <int>                           Cache TTL hours (default: 24)
  --force                                     Ignore cached state and run full self-check
EOF
  exit 2
}

log() {
  printf '[vox-health] %s\n' "$*" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-model)
      [[ $# -ge 2 ]] || usage
      REQUIRED_MODELS+=("$2")
      shift 2
      ;;
    --require-file)
      [[ $# -ge 2 ]] || usage
      REQUIRED_FILES+=("$2")
      shift 2
      ;;
    --ttl-hours)
      [[ $# -ge 2 ]] || usage
      TTL_HOURS="$2"
      shift 2
      ;;
    --force)
      FORCE=1
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

if ! [[ "$TTL_HOURS" =~ ^[0-9]+$ ]] || [[ "$TTL_HOURS" -le 0 ]]; then
  echo "[vox-health] --ttl-hours must be a positive integer" >&2
  exit 2
fi

mkdir -p "$STATE_DIR"

TMP_MODELS="$(mktemp)"
TMP_FILES="$(mktemp)"
TMP_CONFIG="$(mktemp)"
TMP_FP="$(mktemp)"
trap 'rm -f "$TMP_MODELS" "$TMP_FILES" "$TMP_CONFIG" "$TMP_FP"' EXIT

if [[ "${#REQUIRED_MODELS[@]}" -gt 0 ]]; then
  printf '%s\n' "${REQUIRED_MODELS[@]}" | sort -u >"$TMP_MODELS"
fi

if [[ "${#REQUIRED_FILES[@]}" -gt 0 ]]; then
  printf '%s\n' "${REQUIRED_FILES[@]}" >"$TMP_FILES"
fi

if command -v vox >/dev/null 2>&1; then
  if ! vox config show --json >"$TMP_CONFIG" 2>/dev/null; then
    echo '{}' >"$TMP_CONFIG"
  fi
else
  echo '{}' >"$TMP_CONFIG"
fi

python3 - "$TMP_CONFIG" "$TMP_MODELS" <<'PY' >"$TMP_FP"
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
models_path = Path(sys.argv[2])

config_raw = config_path.read_text(encoding="utf-8")
models = [line.strip() for line in models_path.read_text(encoding="utf-8").splitlines() if line.strip()]

def cmd_output(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True).strip()
    except Exception:
        return ""

fingerprint = {
    "platform": f"{platform.system()}/{platform.machine()}",
    "uv_version": cmd_output(["uv", "--version"]),
    "vox_version": cmd_output(["vox", "version"]),
    "config_hash": hashlib.sha256(config_raw.encode("utf-8")).hexdigest(),
    "required_models": models,
}
print(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True))
PY

if [[ "$FORCE" -eq 0 ]] && python3 - "$STATE_FILE" "$TMP_FP" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

state_path = Path(sys.argv[1])
fp_path = Path(sys.argv[2])

if not state_path.exists():
    raise SystemExit(1)

state = json.loads(state_path.read_text(encoding="utf-8"))
fingerprint = json.loads(fp_path.read_text(encoding="utf-8"))

if state.get("last_ok") is not True:
    raise SystemExit(1)

if state.get("fingerprint") != fingerprint:
    raise SystemExit(1)

expires_at = state.get("expires_at")
if not isinstance(expires_at, str) or not expires_at:
    raise SystemExit(1)

expires_at = expires_at.replace("Z", "+00:00")
if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
    raise SystemExit(1)

raise SystemExit(0)
PY
then
  for output in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$output" ]]; then
      log "cache hit but required file missing: $output"
      exit 3
    fi
  done
  log "cache hit: reuse health state at $STATE_FILE"
  exit 0
fi

CMD=(bash "$SELF_CHECK")
for model in "${REQUIRED_MODELS[@]}"; do
  CMD+=(--require-model "$model")
done
for output in "${REQUIRED_FILES[@]}"; do
  CMD+=(--require-file "$output")
done

set +e
"${CMD[@]}"
STATUS=$?
set -e

python3 - "$STATE_FILE" "$TMP_FP" "$TMP_MODELS" "$TMP_FILES" "$TTL_HOURS" "$STATUS" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

state_path = Path(sys.argv[1])
fp_path = Path(sys.argv[2])
models_path = Path(sys.argv[3])
files_path = Path(sys.argv[4])
ttl_hours = int(sys.argv[5])
status = int(sys.argv[6])

fingerprint = json.loads(fp_path.read_text(encoding="utf-8"))
models = [line.strip() for line in models_path.read_text(encoding="utf-8").splitlines() if line.strip()]
files = [line.strip() for line in files_path.read_text(encoding="utf-8").splitlines() if line.strip()]

now = datetime.now(timezone.utc)
payload = {
    "last_ok": status == 0,
    "checked_at": now.isoformat(),
    "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
    "ttl_hours": ttl_hours,
    "fingerprint": fingerprint,
    "required_models": models,
    "required_files": files,
    "reason": None if status == 0 else f"self_check_exit_{status}",
}
state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if [[ "$STATUS" -ne 0 ]]; then
  log "self-check failed (status=$STATUS)"
  exit "$STATUS"
fi

log "self-check passed and state updated: $STATE_FILE"
