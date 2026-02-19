from __future__ import annotations

from pathlib import Path
import inspect
import os

import numpy as np
import soundfile as sf

from ..audio import combine_samples, stable_hash
from ..config import VoxConfig, get_cache_dir
from ..db import list_profile_samples, resolve_profile
from ..services.model_service import ensure_model_downloaded, resolve_model


def _build_prompt_audio_and_text(
    config: VoxConfig,
    profile_id_or_name: str,
    conn,
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
) -> dict:
    spec = resolve_model(config, model_id, kind='tts')
    ensure_result = ensure_model_downloaded(config, spec, allow_download=True)

    prompt_audio, prompt_text, profile_id = _build_prompt_audio_and_text(config, profile_id_or_name, conn)

    previous_endpoint = os.getenv('HF_ENDPOINT')
    active_endpoint = str(ensure_result['endpoint'] or config.hf.endpoints[0])
    os.environ['HF_ENDPOINT'] = active_endpoint

    try:
        from mlx_audio.tts.utils import load_model

        model = load_model(spec.repo_id)

        sig = inspect.signature(model.generate)
        kwargs: dict[str, object] = {}

        if 'ref_audio' in sig.parameters:
            kwargs['ref_audio'] = str(prompt_audio)
        if 'ref_text' in sig.parameters:
            kwargs['ref_text'] = prompt_text
        if instruct and 'instruct' in sig.parameters:
            kwargs['instruct'] = instruct
        if seed is not None and 'seed' in sig.parameters:
            kwargs['seed'] = seed

        chunks: list[np.ndarray] = []
        sample_rate = 24000
        for result in model.generate(text=text, **kwargs):
            audio = getattr(result, 'audio', None)
            if audio is None:
                continue
            chunks.append(np.asarray(audio, dtype=np.float32))
            sample_rate = int(getattr(result, 'sample_rate', sample_rate))

        if not chunks:
            raise RuntimeError('TTS produced no audio chunks')

        output_path.parent.mkdir(parents=True, exist_ok=True)
        final_audio = np.concatenate(chunks)
        sf.write(str(output_path), final_audio, sample_rate)

        return {
            'output_path': str(output_path),
            'sample_rate': sample_rate,
            'duration_sec': float(len(final_audio) / sample_rate),
            'model_id': spec.model_id,
            'repo_id': spec.repo_id,
            'profile_id': profile_id,
            'endpoint': active_endpoint,
            'prompt_audio': str(prompt_audio),
        }
    finally:
        if previous_endpoint is None:
            os.environ.pop('HF_ENDPOINT', None)
        else:
            os.environ['HF_ENDPOINT'] = previous_endpoint
