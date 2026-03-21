from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vox_cli.config import RuntimeConfig, VoxConfig
from vox_cli import main


runner = CliRunner()


def _stub_runtime(monkeypatch, tmp_path: Path) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path)))
    monkeypatch.setattr(main, 'load_config', lambda: config)
    monkeypatch.setattr(main, 'ensure_runtime_dirs', lambda config: None)
    monkeypatch.setattr(main, 'get_db_path', lambda config: tmp_path / 'vox.db')
    monkeypatch.setattr(main, 'init_db', lambda path: None)
    monkeypatch.setattr(main.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(main.platform, 'machine', lambda: 'arm64')


def test_dictation_legacy_invocation_launches(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []
    ready_messages: list[str] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        ready_messages.append('dictation ready')
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', '--lang', 'zh'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['lang'] == 'zh'
    assert calls[0]['type_partial'] is False
    assert calls[0]['subtitle_overlay'] is False
    assert calls[0]['llm_timeout_sec'] is None
    assert ready_messages == ['dictation ready']
    assert 'dictation ready' in result.output


def test_dictation_start_subcommand_launches(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []
    ready_messages: list[str] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        ready_messages.append('dictation ready')
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', 'start', '--lang', 'zh'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['lang'] == 'zh'
    assert calls[0]['type_partial'] is False
    assert calls[0]['subtitle_overlay'] is False
    assert calls[0]['llm_timeout_sec'] is None
    assert ready_messages == ['dictation ready']
    assert 'dictation ready' in result.output


def test_dictation_cli_passes_llm_timeout_override(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', '--lang', 'zh', '--llm-timeout-sec', '8.5'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['llm_timeout_sec'] == 8.5


def test_dictation_cli_passes_type_partial_flag(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', 'start', '--lang', 'zh', '--type-partial'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['type_partial'] is True
    assert calls[0]['subtitle_overlay'] is False


def test_dictation_cli_can_disable_type_partial(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', 'start', '--lang', 'zh', '--no-type-partial'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['type_partial'] is False
    assert calls[0]['subtitle_overlay'] is False


def test_dictation_cli_can_disable_subtitle_overlay(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', 'start', '--lang', 'zh', '--no-subtitle-overlay'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['type_partial'] is False
    assert calls[0]['subtitle_overlay'] is False


def test_dictation_cli_can_enable_subtitle_overlay(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation(**kwargs) -> int:
        kwargs['on_ready']('dictation ready')
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main, 'launch_dictation', fake_launch_dictation)

    result = runner.invoke(main.app, ['dictation', 'start', '--lang', 'zh', '--subtitle-overlay'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['type_partial'] is False
    assert calls[0]['subtitle_overlay'] is True


def test_dictation_ui_command_launches_local_config_panel(monkeypatch, tmp_path: Path) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls: list[dict] = []

    def fake_launch_dictation_ui(config, *, host: str, port: int | None, open_browser: bool) -> str:
        calls.append(
            {
                'config': config,
                'host': host,
                'port': port,
                'open_browser': open_browser,
            }
        )
        return f'http://{host}:{port}'

    monkeypatch.setattr(main, 'launch_dictation_ui', fake_launch_dictation_ui)

    result = runner.invoke(main.app, ['dictation', 'ui', '--port', '8769', '--no-open'])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]['host'] == '127.0.0.1'
    assert calls[0]['port'] == 8769
    assert calls[0]['open_browser'] is False
    assert 'Dictation UI ready at http://127.0.0.1:8769' in result.output
