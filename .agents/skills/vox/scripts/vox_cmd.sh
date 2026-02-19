#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if command -v vox >/dev/null 2>&1; then
  exec vox "$@"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[vox] 'uv' not found. Run: bash scripts/bootstrap.sh" >&2
  exit 127
fi

PACKAGE_SPEC="$(vox_resolve_package_spec)"
exec uv tool run --prerelease allow --from "$PACKAGE_SPEC" vox "$@"
