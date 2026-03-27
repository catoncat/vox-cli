from __future__ import annotations

import io
import json
import urllib.error

from vox_cli.config import (
    DictationConfig,
    DictationHintsConfig,
    DictationHotwordEntry,
    DictationHotwordsConfig,
    DictationLLMConfig,
    DictationTransformConfig,
    VoxConfig,
)
from vox_cli.services.dictation_postprocess_service import (
    DictationTextPostprocessor,
    apply_hotword_aliases,
    apply_dictation_transforms,
    build_text_diff,
)
from vox_cli.services.dictation_context_service import DictationContext


class _FakeHTTPResponse:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        content_type: str = 'application/json',
        raw_lines: list[str] | None = None,
    ) -> None:
        if raw_lines is not None:
            body = ''.join(raw_lines)
        else:
            body = json.dumps(payload or {}, ensure_ascii=False)
        self._stream = io.BytesIO(body.encode('utf-8'))
        self.headers = {'Content-Type': content_type}

    def read(self) -> bytes:
        return self._stream.read()

    def readline(self) -> bytes:
        return self._stream.readline()

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_apply_dictation_transforms_normalizes_spacing_and_punctuation() -> None:
    config = DictationTransformConfig(
        fullwidth_to_halfwidth=True,
        space_around_punct=True,
        strip_trailing_punctuation=True,
    )

    result = apply_dictation_transforms('你好，world。', config)

    assert result == '你好, world'


def test_build_text_diff_marks_replacements() -> None:
    diff = build_text_diff('语音输入法的转换，还有AI的检测能力。', '语音输入法的转换, 以及 AI 的检测能力')

    assert '[-还有-]' in diff or '[-，还有-]' in diff
    assert '[+以及 +]' in diff or '[+ 以及 +]' in diff or '[+, 以及 +]' in diff


def test_apply_hotword_aliases_rewrites_configured_aliases() -> None:
    config = DictationHotwordsConfig(
        enabled=True,
        entries=[
            DictationHotwordEntry(value='潮汕', aliases=['潮上']),
            DictationHotwordEntry(value='Codex CLI', aliases=['ColdX CLI', 'CodeX CLI']),
        ],
    )

    text, replacements = apply_hotword_aliases('我是潮上人，现在在 ColdX CLI 里说话。', config)

    assert text == '我是潮汕人，现在在 Codex CLI 里说话。'
    assert replacements[0].alias == 'ColdX CLI'
    assert replacements[1].alias == '潮上'


