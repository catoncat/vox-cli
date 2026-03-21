from __future__ import annotations

from vox_cli.config import (
    ASRConfig,
    VoxConfig,
    load_config,
    resolve_asr_model_id,
    resolve_dictation_model_id,
)


def test_dictation_auto_prefers_faster_model() -> None:
    config = VoxConfig(asr=ASRConfig(default_model='auto'))

    assert resolve_dictation_model_id(config) == 'qwen-asr-0.6b-4bit'


def test_dictation_respects_explicit_asr_default() -> None:
    config = VoxConfig(asr=ASRConfig(default_model='qwen-asr-1.7b-4bit'))

    assert resolve_asr_model_id(config) == 'qwen-asr-1.7b-4bit'
    assert resolve_dictation_model_id(config) == 'qwen-asr-1.7b-4bit'


def test_load_config_accepts_dictation_llm_env_overrides(monkeypatch, tmp_path) -> None:
    home_dir = tmp_path / 'vox-home'
    home_dir.mkdir()

    monkeypatch.setenv('VOX_HOME', str(home_dir))
    monkeypatch.setenv('VOX_DICTATION_LLM_ENABLED', 'true')
    monkeypatch.setenv('VOX_DICTATION_LLM_PROVIDER', 'openrouter')
    monkeypatch.setenv('VOX_DICTATION_LLM_BASE_URL', 'https://openrouter.ai/api/v1')
    monkeypatch.setenv('VOX_DICTATION_LLM_MODEL', 'openai/gpt-4o-mini')
    monkeypatch.setenv('VOX_DICTATION_LLM_API_KEY_ENV', 'OPENROUTER_API_KEY')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_ENABLED', 'yes')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_MAX_CHARS', '2048')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_CAPTURE_BUDGET_MS', '900')
    monkeypatch.setenv('VOX_DICTATION_HOTWORDS_ENABLED', 'true')
    monkeypatch.setenv('VOX_DICTATION_HINTS_ENABLED', 'true')
    monkeypatch.setenv('VOX_DICTATION_SPACE_BETWEEN_CJK', '1')

    config = load_config()

    assert config.dictation.llm.enabled is True
    assert config.dictation.llm.provider == 'openrouter'
    assert config.dictation.llm.base_url == 'https://openrouter.ai/api/v1'
    assert config.dictation.llm.model == 'openai/gpt-4o-mini'
    assert config.dictation.llm.api_key_env == 'OPENROUTER_API_KEY'
    assert config.dictation.context.enabled is True
    assert config.dictation.context.max_chars == 2048
    assert config.dictation.context.capture_budget_ms == 900
    assert config.dictation.hotwords.enabled is True
    assert config.dictation.hints.enabled is True
    assert config.dictation.transforms.space_between_cjk is True
