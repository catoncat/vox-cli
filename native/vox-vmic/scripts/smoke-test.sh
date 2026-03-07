#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
swift build >/dev/null
./.build/debug/vox-vmicctl prime-sine --seconds 1 --frequency 523.25 >/tmp/vox-vmic-smoke.json
./.build/debug/vox-vmicctl status >/tmp/vox-vmic-status.json
python3 - <<'PY'
import json, pathlib
status = json.loads(pathlib.Path('/tmp/vox-vmic-status.json').read_text())
assert status['snapshot'] is not None, status
assert status['snapshot']['queuedFrames'] > 0, status
PY
make -C driver >/dev/null
test -f "$ROOT/driver/build/VoxVirtualMic.driver/Contents/MacOS/VoxVirtualMic"
echo "smoke: ok"
