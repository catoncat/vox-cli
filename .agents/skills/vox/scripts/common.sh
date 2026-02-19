#!/usr/bin/env bash

vox_resolve_package_spec() {
  local default_vox_git_url
  default_vox_git_url="${VOX_CLI_DEFAULT_GIT_URL:-https://github.com/catoncat/vox-cli.git}"

  if [[ -n "${VOX_CLI_PACKAGE_SPEC:-}" ]]; then
    printf '%s\n' "$VOX_CLI_PACKAGE_SPEC"
    return
  fi

  if [[ -n "${VOX_CLI_GIT_URL:-}" ]]; then
    printf 'git+%s\n' "$VOX_CLI_GIT_URL"
    return
  fi

  printf 'git+%s\n' "$default_vox_git_url"
}
