from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.resources as resources
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, TextIO
import uuid

import websockets

from ..config import VoxConfig, get_home_dir, resolve_dictation_model_id
from .model_service import ensure_model_downloaded, resolve_model


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def native_project_dir() -> Path:
    dev_path = repo_root() / 'native' / 'vox-dictation'
    if dev_path.exists():
        return dev_path
    packaged = resources.files('vox_cli').joinpath('native/vox-dictation')
    return Path(str(packaged))


def native_manifest_path() -> Path:
    return native_project_dir() / 'Cargo.toml'


def native_binary_path() -> Path:
    return native_project_dir() / 'target' / 'release' / 'vox-dictation'


def dictation_logs_dir(config: VoxConfig) -> Path:
    return get_home_dir(config) / 'logs'


def dictation_session_log_path(config: VoxConfig) -> Path:
    return dictation_logs_dir(config) / 'dictation-session.log'


def ensure_dictation_dirs(config: VoxConfig) -> None:
    dictation_logs_dir(config).mkdir(parents=True, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _native_build_inputs() -> list[Path]:
    project_dir = native_project_dir()
    candidates = [
        project_dir / 'Cargo.toml',
        project_dir / 'build.rs',
        *sorted((project_dir / 'src').rglob('*.rs')),
    ]
    return [path for path in candidates if path.exists()]


def _binary_needs_rebuild(binary: Path) -> bool:
    if not binary.exists():
        return True

    try:
        binary_mtime = binary.stat().st_mtime
    except OSError:
        return True

    for path in _native_build_inputs():
        try:
            if path.stat().st_mtime > binary_mtime:
                return True
        except OSError:
            continue
    return False


def _binary_supports_required_flags(binary: Path, required_flags: tuple[str, ...]) -> bool:
    if not required_flags:
        return True
    try:
        help_output = subprocess.check_output([str(binary), '--help'], text=True, timeout=5)
    except Exception:
        return False
    return all(flag in help_output for flag in required_flags)


def _rotate_log_file(path: Path, *, max_bytes: int, backups: int) -> None:
    if max_bytes <= 0 or not path.exists():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    if backups <= 0:
        path.unlink(missing_ok=True)
        return

    for index in range(backups, 0, -1):
        backup_path = path.with_name(f'{path.name}.{index}')
        if index == backups:
            backup_path.unlink(missing_ok=True)
            continue
        next_backup = path.with_name(f'{path.name}.{index + 1}')
        if backup_path.exists():
            backup_path.replace(next_backup)

    path.replace(path.with_name(f'{path.name}.1'))


def _dictation_log_limits(config: VoxConfig) -> tuple[int, int]:
    return (
        max(64 * 1024, int(config.runtime.dictation_log_max_bytes)),
        max(0, int(config.runtime.dictation_log_backups)),
    )


def _prepare_dictation_log(path: Path, config: VoxConfig) -> None:
    max_bytes, backups = _dictation_log_limits(config)
    _rotate_log_file(path, max_bytes=max_bytes, backups=backups)


def _write_log_event(path: Path, *, event: str, **fields: object) -> None:
    payload = {
        'ts': _utc_now(),
        'event': event,
        **fields,
    }
    with path.open('a', encoding='utf-8') as handle:
        handle.write(f'[dictation] {json.dumps(payload, ensure_ascii=False)}\n')


def _should_echo_server_line(line: str) -> bool:
    return line.startswith('[session-server]')


def _should_echo_helper_line(line: str) -> bool:
    return line.startswith('[vox-dictation]')


_ANSI_RESET = '\033[0m'
_TOKEN_RE = re.compile(r'[^\s=]+=(?:"(?:\\.|[^"])*"|[^\s]+)|[^\s]+')
_DIFF_MARKER_RE = re.compile(r'(\[-.*?-\]|\[\+.*?\+\])')
_DEFAULT_VERBOSE_PARTIAL_INTERVAL_MS = 250


def _supports_color(stream: TextIO) -> bool:
    if os.getenv('NO_COLOR') is not None:
        return False
    if os.getenv('CLICOLOR_FORCE') == '1':
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _style(text: str, code: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f'\033[{code}m{text}{_ANSI_RESET}'


def _parse_tokens(payload: str) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    extras: list[str] = []
    for match in _TOKEN_RE.finditer(payload):
        token = match.group(0)
        if '=' not in token:
            extras.append(token)
            continue
        key, value = token.split('=', 1)
        if value.startswith('"'):
            try:
                fields[key] = str(json.loads(value))
                continue
            except Exception:
                fields[key] = value.strip('"')
                continue
        fields[key] = value
    return fields, extras


def _resolve_partial_interval_ms(
    partial_interval_ms: int | None,
    *,
    verbose: bool,
    type_partial: bool,
    subtitle_overlay: bool,
) -> int:
    if partial_interval_ms is not None:
        return max(0, int(partial_interval_ms))
    if type_partial or subtitle_overlay:
        return _DEFAULT_VERBOSE_PARTIAL_INTERVAL_MS
    return 0


class _DictationLogFormatter:
    _STAGE_META = {
        'asr_final': ('ASR', '1;34', '识别完成'),
        'hotwords_done': ('HOT', '1;36', '热词纠正'),
        'rules_done': ('RULES', '1;33', '规则处理'),
        'llm_start': ('LLM', '1;35', '开始润色'),
        'llm_done': ('LLM', '1;35', '润色完成'),
        'llm_error': ('LLM', '1;31', '润色失败'),
        'final_ready': ('DONE', '1;32', '最终输出'),
    }
    _TEXT_LABELS = {
        'asr_final': '原文',
        'hotwords_done': '热词后',
        'rules_done': '规则后',
        'llm_start': 'LLM输入',
        'llm_done': 'LLM输出',
        'final_ready': '最终',
    }

    def __init__(self, stream: TextIO) -> None:
        self._color = _supports_color(stream)

    def format(self, source: str, raw_line: str) -> list[str]:
        line = raw_line.rstrip('\n')
        if source == 'server':
            return self._format_server_line(line)
        return self._format_helper_line(line)

    def _stamp(self) -> str:
        value = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        return _style(value, '2', enabled=self._color)

    def _badge(self, label: str, code: str) -> str:
        return _style(f'[{label}]', code, enabled=self._color)

    def _detail(self, label: str, value: str, code: str) -> str:
        styled_label = _style(label.rjust(7), f'2;{code}' if ';' not in code else code.replace('1;', '2;'), enabled=self._color)
        return f'{" " * 14}{styled_label}  {value}'

    def _truthy(self, value: str | None) -> bool:
        if value is None:
            return False
        return value.lower() in {'1', 'true', 'yes', 'on'}

    def _format_server_line(self, line: str) -> list[str]:
        if not _should_echo_server_line(line):
            return [line]

        payload = line[len('[session-server]') :].strip()
        if payload.startswith('warmup completed '):
            return []

        if payload.startswith('transcribe '):
            fields, _ = _parse_tokens(payload[len('transcribe ') :])
            if fields.get('partial') == 'True':
                return []
            utterance = fields.get('utterance_id', '?')
            audio_ms = int(fields.get('audio_ms', '0') or 0)
            infer_ms = int(fields.get('infer_ms', '0') or 0)
            total_ms = int(fields.get('total_ms', '0') or 0)
            parts = [
                f'音频 {audio_ms / 1000:.2f}s',
                f'infer {infer_ms}ms',
                f'total {total_ms}ms',
            ]
            return [
                f'{self._stamp()} {self._badge(f"ASR #{utterance}", "1;34")} {" | ".join(parts)}'
            ]

        if payload.startswith('dictation_config '):
            fields, _ = _parse_tokens(payload[len('dictation_config ') :])
            parts = [
                f'llm {"on" if self._truthy(fields.get("llm_enabled")) else "off"}',
                f'context {"on" if self._truthy(fields.get("context_enabled")) else "off"}',
                f'hotwords {"on" if self._truthy(fields.get("hotwords_enabled")) else "off"}',
                f'hints {"on" if self._truthy(fields.get("hints_enabled")) else "off"}',
            ]
            if fields.get('context_max_chars'):
                parts.append(f'ctx {fields["context_max_chars"]}字')
            if fields.get('hotword_entries'):
                parts.append(f'hotwords {fields["hotword_entries"]}')
            if fields.get('hint_count'):
                parts.append(f'hints {fields["hint_count"]}')
            lines = [
                f'{self._stamp()} {self._badge("CFG", "1;36")} {" | ".join(parts)}'
            ]
            model_parts = [part for part in (fields.get('llm_provider'), fields.get('llm_model')) if part and part != '-']
            if model_parts:
                lines.append(self._detail('模型', ' / '.join(model_parts), '1;36'))
            if fields.get('llm_timeout_sec'):
                lines.append(self._detail('超时', f'{fields["llm_timeout_sec"]}s', '1;36'))
            if self._truthy(fields.get('hotwords_enabled')):
                hotword_mode = []
                if 'rewrite_aliases' in fields:
                    hotword_mode.append(f'rewrite {"on" if self._truthy(fields.get("rewrite_aliases")) else "off"}')
                if 'case_sensitive' in fields:
                    hotword_mode.append(f'case {"on" if self._truthy(fields.get("case_sensitive")) else "off"}')
                if hotword_mode:
                    lines.append(self._detail('热词', ' | '.join(hotword_mode), '1;36'))
            return lines

        if payload.startswith('dictation_config_hotwords '):
            fields, _ = _parse_tokens(payload[len('dictation_config_hotwords ') :])
            return [self._detail('热词表', fields.get('text', ''), '1;36')]

        if payload.startswith('dictation_config_hints '):
            fields, _ = _parse_tokens(payload[len('dictation_config_hints ') :])
            return [self._detail('提示词', fields.get('text', ''), '1;36')]

        if payload.startswith('dictation_stage '):
            fields, extras = _parse_tokens(payload[len('dictation_stage ') :])
            stage = fields.get('stage', '-')
            label, code, title = self._STAGE_META.get(stage, ('STAGE', '1;37', stage))
            utterance = fields.get('utterance_id', '?')
            rel_ms = fields.get('t_rel_ms')
            rel = f't+{rel_ms}ms' if rel_ms is not None else next((item for item in extras if item.startswith('t+')), 't+0ms')
            parts = [title]
            if 'chars' in fields:
                parts.append(f'{fields["chars"]}字')
            if 'changed' in fields:
                parts.append(f'changed {"yes" if self._truthy(fields["changed"]) else "no"}')
            if 'llm_used' in fields:
                parts.append(f'llm {"yes" if self._truthy(fields["llm_used"]) else "no"}')
            if 'asr_infer_ms' in fields:
                parts.append(f'infer {fields["asr_infer_ms"]}ms')
            if 'asr_total_ms' in fields:
                parts.append(f'total {fields["asr_total_ms"]}ms')
            if 'stage_ms' in fields:
                parts.append(f'stage {fields["stage_ms"]}ms')
            if 'postprocess_ms' in fields:
                parts.append(f'post {fields["postprocess_ms"]}ms')
            if 'timeout_sec' in fields:
                parts.append(f'timeout {fields["timeout_sec"]}s')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            if 'context_selected_chars' in fields and fields['context_selected_chars'] != '0':
                parts.append(f'selected {fields["context_selected_chars"]}字')
            if 'matches' in fields and fields['matches'] != '0':
                parts.append(f'matches {fields["matches"]}')
            if 'hotword_entries' in fields and fields['hotword_entries'] != '0':
                parts.append(f'hotwords {fields["hotword_entries"]}')
            if 'hotword_matches' in fields and fields['hotword_matches'] != '0':
                parts.append(f'hit {fields["hotword_matches"]}')
            if 'hint_count' in fields and fields['hint_count'] != '0':
                parts.append(f'hints {fields["hint_count"]}')

            lines = [
                f'{self._stamp()} {self._badge(f"{label} #{utterance} {rel}", code)} {" | ".join(parts)}'
            ]
            provider = fields.get('provider')
            model = fields.get('model')
            model_parts = [part for part in (provider, model) if part and part != '-']
            if model_parts:
                lines.append(
                    self._detail(
                        '模型',
                        ' / '.join(model_parts),
                        code,
                    )
                )
            if fields.get('context_source'):
                lines.append(self._detail('上下文', fields['context_source'], code))
            if fields.get('replacements'):
                lines.append(self._detail('替换', fields['replacements'], code))
            if 'error' in fields:
                lines.append(self._detail('错误', fields['error'], code))
            return lines

        if payload.startswith('dictation_context '):
            fields, _ = _parse_tokens(payload[len('dictation_context ') :])
            utterance = fields.get('utterance_id', '?')
            state = fields.get('state', '-')
            code = '1;36'
            title = {
                'ready': '上下文已捕获',
                'empty': '未拿到焦点上下文',
                'error': '上下文采集失败',
                'disabled': '上下文未启用',
            }.get(state, f'上下文 {state}')
            parts = [title]
            if 'capture_ms' in fields:
                parts.append(f'{fields["capture_ms"]}ms')
            if 'selected_chars' in fields and fields['selected_chars'] != '0':
                parts.append(f'selected {fields["selected_chars"]}字')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            lines = [
                f'{self._stamp()} {self._badge(f"CTX #{utterance}", code)} {" | ".join(parts)}'
            ]
            meta_parts = []
            if fields.get('source'):
                meta_parts.append(f'source {fields["source"]}')
            if fields.get('app'):
                meta_parts.append(f'app {fields["app"]}')
            if fields.get('window'):
                meta_parts.append(f'window {fields["window"]}')
            if fields.get('role'):
                meta_parts.append(f'role {fields["role"]}')
            if fields.get('url'):
                meta_parts.append(f'url {fields["url"]}')
            if meta_parts:
                lines.append(self._detail('来源', ' | '.join(meta_parts), code))
            if fields.get('error'):
                lines.append(self._detail('错误', fields['error'], '1;31'))
            return lines

        if payload.startswith('dictation_context_selected '):
            fields, _ = _parse_tokens(payload[len('dictation_context_selected ') :])
            return [self._detail('选中文本', fields.get('text', ''), '1;36')]

        if payload.startswith('dictation_context_excerpt '):
            fields, _ = _parse_tokens(payload[len('dictation_context_excerpt ') :])
            return [self._detail('上下文', fields.get('text', ''), '1;36')]

        if payload.startswith('dictation_postprocess_error '):
            fields, _ = _parse_tokens(payload[len('dictation_postprocess_error ') :])
            parts = ['润色失败']
            if 'llm_ms' in fields:
                parts.append(f'{fields["llm_ms"]}ms')
            if 'timeout_sec' in fields:
                parts.append(f'timeout {fields["timeout_sec"]}s')
            lines = [
                f'{self._stamp()} {self._badge("LLM ERR", "1;31")} {" | ".join(parts)}'
            ]
            model_parts = [part for part in (fields.get('provider'), fields.get('model')) if part and part != '-']
            if model_parts:
                lines.append(self._detail('模型', ' / '.join(model_parts), '1;31'))
            if fields.get('llm_error'):
                lines.append(self._detail('错误', fields['llm_error'], '1;31'))
            return lines

        if payload.startswith('dictation_postprocess '):
            fields, _ = _parse_tokens(payload[len('dictation_postprocess ') :])
            parts = [
                f'changed {"yes" if self._truthy(fields.get("changed")) else "no"}',
                f'llm {"yes" if self._truthy(fields.get("llm_used")) else "no"}',
            ]
            if 'postprocess_ms' in fields:
                parts.append(f'post {fields["postprocess_ms"]}ms')
            if 'llm_ms' in fields:
                parts.append(f'llm {fields["llm_ms"]}ms')
            if 'raw_chars' in fields and 'final_chars' in fields:
                parts.append(f'{fields["raw_chars"]}->{fields["final_chars"]}字')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            lines = [
                f'{self._stamp()} {self._badge("POST", "1;32")} {" | ".join(parts)}'
            ]
            model_parts = [part for part in (fields.get('provider'), fields.get('model')) if part and part != '-']
            if model_parts:
                lines.append(self._detail('模型', ' / '.join(model_parts), '1;32'))
            if fields.get('context_source'):
                lines.append(self._detail('上下文', fields['context_source'], '1;32'))
            if fields.get('hint_count') and fields['hint_count'] != '0':
                lines.append(self._detail('提示', f'{fields["hint_count"]} 条', '1;32'))
            return lines

        if payload.startswith('dictation_text '):
            fields, _ = _parse_tokens(payload[len('dictation_text ') :])
            stage = fields.get('stage', '-')
            text = fields.get('text', '')
            label, code, _ = self._STAGE_META.get(stage, ('TEXT', '1;37', stage))
            return [self._detail(self._TEXT_LABELS.get(stage, label), text, code)]

        if payload.startswith('dictation_diff '):
            fields, _ = _parse_tokens(payload[len('dictation_diff ') :])
            stage = fields.get('stage', '-')
            diff = fields.get('diff', '')
            _, code, _ = self._STAGE_META.get(stage, ('DIFF', '1;37', stage))
            return [self._detail('Diff', self._colorize_diff(diff), code)]

        return [line]

    def _format_helper_line(self, line: str) -> list[str]:
        if not _should_echo_helper_line(line):
            return [line]

        payload = line[len('[vox-dictation]') :].strip()
        if payload == 'backend ready':
            return [f'{self._stamp()} {self._badge("BACKEND", "1;36")} 会话后端已就绪']
        if payload == 'subtitle overlay enabled':
            return [f'{self._stamp()} {self._badge("HUD", "1;35")} 底部字幕预览已开启']
        if payload.startswith('ready '):
            return [f'{self._stamp()} {self._badge("READY", "1;36")} {payload}']
        if payload == 'recording started...':
            return [f'{self._stamp()} {self._badge("MIC", "1;36")} 开始录音']
        if payload == 'recording stopped':
            return [f'{self._stamp()} {self._badge("MIC", "1;36")} 结束录音']
        if payload == 'recording cancelled':
            return [f'{self._stamp()} {self._badge("MIC", "1;33")} 已取消本次录音']
        if payload.startswith('native sample rate: '):
            return [self._detail('采样率', payload.split(': ', 1)[1], '1;36')]
        if payload.startswith('engine_start_ms='):
            return [self._detail('启动', f'{payload.split("=", 1)[1]}ms', '1;36')]
        if payload.startswith('voice detected; sending '):
            fields, _ = _parse_tokens(payload.split('sending ', 1)[1])
            parts = []
            if 'preroll_ms' in fields:
                parts.append(f'preroll {fields["preroll_ms"]}ms')
            if 'peak' in fields:
                parts.append(f'peak {fields["peak"]}')
            if 'rms' in fields:
                parts.append(f'rms {fields["rms"]}')
            return [
                f'{self._stamp()} {self._badge("MIC", "1;36")} 检测到语音'
                + (f' | {" | ".join(parts)}' if parts else '')
            ]
        if payload.startswith('backend_warmup '):
            fields, _ = _parse_tokens(payload[len('backend_warmup ') :])
            parts = [f'status {fields.get("status", "-")}']
            if 'elapsed_ms' in fields:
                parts.append(f'{fields["elapsed_ms"]}ms')
            if 'reason' in fields:
                parts.append(fields['reason'])
            return [f'{self._stamp()} {self._badge("WARMUP", "1;36")} {" | ".join(parts)}']
        if payload.startswith('partial: '):
            return [self._detail('局部', payload.split(': ', 1)[1], '1;34')]
        if payload.startswith('partial_typed '):
            fields, _ = _parse_tokens(payload[len('partial_typed ') :])
            parts = []
            if 'chars' in fields:
                parts.append(f'{fields["chars"]}字')
            if 'appended_chars' in fields:
                parts.append(f'+{fields["appended_chars"]}')
            if 'deleted_chars' in fields and fields['deleted_chars'] != '0':
                parts.append(f'-{fields["deleted_chars"]}')
            if 'prefix_chars' in fields and fields['prefix_chars'] != '0':
                parts.append(f'prefix {fields["prefix_chars"]}')
            if 'type_ms' in fields:
                parts.append(f'{fields["type_ms"]}ms')
            return [
                f'{self._stamp()} {self._badge("STREAM", "1;34")} 已同步局部文本'
                + (f' | {" | ".join(parts)}' if parts else '')
            ]
        if payload.startswith('final: '):
            return [
                f'{self._stamp()} {self._badge("TYPE", "1;32")} 开始输入最终文本',
                self._detail('文本', payload.split(': ', 1)[1], '1;32'),
            ]
        if payload.startswith('timings '):
            fields, _ = _parse_tokens(payload[len('timings ') :])
            utterance = fields.get('utterance_id', '?')
            capture_ms = int(fields.get('capture_ms', '0') or 0)
            flush_ms = int(fields.get('flush_roundtrip_ms', '0') or 0)
            infer_ms = int(fields.get('infer_ms', '0') or 0)
            post_ms = int(fields.get('postprocess_ms', '0') or 0)
            llm_ms = int(fields.get('llm_ms', '0') or 0)
            type_ms = int(fields.get('type_ms', '0') or 0)
            backend_ms = int(fields.get('backend_total_ms', '0') or 0)
            head_parts = [
                f'capture {capture_ms / 1000:.2f}s',
                f'flush {flush_ms}ms',
                f'asr {infer_ms}ms',
                f'post {post_ms}ms',
                f'llm {llm_ms}ms',
                f'type {type_ms}ms',
            ]
            detail_parts = [
                f'audio {int(fields.get("audio_ms", "0") or 0) / 1000:.2f}s',
                f'backend {backend_ms}ms',
                f'llm {"yes" if fields.get("llm_used") == "true" else "no"}',
            ]
            if fields.get('llm_timeout_sec') and fields.get('llm_timeout_sec') != '0':
                detail_parts.append(f'timeout {fields["llm_timeout_sec"]}s')
            warmup_reason = fields.get('warmup_reason')
            if warmup_reason and warmup_reason != '-':
                detail_parts.append(f'warmup {warmup_reason}')
            provider = fields.get('llm_provider')
            model = fields.get('llm_model')
            lines = [
                f'{self._stamp()} {self._badge(f"PERF #{utterance}", "1;36")} {" | ".join(head_parts)}',
                self._detail('细节', ' | '.join(detail_parts), '1;36'),
            ]
            model_parts = [part for part in (provider, model) if part and part != '-']
            if model_parts:
                lines.append(
                    self._detail(
                        '模型',
                        ' / '.join(model_parts),
                        '1;36',
                    )
                )
            return lines
        return [line]

    def _colorize_diff(self, diff: str) -> str:
        if not self._color or not diff:
            return diff

        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            if token.startswith('[-'):
                return _style(token, '31', enabled=True)
            return _style(token, '32', enabled=True)

        return _DIFF_MARKER_RE.sub(replace, diff)


def _relay_process_output(
    stream: TextIO,
    log_handle: TextIO,
    *,
    source: str,
    echo: bool,
    lock: threading.Lock,
    formatter: _DictationLogFormatter | None = None,
) -> None:
    for line in iter(stream.readline, ''):
        with lock:
            log_handle.write(line)
            log_handle.flush()
        if not echo:
            continue
        if source == 'server' and not _should_echo_server_line(line):
            continue
        if source == 'helper' and not _should_echo_helper_line(line):
            continue
        rendered_lines = formatter.format(source, line) if formatter is not None else [line.rstrip('\n')]
        if not rendered_lines:
            continue
        with lock:
            for rendered in rendered_lines:
                sys.stderr.write(f'{rendered}\n')
            sys.stderr.flush()


def _helper_version(binary: Path) -> str:
    try:
        return subprocess.check_output([str(binary), '--version'], text=True, timeout=5).strip()
    except Exception:
        return 'unknown'


def _helper_mtime(binary: Path) -> str:
    try:
        return datetime.fromtimestamp(binary.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return 'unknown'


async def _probe_session_server(ws_url: str) -> None:
    async with websockets.connect(ws_url, open_timeout=1.0, max_size=None) as websocket:
        message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
        payload = json.loads(message)
        if payload.get('status') != 'ready':
            raise RuntimeError(f'unexpected session-server hello: {payload}')


def wait_for_session_server(
    host: str,
    port: int,
    *,
    timeout: float = 60.0,
    server_proc: subprocess.Popen | None = None,
) -> None:
    ws_url = f'ws://{host}:{port}'
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        if server_proc is not None and server_proc.poll() is not None:
            raise RuntimeError(
                f'Dictation session server exited before becoming ready (code {server_proc.returncode})'
            )
        try:
            asyncio.run(_probe_session_server(ws_url))
            return
        except Exception as error:
            last_error = error
            time.sleep(0.15)
    raise RuntimeError(
        f'Timed out waiting for dictation session server on {host}:{port}: {last_error}'
    )


def ensure_native_binary(
    *,
    rebuild: bool = False,
    required_flags: tuple[str, ...] = (),
) -> Path:
    manifest = native_manifest_path()
    binary = native_binary_path()

    if not manifest.exists():
        raise RuntimeError(f'Native dictation manifest not found: {manifest}')

    if rebuild or _binary_needs_rebuild(binary) or not _binary_supports_required_flags(binary, required_flags):
        cargo = shutil.which('cargo')
        if not cargo:
            raise RuntimeError('`cargo` not found; install Rust toolchain first')
        subprocess.run(
            [cargo, 'build', '--release', '--manifest-path', str(manifest)],
            cwd=native_project_dir(),
            check=True,
        )

    if not binary.exists():
        raise RuntimeError(f'Native dictation binary missing after build: {binary}')
    return binary


def pick_free_port(host: str = '127.0.0.1') -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def tail_session_log(config: VoxConfig, lines: int = 80) -> str:
    path = dictation_session_log_path(config)
    if not path.exists():
        return ''
    with path.open('r', encoding='utf-8', errors='ignore') as handle:
        return ''.join(deque(handle, maxlen=lines)).strip()


def launch_dictation(
    config: VoxConfig,
    lang: str,
    model: str | None,
    host: str = '127.0.0.1',
    port: int | None = None,
    rebuild_native: bool = False,
    partial_interval_ms: int | None = None,
    type_partial: bool = False,
    subtitle_overlay: bool = False,
    llm_timeout_sec: float | None = None,
    verbose: bool = False,
    on_ready: Callable[[str], None] | None = None,
) -> int:
    resolved_model = resolve_dictation_model_id(config, None if model == 'auto' else model)
    spec = resolve_model(config, resolved_model, kind='asr')
    required_helper_flags: list[str] = []
    if type_partial:
        required_helper_flags.append('--type-partial')
    if subtitle_overlay:
        required_helper_flags.append('--subtitle-overlay')
    binary = ensure_native_binary(
        rebuild=rebuild_native,
        required_flags=tuple(required_helper_flags),
    )
    effective_partial_interval_ms = _resolve_partial_interval_ms(
        partial_interval_ms,
        verbose=verbose,
        type_partial=type_partial,
        subtitle_overlay=subtitle_overlay,
    )
    ensure_dictation_dirs(config)
    port = port or pick_free_port(host)
    log_path = dictation_session_log_path(config)
    _prepare_dictation_log(log_path, config)
    ensure_model_downloaded(config, spec, allow_download=True)
    session_id = uuid.uuid4().hex[:8]
    helper_version = _helper_version(binary)
    helper_mtime = _helper_mtime(binary)

    server_cmd = [
        sys.executable,
        '-u',
        '-m',
        'vox_cli.main',
        'asr',
        'session-server',
        '--host',
        host,
        '--port',
        str(port),
        '--lang',
        lang,
        '--model',
        resolved_model,
        '--dictation-postprocess',
    ]
    if llm_timeout_sec is not None:
        server_cmd.extend(['--dictation-llm-timeout-sec', str(llm_timeout_sec)])
    helper_cmd = [
        str(binary),
        '--server-url',
        f'ws://{host}:{port}',
        '--partial-interval-ms',
        str(effective_partial_interval_ms),
    ]
    if type_partial:
        helper_cmd.append('--type-partial')
    if subtitle_overlay:
        helper_cmd.append('--subtitle-overlay')
    if verbose:
        helper_cmd.append('--verbose')

    _write_log_event(
        log_path,
        event='launch.start',
        session_id=session_id,
        host=host,
        port=port,
        lang=lang,
        model=resolved_model,
        helper_path=str(binary),
        helper_version=helper_version,
        helper_mtime=helper_mtime,
        cwd=str(repo_root()),
        pid=os.getpid(),
        partial_interval_ms=effective_partial_interval_ms,
        type_partial=type_partial,
    )

    with log_path.open('a', encoding='utf-8') as log_handle:
        relay_threads: list[threading.Thread] = []
        relay_lock = threading.Lock()
        formatter = _DictationLogFormatter(sys.stderr) if verbose else None
        if verbose:
            server_proc = subprocess.Popen(
                server_cmd,
                cwd=repo_root(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if server_proc.stdout is None:
                raise RuntimeError('Failed to capture session-server output')
            server_relay_thread = threading.Thread(
                target=_relay_process_output,
                args=(server_proc.stdout, log_handle),
                kwargs={
                    'source': 'server',
                    'echo': True,
                    'lock': relay_lock,
                    'formatter': formatter,
                },
                daemon=True,
            )
            server_relay_thread.start()
            relay_threads.append(server_relay_thread)
        else:
            server_proc = subprocess.Popen(
                server_cmd,
                cwd=repo_root(),
                stdout=log_handle,
                stderr=log_handle,
            )
        _write_log_event(
            log_path,
            event='launch.server_started',
            session_id=session_id,
            server_pid=getattr(server_proc, 'pid', None),
            server_cmd=server_cmd,
        )
        try:
            wait_for_session_server(host, port, server_proc=server_proc)
        except Exception as error:
            _write_log_event(
                log_path,
                event='launch.server_failed',
                session_id=session_id,
                error=str(error),
            )
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
            details = tail_session_log(config)
            if details:
                raise RuntimeError(f'{error}\n\nSession log:\n{details}') from error
            raise

        try:
            if on_ready is not None:
                if subtitle_overlay:
                    on_ready(
                        'Dictation ready. Hold Right Command to record; live subtitles appear at the bottom, and release inserts the final text. Ctrl-C to exit.'
                    )
                else:
                    on_ready(
                        'Dictation ready. Hold Right Command to record, release to transcribe. Ctrl-C to exit.'
                    )
            _write_log_event(
                log_path,
                event='launch.helper_started',
                session_id=session_id,
                helper_cmd=helper_cmd,
            )
            if verbose:
                helper_proc = subprocess.Popen(
                    helper_cmd,
                    cwd=native_project_dir(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if helper_proc.stdout is None:
                    raise RuntimeError('Failed to capture dictation helper output')
                helper_relay_thread = threading.Thread(
                    target=_relay_process_output,
                    args=(helper_proc.stdout, log_handle),
                    kwargs={
                        'source': 'helper',
                        'echo': True,
                        'lock': relay_lock,
                        'formatter': formatter,
                    },
                    daemon=True,
                )
                helper_relay_thread.start()
                relay_threads.append(helper_relay_thread)
                try:
                    exit_code = helper_proc.wait()
                except KeyboardInterrupt:
                    helper_proc.terminate()
                    try:
                        helper_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        helper_proc.kill()
                        helper_proc.wait(timeout=5)
                    raise
            else:
                exit_code = subprocess.call(helper_cmd, cwd=native_project_dir())
            _write_log_event(
                log_path,
                event='launch.helper_exited',
                session_id=session_id,
                helper_exit_code=exit_code,
            )
            return exit_code
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
            for relay_thread in relay_threads:
                relay_thread.join(timeout=1)
            _write_log_event(
                log_path,
                event='launch.server_exited',
                session_id=session_id,
                server_exit_code=server_proc.returncode,
            )
