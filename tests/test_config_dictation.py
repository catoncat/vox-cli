from __future__ import annotations

from vox_cli.config import (
    ASRConfig,
    VoxConfig,
    get_dictation_prompt_preset,
    load_config,
    resolve_dictation_llm_prompts,
    resolve_dictation_prompt_selection,
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
    monkeypatch.setenv('VOX_DICTATION_LLM_PROMPT_PRESET', 'literal')
    monkeypatch.setenv('VOX_DICTATION_LLM_STREAM', 'false')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_ENABLED', 'yes')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_MAX_CHARS', '2048')
    monkeypatch.setenv('VOX_DICTATION_CONTEXT_CAPTURE_BUDGET_MS', '900')
    monkeypatch.setenv('VOX_DICTATION_HOTWORDS_ENABLED', 'true')
    monkeypatch.setenv('VOX_DICTATION_HINTS_ENABLED', 'true')
    monkeypatch.setenv('VOX_DICTATION_SPACE_BETWEEN_CJK', '1')

    config = load_config()

    assert config.dictation.llm_active_profile == 'local-mlx'
    assert config.dictation.llm.enabled is True
    assert config.dictation.llm.provider == 'openrouter'
    assert config.dictation.llm.base_url == 'https://openrouter.ai/api/v1'
    assert config.dictation.llm.model == 'openai/gpt-4o-mini'
    assert config.dictation.llm.api_key_env == 'OPENROUTER_API_KEY'
    assert config.dictation.llm.prompt_preset == 'literal'
    assert config.dictation.llm.stream is False
    assert config.dictation.llm_profiles['local-mlx'].provider == 'openrouter'
    assert config.dictation.context.enabled is True
    assert config.dictation.context.max_chars == 2048
    assert config.dictation.context.capture_budget_ms == 900
    assert config.dictation.hotwords.enabled is True
    assert config.dictation.hints.enabled is True
    assert config.dictation.transforms.space_between_cjk is True


def test_load_config_defaults_to_local_and_aliyun_profiles() -> None:
    config = VoxConfig()

    assert config.dictation.llm_active_profile == 'local-mlx'
    assert 'local-mlx' in config.dictation.llm_profiles
    assert 'aliyun' in config.dictation.llm_profiles


def test_resolve_dictation_llm_prompts_uses_selected_preset_by_default() -> None:
    config = VoxConfig()

    system_prompt, user_prompt_template = resolve_dictation_llm_prompts(config.dictation.llm)

    assert config.dictation.llm.prompt_preset == 'default'
    assert '你不是聊天助手' in system_prompt
    assert '{text}' in user_prompt_template


def test_resolve_dictation_llm_prompts_allows_custom_override() -> None:
    config = VoxConfig()
    config.dictation.llm.prompt_preset = 'literal'
    config.dictation.llm.system_prompt = 'custom system'
    config.dictation.llm.user_prompt_template = 'TEXT={text}'

    system_prompt, user_prompt_template = resolve_dictation_llm_prompts(config.dictation.llm)

    assert get_dictation_prompt_preset('literal').label == '最小改动'
    assert system_prompt == 'custom system'
    assert user_prompt_template == 'TEXT={text}'


def test_deep_clean_preset_summarizes_asr_postprocess_rules() -> None:
    preset = get_dictation_prompt_preset('deep_clean')

    assert preset.label == '深度整理'
    assert '只保留最终确认的信息' in preset.system_prompt
    assert 'Markdown 列表' in preset.system_prompt
    assert 'dictation 深度整理任务' in preset.user_prompt_template


def test_spoken_clean_preset_targets_filler_words_and_self_corrections() -> None:
    preset = get_dictation_prompt_preset('spoken_clean')

    assert preset.label == '口语清理'
    assert '删除语气词' in preset.system_prompt
    assert '我们来测试一下' in preset.system_prompt
    assert '明天不对 后天' in preset.system_prompt
    assert '示例6' in preset.user_prompt_template
    assert '这个功能用起来怎么样? 感觉怎么样' in preset.user_prompt_template
    assert '示例5' in preset.user_prompt_template
    assert 'Codex' in preset.user_prompt_template


def test_resolve_dictation_prompt_selection_maps_builtin_prompt_pair_back_to_preset() -> None:
    config = VoxConfig()
    default_preset = get_dictation_prompt_preset('default')
    config.dictation.llm.prompt_preset = 'arena'
    config.dictation.llm.system_prompt = default_preset.system_prompt
    config.dictation.llm.user_prompt_template = default_preset.user_prompt_template

    prompt_preset, custom_prompt_enabled, system_prompt, user_prompt_template = (
        resolve_dictation_prompt_selection(config.dictation.llm)
    )

    assert prompt_preset == 'default'
    assert custom_prompt_enabled is False
    assert system_prompt == default_preset.system_prompt
    assert user_prompt_template == default_preset.user_prompt_template
