from __future__ import annotations

from pathlib import Path

from vox_cli.config import RuntimeConfig, VoxConfig
from vox_cli.models import MODEL_REGISTRY
from vox_cli.services import model_service
from vox_cli.types import CacheStatus


def test_ensure_model_downloaded_uses_existing_snapshot(monkeypatch, tmp_path: Path) -> None:
    spec = MODEL_REGISTRY['qwen-tts-0.6b-base-8bit']
    cache_dir = tmp_path / 'hf' / 'models--mlx-community--Qwen3-TTS-12Hz-0.6B-Base-8bit'
    snapshot_dir = cache_dir / 'snapshots' / 'rev-123'
    snapshot_dir.mkdir(parents=True)

    monkeypatch.setattr(model_service, 'get_hf_cache_dir', lambda config: tmp_path / 'hf')
    monkeypatch.setattr(
        model_service,
        'inspect_cache',
        lambda spec, hf_cache_dir, deep=False: CacheStatus(
            downloaded=True,
            verified=True,
            cache_dir=cache_dir,
            revision='rev-123',
            has_incomplete=False,
            has_weights=True,
        ),
    )

    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    result = model_service.ensure_model_downloaded(config, spec, allow_download=True)

    assert result['snapshot_path'] == str(snapshot_dir.resolve())
    assert result['verified'] is True
