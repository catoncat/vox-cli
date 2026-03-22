from __future__ import annotations

import io
import json
import os
from pathlib import Path

from vox_cli.config import RuntimeConfig, VoxConfig
from vox_cli.services import dictation_service


class _FakeProc:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.returncode is None else self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeStream(io.StringIO):
    def isatty(self) -> bool:
        return False


class _FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def _lines(result) -> list[str]:
    return list(result.lines)


def test_wait_for_session_server_fails_fast_when_process_exits() -> None:
    proc = _FakeProc(returncode=3)

    try:
        dictation_service.wait_for_session_server(
            '127.0.0.1',
            8765,
            timeout=0.2,
            server_proc=proc,  # type: ignore[arg-type]
        )
    except RuntimeError as error:
        assert 'exited before becoming ready' in str(error)
    else:
        raise AssertionError('expected wait_for_session_server to fail')


def test_should_echo_server_line_matches_compare_and_summary_lines() -> None:
    assert dictation_service._should_echo_server_line(
        '[session-server] transcribe utterance_id=1 partial=False audio_ms=4300'
    )
    assert dictation_service._should_echo_server_line(
        '[session-server] dictation_stage utterance_id=1 stage=llm_done t+123ms'
    )
    assert dictation_service._should_echo_server_line('[session-server] warmup completed')


def test_dictation_log_formatter_formats_server_stage_and_text_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    stage_lines = formatter.format(
        'server',
        '[session-server] dictation_stage | utterance_id=1 | stage="llm_start" | t_rel_ms=12 | timeout_sec=8.0 | provider="openai-compatible" | model="KAT-Coder" | input_chars=44 | context_chars=120 | changed=true',
    )
    text_lines = formatter.format(
        'server',
        '[session-server] dictation_text utterance_id=1 stage=llm_start text="你好，世界"',
    )
    diff_lines = formatter.format(
        'server',
        '[session-server] dictation_diff utterance_id=1 stage=rules_done diff="你好[-，-][+, +]世界"',
    )

    assert any('开始润色' in line for line in _lines(stage_lines))
    assert any('openai-compatible / KAT-Coder' in line for line in _lines(stage_lines))
    assert any('context 120字' in line for line in _lines(stage_lines))
    assert _lines(text_lines) == []
    assert len(_lines(diff_lines)) == 1
    assert '改动' in _lines(diff_lines)[0]
    assert '，->, ' in _lines(diff_lines)[0] or '，->,' in _lines(diff_lines)[0]


def test_dictation_log_formatter_formats_llm_stream_stage() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    stage_lines = formatter.format(
        'server',
        '[session-server] dictation_stage | utterance_id=1 | stage="llm_stream" | t_rel_ms=320 | stage_ms=320 | stream_requested=true | stream_used=true | stream_chunks=3 | first_token_ms=78 | chars=26',
    )
    text_lines = formatter.format(
        'server',
        '[session-server] dictation_text utterance_id=1 stage=llm_stream text="...幸好 AI 救了我"',
    )

    assert _lines(stage_lines) == []
    assert text_lines.live_line is None
    assert len(_lines(text_lines)) == 1
    assert 'LLM #1' in _lines(text_lines)[0]
    assert '流式' in _lines(text_lines)[0]
    assert 'chunks 3' in _lines(text_lines)[0]
    assert '首字 78ms' in _lines(text_lines)[0]
    assert '幸好 AI 救了我' in _lines(text_lines)[0]


def test_dictation_log_formatter_formats_hotword_stage() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'server',
        '[session-server] dictation_stage | utterance_id=1 | stage="hotwords_done" | t_rel_ms=3 | stage_ms=1 | chars=12 | changed=true | matches=2 | replacements="潮上->潮汕 x1; ColdX CLI->Codex CLI x1"',
    )

    assert 'HOT #1' in _lines(lines)[0]
    assert '热词纠正' in _lines(lines)[0]
    assert 'matches 2' in _lines(lines)[0]
    assert any('潮上->潮汕 x1' in line for line in _lines(lines))


def test_dictation_log_formatter_formats_config_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    config_lines = formatter.format(
        'server',
        '[session-server] dictation_config | llm_enabled=true | llm_provider="dashscope" | llm_model="qwen-flash" | llm_timeout_sec=4.0 | llm_stream=true | context_enabled=true | context_max_chars=1200 | hotwords_enabled=true | hotword_entries=3 | rewrite_aliases=true | case_sensitive=false | hints_enabled=true | hint_count=1',
    )
    hotwords_lines = formatter.format(
        'server',
        '[session-server] dictation_config_hotwords | text="潮汕 <- 潮上 | Codex CLI <- ColdX CLI, CodeX CLI, ColdX"',
    )
    hints_lines = formatter.format(
        'server',
        '[session-server] dictation_config_hints | text="说话人前后鼻音不分，优先纠正 an/ang、en/eng 混淆。"',
    )

    assert 'CFG' in _lines(config_lines)[0]
    assert 'llm on' in _lines(config_lines)[0]
    assert 'stream on' in _lines(config_lines)[0]
    assert 'hotwords on' in _lines(config_lines)[0]
    assert any('dashscope / qwen-flash' in line for line in _lines(config_lines))
    assert any('rewrite on' in line for line in _lines(config_lines))
    assert len(_lines(hotwords_lines)) == 1
    assert '热词表' in _lines(hotwords_lines)[0]
    assert '潮汕 <- 潮上' in _lines(hotwords_lines)[0]
    assert len(_lines(hints_lines)) == 1
    assert '提示词' in _lines(hints_lines)[0]
    assert len(config_lines.log_events) == 1
    assert config_lines.log_events[0].event == 'dictation_config_summary'


