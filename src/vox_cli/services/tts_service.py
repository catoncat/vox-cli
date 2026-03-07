from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable, Generator
from pathlib import Path
import inspect
import os
import tempfile

import numpy as np
import soundfile as sf

from ..audio import combine_samples, stable_hash
from ..config import VoxConfig, get_cache_dir, resolve_tts_model_id
from ..db import list_profile_samples, resolve_profile
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock, acquire_runtime_lock_pool, acquire_runtime_locks
from ..services.model_service import ensure_model_downloaded, resolve_model


def _build_runtime_options(config: VoxConfig, runtime_options: RuntimeExecutionOptions | None) -> RuntimeExecutionOptions:
    if runtime_options is not None:
        return runtime_options
    return RuntimeExecutionOptions(
        wait_for_lock=config.runtime.wait_for_lock,
        wait_timeout_sec=max(1, config.runtime.lock_wait_timeout_sec),
    )


def _build_supported_kwargs(fn: Callable, raw_kwargs: dict[str, object]) -> dict[str, object]:
    sig = inspect.signature(fn)
    supported: dict[str, object] = {}
    for key, value in raw_kwargs.items():
        if value is None:
            continue
        if key in sig.parameters:
            supported[key] = value
    return supported


def _soundfile_format(output_path: Path) -> str:
    suffix = output_path.suffix.lower()
    if suffix == '.wav':
        return 'WAV'
    if suffix == '.flac':
        return 'FLAC'
    if suffix == '.ogg':
        return 'OGG'
    return 'WAV'


def _temp_output_path(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}-',
        suffix=output_path.suffix or '.wav',
        dir=str(output_path.parent),
    )
    os.close(fd)
    return Path(tmp_name)


def _run_generation_to_temp_file(
    method: Callable,
    output_path: Path,
    **raw_kwargs: object,
) -> tuple[Path, int, float]:
    kwargs = _build_supported_kwargs(method, raw_kwargs)

    sample_rate = 24000
    total_samples = 0
    temp_path = _temp_output_path(output_path)
    writer: sf.SoundFile | None = None

    try:
        results: Generator = method(**kwargs)
        for result in results:
            audio = getattr(result, 'audio', None)
            if audio is None:
                continue
            chunk = np.asarray(audio, dtype=np.float32)
            if chunk.ndim > 1:
                chunk = np.squeeze(chunk)
            if chunk.size == 0:
                continue

            current_rate = int(getattr(result, 'sample_rate', sample_rate))
            if writer is None:
                sample_rate = current_rate
                writer = sf.SoundFile(
                    str(temp_path),
                    mode='w',
                    samplerate=sample_rate,
                    channels=1,
                    format=_soundfile_format(output_path),
                )
            elif current_rate != sample_rate:
                raise RuntimeError(
                    f'TTS produced inconsistent sample rates: {current_rate} vs {sample_rate}'
                )

            writer.write(chunk)
            total_samples += int(chunk.shape[0])

        if writer is None or total_samples <= 0:
            raise RuntimeError('TTS produced no audio chunks')
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        if writer is not None:
            writer.close()

    duration_sec = float(total_samples / sample_rate)
    return temp_path, sample_rate, duration_sec


def _replace_output(temp_path: Path, output_path: Path) -> None:
    try:
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _tts_infer_slot_resources(config: VoxConfig) -> list[str]:
    capacity = max(1, config.runtime.tts_small_base_max_parallel)
    return [f'tts_infer_slot:{slot}' for slot in range(capacity)]


@contextmanager
def _acquire_tts_infer_lock(
    config: VoxConfig,
    model_id: str,
    options: RuntimeExecutionOptions,
    metadata: dict[str, object],
):
    resources = _tts_infer_slot_resources(config)
    if model_id == 'qwen-tts-0.6b-base-8bit':
        with acquire_runtime_lock_pool(
            config,
            resources,
            options=options,
            metadata=metadata,
            display_resource='tts_infer',
        ) as handle:
            yield handle
        return

    with acquire_runtime_locks(
        config,
        resources,
        options=options,
        metadata=metadata,
    ) as handles:
        yield handles


