#!/usr/bin/env bash
set -euo pipefail

INPUT="${1:-}"
if [[ -z "$INPUT" || ! -f "$INPUT" ]]; then
  echo "usage: $0 /path/to/audio-file" >&2
  exit 1
fi

ffmpeg -hide_banner -re -i "$INPUT" -f f32le -ac 1 -ar 48000 "udp://127.0.0.1:47211?pkt_size=768"