def test_postprocessor_calls_custom_openai_compatible_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['url'] = request.full_url
        captured['timeout'] = timeout
        captured['headers'] = {k.lower(): v for k, v in request.header_items()}
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            transforms=DictationTransformConfig(space_between_cjk=True),
            llm=DictationLLMConfig(
                enabled=True,
                provider='openrouter',
                base_url='https://openrouter.ai/api/v1',
                model='openai/gpt-4o-mini',
                api_key_env='OPENROUTER_API_KEY',
                headers={
                    'HTTP-Referer': 'https://example.com',
                    'X-Title': 'vox-cli',
                },
                system_prompt='system prompt',
                user_prompt_template='LANG={language}\nTEXT={text}',
                temperature=0.2,
                max_tokens=128,
                timeout_sec=12.0,
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world', language='Chinese')

    assert result.text == 'Refined output'
    assert captured['url'] == 'https://openrouter.ai/api/v1/chat/completions'
    assert captured['timeout'] == 12.0
    headers = captured['headers']
    assert headers['authorization'] == 'Bearer sk-test'
    assert headers['http-referer'] == 'https://example.com'
    assert headers['x-title'] == 'vox-cli'
    body = captured['body']
    assert body['model'] == 'openai/gpt-4o-mini'
    assert body['stream'] is True
    assert body['temperature'] == 0.2
    assert body['max_tokens'] == 128
    assert body['messages'][0]['content'] == 'system prompt'
    assert body['messages'][1]['content'] == 'LANG=Chinese\nTEXT=你好 world'
    assert result.metadata['llm_used'] is True
    assert result.metadata['provider'] == 'openrouter'
    assert result.metadata['original_text'] == '你好 world'
    assert result.metadata['final_text'] == 'Refined output'
    assert result.metadata['llm_ms'] >= 0
    assert result.metadata['llm_timeout_sec'] == 12.0
    assert result.metadata['llm_stream_requested'] is True
    assert result.metadata['llm_stream_used'] is False


def test_postprocessor_uses_prompt_preset_when_no_custom_override(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='https://api.openai.com/v1',
                model='gpt-4o-mini',
                api_key_env='OPENAI_API_KEY',
                prompt_preset='arena',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world', language='zh')

    body = captured['body']
    assert '竞技场风格' in body['messages'][0]['content']
    assert '待纠正文本' in body['messages'][1]['content']
    assert result.text == 'Refined output'


def test_postprocessor_uses_deep_clean_preset(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='https://api.openai.com/v1',
                model='gpt-4o-mini',
                api_key_env='OPENAI_API_KEY',
                prompt_preset='deep_clean',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('预算是五十万，不对，六十万', language='zh')

    body = captured['body']
    assert '只保留最终确认的信息' in body['messages'][0]['content']
    assert 'dictation 深度整理任务' in body['messages'][1]['content']
    assert result.text == 'Refined output'


def test_postprocessor_uses_spoken_clean_preset(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='https://api.openai.com/v1',
                model='gpt-4o-mini',
                api_key_env='OPENAI_API_KEY',
                prompt_preset='spoken_clean',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('嗯这个这个功能吧, 就是说, 用起来怎么样', language='zh')

    body = captured['body']
    assert '删除语气词' in body['messages'][0]['content']
    assert '示例5' in body['messages'][1]['content']
    assert 'Codex' in body['messages'][1]['content']
    assert result.text == 'Refined output'


def test_postprocessor_default_preset_is_dictation_safe(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='https://api.openai.com/v1',
                model='gpt-4o-mini',
                api_key_env='OPENAI_API_KEY',
            ),
        )
    )

    DictationTextPostprocessor(config).process('你好 world', language='zh')

    body = captured['body']
    assert '你不是聊天助手' in body['messages'][0]['content']
    assert '待修订文本' in body['messages'][1]['content']


def test_postprocessor_supports_api_key_from_config(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['headers'] = {k.lower(): v for k, v in request.header_items()}
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key='sk-inline',
                api_key_env=None,
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world')

    headers = captured['headers']
    assert headers['authorization'] == 'Bearer sk-inline'
    assert result.text == 'Refined output'
    assert result.metadata['llm_stream_requested'] is True


def test_postprocessor_allows_missing_api_key_for_loopback_base_url(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['headers'] = {k.lower(): v for k, v in request.header_items()}
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='http://127.0.0.1:18080/v1',
                model='Qwen/Qwen3-1.7B-MLX-4bit',
                api_key_env='OPENAI_API_KEY',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world')

    headers = captured['headers']
    assert 'authorization' not in headers
    assert result.text == 'Refined output'


def test_postprocessor_allows_missing_api_key_for_local_mlx_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['headers'] = {k.lower(): v for k, v in request.header_items()}
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': 'Refined output',
                        }
                    }
                ]
            }
        )

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='local-mlx',
                base_url='http://mac-studio.local:18080/v1',
                model='Qwen/Qwen3-1.7B-MLX-4bit',
                api_key_env='OPENAI_API_KEY',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world')

    headers = captured['headers']
    assert 'authorization' not in headers
    assert result.text == 'Refined output'


def test_postprocessor_requires_api_key_for_remote_compatible_provider(monkeypatch) -> None:
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='openai-compatible',
                base_url='https://api.openai.com/v1',
                model='gpt-4o-mini',
                api_key_env='OPENAI_API_KEY',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好 world')

    assert result.text == '你好 world'
    assert result.metadata['llm_used'] is False
    assert result.metadata['llm_error'] == 'OPENAI_API_KEY is not set'