def test_dictation_log_formatter_formats_context_and_postprocess_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    context_lines = formatter.format(
        'server',
        '[session-server] dictation_context | utterance_id=2 | state="ready" | source="ghostty" | app="Ghostty" | window="codex" | role="AXTextArea" | capture_ms=87 | selected_chars=0 | context_chars=188',
    )
    excerpt_lines = formatter.format(
        'server',
        '[session-server] dictation_context_excerpt | utterance_id=2 | text="当前终端里的上下文"',
    )
    post_lines = formatter.format(
        'server',
        '[session-server] dictation_postprocess | changed=true | rules_changed=false | llm_used=true | llm_ms=511 | postprocess_ms=530 | timeout_sec=8.0 | stream_requested=true | stream_used=true | stream_chunks=5 | first_token_ms=92 | provider="openai-compatible" | model="KAT-Coder" | raw_chars=19 | final_chars=18 | context_source="ghostty" | context_chars=188',
    )

    assert 'CTX #2' in _lines(context_lines)[0]
    assert '上下文已捕获' in _lines(context_lines)[0]
    assert 'context 188字' in _lines(context_lines)[0]
    assert any('source ghostty' in line for line in _lines(context_lines))
    assert _lines(excerpt_lines) == []
    assert 'POST' in _lines(post_lines)[0]
    assert 'llm yes' in _lines(post_lines)[0]
    assert 'stream on' in _lines(post_lines)[0]
    assert 'used yes' in _lines(post_lines)[0]
    assert 'chunks 5' in _lines(post_lines)[0]
    assert '19->18字' in _lines(post_lines)[0]
    assert len(_lines(post_lines)) == 1


def test_dictation_log_formatter_formats_context_prefetch_and_partial_pipeline_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    context_prefetch = formatter.format(
        'server',
        '[session-server] dictation_context_prefetch | utterance_id=2 | source="ghostty" | app="Ghostty" | window="codex" | role="AXTextArea" | capture_ms=641 | context_chars=188',
    )
    pre_start = formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=2 | state="job_started" | stable_chars=18 | completed_chars=0 | context_ready=true',
    )
    pre_done = formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=2 | state="job_completed" | stable_chars=18 | output_chars=18 | changed=true | llm_used=true | llm_ms=320',
    )
    pre_flush = formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=2 | state="flush" | reused_chars=12 | stable_chars=18 | completed_chars=18',
    )

    assert 'CTX PRE #2' in _lines(context_prefetch)[0]
    assert '预采集就绪' in _lines(context_prefetch)[0]
    assert 'PRE #2' in _lines(pre_start)[0]
    assert '预跑启动' in _lines(pre_start)[0]
    assert 'ctx yes' in _lines(pre_start)[0]
    assert 'PRE #2' in _lines(pre_done)[0]
    assert '预跑完成' in _lines(pre_done)[0]
    assert 'llm 320ms' in _lines(pre_done)[0]
    assert 'PRE #2' in _lines(pre_flush)[0]
    assert '预跑命中 12字' in _lines(pre_flush)[0]


def test_dictation_log_formatter_emits_postprocess_error_log_event() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'server',
        '[session-server] dictation_postprocess_error | utterance_id=7 | llm_ms=811 | timeout_sec=8.0 | provider="dashscope" | model="qwen-turbo" | llm_error="timeout"',
    )

    assert 'LLM ERR' in _lines(lines)[0]
    assert len(lines.log_events) == 1
    assert lines.log_events[0].event == 'postprocess_error'


def test_dictation_log_formatter_formats_helper_timing_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'helper',
        '[vox-dictation] timings utterance_id=3 capture_ms=10624 flush_roundtrip_ms=973 audio_ms=10100 warmup_ms=0 infer_ms=361 postprocess_ms=607 llm_ms=606 llm_used=false llm_timeout_sec=8 llm_provider=openai-compatible llm_model=KAT-Coder backend_total_ms=968 type_ms=76 warmup_reason=-',
    )

    assert 'PERF #3' in _lines(lines)[0]
    assert 'audio 10.10s' in _lines(lines)[0]
    assert 'flush 973ms' in _lines(lines)[0]
    assert 'llm off' in _lines(lines)[0]
    assert 'backend 968ms' in _lines(lines)[1]
    assert 'timeout 8s' in _lines(lines)[1]
    assert 'WHY #3' in _lines(lines)[2]
    assert 'openai-compatible / KAT-Coder' in _lines(lines)[3]
    assert len(lines.log_events) == 1
    assert lines.log_events[0].event == 'utterance_summary'


