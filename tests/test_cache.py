from pathlib import Path

from vox_cli.cache import inspect_cache
from vox_cli.models import MODEL_REGISTRY


def _prepare_snapshot(root: Path, repo_id: str, revision: str = 'abc123') -> Path:
    repo_dir = root / ('models--' + repo_id.replace('/', '--'))
    (repo_dir / 'refs').mkdir(parents=True, exist_ok=True)
    (repo_dir / 'refs' / 'main').write_text(revision)
    snapshot_dir = repo_dir / 'snapshots' / revision
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / 'model.safetensors').write_text('fake')
    return repo_dir


def test_inspect_cache_fast_skips_incomplete_scan(tmp_path: Path) -> None:
    spec = MODEL_REGISTRY['qwen-tts-0.6b-base-8bit']
    repo_dir = _prepare_snapshot(tmp_path, spec.repo_id)
    (repo_dir / 'blobs').mkdir(exist_ok=True)
    (repo_dir / 'blobs' / 'stale.incomplete').write_text('partial')

    status = inspect_cache(spec, tmp_path, deep=False)

    assert status.verified is True
    assert status.has_incomplete is None
    assert status.has_weights is True


def test_inspect_cache_deep_detects_incomplete_files(tmp_path: Path) -> None:
    spec = MODEL_REGISTRY['qwen-tts-0.6b-base-8bit']
    repo_dir = _prepare_snapshot(tmp_path, spec.repo_id)
    (repo_dir / 'blobs').mkdir(exist_ok=True)
    (repo_dir / 'blobs' / 'stale.incomplete').write_text('partial')

    status = inspect_cache(spec, tmp_path, deep=True)

    assert status.verified is False
    assert status.has_incomplete is True