def test_postprocessor_feeds_raw_asr_text_to_llm_then_applies_rules(monkeypatch) -> None:
    captured: dict[str, object] = {}
    stages: list[str] = []

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '你好，world。',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('TEST_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            transforms=DictationTransformConfig(
                fullwidth_to_halfwidth=True,
                space_around_punct=True,
                strip_trailing_punctuation=True,
            ),
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key_env='TEST_API_KEY',
                user_prompt_template='TEXT={text}',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process(
        '你好，world。',
        emit=lambda stage, fields: stages.append(stage),
    )

    body = captured['body']
    assert body['messages'][1]['content'] == 'TEXT=你好，world。'
    assert body['stream'] is True
    assert result.text == '你好, world'
    assert result.metadata['llm_input_text'] == '你好，world。'
    assert result.metadata['rules_input_text'] == '你好，world。'
    assert stages == ['llm_start', 'llm_done', 'rules_done', 'final_ready']


def test_postprocessor_can_skip_llm_for_partial_preview(monkeypatch) -> None:
    def fail_urlopen(request, timeout):
        raise AssertionError('LLM should not be called when allow_llm=False')

    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fail_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            transforms=DictationTransformConfig(
                fullwidth_to_halfwidth=True,
                space_around_punct=True,
                strip_trailing_punctuation=True,
            ),
            hotwords=DictationHotwordsConfig(
                enabled=True,
                entries=[DictationHotwordEntry(value='Codex CLI', aliases=['ColdX CLI'])],
            ),
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key='sk-inline',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process(
        '我在 ColdX CLI 里说话。',
        allow_llm=False,
    )

    assert result.text == '我在 Codex CLI 里说话'
    assert result.metadata['llm_used'] is False
    assert result.metadata['llm_enabled'] is False
    assert result.metadata['llm_skipped'] is True


def test_postprocessor_injects_context_block_when_template_has_no_placeholder(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '整理后的文本',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('TEST_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key_env='TEST_API_KEY',
                user_prompt_template='LANG={language}\nTEXT={text}',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process(
        '原始文本',
        language='Chinese',
        context=DictationContext(
            source='ghostty',
            app_name='Ghostty',
            window_title='codex',
            element_role='AXTextArea',
            context_text='当前终端里的上下文',
        ),
    )

    body = captured['body']
    prompt = body['messages'][1]['content']
    assert prompt.startswith('当前输入环境:\n- source: ghostty\n- app: Ghostty\n- window: codex\n- focus_role: AXTextArea')
    assert '最近内容:\n<<<\n当前终端里的上下文\n>>>' in prompt
    assert '使用约束:\n- 上下文仅用于判断专有词、术语、当前主题和代词所指' in prompt
    assert prompt.endswith('LANG=Chinese\nTEXT=原始文本')
    assert result.metadata['context_used'] is True
    assert result.metadata['context_source'] == 'ghostty'


def test_postprocessor_applies_hotwords_before_llm_and_injects_hints(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '我是潮汕人，现在用 Codex CLI 讲话。',
                        }
                    }
                ]
            }
        )

    monkeypatch.setenv('TEST_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key_env='TEST_API_KEY',
                user_prompt_template='TEXT={text}',
            ),
            hotwords=DictationHotwordsConfig(
                enabled=True,
                entries=[
                    DictationHotwordEntry(value='潮汕', aliases=['潮上']),
                    DictationHotwordEntry(value='Codex CLI', aliases=['ColdX CLI']),
                ],
            ),
            hints=DictationHintsConfig(
                enabled=True,
                items=['说话人前后鼻音不分，优先纠正 an/ang、en/eng、in/ing 的混淆。'],
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('我是潮上人，现在用 ColdX CLI 讲话。')

    prompt = captured['body']['messages'][1]['content']
    assert captured['body']['stream'] is True
    assert prompt.startswith('说话人纠错提示:\n- 说话人前后鼻音不分')
    assert '热词与优先写法:\n- 潮汕 <- 潮上\n- Codex CLI <- ColdX CLI' in prompt
    assert prompt.endswith('TEXT=我是潮汕人，现在用 Codex CLI 讲话。')
    assert result.metadata['hotwords_changed'] is True
    assert result.metadata['hotword_matches'] == 2
    assert result.metadata['hint_count'] == 1
    assert result.metadata['llm_input_text'] == '我是潮汕人，现在用 Codex CLI 讲话。'


def test_postprocessor_streams_llm_chunks_and_emits_progress(monkeypatch) -> None:
    captured: dict[str, object] = {}
    stages: list[str] = []

    def fake_urlopen(request, timeout):
        captured['body'] = json.loads(request.data.decode('utf-8'))
        return _FakeHTTPResponse(
            content_type='text/event-stream',
            raw_lines=[
                'data: {"choices":[{"delta":{"content":"作为一个潮"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"汕人"}}]}\n\n',
                'data: [DONE]\n\n',
            ],
        )

    monkeypatch.setenv('TEST_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key_env='TEST_API_KEY',
                user_prompt_template='TEXT={text}',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process(
        '作为一个潮上人',
        emit=lambda stage, fields: stages.append(stage),
    )

    assert captured['body']['stream'] is True
    assert result.text == '作为一个潮汕人'
    assert result.metadata['llm_stream_requested'] is True
    assert result.metadata['llm_stream_used'] is True
    assert result.metadata['llm_stream_chunks'] == 2
    assert result.metadata['llm_first_token_ms'] is not None
    assert 'llm_stream' in stages


def test_postprocessor_strips_think_blocks_from_llm_output(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '<think>先想一下</think>\n\n你好，world。',
                        }
                    }
                ]
            }
        )

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='local-mlx',
                base_url='http://127.0.0.1:18080/v1',
                model='Qwen/Qwen3-1.7B-MLX-4bit',
                api_key_env='OPENAI_API_KEY',
                user_prompt_template='TEXT={text}',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好，world。')

    assert result.text == '你好，world。'
    assert result.metadata['llm_used'] is True
    assert result.metadata['llm_output_text'] == '你好，world。'
    assert result.metadata['llm_guard_fallback'] is False


