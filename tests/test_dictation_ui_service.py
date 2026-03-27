from __future__ import annotations

from vox_cli.config import RuntimeConfig, VoxConfig, get_dictation_prompt_preset
from vox_cli.services.dictation_ui_service import (
    _parse_context_delay_ms,
    build_dictation_ui_state,
    render_dictation_ui_sections,
    save_dictation_ui_state,
    strip_managed_dictation_ui_sections,
)


def test_strip_managed_dictation_ui_sections_preserves_other_sections() -> None:
    raw = """
[runtime]
home_dir = "~/.vox"

[dictation]
llm_active_profile = "local-mlx"

[dictation.transforms]
fullwidth_to_halfwidth = true

[dictation.llm_profiles."local-mlx"]
enabled = true

[dictation.llm_profiles."aliyun"]
enabled = true
api_key = "sk-demo"

[dictation.context]
enabled = true
max_chars = 1200

[dictation.hotwords]
enabled = true

[[dictation.hotwords.entries]]
value = "潮汕"
aliases = ["潮上"]

[dictation.hints]
enabled = true
items = ["前后鼻音不分"]

[tts]
default_model = "demo"
""".strip()

    result = strip_managed_dictation_ui_sections(raw)

    assert '[runtime]' in result
    assert '[tts]' in result
    assert '[dictation]' not in result
    assert '[dictation.transforms]' not in result
    assert '[dictation.llm_profiles."local-mlx"]' not in result
    assert '[dictation.context]' not in result
    assert '[dictation.hotwords]' not in result
    assert '[dictation.hints]' not in result


def test_render_dictation_ui_sections_outputs_expected_toml() -> None:
    rendered = render_dictation_ui_sections(
        {
            'transforms': {
                'fullwidth_to_halfwidth': True,
                'space_around_punct': False,
                'space_between_cjk': True,
                'strip_trailing_punctuation': True,
            },
            'llm_active_profile': 'aliyun',
            'llm_profiles': {
                'local-mlx': {
                    'enabled': True,
                    'provider': 'local-mlx',
                    'base_url': 'http://127.0.0.1:18080/v1',
                    'model': 'mlx-community/Qwen2.5-1.5B-Instruct-4bit',
                    'api_key_env': '',
                    'timeout_sec': 4.0,
                    'stream': True,
                    'temperature': 0.0,
                    'max_tokens': 96,
                    'prompt_preset': 'spoken_clean',
                    'custom_prompt_enabled': False,
                    'system_prompt': '',
                    'user_prompt_template': '',
                    'api_key_present': False,
                },
                'aliyun': {
                    'enabled': True,
                    'provider': 'dashscope',
                    'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                    'model': 'qwen-turbo-latest',
                    'api_key_env': 'OPENAI_API_KEY',
                    'timeout_sec': 4.0,
                    'stream': True,
                    'temperature': 0.0,
                    'max_tokens': 96,
                    'prompt_preset': 'deep_clean',
                    'custom_prompt_enabled': True,
                    'system_prompt': '第一行\n第二行',
                    'user_prompt_template': 'LANG={language}\nTEXT={text}',
                    'api_key_present': True,
                },
            },
            'context': {'enabled': True, 'max_chars': 1400, 'capture_budget_ms': 900},
            'hotwords': {
                'enabled': True,
                'rewrite_aliases': True,
                'case_sensitive': False,
                'entries': [
                    {'value': '潮汕', 'aliases': ['潮上']},
                    {'value': 'Codex CLI', 'aliases': ['ColdX CLI', 'CodeX CLI']},
                ],
            },
            'hints': {
                'enabled': True,
                'items': ['说话人前后鼻音不分。'],
            },
        },
        preserved_llm_api_keys={'aliyun': 'sk-demo'},
    )

    assert '[dictation]' in rendered
    assert 'llm_active_profile = "aliyun"' in rendered
    assert '[dictation.transforms]' in rendered
    assert '[dictation.llm_profiles."local-mlx"]' in rendered
    assert 'provider = "local-mlx"' in rendered
    assert 'prompt_preset = "spoken_clean"' in rendered
    assert '[dictation.llm_profiles."aliyun"]' in rendered
    assert 'provider = "dashscope"' in rendered
    assert 'model = "qwen-turbo-latest"' in rendered
    assert 'api_key = "sk-demo"' in rendered
    assert 'api_key_env = "OPENAI_API_KEY"' in rendered
    assert "system_prompt = '''" in rendered
    assert '第一行' in rendered
    assert 'user_prompt_template = ' in rendered
    assert '[dictation.context]' in rendered
    assert 'max_chars = 1400' in rendered
    assert 'capture_budget_ms = 900' in rendered
    assert '[[dictation.hotwords.entries]]' in rendered
    assert 'value = "潮汕"' in rendered
    assert 'aliases = ["潮上"]' in rendered
    assert '[dictation.hints]' in rendered
    assert '"说话人前后鼻音不分。"' in rendered