def test_relay_process_output_writes_structured_events_even_without_echo() -> None:
    stream = io.StringIO(
        '[vox-dictation] timings utterance_id=3 capture_ms=10624 flush_roundtrip_ms=973 '
        'audio_ms=10100 warmup_ms=0 infer_ms=361 postprocess_ms=607 llm_ms=606 '
        'llm_used=false llm_timeout_sec=8 llm_provider=openai-compatible '
        'llm_model=KAT-Coder backend_total_ms=968 type_ms=76 warmup_reason=-\n'
    )
    log_handle = io.StringIO()
    agent_log_handle = io.StringIO()

    dictation_service._relay_process_output(
        stream,
        log_handle,
        agent_log_handle=agent_log_handle,
        source='helper',
        echo=False,
        lock=dictation_service.threading.Lock(),
        formatter=dictation_service._DictationLogFormatter(_FakeTTYStream()),
    )

    written = log_handle.getvalue()
    assert '[vox-dictation] timings utterance_id=3' in written
    assert '"event": "utterance_summary"' in written
    assert '"flush_roundtrip_ms": 973' in written
    compact = json.loads(agent_log_handle.getvalue().strip())
    assert compact['e'] == 'u'
    assert compact['u'] == 3
    assert compact['fl'] == 973
    assert compact['be'] == 968


def test_serialize_agent_log_event_uses_compact_keys() -> None:
    line = dictation_service._serialize_agent_log_event(
        event='utterance_summary',
        utterance_id=9,
        audio_ms=7400,
        capture_ms=7970,
        flush_roundtrip_ms=8811,
        context_capture_ms=1012,
        context_wait_ms=120,
        context_overlap_ms=892,
        context_status='ready',
        context_budget_state='ready',
        context_source='ghostty',
        asr_infer_ms=525,
        asr_total_ms=525,
        llm_used=True,
        llm_stream_used=True,
        llm_first_token_ms=420,
        llm_ms=2554,
        llm_stream_ms=2134,
        llm_stream_chunks=12,
        type_ms=38,
        backend_total_ms=3080,
        postprocess_ms=2555,
        bottleneck='llm_stream_tail',
        final_chars=29,
        raw_chars=31,
    )

    assert line is not None
    payload = json.loads(line)
    assert payload == {
        'e': 'u',
        'u': 9,
        'aud': 7400,
        'cap': 7970,
        'fl': 8811,
        'ctxc': 1012,
        'ctxw': 120,
        'ctxo': 892,
        'ctxs': 'ready',
        'ctxb': 'ready',
        'src': 'ghostty',
        'asr': 525,
        'asrt': 525,
        'lu': 1,
        'ls': 1,
        'ft': 420,
        'llm': 2554,
        'lst': 2134,
        'lsch': 12,
        'ty': 38,
        'be': 3080,
        'post': 2555,
        'bot': 'llm_stream_tail',
        'fin': 29,
        'raw': 31,
    }


