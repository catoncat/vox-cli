from __future__ import annotations

from pathlib import Path
import os
import platform
import subprocess
from typing import Literal

from pydantic import BaseModel, Field

from .models import ASR_MODEL_CANDIDATES, DEFAULT_TTS_MODEL_ID


class RuntimeConfig(BaseModel):
    home_dir: str = '~/.vox'


class HFConfig(BaseModel):
    endpoints: list[str] = Field(default_factory=lambda: ['https://hf-mirror.com', 'https://huggingface.co'])
    cache_dir: str | None = None


class ASRConfig(BaseModel):
    default_model: Literal['auto', 'qwen-asr-1.7b-8bit', 'qwen-asr-1.7b-4bit'] = 'auto'
    memory_threshold_gb: int = 32


class TTSConfig(BaseModel):
    default_model: str = DEFAULT_TTS_MODEL_ID


class VoxConfig(BaseModel):
    runtime: RuntimeConfig = RuntimeConfig()
    hf: HFConfig = HFConfig()
    asr: ASRConfig = ASRConfig()
    tts: TTSConfig = TTSConfig()


def get_home_dir(config: VoxConfig) -> Path:
    return Path(config.runtime.home_dir).expanduser().resolve()


def get_config_path(config: VoxConfig | None = None) -> Path:
    if config is None:
        home = Path('~/.vox').expanduser()
    else:
        home = get_home_dir(config)
    return home / 'config.toml'


def get_db_path(config: VoxConfig) -> Path:
    return get_home_dir(config) / 'vox.db'


def get_profiles_dir(config: VoxConfig) -> Path:
    return get_home_dir(config) / 'profiles'


def get_outputs_dir(config: VoxConfig) -> Path:
    return get_home_dir(config) / 'outputs'


def get_cache_dir(config: VoxConfig) -> Path:
    return get_home_dir(config) / 'cache'


def get_hf_cache_dir(config: VoxConfig) -> Path:
    env_cache = os.getenv('HF_HUB_CACHE')
    if env_cache:
        return Path(env_cache).expanduser().resolve()
    if config.hf.cache_dir:
        return Path(config.hf.cache_dir).expanduser().resolve()
    return Path('~/.cache/huggingface/hub').expanduser().resolve()


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        import tomli as tomllib
    with path.open('rb') as f:
        return tomllib.load(f)


def load_config() -> VoxConfig:
    home_override = os.getenv('VOX_HOME')
    base_home = Path(home_override).expanduser() if home_override else Path('~/.vox').expanduser()

    defaults = VoxConfig(runtime=RuntimeConfig(home_dir=str(base_home)))
    cfg_path = get_config_path(defaults)

    data = _load_toml(cfg_path)
    merged = VoxConfig(**data) if data else defaults

    # Runtime home override has highest precedence.
    if home_override:
        merged.runtime.home_dir = str(base_home)

    # Hugging Face endpoints override.
    if (raw := os.getenv('VOX_HF_ENDPOINTS')):
        merged.hf.endpoints = [x.strip() for x in raw.split(',') if x.strip()]

    # If user set HF_ENDPOINT explicitly, prioritize it.
    if (single_endpoint := os.getenv('HF_ENDPOINT')):
        dedup = [single_endpoint]
        for ep in merged.hf.endpoints:
            if ep not in dedup:
                dedup.append(ep)
        merged.hf.endpoints = dedup

    if (asr_default := os.getenv('VOX_ASR_DEFAULT_MODEL')):
        merged.asr.default_model = asr_default  # type: ignore[assignment]

    if (threshold := os.getenv('VOX_ASR_MEMORY_THRESHOLD_GB')):
        merged.asr.memory_threshold_gb = int(threshold)

    return merged


def ensure_runtime_dirs(config: VoxConfig) -> None:
    get_home_dir(config).mkdir(parents=True, exist_ok=True)
    get_profiles_dir(config).mkdir(parents=True, exist_ok=True)
    get_outputs_dir(config).mkdir(parents=True, exist_ok=True)
    get_cache_dir(config).mkdir(parents=True, exist_ok=True)


def get_total_memory_gb() -> float | None:
    if platform.system() != 'Darwin':
        return None
    try:
        out = subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True).strip()
        return int(out) / (1024 ** 3)
    except Exception:
        return None


def resolve_asr_model_id(config: VoxConfig, model_override: str | None = None) -> str:
    if model_override and model_override != 'auto':
        return model_override

    chosen = config.asr.default_model
    if chosen != 'auto':
        return chosen

    memory_gb = get_total_memory_gb()
    if memory_gb is None:
        return ASR_MODEL_CANDIDATES[0]

    if memory_gb >= config.asr.memory_threshold_gb:
        return 'qwen-asr-1.7b-8bit'
    return 'qwen-asr-1.7b-4bit'