def test_save_dictation_ui_state_updates_only_managed_sections(tmp_path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    config_path = tmp_path / 'config.toml'
    config_path.write_text(
        '[runtime]\n'
        'home_dir = "~/.vox"\n\n'
        '[dictation]\n'
        'llm_active_profile = "aliyun"\n\n'
        '[dictation.llm_profiles."aliyun"]\n'
        'enabled = true\n'
        'api_key = "sk-existing"\n\n'
        '[tts]\n'
        'default_model = "base"\n',
        encoding='utf-8',
    )

    state = save_dictation_ui_state(
        config,
        {
            'transforms': {
                'fullwidth_to_halfwidth': True,
                'space_around_punct': False,
                'space_between_cjk': False,
                'strip_trailing_punctuation': True,
            },
            'llm_active_profile': 'local-mlx',
            'llm_profiles': {
                'local-mlx': {
                    'enabled': True,
                    'provider': 'local-mlx',
                    'base_url': 'http://127.0.0.1:18080/v1',
                    'model': 'mlx-community/Qwen2.5-1.5B-Instruct-4bit',
                    'api_key_env': '',
                    'timeout_sec': 4.0,
                    'stream': True,
                    'temperature': 0.0,
                    'max_tokens': 96,
                    'prompt_preset': 'spoken_clean',
                    'custom_prompt_enabled': False,
                    'system_prompt': '',
                    'user_prompt_template': '',
                    'api_key_present': False,
                },
                'aliyun': {
                    'enabled': True,
                    'provider': 'dashscope',
                    'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                    'model': 'qwen-turbo-latest',
                    'api_key_env': 'OPENAI_API_KEY',
                    'timeout_sec': 6.5,
                    'stream': True,
                    'temperature': 0.0,
                    'max_tokens': 128,
                    'prompt_preset': 'deep_clean',
                    'custom_prompt_enabled': True,
                    'system_prompt': '你是修订器',
                    'user_prompt_template': 'TEXT={text}',
                    'api_key_present': True,
                },
            },
            'context': {'enabled': True, 'max_chars': 900, 'capture_budget_ms': 750},
            'hotwords': {
                'enabled': True,
                'rewrite_aliases': True,
                'case_sensitive': False,
                'entries': [
                    {'value': '潮汕', 'aliases': ['潮上']},
                ],
            },
            'hints': {
                'enabled': True,
                'items': ['说话人前后鼻音不分。'],
            },
        },
    )

    text = config_path.read_text(encoding='utf-8')
    assert '[runtime]' in text
    assert '[tts]' in text
    assert '[dictation]' in text
    assert 'llm_active_profile = "local-mlx"' in text
    assert '[dictation.llm_profiles."local-mlx"]' in text
    assert '[dictation.llm_profiles."aliyun"]' in text
    assert 'provider = "dashscope"' in text
    assert 'api_key = "sk-existing"' in text
    assert '[dictation.context]' in text
    assert '[dictation.hotwords]' in text
    assert '[dictation.hints]' in text
    assert state['state']['llm_profiles']['aliyun']['api_key_present'] is True
    assert state['state']['llm_active_profile'] == 'local-mlx'
    assert state['state']['context']['max_chars'] == 900
    assert state['state']['hotwords']['entries'][0]['value'] == '潮汕'


def test_build_dictation_ui_state_does_not_expose_inline_api_key(tmp_path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    config_path = tmp_path / 'config.toml'
    config_path.write_text(
        '[dictation]\n'
        'llm_active_profile = "aliyun"\n\n'
        '[dictation.llm_profiles."aliyun"]\n'
        'enabled = true\n'
        'api_key = "sk-secret"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        'system_prompt = "keep"\n',
        encoding='utf-8',
    )

    payload = build_dictation_ui_state(config)

    assert payload['state']['llm_active_profile'] == 'aliyun'
    assert payload['state']['llm_profiles']['aliyun']['enabled'] is True
    assert payload['state']['llm_profiles']['aliyun']['api_key_present'] is True
    assert payload['state']['llm_profiles']['aliyun']['custom_prompt_enabled'] is True
    assert payload['prompt_presets']
    assert 'api_key' not in payload['state']['llm_profiles']['aliyun']


def test_build_dictation_ui_state_recognizes_builtin_prompt_as_preset(tmp_path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    config_path = tmp_path / 'config.toml'
    preset = get_dictation_prompt_preset('default')
    config_path.write_text(
        '[dictation]\n'
        'llm_active_profile = "local-mlx"\n\n'
        '[dictation.llm_profiles."local-mlx"]\n'
        f"system_prompt = '''\n{preset.system_prompt}\n'''\n"
        f"user_prompt_template = '''\n{preset.user_prompt_template}\n'''\n",
        encoding='utf-8',
    )

    payload = build_dictation_ui_state(config)

    assert payload['state']['llm_profiles']['local-mlx']['prompt_preset'] == 'default'
    assert payload['state']['llm_profiles']['local-mlx']['custom_prompt_enabled'] is False
    assert payload['state']['llm_profiles']['local-mlx']['system_prompt'] == preset.system_prompt
    assert payload['state']['llm_profiles']['local-mlx']['user_prompt_template'] == preset.user_prompt_template


def test_render_dictation_ui_sections_omits_prompt_override_when_using_preset() -> None:
    rendered = render_dictation_ui_sections(
        {
            'llm_active_profile': 'local-mlx',
            'llm_profiles': {
                'local-mlx': {
                    'enabled': True,
                    'provider': 'local-mlx',
                    'prompt_preset': 'literal',
                    'custom_prompt_enabled': False,
                    'system_prompt': 'should not persist',
                    'user_prompt_template': 'TEXT={text}',
                    'api_key_present': False,
                },
            },
        }
    )

    assert 'prompt_preset = "literal"' in rendered
    assert 'system_prompt =' not in rendered
    assert 'user_prompt_template =' not in rendered


def test_parse_context_delay_ms_clamps_to_safe_range() -> None:
    assert _parse_context_delay_ms('0') == 0
    assert _parse_context_delay_ms('-50') == 0
    assert _parse_context_delay_ms('2000') == 2000
    assert _parse_context_delay_ms('999999') == 10_000
    assert _parse_context_delay_ms('oops') == 0