def _build_prompt_audio_and_text(
    config: VoxConfig,
    profile_id_or_name: str,
    conn,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> tuple[Path, str, str]:
    profile = resolve_profile(conn, profile_id_or_name)
    if profile is None:
        raise ValueError(f'Profile not found: {profile_id_or_name}')

    samples = list_profile_samples(conn, str(profile['id']))
    if not samples:
        raise ValueError(f'Profile {profile_id_or_name} has no samples')

    sample_paths = [Path(row['audio_path']) for row in samples]
    sample_texts = [str(row['reference_text']) for row in samples]

    if len(sample_paths) == 1:
        return sample_paths[0], sample_texts[0], str(profile['id'])

    prompt_key = stable_hash([str(profile['id']), *[str(p) for p in sample_paths], *sample_texts])
    prompt_dir = get_cache_dir(config) / 'voice_prompt'
    prompt_audio_path = prompt_dir / f'{profile["id"]}-{prompt_key}.wav'

    if not prompt_audio_path.exists():
        options = _build_runtime_options(config, runtime_options)
        with acquire_runtime_lock(
            config,
            f'prompt_build:{profile["id"]}:{prompt_key}',
            options=options,
            metadata={'profile': str(profile['id']), 'out': str(prompt_audio_path)},
        ):
            if not prompt_audio_path.exists():
                combine_samples(sample_paths, prompt_audio_path)

    combined_text = ' '.join(sample_texts)
    return prompt_audio_path, combined_text, str(profile['id'])


def clone_to_file(
    config: VoxConfig,
    conn,
    profile_id_or_name: str,
    text: str,
    output_path: Path,
    model_id: str | None,
    seed: int | None,
    instruct: str | None,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> dict:
    resolved_model_id = resolve_tts_model_id(config, 'clone', model_id)
    spec = resolve_model(config, resolved_model_id, kind='tts')
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=runtime_options,
    )

    prompt_audio, prompt_text, profile_id = _build_prompt_audio_and_text(
        config,
        profile_id_or_name,
        conn,
        runtime_options=runtime_options,
    )

    options = _build_runtime_options(config, runtime_options)
    output_abs = output_path.expanduser().resolve()
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
        f'output_write:{output_abs}',
        options=options,
        metadata={'model_id': spec.model_id, 'profile': profile_id, 'out': str(output_abs)},
    ):
        with _acquire_tts_infer_lock(
            config,
            spec.model_id,
            options,
            {'model_id': spec.model_id, 'profile': profile_id, 'out': str(output_abs)},
        ):
            from mlx_audio.tts.utils import load_model

            model = load_model(model_path)
            temp_path, sample_rate, duration_sec = _run_generation_to_temp_file(
                model.generate,
                output_path=output_abs,
                text=text,
                ref_audio=str(prompt_audio),
                ref_text=prompt_text,
                instruct=instruct,
                seed=seed,
            )
        _replace_output(temp_path, output_abs)

    return {
        'output_path': str(output_abs),
        'sample_rate': sample_rate,
        'duration_sec': duration_sec,
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'profile_id': profile_id,
        'endpoint': ensure_result['endpoint'],
        'prompt_audio': str(prompt_audio),
    }


def custom_to_file(
    config: VoxConfig,
    text: str,
    output_path: Path,
    model_id: str | None,
    speaker: str,
    language: str,
    instruct: str | None,
    seed: int | None,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> dict:
    resolved_model_id = resolve_tts_model_id(config, 'custom', model_id)
    spec = resolve_model(config, resolved_model_id, kind='tts')
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=runtime_options,
    )
    options = _build_runtime_options(config, runtime_options)
    output_abs = output_path.expanduser().resolve()
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
        f'output_write:{output_abs}',
        options=options,
        metadata={'model_id': spec.model_id, 'speaker': speaker, 'out': str(output_abs)},
    ):
        with _acquire_tts_infer_lock(
            config,
            spec.model_id,
            options,
            {'model_id': spec.model_id, 'speaker': speaker, 'out': str(output_abs)},
        ):
            from mlx_audio.tts.utils import load_model

            model = load_model(model_path)
            method = getattr(model, 'generate_custom_voice', None)
            if method is None:
                raise RuntimeError(
                    f'Model {spec.model_id} does not support custom voice generation. '
                    'Use a `*-customvoice-*` TTS model.'
                )

            temp_path, sample_rate, duration_sec = _run_generation_to_temp_file(
                method,
                output_path=output_abs,
                text=text,
                speaker=speaker,
                language=language,
                instruct=instruct,
                seed=seed,
            )
        _replace_output(temp_path, output_abs)

    return {
        'output_path': str(output_abs),
        'sample_rate': sample_rate,
        'duration_sec': duration_sec,
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'mode': 'custom_voice',
        'speaker': speaker,
        'language': language,
        'endpoint': ensure_result['endpoint'],
    }


def design_to_file(
    config: VoxConfig,
    text: str,
    output_path: Path,
    model_id: str | None,
    instruct: str,
    language: str,
    seed: int | None,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> dict:
    resolved_model_id = resolve_tts_model_id(config, 'design', model_id)
    spec = resolve_model(config, resolved_model_id, kind='tts')
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=runtime_options,
    )
    options = _build_runtime_options(config, runtime_options)
    output_abs = output_path.expanduser().resolve()
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
        f'output_write:{output_abs}',
        options=options,
        metadata={'model_id': spec.model_id, 'out': str(output_abs)},
    ):
        with _acquire_tts_infer_lock(
            config,
            spec.model_id,
            options,
            {'model_id': spec.model_id, 'out': str(output_abs)},
        ):
            from mlx_audio.tts.utils import load_model

            model = load_model(model_path)
            method = getattr(model, 'generate_voice_design', None)
            if method is None:
                raise RuntimeError(
                    f'Model {spec.model_id} does not support voice design generation. '
                    'Use a `*-voicedesign-*` TTS model.'
                )

            temp_path, sample_rate, duration_sec = _run_generation_to_temp_file(
                method,
                output_path=output_abs,
                text=text,
                instruct=instruct,
                language=language,
                seed=seed,
            )
        _replace_output(temp_path, output_abs)

    return {
        'output_path': str(output_abs),
        'sample_rate': sample_rate,
        'duration_sec': duration_sec,
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'mode': 'voice_design',
        'language': language,
        'instruct': instruct,
        'endpoint': ensure_result['endpoint'],
    }
