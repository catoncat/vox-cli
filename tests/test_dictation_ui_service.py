from __future__ import annotations

from vox_cli.config import RuntimeConfig, VoxConfig
from vox_cli.services.dictation_ui_service import (
    _parse_context_delay_ms,
    render_dictation_ui_sections,
    save_dictation_ui_state,
    strip_managed_dictation_ui_sections,
)


def test_strip_managed_dictation_ui_sections_preserves_other_sections() -> None:
    raw = """
[runtime]
home_dir = "~/.vox"

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
    assert '[dictation.context]' not in result
    assert '[dictation.hotwords]' not in result
    assert '[dictation.hints]' not in result


def test_render_dictation_ui_sections_outputs_expected_toml() -> None:
    rendered = render_dictation_ui_sections(
        {
            'context': {'enabled': True, 'max_chars': 1400},
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
        }
    )

    assert '[dictation.context]' in rendered
    assert 'max_chars = 1400' in rendered
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
        '[tts]\n'
        'default_model = "base"\n',
        encoding='utf-8',
    )

    state = save_dictation_ui_state(
        config,
        {
            'context': {'enabled': True, 'max_chars': 900},
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
    assert '[dictation.context]' in text
    assert '[dictation.hotwords]' in text
    assert '[dictation.hints]' in text
    assert state['state']['context']['max_chars'] == 900
    assert state['state']['hotwords']['entries'][0]['value'] == '潮汕'


def test_parse_context_delay_ms_clamps_to_safe_range() -> None:
    assert _parse_context_delay_ms('0') == 0
    assert _parse_context_delay_ms('-50') == 0
    assert _parse_context_delay_ms('2000') == 2000
    assert _parse_context_delay_ms('999999') == 10_000
    assert _parse_context_delay_ms('oops') == 0