def test_build_dictation_agent_digest_summarizes_recent_window(tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    log_path = dictation_service.dictation_agent_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'e': 'ls',
                        's': 'sess-1',
                        'lang': 'zh',
                        'am': 'qwen-asr-0.6b-4bit',
                        'pi': 250,
                        'tp': 0,
                        'tv': 1,
                        'hv': 'v0.1.0',
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'cfg',
                        'lu': 1,
                        'ls': 1,
                        'lp': 'dashscope',
                        'lm': 'qwen-turbo-latest',
                        'lt': 8,
                        'ce': 1,
                        'cc': 1200,
                        'he': 1,
                        'hn': 2,
                        'hr': 1,
                        'cs': 0,
                        'ie': 1,
                        'in': 1,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'u',
                        'u': 1,
                        'aud': 4000,
                        'cap': 5000,
                        'fl': 900,
                        'ctxc': 400,
                        'ctxw': 50,
                        'ctxo': 350,
                        'ctxs': 'ready',
                        'ctxb': 'ready',
                        'src': 'ghostty',
                        'asr': 300,
                        'asrt': 300,
                        'lu': 1,
                        'ls': 1,
                        'ft': 200,
                        'llm': 1200,
                        'lst': 1000,
                        'lsch': 6,
                        'ty': 30,
                        'be': 1600,
                        'post': 1210,
                        'bot': 'balanced',
                        'fin': 10,
                        'raw': 11,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'u',
                        'u': 2,
                        'aud': 4200,
                        'cap': 6100,
                        'fl': 1200,
                        'ctxc': 450,
                        'ctxw': 80,
                        'ctxo': 370,
                        'ctxs': 'ready',
                        'ctxb': 'ready',
                        'src': 'ghostty',
                        'asr': 320,
                        'asrt': 320,
                        'lu': 1,
                        'ls': 1,
                        'ft': 260,
                        'llm': 1500,
                        'lst': 1240,
                        'lsch': 7,
                        'ty': 32,
                        'be': 1900,
                        'post': 1515,
                        'bot': 'llm_stream_tail',
                        'fin': 12,
                        'raw': 13,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'u',
                        'u': 3,
                        'aud': 4300,
                        'cap': 7200,
                        'fl': 1600,
                        'ctxc': 500,
                        'ctxw': 140,
                        'ctxo': 360,
                        'ctxs': 'ready',
                        'ctxb': 'ready',
                        'src': 'ghostty',
                        'asr': 340,
                        'asrt': 340,
                        'lu': 1,
                        'ls': 1,
                        'ft': 420,
                        'llm': 2100,
                        'lst': 1680,
                        'lsch': 9,
                        'ty': 35,
                        'be': 2500,
                        'post': 2110,
                        'bot': 'llm_stream_tail',
                        'fin': 14,
                        'raw': 15,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'pe',
                        'u': 3,
                        'llm': 2100,
                        'lt': 8,
                        'lp': 'dashscope',
                        'lm': 'qwen-turbo-latest',
                        'err': 'timeout',
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    digest = dictation_service.build_dictation_agent_digest(
        config,
        utterances=2,
        slowest=1,
        errors=1,
    )

    assert digest['exists'] is True
    assert digest['total_events'] == 6
    assert digest['window'] == {
        'requested_utterances': 2,
        'analyzed_utterances': 2,
        'first_utterance_id': 2,
        'last_utterance_id': 3,
    }
    assert digest['launch'] == {
        'session_id': 'sess-1',
        'lang': 'zh',
        'asr_model': 'qwen-asr-0.6b-4bit',
        'partial_interval_ms': 250,
        'type_partial': False,
        'tty_verbose': True,
        'helper_version': 'v0.1.0',
    }
    assert digest['config'] == {
        'llm_enabled': True,
        'llm_stream': True,
        'provider': 'dashscope',
        'model': 'qwen-turbo-latest',
        'timeout_sec': 8,
        'context_enabled': True,
        'context_max_chars': 1200,
        'hotwords_enabled': True,
        'hotword_entries': 2,
        'rewrite_aliases': True,
        'case_sensitive': False,
        'hints_enabled': True,
        'hint_count': 1,
    }
    assert digest['metrics']['capture_ms'] == {'n': 2, 'avg': 6650, 'p50': 6100, 'p95': 7200, 'max': 7200}
    assert digest['metrics']['llm_ms'] == {'n': 2, 'avg': 1800, 'p50': 1500, 'p95': 2100, 'max': 2100}
    assert digest['partial_pipeline'] == {
        'instrumented': False,
        'active': False,
        'analyzed_utterances': 2,
        'active_utterances': 0,
        'preview_total': 0,
        'stable_advances_total': 0,
        'sent_total': 0,
        'skipped_total': 0,
        'skip_rate': 0,
        'jobs_started_total': 0,
        'jobs_completed_total': 0,
        'jobs_completion_rate': 0,
        'hit_utterances': 0,
        'hit_rate': 0,
        'reused_chars_total': 0,
        'reused_chars_avg': 0,
        'reused_chars_max': 0,
        'stable_chars_max': 0,
    }
    assert digest['bottlenecks'] == [{'name': 'llm_stream_tail', 'count': 2}]
    assert digest['trends'] == {}
    assert digest['diagnosis'] == {
        'status': 'ok',
        'primary': 'llm_stream_tail',
        'confidence': 'high',
        'summary': 'LLM 持续生成偏慢，尾段输出拖长',
        'signals': [
            'type_fast',
            'asr_ok',
            'llm_stream_tail_slow',
            'backend_high',
        ],
        'evidence': {
            'samples': 2,
            'primary_bottleneck_count': 2,
            'llm_first_token_avg_ms': 340,
            'llm_first_token_p95_ms': 420,
            'llm_p95_ms': 2100,
            'llm_stream_tail_p95_ms': 1680,
            'context_wait_max_ms': 140,
            'asr_p95_ms': 340,
            'type_max_ms': 35,
            'flush_p95_ms': 1600,
            'backend_p95_ms': 2500,
            'partial_preview_total': 0,
            'partial_sent_total': 0,
            'partial_skipped_total': 0,
            'partial_skip_rate': 0,
            'partial_hit_rate': 0,
            'partial_jobs_started_total': 0,
            'partial_jobs_completed_total': 0,
            'partial_reused_chars_max': 0,
        },
        'next_actions': [
            '优先缩短输出长度或换更快的流式模型',
        ],
    }
    assert digest['slowest_utterances'] == [
        {
            'utterance_id': 3,
            'audio_ms': 4300,
            'capture_ms': 7200,
            'flush_ms': 1600,
            'context_capture_ms': 500,
            'context_wait_ms': 140,
            'context_overlap_ms': 360,
            'context_status': 'ready',
            'context_budget_state': 'ready',
            'context_source': 'ghostty',
            'context_surface': None,
            'context_chars': 0,
            'asr_ms': 340,
            'asr_total_ms': 340,
            'llm_used': True,
            'llm_stream': True,
            'llm_first_token_ms': 420,
            'llm_ms': 2100,
            'llm_stream_tail_ms': 1680,
            'llm_stream_chunks': 9,
            'type_ms': 35,
            'backend_ms': 2500,
            'postprocess_ms': 2110,
            'bottleneck': 'llm_stream_tail',
            'final_chars': 14,
            'raw_chars': 15,
            'partial_preview_count': 0,
            'partial_stable_advance_count': 0,
            'partial_jobs_started': 0,
            'partial_jobs_completed': 0,
            'partial_reused_chars': 0,
            'partial_stable_chars': 0,
            'partial_sent_count': 0,
            'partial_skipped_count': 0,
            'commit_mode': None,
            'guard_fallback': False,
            'guard_reason': None,
        }
    ]
    assert digest['recent_errors'] == [
        {
            'event': 'postprocess_error',
            'utterance_id': 3,
            'llm_ms': 2100,
            'timeout_sec': 8,
            'provider': 'dashscope',
            'model': 'qwen-turbo-latest',
            'error': 'timeout',
        }
    ]


def test_dictation_log_formatter_tracks_partial_pipeline_in_summary() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=5 | state="stable" | stable_chars=18 | advance_chars=8 | partial_chars=24',
    )
    formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=5 | state="job_started" | stable_chars=18 | completed_chars=0 | context_ready=true',
    )
    formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=5 | state="job_completed" | stable_chars=18 | output_chars=18 | changed=true | llm_used=true | llm_ms=320',
    )
    formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=5 | state="preview" | partial_chars=24 | preview_chars=24 | stable_chars=18 | reused_chars=12',
    )
    formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=5 | state="flush" | reused_chars=12 | stable_chars=18 | completed_chars=18',
    )
    lines = formatter.format(
        'helper',
        '[vox-dictation] timings utterance_id=5 capture_ms=4200 flush_roundtrip_ms=720 audio_ms=3800 warmup_ms=0 infer_ms=280 postprocess_ms=640 llm_ms=630 llm_used=true llm_timeout_sec=4 llm_provider=dashscope llm_model=qwen-turbo-latest backend_total_ms=910 type_ms=28 warmup_reason=-',
    )

    assert 'partial shown 1x' in _lines(lines)[1]
    assert 'jobs 1/1' in _lines(lines)[1]
    assert 'hit 12字' in _lines(lines)[1]
    assert len(lines.log_events) == 1
    assert lines.log_events[0].fields['partial_preview_count'] == 1
    assert lines.log_events[0].fields['partial_stable_advance_count'] == 1
    assert lines.log_events[0].fields['partial_jobs_started'] == 1
    assert lines.log_events[0].fields['partial_jobs_completed'] == 1
    assert lines.log_events[0].fields['partial_reused_chars'] == 12
    assert lines.log_events[0].fields['partial_stable_chars'] == 18


