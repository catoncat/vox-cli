#!/usr/bin/env bash
set -euo pipefail
sudo launchctl kickstart -k system/com.apple.audio.coreaudiod
