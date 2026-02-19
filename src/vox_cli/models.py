from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelKind = Literal['tts', 'asr']


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    repo_id: str
    kind: ModelKind
    quantization: str | None = None


MODEL_REGISTRY: dict[str, ModelSpec] = {
    'qwen-tts-1.7b': ModelSpec(
        model_id='qwen-tts-1.7b',
        repo_id='mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16',
        kind='tts',
    ),
    'qwen-tts-1.7b-base-8bit': ModelSpec(
        model_id='qwen-tts-1.7b-base-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-tts-1.7b-customvoice-8bit': ModelSpec(
        model_id='qwen-tts-1.7b-customvoice-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-tts-1.7b-voicedesign-8bit': ModelSpec(
        model_id='qwen-tts-1.7b-voicedesign-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-tts-0.6b-base-8bit': ModelSpec(
        model_id='qwen-tts-0.6b-base-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-tts-0.6b-customvoice-8bit': ModelSpec(
        model_id='qwen-tts-0.6b-customvoice-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-tts-0.6b-voicedesign-8bit': ModelSpec(
        model_id='qwen-tts-0.6b-voicedesign-8bit',
        repo_id='mlx-community/Qwen3-TTS-12Hz-0.6B-VoiceDesign-8bit',
        kind='tts',
        quantization='8bit',
    ),
    'qwen-asr-1.7b-8bit': ModelSpec(
        model_id='qwen-asr-1.7b-8bit',
        repo_id='mlx-community/Qwen3-ASR-1.7B-8bit',
        kind='asr',
        quantization='8bit',
    ),
    'qwen-asr-1.7b-4bit': ModelSpec(
        model_id='qwen-asr-1.7b-4bit',
        repo_id='mlx-community/Qwen3-ASR-1.7B-4bit',
        kind='asr',
        quantization='4bit',
    ),
}

DEFAULT_TTS_MODEL_ID = 'qwen-tts-1.7b'
ASR_MODEL_CANDIDATES = ('qwen-asr-1.7b-8bit', 'qwen-asr-1.7b-4bit')