def test_postprocessor_strips_prompt_echo_wrappers_from_llm_output(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        return _FakeHTTPResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '<<<\n把这个功能先发到测试环境，没问题再发到正式环境。\n>>>',
                        }
                    }
                ]
            }
        )

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            llm=DictationLLMConfig(
                enabled=True,
                provider='local-mlx',
                base_url='http://127.0.0.1:18080/v1',
                model='mlx-community/Qwen2.5-1.5B-Instruct-4bit',
                api_key_env='OPENAI_API_KEY',
                user_prompt_template='TEXT={text}',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('把这个功能先发测试环境 没问题再发正式')

    assert result.text == '把这个功能先发到测试环境，没问题再发到正式环境。'
    assert result.metadata['llm_used'] is True
    assert result.metadata['llm_output_text'] == '把这个功能先发到测试环境，没问题再发到正式环境。'
    assert result.metadata['llm_guard_fallback'] is False


def test_postprocessor_falls_back_to_rules_when_llm_fails(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError('boom')

    monkeypatch.setenv('TEST_API_KEY', 'sk-test')
    monkeypatch.setattr(
        'vox_cli.services.dictation_postprocess_service.urllib.request.urlopen',
        fake_urlopen,
    )

    config = VoxConfig(
        dictation=DictationConfig(
            transforms=DictationTransformConfig(
                fullwidth_to_halfwidth=True,
                space_around_punct=True,
                strip_trailing_punctuation=True,
            ),
            llm=DictationLLMConfig(
                enabled=True,
                provider='custom',
                base_url='https://llm.example.com/v1',
                model='demo-model',
                api_key_env='TEST_API_KEY',
            ),
        )
    )

    result = DictationTextPostprocessor(config).process('你好，world。')

    assert result.text == '你好, world'
    assert result.metadata['llm_used'] is False
    assert 'llm_error' in result.metadata
    assert result.metadata['original_text'] == '你好，world。'
    assert result.metadata['final_text'] == '你好, world'
    assert result.metadata['llm_ms'] >= 0