def test_build_dictation_agent_digest_reports_effective_partial_pipeline(tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    log_path = dictation_service.dictation_agent_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'e': 'ls',
                        's': 'sess-2',
                        'lang': 'zh',
                        'am': 'qwen-asr-0.6b-4bit',
                        'pi': 250,
                        'tp': 0,
                        'tv': 1,
                        'hv': 'v0.1.0',
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'cfg',
                        'lu': 1,
                        'ls': 1,
                        'lp': 'dashscope',
                        'lm': 'qwen-turbo-latest',
                        'lt': 4,
                        'ce': 1,
                        'cc': 800,
                        'he': 1,
                        'hn': 4,
                        'hr': 0,
                        'cs': 0,
                        'ie': 1,
                        'in': 1,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'u',
                        'u': 11,
                        'aud': 3600,
                        'cap': 5200,
                        'fl': 900,
                        'ctxc': 420,
                        'ctxw': 40,
                        'ctxo': 380,
                        'ctxs': 'ready',
                        'ctxb': 'ready',
                        'src': 'ghostty',
                        'asr': 310,
                        'asrt': 310,
                        'lu': 1,
                        'ls': 1,
                        'ft': 980,
                        'llm': 1500,
                        'lst': 520,
                        'lsch': 6,
                        'ty': 32,
                        'be': 1800,
                        'post': 1510,
                        'bot': 'llm_first_token',
                        'fin': 16,
                        'raw': 17,
                        'pp': 2,
                        'psa': 1,
                        'pjs': 1,
                        'pjc': 1,
                        'prc': 10,
                        'psc': 16,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'e': 'u',
                        'u': 12,
                        'aud': 3400,
                        'cap': 5000,
                        'fl': 860,
                        'ctxc': 400,
                        'ctxw': 30,
                        'ctxo': 370,
                        'ctxs': 'ready',
                        'ctxb': 'ready',
                        'src': 'ghostty',
                        'asr': 290,
                        'asrt': 290,
                        'lu': 1,
                        'ls': 1,
                        'ft': 1040,
                        'llm': 1480,
                        'lst': 440,
                        'lsch': 5,
                        'ty': 30,
                        'be': 1750,
                        'post': 1490,
                        'bot': 'llm_first_token',
                        'fin': 14,
                        'raw': 15,
                        'pp': 1,
                        'psa': 1,
                        'pjs': 1,
                        'pjc': 1,
                        'prc': 8,
                        'psc': 14,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    digest = dictation_service.build_dictation_agent_digest(
        config,
        utterances=2,
        slowest=1,
        errors=0,
    )

    assert digest['partial_pipeline'] == {
        'instrumented': True,
        'active': True,
        'analyzed_utterances': 2,
        'active_utterances': 2,
        'preview_total': 3,
        'stable_advances_total': 2,
        'sent_total': 0,
        'skipped_total': 0,
        'skip_rate': 0,
        'jobs_started_total': 2,
        'jobs_completed_total': 2,
        'jobs_completion_rate': 100,
        'hit_utterances': 2,
        'hit_rate': 100,
        'reused_chars_total': 18,
        'reused_chars_avg': 9,
        'reused_chars_max': 10,
        'stable_chars_max': 16,
    }
    assert 'llm_first_token_slow' in digest['diagnosis']['signals']
    assert 'partial_preview_healthy' in digest['diagnosis']['signals']
    assert digest['diagnosis']['summary'] == 'LLM 首包慢；上下文未阻塞，ASR 与文本注入基本正常'
    assert digest['diagnosis']['evidence']['partial_hit_rate'] == 100
    assert digest['diagnosis']['evidence']['partial_sent_total'] == 0
    assert digest['diagnosis']['evidence']['partial_skipped_total'] == 0
    assert digest['diagnosis']['evidence']['partial_jobs_started_total'] == 2
    assert digest['diagnosis']['evidence']['partial_reused_chars_max'] == 10


def test_dictation_log_formatter_formats_helper_partial_typed_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'helper',
        '[vox-dictation] partial_typed chars=12 prefix_chars=9 deleted_chars=1 appended_chars=3 type_ms=24',
    )

    assert len(_lines(lines)) == 1
    assert 'STREAM' in _lines(lines)[0]
    assert '已同步局部文本' in _lines(lines)[0]
    assert '12字' in _lines(lines)[0]
    assert '+3' in _lines(lines)[0]
    assert '-1' in _lines(lines)[0]
    assert '24ms' in _lines(lines)[0]


