#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VOX_CMD="$SCRIPT_DIR/vox_cmd.sh"

log() {
  printf '[vox-model] %s\n' "$*" >&2
}

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/ensure_model.sh <model>

Supported model aliases:
  asr-auto     Resolve to doctor.checks.resolved_asr_model.value
  tts-default  Resolve to config.tts.default_model
  <model-id>   Explicit model id, e.g. qwen-asr-1.7b-8bit
EOF
  exit 2
}

resolve_model_alias() {
  local raw="$1"
  local tmp
  tmp="$(mktemp)"

  case "$raw" in
    asr-auto)
      "$VOX_CMD" doctor --json >"$tmp"
      python3 - "$tmp" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
resolved = (
    payload.get("checks", {})
    .get("resolved_asr_model", {})
    .get("value", "")
)
if not resolved:
    raise SystemExit(1)
print(resolved)
PY
      ;;
    tts-default)
      "$VOX_CMD" config show --json >"$tmp"
      python3 - "$tmp" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
resolved = payload.get("tts", {}).get("default_model", "")
if not resolved:
    raise SystemExit(1)
print(resolved)
PY
      ;;
    *)
      printf '%s\n' "$raw"
      ;;
  esac
}

if [[ $# -ne 1 ]]; then
  usage
fi

REQUESTED_MODEL="$1"
MODEL_ID="$(resolve_model_alias "$REQUESTED_MODEL")"

if [[ -z "$MODEL_ID" ]]; then
  log "Cannot resolve model from input: $REQUESTED_MODEL"
  exit 3
fi

VERIFY_JSON="$(mktemp)"
if "$VOX_CMD" model verify --model "$MODEL_ID" --json >"$VERIFY_JSON" 2>/dev/null; then
  log "Model already verified: $MODEL_ID"
  printf '%s\n' "$MODEL_ID"
  exit 0
fi

log "Model not verified, pulling: $MODEL_ID"
"$VOX_CMD" model pull --model "$MODEL_ID" --json >/dev/null
"$VOX_CMD" model verify --model "$MODEL_ID" --json >/dev/null

log "Model ready: $MODEL_ID"
printf '%s\n' "$MODEL_ID"
