from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path, dry_run: bool) -> None:
    if dry_run:
        return
    subprocess.run(cmd, cwd=cwd, check=True)


def _resolve_repo(repo: Path) -> Path:
    repo = repo.expanduser().resolve()
    pyproject = repo / 'pyproject.toml'
    if not pyproject.exists():
        raise RuntimeError(f'pyproject.toml not found under {repo}')
    text = pyproject.read_text(encoding='utf-8', errors='ignore')
    if 'name = "vox-cli"' not in text and "name = 'vox-cli'" not in text:
        raise RuntimeError(f'{repo} does not look like the vox-cli repository')
    return repo


def _latest_wheel(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob('vox_cli-*.whl'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wheels:
        raise RuntimeError(f'No wheel found under {dist_dir}; run `uv build` first')
    return wheels[0]


def build_update_commands(repo: Path) -> tuple[list[str], list[str], Path]:
    repo = _resolve_repo(repo)
    build_cmd = ['uv', 'build']
    wheel = _latest_wheel(repo / 'dist') if (repo / 'dist').exists() else None
    if wheel is None:
        wheel = repo / 'dist' / 'vox_cli-*.whl'
    install_cmd = ['uv', 'tool', 'install', '--force', '--prerelease=allow', str(wheel)]
    return build_cmd, install_cmd, repo


def update_global_install(repo: Path, *, dry_run: bool = False) -> dict:
    repo = _resolve_repo(repo)
    build_cmd = ['uv', 'build']
    _run(build_cmd, cwd=repo, dry_run=dry_run)

    wheel = _latest_wheel(repo / 'dist')
    install_cmd = ['uv', 'tool', 'install', '--force', '--prerelease=allow', str(wheel)]
    _run(install_cmd, cwd=repo, dry_run=dry_run)

    return {
        'repo': str(repo),
        'wheel': str(wheel),
        'build_cmd': ' '.join(build_cmd),
        'install_cmd': ' '.join(install_cmd),
        'dry_run': dry_run,
    }
