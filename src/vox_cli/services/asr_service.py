from __future__ import annotations

from pathlib import Path
import json

from ..config import VoxConfig
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock
from ..services.model_service import ensure_model_downloaded, resolve_model


def _map_language(language: str | None) -> str | None:
    if not language:
        return None
    lowered = language.strip().lower()
    mapping = {
        'zh': 'Chinese',
        'en': 'English',
        'auto': None,
        'chinese': 'Chinese',
        'english': 'English',
    }
    return mapping.get(lowered, language)


def _extract_text(result: object) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get('text', '')).strip()
    if hasattr(result, 'text'):
        return str(getattr(result, 'text')).strip()
    return str(result).strip()


def _build_runtime_options(config: VoxConfig, runtime_options: RuntimeExecutionOptions | None) -> RuntimeExecutionOptions:
    if runtime_options is not None:
        return runtime_options
    return RuntimeExecutionOptions(
        wait_for_lock=config.runtime.wait_for_lock,
        wait_timeout_sec=max(1, config.runtime.lock_wait_timeout_sec),
    )


def transcribe_file(
    config: VoxConfig,
    audio_path: Path,
    model_id: str | None,
    language: str | None,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> dict:
    spec = resolve_model(config, model_id, kind='asr')
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=runtime_options,
    )
    options = _build_runtime_options(config, runtime_options)
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
        'asr_infer',
        options=options,
        metadata={'model_id': spec.model_id, 'audio': str(audio_path)},
    ):
        from mlx_audio.stt import load

        model = load(model_path)
        decode_options: dict[str, object] = {}
        mapped_language = _map_language(language)
        if mapped_language:
            decode_options['language'] = mapped_language

        result = model.generate(str(audio_path), **decode_options)
        text = _extract_text(result)

        segments = None
        if hasattr(result, 'segments'):
            raw_segments = getattr(result, 'segments')
            try:
                segments = [
                    {
                        'start': float(seg['start']),
                        'end': float(seg['end']),
                        'text': str(seg['text']).strip(),
                    }
                    for seg in raw_segments
                ]
            except Exception:
                segments = None

        return {
            'text': text,
            'segments': segments,
            'model_id': spec.model_id,
            'repo_id': spec.repo_id,
            'endpoint': ensure_result['endpoint'],
        }


def stream_transcribe_file(
    config: VoxConfig,
    audio_path: Path,
    model_id: str | None,
    language: str | None,
    runtime_options: RuntimeExecutionOptions | None = None,
):
    spec = resolve_model(config, model_id, kind='asr')
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=runtime_options,
    )
    options = _build_runtime_options(config, runtime_options)
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
        'asr_infer',
        options=options,
        metadata={'model_id': spec.model_id, 'audio': str(audio_path)},
    ):
        from mlx_audio.stt import load

        model = load(model_path)
        mapped_language = _map_language(language)

        kwargs: dict[str, object] = {}
        if mapped_language:
            kwargs['language'] = mapped_language

        for chunk in model.stream_transcribe(str(audio_path), **kwargs):
            yield str(chunk)


def stream_to_ndjson(chunks: list[str], session_id: str) -> list[str]:
    rows = []
    for idx, chunk in enumerate(chunks):
        rows.append(
            json.dumps(
                {
                    'session_id': session_id,
                    'index': idx,
                    'chunk': chunk,
                    'is_final': False,
                },
                ensure_ascii=False,
            )
        )
    rows.append(json.dumps({'session_id': session_id, 'chunk': '', 'is_final': True}, ensure_ascii=False))
    return rows
