from __future__ import annotations

from pathlib import Path

from ..cache import inspect_cache
from ..config import VoxConfig, get_hf_cache_dir, resolve_asr_model_id
from ..download import download_with_fallback
from ..models import MODEL_REGISTRY, ModelSpec


def resolve_model(config: VoxConfig, model_id: str | None, kind: str | None = None) -> ModelSpec:
    if model_id is None:
        if kind == 'asr':
            model_id = resolve_asr_model_id(config)
        elif kind == 'tts':
            model_id = config.tts.default_model

    if model_id is None:
        raise ValueError('Model ID is required')

    spec = MODEL_REGISTRY.get(model_id)
    if spec is None:
        allowed = ', '.join(sorted(MODEL_REGISTRY.keys()))
        raise ValueError(f'Unknown model {model_id}. Allowed: {allowed}')

    if kind and spec.kind != kind:
        raise ValueError(f'Model {model_id} is {spec.kind}, expected {kind}')

    return spec


def list_model_statuses(config: VoxConfig) -> list[dict]:
    hf_cache = get_hf_cache_dir(config)
    rows: list[dict] = []

    for spec in MODEL_REGISTRY.values():
        cache = inspect_cache(spec, hf_cache)
        rows.append(
            {
                'model_id': spec.model_id,
                'repo_id': spec.repo_id,
                'kind': spec.kind,
                'quantization': spec.quantization,
                'downloaded': cache.downloaded,
                'verified': cache.verified,
                'cache_dir': str(cache.cache_dir),
                'revision': cache.revision,
                'has_incomplete': cache.has_incomplete,
                'has_weights': cache.has_weights,
            }
        )

    return rows


def ensure_model_downloaded(config: VoxConfig, spec: ModelSpec, allow_download: bool = True) -> dict:
    hf_cache = get_hf_cache_dir(config)
    status = inspect_cache(spec, hf_cache)
    if status.verified:
        return {
            'model_id': spec.model_id,
            'repo_id': spec.repo_id,
            'downloaded': True,
            'verified': True,
            'endpoint': None,
            'snapshot_path': str((status.cache_dir / 'snapshots' / (status.revision or '')).resolve()),
            'cache_dir': str(status.cache_dir),
        }

    if not allow_download:
        raise RuntimeError(
            f'Model {spec.model_id} is not verified in cache. Run `vox model pull --model {spec.model_id}` first.'
        )

    result = download_with_fallback(spec.repo_id, config.hf.endpoints, hf_cache)
    status2 = inspect_cache(spec, hf_cache)
    if not status2.verified:
        raise RuntimeError(
            f'Model downloaded but cache verification failed for {spec.model_id}. '
            f'Cache dir: {status2.cache_dir}'
        )

    return {
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'downloaded': True,
        'verified': True,
        'endpoint': result.endpoint,
        'snapshot_path': str(result.snapshot_path),
        'cache_dir': str(status2.cache_dir),
    }
