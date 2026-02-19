#!/usr/bin/env bash
set -euo pipefail

resolve_package_spec() {
  if [[ -n "${VOX_CLI_GIT_URL:-}" ]]; then
    printf 'git+%s\n' "$VOX_CLI_GIT_URL"
    return
  fi
  printf '%s\n' "${VOX_CLI_PACKAGE_SPEC:-vox-cli}"
}

if command -v vox >/dev/null 2>&1; then
  exec vox "$@"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[vox] 'uv' not found. Run: bash scripts/bootstrap.sh" >&2
  exit 127
fi

PACKAGE_SPEC="$(resolve_package_spec)"
exec uv tool run --from "$PACKAGE_SPEC" vox "$@"
