#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VOX_CMD="$SCRIPT_DIR/vox_cmd.sh"
MODE="install"
DEFAULT_VOX_GIT_URL="${VOX_CLI_DEFAULT_GIT_URL:-https://github.com/catoncat/vox-cli.git}"

log() {
  printf '[vox-bootstrap] %s\n' "$*" >&2
}

fail() {
  log "$1"
  exit "${2:-1}"
}

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/bootstrap.sh [--check]

Options:
  --check   Run non-mutating checks only (no install).
EOF
  exit 2
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --check)
        MODE="check"
        shift
        ;;
      -h|--help)
        usage
        ;;
      *)
        usage
        ;;
    esac
  done
}

resolve_package_spec() {
  if [[ -n "${VOX_CLI_GIT_URL:-}" ]]; then
    printf 'git+%s\n' "$VOX_CLI_GIT_URL"
    return
  fi
  if [[ -n "${VOX_CLI_PACKAGE_SPEC:-}" ]]; then
    printf '%s\n' "$VOX_CLI_PACKAGE_SPEC"
    return
  fi
  printf 'git+%s\n' "$DEFAULT_VOX_GIT_URL"
}

ensure_brew_on_path() {
  if command -v brew >/dev/null 2>&1; then
    return
  fi

  if [[ -x /opt/homebrew/bin/brew ]]; then
    export PATH="/opt/homebrew/bin:$PATH"
    return
  fi

  if [[ -x /usr/local/bin/brew ]]; then
    export PATH="/usr/local/bin:$PATH"
    return
  fi
}

ensure_macos_arm64() {
  local os
  local arch
  os="$(uname -s)"
  arch="$(uname -m)"
  if [[ "$os" != "Darwin" || "$arch" != "arm64" ]]; then
    fail "Unsupported platform: ${os}/${arch}. Vox supports Apple Silicon macOS only." 2
  fi
}

install_homebrew_if_missing() {
  ensure_brew_on_path
  if command -v brew >/dev/null 2>&1; then
    return
  fi

  log "Homebrew not found. Installing Homebrew..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "Failed to install Homebrew. Install it manually, then re-run bootstrap." 3

  ensure_brew_on_path
  command -v brew >/dev/null 2>&1 || fail "Homebrew install finished but 'brew' is still unavailable." 3
}

ensure_brew_package() {
  local pkg="$1"
  if brew list --versions "$pkg" >/dev/null 2>&1; then
    log "brew package already installed: $pkg"
    return
  fi

  log "Installing brew package: $pkg"
  brew install "$pkg"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv already installed"
    return
  fi

  install_homebrew_if_missing
  ensure_brew_package uv

  if [[ -x /opt/homebrew/bin/uv ]]; then
    export PATH="/opt/homebrew/bin:$PATH"
  elif [[ -x /usr/local/bin/uv ]]; then
    export PATH="/usr/local/bin:$PATH"
  fi

  command -v uv >/dev/null 2>&1 || fail "uv installation failed." 4
}

ensure_system_dependencies() {
  install_homebrew_if_missing
  ensure_brew_package ffmpeg
  ensure_brew_package portaudio
}

install_vox_cli() {
  local package_spec
  package_spec="$(resolve_package_spec)"
  log "Installing Vox CLI with uv: $package_spec"
  uv tool install --force --prerelease allow --with sounddevice "$package_spec" \
    || fail "Failed to install Vox CLI package: $package_spec" 5
  uv tool update-shell >/dev/null 2>&1 || true
}

check_doctor_ok() {
  local doctor_json
  doctor_json="$(mktemp)"

  "$VOX_CMD" doctor --json >"$doctor_json" \
    || fail "vox doctor failed. Run: $VOX_CMD doctor --json" 6

  python3 - "$doctor_json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is True:
    print("[vox-bootstrap] doctor check passed")
    raise SystemExit(0)

print("[vox-bootstrap] doctor check failed")
checks = payload.get("checks", {})
for key, value in checks.items():
    if value.get("ok", True) is False:
        print(f"[vox-bootstrap] failed check: {key} -> {value}")
raise SystemExit(1)
PY
}

check_only() {
  local failed=0
  ensure_macos_arm64
  ensure_brew_on_path

  if command -v brew >/dev/null 2>&1; then
    log "check: brew ok"
  else
    log "check: brew missing"
    failed=1
  fi

  if command -v uv >/dev/null 2>&1; then
    log "check: uv ok"
  else
    log "check: uv missing"
    failed=1
  fi

  if command -v ffmpeg >/dev/null 2>&1; then
    log "check: ffmpeg ok"
  else
    log "check: ffmpeg missing"
    failed=1
  fi

  if command -v brew >/dev/null 2>&1 && brew list --versions portaudio >/dev/null 2>&1; then
    log "check: portaudio ok"
  else
    log "check: portaudio missing"
    failed=1
  fi

  if command -v vox >/dev/null 2>&1; then
    log "check: vox command found, validating doctor..."
    vox doctor --json >/tmp/vox-bootstrap-check-doctor.json && log "check: doctor ok"
  else
    log "check: vox command missing (run bootstrap without --check)"
    failed=1
  fi

  if [[ "$failed" -ne 0 ]]; then
    fail "Check mode failed. Run: bash scripts/bootstrap.sh" 7
  fi

  log "Check mode passed."
}

main() {
  parse_args "$@"
  if [[ "$MODE" == "check" ]]; then
    check_only
    return
  fi

  ensure_macos_arm64
  ensure_system_dependencies
  ensure_uv
  install_vox_cli
  check_doctor_ok
  log "Bootstrap completed."
}

main "$@"
