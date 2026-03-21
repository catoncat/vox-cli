from __future__ import annotations

import io
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

    assert any('开始润色' in line for line in stage_lines)
    assert any('openai-compatible / KAT-Coder' in line for line in stage_lines)
    assert any('context 120字' in line for line in stage_lines)
    assert len(text_lines) == 1
    assert 'LLM输入' in text_lines[0]
    assert '你好，世界' in text_lines[0]
    assert len(diff_lines) == 1
    assert 'Diff' in diff_lines[0]
    assert '[-，-][+, +]' in diff_lines[0]


def test_dictation_log_formatter_formats_hotword_stage() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'server',
        '[session-server] dictation_stage | utterance_id=1 | stage="hotwords_done" | t_rel_ms=3 | stage_ms=1 | chars=12 | changed=true | matches=2 | replacements="潮上->潮汕 x1; ColdX CLI->Codex CLI x1"',
    )

    assert 'HOT #1' in lines[0]
    assert '热词纠正' in lines[0]
    assert 'matches 2' in lines[0]
    assert any('潮上->潮汕 x1' in line for line in lines)


def test_dictation_log_formatter_formats_config_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    config_lines = formatter.format(
        'server',
        '[session-server] dictation_config | llm_enabled=true | llm_provider="dashscope" | llm_model="qwen-flash" | llm_timeout_sec=4.0 | context_enabled=true | context_max_chars=1200 | hotwords_enabled=true | hotword_entries=3 | rewrite_aliases=true | case_sensitive=false | hints_enabled=true | hint_count=1',
    )
    hotwords_lines = formatter.format(
        'server',
        '[session-server] dictation_config_hotwords | text="潮汕 <- 潮上 | Codex CLI <- ColdX CLI, CodeX CLI, ColdX"',
    )
    hints_lines = formatter.format(
        'server',
        '[session-server] dictation_config_hints | text="说话人前后鼻音不分，优先纠正 an/ang、en/eng 混淆。"',
    )

    assert 'CFG' in config_lines[0]
    assert 'llm on' in config_lines[0]
    assert 'hotwords on' in config_lines[0]
    assert any('dashscope / qwen-flash' in line for line in config_lines)
    assert any('rewrite on' in line for line in config_lines)
    assert len(hotwords_lines) == 1
    assert '热词表' in hotwords_lines[0]
    assert '潮汕 <- 潮上' in hotwords_lines[0]
    assert len(hints_lines) == 1
    assert '提示词' in hints_lines[0]


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
        '[session-server] dictation_postprocess | changed=true | rules_changed=false | llm_used=true | llm_ms=511 | postprocess_ms=530 | timeout_sec=8.0 | provider="openai-compatible" | model="KAT-Coder" | raw_chars=19 | final_chars=18 | context_source="ghostty" | context_chars=188',
    )

    assert 'CTX #2' in context_lines[0]
    assert '上下文已捕获' in context_lines[0]
    assert 'context 188字' in context_lines[0]
    assert any('source ghostty' in line for line in context_lines)
    assert len(excerpt_lines) == 1
    assert '上下文' in excerpt_lines[0]
    assert '当前终端里的上下文' in excerpt_lines[0]
    assert 'POST' in post_lines[0]
    assert 'llm yes' in post_lines[0]
    assert '19->18字' in post_lines[0]
    assert any('openai-compatible / KAT-Coder' in line for line in post_lines)


def test_dictation_log_formatter_formats_helper_timing_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'helper',
        '[vox-dictation] timings utterance_id=3 capture_ms=10624 flush_roundtrip_ms=973 audio_ms=10100 warmup_ms=0 infer_ms=361 postprocess_ms=607 llm_ms=606 llm_used=false llm_timeout_sec=8 llm_provider=openai-compatible llm_model=KAT-Coder backend_total_ms=968 type_ms=76 warmup_reason=-',
    )

    assert 'PERF #3' in lines[0]
    assert 'capture 10.62s' in lines[0]
    assert 'llm 606ms' in lines[0]
    assert 'backend 968ms' in lines[1]
    assert 'timeout 8s' in lines[1]
    assert 'openai-compatible / KAT-Coder' in lines[2]


def test_dictation_log_formatter_formats_helper_partial_typed_lines() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format(
        'helper',
        '[vox-dictation] partial_typed chars=12 prefix_chars=9 deleted_chars=1 appended_chars=3 type_ms=24',
    )

    assert len(lines) == 1
    assert 'STREAM' in lines[0]
    assert '已同步局部文本' in lines[0]
    assert '12字' in lines[0]
    assert '+3' in lines[0]
    assert '-1' in lines[0]
    assert '24ms' in lines[0]


def test_dictation_log_formatter_formats_helper_subtitle_overlay_line() -> None:
    formatter = dictation_service._DictationLogFormatter(_FakeStream())

    lines = formatter.format('helper', '[vox-dictation] subtitle overlay enabled')

    assert len(lines) == 1
    assert 'HUD' in lines[0]
    assert '底部字幕预览已开启' in lines[0]


def test_resolve_partial_interval_ms_defaults_to_streaming_when_enabled() -> None:
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=False,
            subtitle_overlay=False,
        )
        == 0
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=True,
            type_partial=False,
            subtitle_overlay=False,
        )
        == 0
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=True,
            subtitle_overlay=False,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            None,
            verbose=False,
            type_partial=False,
            subtitle_overlay=True,
        )
        == 250
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            0,
            verbose=True,
            type_partial=True,
            subtitle_overlay=True,
        )
        == 0
    )
    assert (
        dictation_service._resolve_partial_interval_ms(
            600,
            verbose=False,
            type_partial=True,
            subtitle_overlay=False,
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

    fake_proc = _FakeProc()
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr: popen_calls.append(cmd) or fake_proc,
    )
    monkeypatch.setattr(
        dictation_service,
        'wait_for_session_server',
        lambda host, port, timeout=60.0, server_proc=None: calls.append(('wait_for_session_server', str(port))),
    )
    monkeypatch.setattr(
        dictation_service.subprocess,
        'call',
        lambda cmd, cwd: calls.append(('subprocess.call', cmd[0])) or 0,
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
    assert calls[2][0] == 'subprocess.call'
    assert '--dictation-postprocess' in popen_calls[0]
    model_index = popen_calls[0].index('--model')
    assert popen_calls[0][model_index + 1] == 'qwen-asr-0.6b-4bit'


def test_launch_dictation_keeps_partial_streaming_disabled_in_verbose_only(monkeypatch, tmp_path: Path) -> None:
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

    fake_proc = _FakeProc()
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
        lambda cmd, cwd, stdout, stderr: popen_calls.append(cmd) or _PipeProc(),
    )
    monkeypatch.setattr(dictation_service, 'wait_for_session_server', lambda host, port, timeout=60.0, server_proc=None: None)
    monkeypatch.setattr(dictation_service.subprocess, 'call', lambda cmd, cwd: popen_calls.append(cmd) or 0)

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

    fake_proc = _FakeProc()
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        dictation_service.subprocess,
        'Popen',
        lambda cmd, cwd, stdout, stderr: popen_calls.append(cmd) or fake_proc,
    )
    monkeypatch.setattr(
        dictation_service,
        'wait_for_session_server',
        lambda host, port, timeout=60.0, server_proc=None: None,
    )
    monkeypatch.setattr(dictation_service.subprocess, 'call', lambda cmd, cwd: 0)

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
