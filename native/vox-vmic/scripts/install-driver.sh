#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/driver/build/VoxVirtualMic.driver"
DST="/Library/Audio/Plug-Ins/HAL/VoxVirtualMic.driver"

if [[ ! -d "$SRC" ]]; then
  bash "$ROOT/scripts/build-driver.sh"
fi

echo "About to install to $DST"
sudo rm -rf "$DST"
sudo cp -R "$SRC" "$DST"
echo "installed: $DST"
echo "next: sudo launchctl kickstart -k system/com.apple.audio.coreaudiod"
