from __future__ import annotations

import json

from vox_cli.config import DictationConfig, DictationContextConfig, VoxConfig
from vox_cli.services import dictation_context_service
from vox_cli.services.dictation_context_service import DictationContext


def test_capture_dictation_context_returns_none_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(dictation_context_service, '_frontmost_app_name', lambda: 'Ghostty')

    config = VoxConfig(dictation=DictationConfig(context=DictationContextConfig(enabled=False)))

    assert dictation_context_service.capture_dictation_context(config) is None


def test_capture_ghostty_context_truncates_tail(monkeypatch) -> None:
    monkeypatch.setattr(dictation_context_service, '_read_window_title', lambda app: 'codex')

    def fake_read_focused_attribute(app_name: str, attribute: str) -> str:
        values = {
            'AXRole': 'AXTextArea',
            'AXTitle': '',
            'AXSelectedText': '',
            'AXValue': '0123456789abcdef',
        }
        return values[attribute]

    monkeypatch.setattr(dictation_context_service, '_read_focused_attribute', fake_read_focused_attribute)

    context = dictation_context_service._capture_ghostty_context('Ghostty', 6)

    assert context == DictationContext(
        source='ghostty',
        app_name='Ghostty',
        window_title='codex',
        surface='terminal_chat',
        element_role='AXTextArea',
        element_title=None,
        selected_text=None,
        focus_text='abcdef',
        context_text='abcdef',
    )


def test_capture_chromium_context_prefers_page_context_for_chat_surfaces(monkeypatch) -> None:
    outputs = iter(
        [
            'Codex Chat\nhttps://example.com/chat',
            json.dumps(
                {
                    'title': 'Codex Chat',
                    'selection': 'selected text',
                    'isEditable': True,
                    'activeValue': 'input text',
                    'nearbyText': 'nearby page text',
                    'mainText': 'main page text',
                    'bodyText': 'page text',
                },
                ensure_ascii=False,
            ),
        ]
    )
    monkeypatch.setattr(dictation_context_service, '_run_osascript', lambda lines, language=None: next(outputs))

    context = dictation_context_service._capture_chromium_context('Google Chrome', 20)

    assert context == DictationContext(
        source='chromium',
        app_name='Google Chrome',
        window_title='Codex Chat',
        surface='browser_chat',
        page_url='https://example.com/chat',
        selected_text='selected text',
        focus_text='input text',
        context_text='nearby page text',
    )


def test_capture_chromium_context_prefers_page_context_over_input_box(monkeypatch) -> None:
    outputs = iter(
        [
            'Codex Chat\nhttps://example.com/chat',
            json.dumps(
                {
                    'title': 'Codex Chat',
                    'selection': '',
                    'isEditable': True,
                    'activeValue': 'draft in input',
                    'nearbyText': '',
                    'mainText': '',
                    'bodyText': '上一轮对话\n这个页面真正有用的上下文',
                },
                ensure_ascii=False,
            ),
        ]
    )
    monkeypatch.setattr(dictation_context_service, '_run_osascript', lambda lines, language=None: next(outputs))

    context = dictation_context_service._capture_chromium_context('Google Chrome', 40)

    assert context == DictationContext(
        source='chromium',
        app_name='Google Chrome',
        window_title='Codex Chat',
        surface='browser_chat',
        page_url='https://example.com/chat',
        selected_text=None,
        focus_text='draft in input',
        context_text='上一轮对话\n这个页面真正有用的上下文',
    )


def test_capture_dictation_context_uses_frontmost_app(monkeypatch) -> None:
    monkeypatch.setattr(dictation_context_service, '_frontmost_app_name', lambda: 'ghostty')
    expected = DictationContext(source='ghostty', app_name='ghostty', context_text='context')
    monkeypatch.setattr(dictation_context_service, '_capture_ghostty_context', lambda app, max_chars: expected)

    config = VoxConfig(dictation=DictationConfig(context=DictationContextConfig(enabled=True, max_chars=1200)))

    assert dictation_context_service.capture_dictation_context(config) == expected


def test_sanitize_terminal_context_prefers_natural_language_lines() -> None:
    raw = """
Last login: Sat Mar 21 18:57:16 on ttys033
❯ vox dictation start --lang zh --llm-timeout-sec 8 --verbose
This feels like the right moment to proceed since ASR has a clear happy path in the repo.
I could say, "如果你愿意，我下一步可以直接帮你跑一轮。"
────────────────────────────────────────────────────
• Explored
└ Read asr-playbook.md, README.md
作为一个潮汕人，我前后鼻音不分。
"""

    result = dictation_context_service._sanitize_terminal_context(raw, 120)

    assert result == 'I could say, "如果你愿意，我下一步可以直接帮你跑一轮。"\n作为一个潮汕人，我前后鼻音不分。'


def test_sanitize_terminal_context_keeps_recent_mixed_language_context() -> None:
    raw = """
❯ uv run vox dictation start --lang zh --verbose
model qwen-turbo-latest stream=true first_token_ms=448
我们现在来测一下 Ghostty 的上下文效果
Codex CLI is editing realtime_asr_service.py right now
"""

    result = dictation_context_service._sanitize_terminal_context(raw, 200)

    assert result == (
        'model qwen-turbo-latest stream=true first_token_ms=448\n'
        '我们现在来测一下 Ghostty 的上下文效果\n'
        'Codex CLI is editing realtime_asr_service.py right now'
    )


def test_sanitize_page_context_avoids_being_dominated_by_log_lines() -> None:
    raw = """
Vox Dictation 设置
本地网页面板，直接管理 ~/.vox/config.toml
上下文策略
录音开始前预先采集焦点内容，用于后续润色与纠错。
[session-server] dictation_stage | utterance_id=8 | stage="llm_start" | timeout_sec=8.0
[vox-dictation] final: 这个好像一个网页博客啊
21:23:04.123 [POST] changed yes | llm yes | post 988ms
热词词库
维护标准写法与常见误识别，优先修正稳定错词。
"""

    result = dictation_context_service._sanitize_page_context(raw, 200)

    assert result == (
        'Vox Dictation 设置\n'
        '本地网页面板，直接管理 ~/.vox/config.toml\n'
        '上下文策略\n'
        '录音开始前预先采集焦点内容，用于后续润色与纠错。\n'
        '热词词库\n'
        '维护标准写法与常见误识别，优先修正稳定错词。'
    )
