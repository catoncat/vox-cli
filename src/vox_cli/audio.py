from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib

import numpy as np
import soundfile as sf


@dataclass
class AudioMetrics:
    sample_rate: int
    duration_sec: float
    rms: float


def analyze_audio(path: Path) -> AudioMetrics:
    samples, sample_rate = sf.read(str(path), dtype='float32', always_2d=False)
    if samples.ndim > 1:
        samples = np.mean(samples, axis=1)
    duration_sec = len(samples) / float(sample_rate)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    return AudioMetrics(sample_rate=sample_rate, duration_sec=duration_sec, rms=rms)


def copy_as_wav(src: Path, dst: Path) -> AudioMetrics:
    samples, sample_rate = sf.read(str(src), dtype='float32', always_2d=False)
    if samples.ndim > 1:
        samples = np.mean(samples, axis=1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), samples, sample_rate)
    duration_sec = len(samples) / float(sample_rate)
    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    return AudioMetrics(sample_rate=sample_rate, duration_sec=duration_sec, rms=rms)


def combine_samples(sample_paths: list[Path], output_path: Path) -> None:
    if not sample_paths:
        raise ValueError('No sample paths provided')

    all_audio: list[np.ndarray] = []
    base_sr: int | None = None
    silence: np.ndarray | None = None

    for p in sample_paths:
        audio, sr = sf.read(str(p), dtype='float32', always_2d=False)
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        if base_sr is None:
            base_sr = sr
            silence = np.zeros(int(0.15 * sr), dtype=np.float32)
        elif sr != base_sr:
            raise ValueError(f'Sample rate mismatch: {p} uses {sr}, expected {base_sr}')

        all_audio.append(audio.astype(np.float32))
        if silence is not None:
            all_audio.append(silence)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), np.concatenate(all_audio), base_sr or 24000)


def stable_hash(parts: list[str]) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()[:16]
