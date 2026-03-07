#!/usr/bin/env bash
set -euo pipefail
DST="/Library/Audio/Plug-Ins/HAL/VoxVirtualMic.driver"
echo "About to remove $DST"
sudo rm -rf "$DST"
echo "removed: $DST"
echo "next: sudo launchctl kickstart -k system/com.apple.audio.coreaudiod"
