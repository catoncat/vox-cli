from __future__ import annotations

from pathlib import Path

from .models import ModelSpec
from .types import CacheStatus

WEIGHT_SUFFIXES = ('.safetensors', '.bin', '.npz')


def get_repo_cache_dir(hf_cache_dir: Path, repo_id: str) -> Path:
    return hf_cache_dir / ('models--' + repo_id.replace('/', '--'))


def inspect_cache(model: ModelSpec, hf_cache_dir: Path) -> CacheStatus:
    repo_dir = get_repo_cache_dir(hf_cache_dir, model.repo_id)
    refs_main = repo_dir / 'refs' / 'main'

    if not repo_dir.exists():
        return CacheStatus(
            downloaded=False,
            verified=False,
            cache_dir=repo_dir,
            revision=None,
            has_incomplete=False,
            has_weights=False,
        )

    has_incomplete = any(repo_dir.rglob('*.incomplete'))

    revision = None
    if refs_main.exists():
        try:
            revision = refs_main.read_text().strip()
        except Exception:
            revision = None

    snapshot_dir = repo_dir / 'snapshots' / revision if revision else repo_dir / 'snapshots'
    has_weights = False
    if snapshot_dir.exists():
        has_weights = any(
            p.is_file() and p.name.endswith(WEIGHT_SUFFIXES)
            for p in snapshot_dir.rglob('*')
        )

    verified = bool(refs_main.exists() and revision and snapshot_dir.exists() and has_weights and not has_incomplete)

    return CacheStatus(
        downloaded=repo_dir.exists(),
        verified=verified,
        cache_dir=repo_dir,
        revision=revision,
        has_incomplete=has_incomplete,
        has_weights=has_weights,
    )
