#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VOX_CMD="$SCRIPT_DIR/vox_cmd.sh"

declare -a REQUIRED_MODELS=()
declare -a REQUIRED_FILES=()

log() {
  printf '[vox-self-check] %s\n' "$*" >&2
}

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/self_check.sh [--require-model <id|asr-auto|tts-default>]... [--require-file <path>]...
EOF
  exit 2
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
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

DOCTOR_JSON="$(mktemp)"
"$VOX_CMD" doctor --json >"$DOCTOR_JSON"

python3 - "$DOCTOR_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is not True:
    print("[vox-self-check] doctor ok=false", file=sys.stderr)
    raise SystemExit(1)
print("[vox-self-check] doctor ok=true", file=sys.stderr)
PY

for model in "${REQUIRED_MODELS[@]}"; do
  bash "$SCRIPT_DIR/ensure_model.sh" "$model" >/dev/null
done

for output in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$output" ]]; then
    log "Required file missing: $output"
    exit 3
  fi
done

if ! command -v ffmpeg >/dev/null 2>&1; then
  log "ffmpeg not found in PATH"
  exit 4
fi

log "Self-check passed."