def test_dictation_log_formatter_formats_helper_subtitle_overlay_line() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format('helper', '[vox-dictation] subtitle overlay enabled')

    assert len(_lines(lines)) == 1
    assert 'HUD' in _lines(lines)[0]
    assert '底部字幕预览已开启' in _lines(lines)[0]


def test_dictation_log_formatter_keeps_tty_output_compact(monkeypatch) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    formatter = dictation_service._DictationLogFormatter(_FakeTTYStream())

    rec_start = formatter.format(
        'helper',
        '[vox-dictation] recording started...',
    )
    voice = formatter.format(
        'helper',
        '[vox-dictation] voice detected; sending preroll_ms=200 peak=0.0404 rms=0.0064',
    )
    partial_text = formatter.format(
        'helper',
        '[vox-dictation] partial: 现在来试一下 LLM 的流式能力',
    )
    rec_stop = formatter.format(
        'helper',
        '[vox-dictation] recording stopped',
    )
    llm_stage = formatter.format(
        'server',
        '[session-server] dictation_stage | utterance_id=1 | stage="llm_stream" | t_rel_ms=320 | stage_ms=320 | stream_requested=true | stream_used=true | stream_chunks=3 | first_token_ms=78 | chars=26',
    )
    llm_text = formatter.format(
        'server',
        '[session-server] dictation_text utterance_id=1 stage=llm_stream text="...幸好 AI 救了我"',
    )
    ctx_prefetch = formatter.format(
        'server',
        '[session-server] dictation_context_prefetch | utterance_id=1 | source="ghostty" | app="Ghostty" | window="codex" | role="AXTextArea" | capture_ms=678 | context_chars=1200',
    )
    pre_line = formatter.format(
        'server',
        '[session-server] dictation_partial_pipeline | utterance_id=1 | state="job_started" | stable_chars=12 | completed_chars=0 | context_ready=true',
    )
    final_text = formatter.format(
        'server',
        '[session-server] dictation_text utterance_id=1 stage=final_ready text="现在来试一下 LLM 的流式能力"',
    )
    post_line = formatter.format(
        'server',
        '[session-server] dictation_postprocess | changed=true | rules_changed=false | llm_used=true | llm_ms=511 | postprocess_ms=530 | timeout_sec=8.0 | stream_requested=true | stream_used=true | stream_chunks=5 | first_token_ms=92 | provider="openai-compatible" | model="KAT-Coder" | raw_chars=19 | final_chars=18 | context_source="ghostty" | context_chars=188',
    )
    perf_line = formatter.format(
        'helper',
        '[vox-dictation] timings utterance_id=1 capture_ms=5040 flush_roundtrip_ms=3093 audio_ms=4600 warmup_ms=0 infer_ms=203 context_capture_ms=1053 context_available=true context_source=ghostty postprocess_ms=2618 llm_ms=2616 llm_used=true llm_timeout_sec=8 llm_provider=dashscope llm_model=qwen-turbo-latest backend_total_ms=2821 type_ms=56 partial_sent=5 partial_returned=5 partial_skipped=2 warmup_reason=-',
    )

    assert rec_start.live_line is not None
    assert '录音' in rec_start.live_line
    assert '转写' in rec_start.live_line
    assert '润色' in rec_start.live_line
    assert voice.live_line is not None
    assert '已收声' in voice.live_line
    assert llm_stage.lines == []
    assert llm_text.live_line is not None
    assert 'RUN #1' in llm_text.live_line
    assert '润色' in llm_text.live_line
    assert '幸好 AI 救了我' not in llm_text.live_line
    assert ctx_prefetch.lines == []
    assert pre_line.lines == []
    assert partial_text.live_line is not None
    assert '录音' in partial_text.live_line
    assert '转写' in partial_text.live_line
    assert '实时' in partial_text.live_line
    assert '现在来试一下' not in partial_text.live_line
    assert rec_stop.live_line is not None
    assert '转写' in rec_stop.live_line
    assert final_text.lines == []
    assert post_line.lines == []
    assert 'RUN #1' in _lines(perf_line)[0]
    assert '录音' in _lines(perf_line)[1]
    assert '转写' in _lines(perf_line)[2]
    assert '润色' in _lines(perf_line)[3]
    assert '输出' in _lines(perf_line)[4]


