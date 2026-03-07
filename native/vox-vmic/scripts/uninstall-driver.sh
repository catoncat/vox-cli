#!/usr/bin/env bash
set -euo pipefail
DST="/Library/Audio/Plug-Ins/HAL/VoxVirtualMic.driver"

echo "Removing $DST"
sudo rm -rf "$DST"
sudo killall coreaudiod || true

echo "removed: $DST"
echo "reloaded: coreaudiod"
