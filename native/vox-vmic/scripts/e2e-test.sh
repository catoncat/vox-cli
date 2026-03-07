#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd -- "$ROOT/../.." && pwd)"
ARTIFACT_DIR="$ROOT/artifacts"
mkdir -p "$ARTIFACT_DIR"

AUDIO_INPUT="${1:-}"
if [[ -z "$AUDIO_INPUT" ]]; then
  AUDIO_INPUT="/Users/envvar/.vox/outputs/pipeline-75c727e4.wav"
fi

if [[ ! -f "$AUDIO_INPUT" ]]; then
  echo "[e2e] input audio not found: $AUDIO_INPUT" >&2
  exit 1
fi

ensure_driver() {
  if system_profiler SPAudioDataType 2>/dev/null | rg -q 'Vox Virtual Mic'; then
    return 0
  fi
  echo '[e2e] Vox Virtual Mic not found, installing driver...'
  bash "$ROOT/scripts/install-driver.sh"
  sleep 2
  if ! system_profiler SPAudioDataType 2>/dev/null | rg -q 'Vox Virtual Mic'; then
    echo '[e2e] driver installed but device still not visible' >&2
    exit 1
  fi
}

find_device_index() {
  (ffmpeg -hide_banner -f avfoundation -list_devices true -i '' 2>&1 || true) | python3 -c '
import re, sys
in_audio = False
for line in sys.stdin:
    if "AVFoundation audio devices:" in line:
        in_audio = True
        continue
    if not in_audio:
        continue
    m = re.search(r"\[AVFoundation indev @ .*\] \[(\d+)\] (.+)$", line.strip())
    if m and m.group(2) == "Vox Virtual Mic":
        print(m.group(1))
        raise SystemExit(0)
'
}

ensure_driver
DEVICE_INDEX="$(find_device_index)"
if [[ -z "$DEVICE_INDEX" ]]; then
  echo '[e2e] failed to resolve Vox Virtual Mic device index' >&2
  exit 1
fi

DURATION_RAW="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$AUDIO_INPUT")"
CAPTURE_SECONDS="$(python3 - <<PY
import math
value = float(${DURATION_RAW:-2.0})
print(max(2, math.ceil(value + 1.0)))
PY
)"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$ARTIFACT_DIR/e2e-capture-$STAMP.wav"

cd "$REPO_ROOT"
echo "[e2e] input=$AUDIO_INPUT"
echo "[e2e] device_index=$DEVICE_INDEX"
echo "[e2e] capture_seconds=$CAPTURE_SECONDS"
bash "$ROOT/scripts/stream-file.sh" "$AUDIO_INPUT" >/tmp/vox-vmic-stream.log 2>&1 &
STREAM_PID=$!
trap "kill $STREAM_PID 2>/dev/null || true" EXIT
ffmpeg -hide_banner -y -f avfoundation -i ":$DEVICE_INDEX" -t "$CAPTURE_SECONDS" -ac 1 -ar 48000 "$OUT_FILE"
wait $STREAM_PID || true

python3 - <<PY
import json, struct, subprocess, wave
from pathlib import Path
out_path = Path(r'''$OUT_FILE''')
probe = subprocess.check_output([
  'ffprobe','-v','error','-show_entries','stream=codec_name,sample_rate,channels','-show_entries','format=duration,size','-of','json',str(out_path)
], text=True)
meta = json.loads(probe)
with wave.open(str(out_path), 'rb') as w:
    frames = w.readframes(w.getnframes())
    vals = struct.unpack('<' + 'h' * (len(frames) // 2), frames)
    rms = (sum(v * v for v in vals) / max(1, len(vals))) ** 0.5
    peak = max(abs(v) for v in vals) if vals else 0
fmt = meta.get('format', {})
payload = {
  'input': r'''$AUDIO_INPUT''',
  'output': str(out_path),
  'duration_sec': float(fmt.get('duration', 0)),
  'size_bytes': int(fmt.get('size', 0)),
  'rms': rms,
  'peak': peak,
  'pass_non_silent': bool(rms > 20 and peak > 100),
}
print('--- summary ---')
print(json.dumps(payload, ensure_ascii=False, indent=2))
if not payload['pass_non_silent']:
    raise SystemExit(1)
PY
