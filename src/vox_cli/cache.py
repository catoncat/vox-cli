from __future__ import annotations

from pathlib import Path

from .models import ModelSpec
from .types import CacheStatus

WEIGHT_SUFFIXES = ('.safetensors', '.bin', '.npz')


def get_repo_cache_dir(hf_cache_dir: Path, repo_id: str) -> Path:
    return hf_cache_dir / ('models--' + repo_id.replace('/', '--'))


def _snapshot_has_weights(snapshot_dir: Path, deep: bool) -> bool:
    if not snapshot_dir.exists():
        return False

    return any(
        p.is_file() and p.name.endswith(WEIGHT_SUFFIXES)
        for p in snapshot_dir.rglob('*')
    )


def inspect_cache(model: ModelSpec, hf_cache_dir: Path, deep: bool = True) -> CacheStatus:
    repo_dir = get_repo_cache_dir(hf_cache_dir, model.repo_id)
    refs_main = repo_dir / 'refs' / 'main'

    if not repo_dir.exists():
        return CacheStatus(
            downloaded=False,
            verified=False,
            cache_dir=repo_dir,
            revision=None,
            has_incomplete=False if deep else None,
            has_weights=False,
        )

    has_incomplete = any(repo_dir.rglob('*.incomplete')) if deep else None

    revision = None
    if refs_main.exists():
        try:
            revision = refs_main.read_text().strip()
        except Exception:
            revision = None

    snapshot_dir = repo_dir / 'snapshots' / revision if revision else repo_dir / 'snapshots'
    has_weights = _snapshot_has_weights(snapshot_dir, deep=deep)
    verified = bool(
        refs_main.exists()
        and revision
        and snapshot_dir.exists()
        and has_weights
        and has_incomplete is not True
    )

    return CacheStatus(
        downloaded=repo_dir.exists(),
        verified=verified,
        cache_dir=repo_dir,
        revision=revision,
        has_incomplete=has_incomplete,
        has_weights=has_weights,
    )


def inspect_cache_quick(model: ModelSpec, hf_cache_dir: Path) -> CacheStatus:
    return inspect_cache(model, hf_cache_dir, deep=False)
