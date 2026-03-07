from __future__ import annotations

from pathlib import Path

from ..cache import inspect_cache
from ..config import VoxConfig, get_hf_cache_dir, resolve_asr_model_id
from ..download import download_with_fallback
from ..models import MODEL_REGISTRY, ModelSpec
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock


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


def _build_runtime_options(config: VoxConfig, runtime_options: RuntimeExecutionOptions | None) -> RuntimeExecutionOptions:
    if runtime_options is not None:
        return runtime_options
    return RuntimeExecutionOptions(
        wait_for_lock=config.runtime.wait_for_lock,
        wait_timeout_sec=max(1, config.runtime.lock_wait_timeout_sec),
    )


def _snapshot_path(cache_dir: Path, revision: str | None) -> Path:
    if revision:
        return (cache_dir / 'snapshots' / revision).resolve()
    return (cache_dir / 'snapshots').resolve()


def _status_payload(spec: ModelSpec, *, endpoint: str | None, cache_dir: Path, revision: str | None) -> dict:
    return {
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'downloaded': True,
        'verified': True,
        'endpoint': endpoint,
        'snapshot_path': str(_snapshot_path(cache_dir, revision)),
        'cache_dir': str(cache_dir),
    }


def list_model_statuses(config: VoxConfig) -> list[dict]:
    hf_cache = get_hf_cache_dir(config)
    rows: list[dict] = []

    for spec in MODEL_REGISTRY.values():
        cache = inspect_cache(spec, hf_cache, deep=False)
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


def ensure_model_downloaded(
    config: VoxConfig,
    spec: ModelSpec,
    allow_download: bool = True,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> dict:
    hf_cache = get_hf_cache_dir(config)
    status = inspect_cache(spec, hf_cache, deep=False)
    if status.verified:
        return _status_payload(spec, endpoint=None, cache_dir=status.cache_dir, revision=status.revision)

    if not allow_download:
        raise RuntimeError(
            f'Model {spec.model_id} is not verified in cache. Run `vox model pull --model {spec.model_id}` first.'
        )

    options = _build_runtime_options(config, runtime_options)
    with acquire_runtime_lock(
        config,
        f'model_download:{spec.model_id}',
        options=options,
        metadata={'model_id': spec.model_id, 'repo_id': spec.repo_id},
    ):
        status = inspect_cache(spec, hf_cache, deep=False)
        if status.verified:
            return _status_payload(spec, endpoint=None, cache_dir=status.cache_dir, revision=status.revision)

        result = download_with_fallback(spec.repo_id, config.hf.endpoints, hf_cache)
        status2 = inspect_cache(spec, hf_cache, deep=False)
        if not status2.verified:
            raise RuntimeError(
                f'Model downloaded but cache verification failed for {spec.model_id}. '
                f'Cache dir: {status2.cache_dir}'
            )

        return _status_payload(
            spec,
            endpoint=result.endpoint,
            cache_dir=status2.cache_dir,
            revision=status2.revision,
        )