def test_resolve_partial_interval_ms_defaults_to_streaming_when_enabled() -> None:
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=False,
            subtitle_overlay=False,
            background_partial_streaming=False,
        )
        == 0
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=True,
            type_partial=False,
            subtitle_overlay=False,
            background_partial_streaming=False,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=False,
            subtitle_overlay=False,
            background_partial_streaming=True,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=True,
            subtitle_overlay=False,
            background_partial_streaming=False,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=False,
            subtitle_overlay=True,
            background_partial_streaming=False,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            0,
            verbose=True,
            type_partial=True,
            subtitle_overlay=True,
            background_partial_streaming=True,
        )
        == 0
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            600,
            verbose=False,
            type_partial=True,
            subtitle_overlay=False,
            background_partial_streaming=True,
        )
        == 600
    )


def test_launch_dictation_prepares_model_before_start(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: calls.append(('ensure_model_downloaded', spec.model_id)) or {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    popen_calls: list[list[str]] = []

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr, text=None, bufsize=None: popen_calls.append(cmd) or _PipeProc(),
    )
    monkeypatch.setattr(
        dictation_service,
        'wait_for_session_server',
        lambda host, port, timeout=60.0, server_proc=None: calls.append(('wait_for_session_server', str(port))),
    )

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        verbose=False,
    )

    assert exit_code == 0
    assert calls[0] == ('ensure_model_downloaded', 'qwen-asr-0.6b-4bit')
    assert calls[1] == ('wait_for_session_server', '8765')
    assert '--dictation-postprocess' in popen_calls[0]
    model_index = popen_calls[0].index('--model')
    assert popen_calls[0][model_index + 1] == 'qwen-asr-0.6b-4bit'
    assert '--verbose' in popen_calls[1]


def test_launch_dictation_enables_partial_streaming_in_verbose(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    popen_calls: list[list[str]] = []

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    def fake_popen(cmd, cwd, stdout, stderr, text=None, bufsize=None):
        popen_calls.append(cmd)
        if cmd and cmd[0] == str(tmp_path / 'vox-dictation'):
            return _PipeProc()
        return _PipeProc()

    monkeypatch.setattr(dictation_service.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        verbose=True,
    )

    assert exit_code == 0
    helper_cmd = popen_calls[1]
    interval_index = helper_cmd.index('--partial-interval-ms')
    assert helper_cmd[interval_index + 1] == '250'


def test_launch_dictation_keeps_background_partial_streaming_disabled_when_llm_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    config.dictation.llm.enabled = True

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    popen_calls: list[list[str]] = []

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr, text=None, bufsize=None: popen_calls.append(cmd) or _PipeProc(),
    )
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        verbose=False,
    )

    assert exit_code == 0
    helper_cmd = popen_calls[1]
    interval_index = helper_cmd.index('--partial-interval-ms')
    assert helper_cmd[interval_index + 1] == '0'


def test_launch_dictation_enables_partial_streaming_by_default_when_subtitle_overlay(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )
    popen_calls: list[list[str]] = []

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr, text=None, bufsize=None: popen_calls.append(cmd)
        or _PipeProc(),
    )
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)
    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        verbose=False,
        subtitle_overlay=True,
    )

    assert exit_code == 0
    helper_cmd = popen_calls[1]
    interval_index = helper_cmd.index('--partial-interval-ms')
    assert helper_cmd[interval_index + 1] == '250'
    assert '--subtitle-overlay' in helper_cmd
    assert '--verbose' in helper_cmd


def test_launch_dictation_passes_llm_timeout_override_to_server(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr, text=None, bufsize=None: popen_calls.append(cmd) or _PipeProc(),
    )
    monkeypatch.setattr(
        dictation_service,
        'wait_for_session_server',
        lambda host, port, timeout=60.0, server_proc=None: None,
    )

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        llm_timeout_sec=8.5,
    )

    assert exit_code == 0
    assert '--dictation-llm-timeout-sec' in popen_calls[0]
    timeout_index = popen_calls[0].index('--dictation-llm-timeout-sec')
    assert popen_calls[0][timeout_index + 1] == '8.5'
    assert '--verbose' in popen_calls[1]


