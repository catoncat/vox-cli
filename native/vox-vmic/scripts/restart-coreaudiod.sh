#!/usr/bin/env bash
set -euo pipefail
sudo killall coreaudiod || true
echo "reloaded: coreaudiod"
