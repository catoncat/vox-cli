#!/usr/bin/env bash
set -euo pipefail

echo "--- system_profiler ---"
system_profiler SPAudioDataType 2>/dev/null | sed -n '1,240p'

echo

echo "--- ffmpeg devices ---"
(ffmpeg -hide_banner -f avfoundation -list_devices true -i '' 2>&1 || true) | sed -n '1,240p'