def test_launch_dictation_passes_type_partial_to_helper(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    popen_calls: list[list[str]] = []

    def fake_popen(cmd, cwd, stdout, stderr, text=None, bufsize=None):
        popen_calls.append(cmd)
        return _PipeProc()

    monkeypatch.setattr(dictation_service.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        type_partial=True,
        verbose=True,
    )

    assert exit_code == 0
    helper_cmd = popen_calls[1]
    assert '--type-partial' in helper_cmd


def test_launch_dictation_passes_subtitle_overlay_to_helper_when_enabled(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))

    monkeypatch.setattr(
        dictation_service,
        'ensure_native_binary',
        lambda rebuild=False, required_flags=(): tmp_path / 'vox-dictation',
    )
    monkeypatch.setattr(dictation_service, 'pick_free_port', lambda host='127.0.0.1': 8765)
    monkeypatch.setattr(dictation_service, 'resolve_model', lambda config, model_id, kind=None: type('Spec', (), {'model_id': model_id, 'repo_id': 'repo', 'kind': 'asr'})())
    monkeypatch.setattr(
        dictation_service,
        'ensure_model_downloaded',
        lambda config, spec, allow_download=True: {'snapshot_path': str(tmp_path / 'snapshots' / 'rev')},
    )

    class _PipeProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stdout = io.StringIO('')

    popen_calls: list[list[str]] = []

    def fake_popen(cmd, cwd, stdout, stderr, text=None, bufsize=None):
        popen_calls.append(cmd)
        return _PipeProc()

    monkeypatch.setattr(dictation_service.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)

    exit_code = dictation_service.launch_dictation(
        config=config,
        lang='zh',
        model='auto',
        verbose=True,
        subtitle_overlay=True,
    )

    assert exit_code == 0
    helper_cmd = popen_calls[1]
    assert '--subtitle-overlay' in helper_cmd


def test_rotate_log_file_keeps_backup_when_oversized(tmp_path: Path) -> None:
    log_path = tmp_path / 'dictation-session.log'
    log_path.write_text('x' * 128, encoding='utf-8')

    dictation_service._rotate_log_file(log_path, max_bytes=64, backups=2)

    assert not log_path.exists()
    assert (tmp_path / 'dictation-session.log.1').read_text(encoding='utf-8') == 'x' * 128


def test_ensure_native_binary_rebuilds_when_sources_are_newer(monkeypatch, tmp_path: Path) -> None:
    project_dir = tmp_path / 'vox-dictation'
    src_dir = project_dir / 'src'
    target_dir = project_dir / 'target' / 'release'
    src_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)

    manifest = project_dir / 'Cargo.toml'
    build_rs = project_dir / 'build.rs'
    source = src_dir / 'main.rs'
    binary = target_dir / 'vox-dictation'

    manifest.write_text('[package]\nname = "vox-dictation"\nversion = "0.1.0"\n', encoding='utf-8')
    build_rs.write_text('fn main() {}\n', encoding='utf-8')
    source.write_text('fn main() {}\n', encoding='utf-8')
    binary.write_text('binary', encoding='utf-8')

    os.utime(binary, (100, 100))
    os.utime(manifest, (200, 200))
    os.utime(build_rs, (200, 200))
    os.utime(source, (200, 200))

    calls: list[list[str]] = []
    monkeypatch.setattr(dictation_service, 'native_project_dir', lambda: project_dir)
    monkeypatch.setattr(dictation_service, 'native_manifest_path', lambda: manifest)
    monkeypatch.setattr(dictation_service, 'native_binary_path', lambda: binary)
    monkeypatch.setattr(dictation_service.shutil, 'which', lambda name: '/usr/bin/cargo')
    monkeypatch.setattr(
        dictation_service.subprocess,
        'run',
        lambda cmd, cwd, check: calls.append(cmd),
    )

    result = dictation_service.ensure_native_binary()

    assert result == binary
    assert calls == [['/usr/bin/cargo', 'build', '--release', '--manifest-path', str(manifest)]]


def test_ensure_native_binary_rebuilds_when_required_flag_missing(monkeypatch, tmp_path: Path) -> None:
    project_dir = tmp_path / 'vox-dictation'
    src_dir = project_dir / 'src'
    target_dir = project_dir / 'target' / 'release'
    src_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)

    manifest = project_dir / 'Cargo.toml'
    build_rs = project_dir / 'build.rs'
    source = src_dir / 'main.rs'
    binary = target_dir / 'vox-dictation'

    manifest.write_text('[package]\nname = "vox-dictation"\nversion = "0.1.0"\n', encoding='utf-8')
    build_rs.write_text('fn main() {}\n', encoding='utf-8')
    source.write_text('fn main() {}\n', encoding='utf-8')
    binary.write_text('binary', encoding='utf-8')

    os.utime(binary, (200, 200))
    os.utime(manifest, (100, 100))
    os.utime(build_rs, (100, 100))
    os.utime(source, (100, 100))

    calls: list[list[str]] = []
    monkeypatch.setattr(dictation_service, 'native_project_dir', lambda: project_dir)
    monkeypatch.setattr(dictation_service, 'native_manifest_path', lambda: manifest)
    monkeypatch.setattr(dictation_service, 'native_binary_path', lambda: binary)
    monkeypatch.setattr(dictation_service.shutil, 'which', lambda name: '/usr/bin/cargo')
    monkeypatch.setattr(
        dictation_service.subprocess,
        'check_output',
        lambda cmd, text, timeout: 'Usage: vox-dictation --server-url <SERVER_URL>\n      --verbose\n',
    )
    monkeypatch.setattr(
        dictation_service.subprocess,
        'run',
        lambda cmd, cwd, check: calls.append(cmd),
    )

    result = dictation_service.ensure_native_binary(required_flags=('--subtitle-overlay',))

    assert result == binary
    assert calls == [['/usr/bin/cargo', 'build', '--release', '--manifest-path', str(manifest)]]
