from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
import unicodedata
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


def dictation_agent_log_path(config: VoxConfig) -> Path:
    return dictation_logs_dir(config) / 'dictation-session.agent.jsonl'


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
    with path.open('a', encoding='utf-8') as handle:
        handle.write(_serialize_log_event(event=event, **fields))


def _write_dual_log_event(
    path: Path,
    *,
    agent_path: Path | None = None,
    event: str,
    **fields: object,
) -> None:
    _write_log_event(path, event=event, **fields)
    if agent_path is not None:
        _write_agent_log_event(agent_path, event=event, **fields)


def _serialize_log_event(*, event: str, **fields: object) -> str:
    payload = {
        'ts': _utc_now(),
        'event': event,
        **fields,
    }
    return f'[dictation] {json.dumps(payload, ensure_ascii=False)}\n'


def _should_echo_server_line(line: str) -> bool:
    return line.startswith('[session-server]')


def _should_echo_helper_line(line: str) -> bool:
    return line.startswith('[vox-dictation]')


_ANSI_RESET = '\033[0m'
_TOKEN_RE = re.compile(r'[^\s=]+=(?:"(?:\\.|[^"])*"|[^\s]+)|[^\s]+')
_DIFF_MARKER_RE = re.compile(r'(\[-.*?-\]|\[\+.*?\+\])')
_DEFAULT_VERBOSE_PARTIAL_INTERVAL_MS = 250
_CLEAR_LINE = '\r\033[2K'


@dataclass
class _LogEvent:
    event: str
    fields: dict[str, object]


@dataclass
class _FormatResult:
    lines: list[str] = field(default_factory=list)
    live_line: str | None = None
    finalize_live_before: bool = False
    log_events: list[_LogEvent] = field(default_factory=list)


@dataclass
class _UtteranceRenderState:
    utterance_id: str
    audio_ms: int = 0
    capture_ms: int = 0
    flush_roundtrip_ms: int = 0
    asr_infer_ms: int = 0
    asr_total_ms: int = 0
    context_capture_ms: int = 0
    context_wait_ms: int = 0
    context_overlap_ms: int = 0
    context_status: str = '-'
    context_budget_state: str = '-'
    context_source: str = '-'
    context_surface: str = '-'
    context_chars: int = 0
    llm_used: bool = False
    llm_ms: int = 0
    llm_first_token_ms: int = 0
    llm_stream_used: bool = False
    llm_stream_chunks: int = 0
    llm_stream_ms: int = 0
    llm_timeout_sec: float = 0.0
    llm_provider: str = '-'
    llm_model: str = '-'
    postprocess_ms: int = 0
    raw_chars: int = 0
    final_chars: int = 0
    type_ms: int = 0
    backend_total_ms: int = 0
    bottleneck: str = 'balanced'
    summary_written: bool = False
    last_stream_chars: int = 0
    last_stream_text: str = ''
    last_partial_text: str = ''
    last_final_text: str = ''
    last_diff_summary: str = ''
    partial_preview_count: int = 0
    partial_stable_advance_count: int = 0
    partial_jobs_started: int = 0
    partial_jobs_completed: int = 0
    partial_reused_chars: int = 0
    partial_stable_chars: int = 0
    partial_sent_count: int = 0
    partial_skipped_count: int = 0
    commit_mode: str = 'full_final'
    guard_fallback: bool = False
    guard_reason: str = ''


@dataclass
class _LivePipelineState:
    utterance_id: str = '?'
    recording_started_at: float | None = None
    recording_ms: int = 0
    voice_detected: bool = False
    asr_live_active: bool = False
    asr_started_at: float | None = None
    asr_ms: int = 0
    llm_started_at: float | None = None
    llm_ms: int = 0
    llm_first_token_ms: int = 0
    llm_stream_chunks: int = 0
    llm_enabled: bool | None = None
    active_stage: str = 'idle'


def _supports_color(stream: TextIO) -> bool:
    if os.getenv('NO_COLOR') is not None:
        return False
    if os.getenv('CLICOLOR_FORCE') == '1':
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _supports_live_updates(stream: TextIO) -> bool:
    if os.getenv('NO_COLOR') is not None:
        return False
    if os.getenv('TERM') in {None, '', 'dumb'}:
        return False
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


def _compact_bool(value: object | None) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _as_int(value: str | None) -> int:
    if value in (None, '', '-'):
        return 0
    try:
        return int(float(value))
    except Exception:
        return 0


def _as_float(value: str | None) -> float:
    if value in (None, '', '-'):
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _compact_value(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 3)
    if isinstance(value, str) and value == '':
        return None
    return value


def _compact_payload(*, event: str, fields: dict[str, object]) -> dict[str, object] | None:
    if event == 'launch.start':
        return {
            'e': 'ls',
            's': fields.get('session_id'),
            'lang': fields.get('lang'),
            'am': fields.get('model'),
            'pi': fields.get('partial_interval_ms'),
            'tp': _compact_bool(fields.get('type_partial')),
            'tv': _compact_bool(fields.get('tty_verbose')),
            'hv': fields.get('helper_version'),
        }
    if event == 'launch.server_started':
        return {
            'e': 'ss',
            's': fields.get('session_id'),
            'pid': fields.get('server_pid'),
        }
    if event == 'launch.server_failed':
        return {
            'e': 'sf',
            's': fields.get('session_id'),
            'err': fields.get('error'),
        }
    if event == 'launch.helper_started':
        return {
            'e': 'hs',
            's': fields.get('session_id'),
        }
    if event == 'launch.helper_exited':
        return {
            'e': 'hx',
            's': fields.get('session_id'),
            'code': fields.get('helper_exit_code'),
        }
    if event == 'launch.server_exited':
        return {
            'e': 'sx',
            's': fields.get('session_id'),
            'code': fields.get('server_exit_code'),
        }
    if event == 'dictation_config_summary':
        return {
            'e': 'cfg',
            'lu': _compact_bool(fields.get('llm_enabled')),
            'ls': _compact_bool(fields.get('llm_stream')),
            'lp': fields.get('llm_provider'),
            'lm': fields.get('llm_model'),
            'lt': fields.get('llm_timeout_sec'),
            'dp': fields.get('prompt_preset'),
            'cp': _compact_bool(fields.get('custom_prompt_enabled')),
            'ce': _compact_bool(fields.get('context_enabled')),
            'cc': fields.get('context_max_chars'),
            'he': _compact_bool(fields.get('hotwords_enabled')),
            'hn': fields.get('hotword_entries'),
            'hr': _compact_bool(fields.get('rewrite_aliases')),
            'cs': _compact_bool(fields.get('case_sensitive')),
            'ie': _compact_bool(fields.get('hints_enabled')),
            'in': fields.get('hint_count'),
        }
    if event == 'postprocess_error':
        return {
            'e': 'pe',
            'u': fields.get('utterance_id'),
            'llm': fields.get('llm_ms'),
            'lt': fields.get('timeout_sec'),
            'lp': fields.get('provider'),
            'lm': fields.get('model'),
            'err': fields.get('llm_error'),
        }
    if event == 'utterance_summary':
        return {
            'e': 'u',
            'u': fields.get('utterance_id'),
            'aud': fields.get('audio_ms'),
            'cap': fields.get('capture_ms'),
            'fl': fields.get('flush_roundtrip_ms'),
            'ctxc': fields.get('context_capture_ms'),
            'ctxw': fields.get('context_wait_ms'),
            'ctxo': fields.get('context_overlap_ms'),
            'ctxs': fields.get('context_status'),
            'ctxb': fields.get('context_budget_state'),
            'src': fields.get('context_source'),
            'srf': fields.get('context_surface'),
            'ctxr': fields.get('context_chars'),
            'asr': fields.get('asr_infer_ms'),
            'asrt': fields.get('asr_total_ms'),
            'lu': _compact_bool(fields.get('llm_used')),
            'ls': _compact_bool(fields.get('llm_stream_used')),
            'ft': fields.get('llm_first_token_ms'),
            'llm': fields.get('llm_ms'),
            'lst': fields.get('llm_stream_ms'),
            'lsch': fields.get('llm_stream_chunks'),
            'ty': fields.get('type_ms'),
            'be': fields.get('backend_total_ms'),
            'post': fields.get('postprocess_ms'),
            'bot': fields.get('bottleneck'),
            'fin': fields.get('final_chars'),
            'raw': fields.get('raw_chars'),
            'pp': fields.get('partial_preview_count'),
            'psa': fields.get('partial_stable_advance_count'),
            'pjs': fields.get('partial_jobs_started'),
            'pjc': fields.get('partial_jobs_completed'),
            'prc': fields.get('partial_reused_chars'),
            'psc': fields.get('partial_stable_chars'),
            'psn': fields.get('partial_sent_count'),
            'psk': fields.get('partial_skipped_count'),
            'cm': fields.get('commit_mode'),
            'gf': _compact_bool(fields.get('guard_fallback')),
            'gr': fields.get('guard_reason'),
        }
    return None


def _serialize_agent_log_event(*, event: str, **fields: object) -> str | None:
    payload = _compact_payload(event=event, fields=fields)
    if payload is None:
        return None
    compact = {
        key: compacted
        for key, value in payload.items()
        if (compacted := _compact_value(value)) is not None
    }
    if not compact:
        return None
    return json.dumps(compact, ensure_ascii=False, separators=(',', ':')) + '\n'


def _write_agent_log_event(path: Path, *, event: str, **fields: object) -> None:
    line = _serialize_agent_log_event(event=event, **fields)
    if line is None:
        return
    with path.open('a', encoding='utf-8') as handle:
        handle.write(line)


def _label_text(text: str) -> str:
    if not text or not text.strip():
        return '空格'
    compact = text.replace('\n', '\\n')
    if compact.isspace():
        return '空格'
    if len(compact) > 20:
        compact = f'{compact[:20]}…'
    return compact


def _clip_text(text: str, *, max_chars: int = 96) -> str:
    cleaned = ' '.join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= 1:
        return cleaned[:max_chars]
    return f'{cleaned[: max_chars - 1]}…'


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if char in {'\n', '\r'}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1
    return width


def _clip_display(text: str, *, max_width: int) -> str:
    cleaned = ' '.join(text.split())
    if max_width <= 0:
        return ''
    if _display_width(cleaned) <= max_width:
        return cleaned
    ellipsis = '…'
    budget = max(1, max_width - _display_width(ellipsis))
    out: list[str] = []
    used = 0
    for char in cleaned:
        char_width = 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1
        if used + char_width > budget:
            break
        out.append(char)
        used += char_width
    return ''.join(out).rstrip() + ellipsis


def _summarize_diff(diff: str, *, max_items: int = 4) -> str:
    if not diff or diff == '(no change)':
        return '无关键改动'

    markers = _DIFF_MARKER_RE.findall(diff)
    if not markers:
        return '无关键改动'

    parts: list[str] = []
    index = 0
    while index < len(markers) and len(parts) < max_items:
        marker = markers[index]
        if marker.startswith('[-'):
            old = marker[2:-2]
            if index + 1 < len(markers) and markers[index + 1].startswith('[+'):
                new = markers[index + 1][2:-2]
                parts.append(f'{_label_text(old)}->{_label_text(new)}')
                index += 2
                continue
            parts.append(f'删 {_label_text(old)}')
            index += 1
            continue
        if marker.startswith('[+'):
            new = marker[2:-2]
            parts.append(f'补 {_label_text(new)}')
        index += 1

    if not parts:
        return '无关键改动'
    return '; '.join(parts)


def _resolve_partial_interval_ms(
    partial_interval_ms: int | None,
    *,
    verbose: bool,
    type_partial: bool,
    subtitle_overlay: bool,
    background_partial_streaming: bool = False,
) -> int:
    if partial_interval_ms is not None:
        return max(0, int(partial_interval_ms))
    if verbose or type_partial or subtitle_overlay or background_partial_streaming:
        return _DEFAULT_VERBOSE_PARTIAL_INTERVAL_MS
    return 0


class _DictationLogFormatter:
    _SPINNER_FRAMES = ('◐', '◓', '◑', '◒')
    _STAGE_META = {
        'asr_final': ('ASR', '1;34', '识别完成'),
        'hotwords_done': ('HOT', '1;36', '热词纠正'),
        'rules_done': ('RULES', '1;33', '规则处理'),
        'llm_start': ('LLM', '1;35', '开始润色'),
        'llm_stream': ('LLM', '1;35', '流式润色'),
        'llm_done': ('LLM', '1;35', '润色完成'),
        'llm_guard': ('GUARD', '1;33', '结构护栏回退'),
        'llm_error': ('LLM', '1;31', '润色失败'),
        'final_ready': ('DONE', '1;32', '最终输出'),
    }
    _TEXT_LABELS = {
        'asr_final': '原文',
        'hotwords_done': '热词后',
        'rules_done': '规则后',
        'llm_start': 'LLM输入',
        'llm_stream': '流式',
        'llm_done': 'LLM输出',
        'final_ready': '最终',
    }
    _BUDGET_TITLES = {
        'ready': '预算内完成',
        'timeout': '上下文等待超时',
        'expired': '上下文预算到期',
    }
    _BOTTLENECK_LABELS = {
        'context_budget': '上下文等待',
        'llm_first_token': 'LLM 首包',
        'asr_infer': 'ASR 推理',
        'llm_stream_tail': 'LLM 持续生成',
        'text_injection': '文本注入',
        'balanced': '整体平稳',
    }

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._color = _supports_color(stream)
        self._live_enabled = _supports_live_updates(stream)
        self._live_active = False
        self._utterances: dict[str, _UtteranceRenderState] = {}
        self._tty_width = shutil.get_terminal_size((120, 20)).columns
        self._live_pipeline = _LivePipelineState()

    def format(self, source: str, raw_line: str) -> _FormatResult:
        line = raw_line.rstrip('\n')
        if source == 'server':
            result = self._format_server_line(line)
        else:
            result = self._format_helper_line(line)

        if self._live_enabled:
            if result.live_line is not None:
                self._live_active = True
            elif result.lines and self._live_active:
                result.finalize_live_before = True
                self._live_active = False
        elif result.live_line is not None:
            result.lines.append(result.live_line)
            result.live_line = None
        return result

    def _stamp(self) -> str:
        value = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        return _style(value, '2', enabled=self._color)

    def _badge(self, label: str, code: str) -> str:
        return _style(f'[{label}]', code, enabled=self._color)

    def _detail(self, label: str, value: str, code: str) -> str:
        styled_label = _style(label.rjust(7), f'2;{code}' if ';' not in code else code.replace('1;', '2;'), enabled=self._color)
        prefix = f'{" " * 14}{styled_label}  '
        available = max(24, self._tty_width - 24)
        clipped = _clip_display(value, max_width=available)
        return f'{prefix}{clipped}'

    def _truthy(self, value: str | None) -> bool:
        if value is None:
            return False
        return value.lower() in {'1', 'true', 'yes', 'on'}

    def _state(self, utterance_id: str | None) -> _UtteranceRenderState:
        key = utterance_id or '0'
        state = self._utterances.get(key)
        if state is None:
            state = _UtteranceRenderState(utterance_id=key)
            self._utterances[key] = state
        return state

    def _live_text(self, badge: str, code: str, text: str, *, detail: str | None = None) -> str:
        available = max(24, self._tty_width - 44)
        parts = [f'{self._stamp()} {self._badge(badge, code)} {_clip_display(text, max_width=available)}']
        if detail:
            parts.append(detail)
        return ' | '.join(parts)

    def _session_line(self, parts: list[str]) -> str:
        return f'{self._stamp()} {self._badge("SESSION", "1;36")} {" | ".join(parts)}'

    def live_heartbeat(self) -> str | None:
        if not self._live_enabled or not self._live_active:
            return None
        return self._pipeline_live_line()

    def live_updates_enabled(self) -> bool:
        return self._live_enabled

    def _elapsed_ms(self, started_at: float | None) -> int:
        if started_at is None:
            return 0
        return max(0, int((time.monotonic() - started_at) * 1000))

    def _spinner(self) -> str:
        index = int(time.monotonic() * 8) % len(self._SPINNER_FRAMES)
        return self._SPINNER_FRAMES[index]

    def _pipeline_segment(
        self,
        label: str,
        *,
        state: str,
        detail: str = '',
        active_code: str,
    ) -> str:
        if state == 'done':
            marker = _style('✓', '1;32', enabled=self._color)
            text = _style(label, '1;32', enabled=self._color)
        elif state == 'active':
            marker = _style(self._spinner(), active_code, enabled=self._color)
            text = _style(label, active_code, enabled=self._color)
        elif state == 'off':
            marker = _style('×', '2;37', enabled=self._color)
            text = _style(label, '2;37', enabled=self._color)
        else:
            marker = _style('·', '2;37', enabled=self._color)
            text = _style(label, '2;37', enabled=self._color)
        segment = f'{text} {marker}'
        if detail:
            segment = f'{segment} {detail}'
        return segment

    def _pipeline_live_line(self) -> str | None:
        state = self._live_pipeline
        if state.active_stage == 'idle':
            return None

        if state.active_stage == 'recording':
            rec_state = 'active'
        elif state.recording_ms > 0:
            rec_state = 'done'
        else:
            rec_state = 'wait'

        if state.active_stage == 'asr' or (
            state.active_stage == 'recording' and state.asr_live_active
        ):
            asr_state = 'active'
        elif state.asr_ms > 0:
            asr_state = 'done'
        else:
            asr_state = 'wait'

        if state.llm_enabled is False:
            llm_state = 'off'
        elif state.active_stage == 'llm':
            llm_state = 'active'
        elif state.llm_ms > 0:
            llm_state = 'done'
        else:
            llm_state = 'wait'

        recording_ms = (
            self._elapsed_ms(state.recording_started_at)
            if state.active_stage == 'recording'
            else state.recording_ms
        )
        asr_ms = (
            self._elapsed_ms(state.asr_started_at)
            if state.active_stage == 'asr'
            else state.asr_ms
        )
        llm_ms = (
            self._elapsed_ms(state.llm_started_at)
            if state.active_stage == 'llm'
            else state.llm_ms
        )

        rec_detail = ''
        if recording_ms > 0:
            rec_detail = f'{recording_ms / 1000:.1f}s'
        if state.active_stage == 'recording' and state.voice_detected:
            rec_detail = f'{rec_detail} 已收声'.strip()
        elif state.active_stage == 'recording' and rec_detail:
            rec_detail = f'{rec_detail} 待触发'

        asr_detail = ''
        if asr_state == 'active' and state.active_stage == 'recording':
            asr_detail = '实时'
        elif asr_state == 'active':
            asr_detail = f'{asr_ms}ms'
        elif asr_ms > 0:
            asr_detail = f'{asr_ms}ms'

        llm_detail = ''
        if llm_state == 'active':
            llm_detail = f'{llm_ms}ms'
            if state.llm_first_token_ms > 0:
                llm_detail = f'{llm_detail} 首字{state.llm_first_token_ms}ms'
        elif llm_ms > 0:
            llm_detail = f'{llm_ms}ms'
            if state.llm_stream_chunks > 0:
                llm_detail = f'{llm_detail} / {state.llm_stream_chunks}块'

        parts = [
            self._pipeline_segment('录音', state=rec_state, detail=rec_detail, active_code='1;36'),
            self._pipeline_segment('转写', state=asr_state, detail=asr_detail, active_code='1;34'),
            self._pipeline_segment('润色', state=llm_state, detail=llm_detail, active_code='1;35'),
        ]
        badge = self._badge(
            f"RUN #{state.utterance_id}" if state.utterance_id != '?' else 'RUN',
            '1;36',
        )
        return f'{badge} {" | ".join(parts)}'

    def _reset_live_pipeline(self) -> None:
        llm_enabled = self._live_pipeline.llm_enabled
        self._live_pipeline = _LivePipelineState(llm_enabled=llm_enabled)

    def _live_flow_line(
        self,
        *,
        phase: str,
        text: str,
        code: str,
        utterance: str | None = None,
        detail_parts: list[str] | None = None,
    ) -> str:
        badge = f"FLOW #{utterance}" if utterance and utterance != '?' else "FLOW"
        detail: str | None = None
        if detail_parts:
            detail = ' | '.join(part for part in detail_parts if part)
        return self._live_text(badge, code, f'{phase} {text}', detail=detail)

    def _format_server_line(self, line: str) -> _FormatResult:
        if not _should_echo_server_line(line):
            return _FormatResult(lines=[line])

        payload = line[len('[session-server]') :].strip()
        if payload.startswith('warmup completed '):
            return _FormatResult()

        if payload.startswith('transcribe '):
            fields, _ = _parse_tokens(payload[len('transcribe ') :])
            if fields.get('partial') == 'True':
                return _FormatResult()
            utterance = fields.get('utterance_id', '?')
            state = self._state(utterance)
            audio_ms = int(fields.get('audio_ms', '0') or 0)
            infer_ms = int(fields.get('infer_ms', '0') or 0)
            total_ms = int(fields.get('total_ms', '0') or 0)
            state.audio_ms = audio_ms
            state.asr_infer_ms = infer_ms
            state.asr_total_ms = total_ms
            self._live_pipeline.utterance_id = utterance
            self._live_pipeline.recording_ms = max(
                self._live_pipeline.recording_ms,
                audio_ms,
            )
            self._live_pipeline.asr_live_active = False
            self._live_pipeline.asr_started_at = None
            self._live_pipeline.asr_ms = infer_ms
            self._live_pipeline.active_stage = 'asr_done'
            if self._live_enabled:
                return _FormatResult(live_line=self._pipeline_live_line())
            parts = [
                f'音频 {audio_ms / 1000:.2f}s',
                f'infer {infer_ms}ms',
                f'total {total_ms}ms',
            ]
            return _FormatResult(
                lines=[
                    f'{self._stamp()} {self._badge(f"ASR #{utterance}", "1;34")} {" | ".join(parts)}'
                ]
            )

        if payload.startswith('dictation_config '):
            fields, _ = _parse_tokens(payload[len('dictation_config ') :])
            model_parts = [part for part in (fields.get('llm_provider'), fields.get('llm_model')) if part and part != '-']
            preset_text = str(fields.get('prompt_preset') or 'default')
            self._live_pipeline.llm_enabled = self._truthy(fields.get('llm_enabled'))
            parts = [
                f'llm {"on" if self._truthy(fields.get("llm_enabled")) else "off"}',
                f'stream {"on" if self._truthy(fields.get("llm_stream")) else "off"}',
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
            if self._live_enabled:
                session_parts = []
                if model_parts:
                    session_parts.append(' / '.join(model_parts))
                session_parts.append(f'preset {preset_text}')
                session_parts.extend(parts)
                lines = [self._session_line(session_parts)]
            else:
                lines = [f'{self._stamp()} {self._badge("CFG", "1;36")} {" | ".join(parts)}']
            if not self._live_enabled:
                if model_parts:
                    lines.append(self._detail('模型', ' / '.join(model_parts), '1;36'))
                if fields.get('prompt_preset'):
                    preset_text = str(fields.get('prompt_preset'))
                    if 'custom_prompt_enabled' in fields:
                        preset_text = (
                            f'{preset_text} | custom {"on" if self._truthy(fields.get("custom_prompt_enabled")) else "off"}'
                        )
                    lines.append(self._detail('预设', preset_text, '1;36'))
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
            return _FormatResult(
                lines=lines,
                log_events=[
                    _LogEvent(
                        event='dictation_config_summary',
                        fields={
                            'llm_enabled': self._truthy(fields.get('llm_enabled')),
                            'llm_stream': self._truthy(fields.get('llm_stream')),
                            'llm_provider': fields.get('llm_provider'),
                            'llm_model': fields.get('llm_model'),
                            'llm_timeout_sec': _as_float(fields.get('llm_timeout_sec')),
                            'prompt_preset': fields.get('prompt_preset'),
                            'custom_prompt_enabled': self._truthy(fields.get('custom_prompt_enabled')),
                            'context_enabled': self._truthy(fields.get('context_enabled')),
                            'context_max_chars': _as_int(fields.get('context_max_chars')),
                            'hotwords_enabled': self._truthy(fields.get('hotwords_enabled')),
                            'hotword_entries': _as_int(fields.get('hotword_entries')),
                            'rewrite_aliases': self._truthy(fields.get('rewrite_aliases')),
                            'case_sensitive': self._truthy(fields.get('case_sensitive')),
                            'hints_enabled': self._truthy(fields.get('hints_enabled')),
                            'hint_count': _as_int(fields.get('hint_count')),
                        },
                    )
                ],
            )

        if payload.startswith('dictation_config_hotwords '):
            fields, _ = _parse_tokens(payload[len('dictation_config_hotwords ') :])
            if self._live_enabled:
                return _FormatResult()
            label = 'LEXICON' if self._live_enabled else '热词表'
            return _FormatResult(lines=[self._detail(label, fields.get('text', ''), '1;36')])

        if payload.startswith('dictation_config_hints '):
            fields, _ = _parse_tokens(payload[len('dictation_config_hints ') :])
            if self._live_enabled:
                return _FormatResult()
            label = 'PROMPT' if self._live_enabled else '提示词'
            return _FormatResult(lines=[self._detail(label, fields.get('text', ''), '1;36')])

        if payload.startswith('dictation_stage '):
            fields, extras = _parse_tokens(payload[len('dictation_stage ') :])
            stage = fields.get('stage', '-')
            label, code, title = self._STAGE_META.get(stage, ('STAGE', '1;37', stage))
            utterance = fields.get('utterance_id', '?')
            state = self._state(utterance)
            if fields.get('provider') and fields.get('provider') != '-':
                state.llm_provider = fields['provider']
            if fields.get('model') and fields.get('model') != '-':
                state.llm_model = fields['model']
            if 'timeout_sec' in fields:
                state.llm_timeout_sec = _as_float(fields.get('timeout_sec'))
            if stage == 'llm_start':
                self._live_pipeline.utterance_id = utterance
                self._live_pipeline.llm_started_at = time.monotonic()
                self._live_pipeline.llm_ms = 0
                self._live_pipeline.llm_first_token_ms = 0
                self._live_pipeline.llm_stream_chunks = 0
                self._live_pipeline.active_stage = 'llm'
                if self._live_enabled:
                    return _FormatResult(live_line=self._pipeline_live_line())
            if stage == 'llm_stream':
                state.llm_stream_used = self._truthy(fields.get('stream_used'))
                state.llm_stream_chunks = _as_int(fields.get('stream_chunks'))
                state.llm_first_token_ms = _as_int(fields.get('first_token_ms'))
                state.last_stream_chars = _as_int(fields.get('chars'))
                self._live_pipeline.llm_first_token_ms = state.llm_first_token_ms
                self._live_pipeline.llm_stream_chunks = state.llm_stream_chunks
                return _FormatResult()
            if stage == 'llm_done':
                self._live_pipeline.llm_started_at = None
                self._live_pipeline.llm_ms = _as_int(fields.get('stage_ms'))
                self._live_pipeline.llm_first_token_ms = _as_int(fields.get('first_token_ms'))
                self._live_pipeline.llm_stream_chunks = _as_int(fields.get('stream_chunks'))
                self._live_pipeline.active_stage = 'llm_done'
            if self._live_enabled and stage in {'hotwords_done', 'rules_done', 'asr_final', 'llm_start', 'llm_done', 'final_ready'}:
                return _FormatResult()
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
            if 'stream_requested' in fields:
                parts.append(f'stream {"on" if self._truthy(fields["stream_requested"]) else "off"}')
            if 'stream_used' in fields:
                parts.append(f'used {"yes" if self._truthy(fields["stream_used"]) else "no"}')
            if 'stream_chunks' in fields and fields['stream_chunks'] != '0':
                parts.append(f'chunks {fields["stream_chunks"]}')
            if 'first_token_ms' in fields:
                parts.append(f'first {fields["first_token_ms"]}ms')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            if 'context_selected_chars' in fields and fields['context_selected_chars'] != '0':
                parts.append(f'selected {fields["context_selected_chars"]}字')
            if 'context_focus_chars' in fields and fields['context_focus_chars'] != '0':
                parts.append(f'focus {fields["context_focus_chars"]}字')
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
            if not self._live_enabled:
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
                if fields.get('context_surface'):
                    lines.append(self._detail('界面', fields['context_surface'], code))
                if fields.get('replacements'):
                    lines.append(self._detail('替换', fields['replacements'], code))
                if fields.get('reason'):
                    lines.append(self._detail('原因', fields['reason'], code))
                if fields.get('fallback'):
                    lines.append(self._detail('回退', fields['fallback'], code))
                if 'error' in fields:
                    lines.append(self._detail('错误', fields['error'], code))
            return _FormatResult(lines=lines)

        if payload.startswith('dictation_context '):
            fields, _ = _parse_tokens(payload[len('dictation_context ') :])
            utterance = fields.get('utterance_id', '?')
            context_state = fields.get('state', '-')
            code = '1;36'
            title = {
                'ready': '上下文已捕获',
                'empty': '未拿到焦点上下文',
                'error': '上下文采集失败',
                'disabled': '上下文未启用',
            }.get(context_state, f'上下文 {context_state}')
            utterance_state = self._state(utterance)
            utterance_state.context_status = context_state
            utterance_state.context_capture_ms = _as_int(fields.get('capture_ms'))
            utterance_state.context_chars = _as_int(fields.get('context_chars'))
            if fields.get('source'):
                utterance_state.context_source = fields['source']
            if fields.get('surface'):
                utterance_state.context_surface = fields['surface']
            if self._live_enabled:
                return _FormatResult()
            parts = [title]
            if 'capture_ms' in fields:
                parts.append(f'{fields["capture_ms"]}ms')
            if 'selected_chars' in fields and fields['selected_chars'] != '0':
                parts.append(f'selected {fields["selected_chars"]}字')
            if 'focus_chars' in fields and fields['focus_chars'] != '0':
                parts.append(f'focus {fields["focus_chars"]}字')
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
            if fields.get('surface'):
                meta_parts.append(f'surface {fields["surface"]}')
            if fields.get('role'):
                meta_parts.append(f'role {fields["role"]}')
            if fields.get('url'):
                meta_parts.append(f'url {fields["url"]}')
            if meta_parts and not self._live_enabled:
                lines.append(self._detail('来源', ' | '.join(meta_parts), code))
            if fields.get('error') and not self._live_enabled:
                lines.append(self._detail('错误', fields['error'], '1;31'))
            return _FormatResult(lines=lines)

        if payload.startswith('dictation_context_prefetch '):
            fields, _ = _parse_tokens(payload[len('dictation_context_prefetch ') :])
            utterance = fields.get('utterance_id', '?')
            if self._live_enabled:
                return _FormatResult()
            parts = ['预采集就绪']
            if 'capture_ms' in fields:
                parts.append(f'{fields["capture_ms"]}ms')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            if 'focus_chars' in fields and fields['focus_chars'] != '0':
                parts.append(f'focus {fields["focus_chars"]}字')
            lines = [
                f'{self._stamp()} {self._badge(f"CTX PRE #{utterance}", "1;36")} {" | ".join(parts)}'
            ]
            meta_parts = []
            if fields.get('source'):
                meta_parts.append(f'source {fields["source"]}')
            if fields.get('app'):
                meta_parts.append(f'app {fields["app"]}')
            if fields.get('window'):
                meta_parts.append(f'window {fields["window"]}')
            if fields.get('surface'):
                meta_parts.append(f'surface {fields["surface"]}')
            if fields.get('role'):
                meta_parts.append(f'role {fields["role"]}')
            if meta_parts:
                lines.append(self._detail('来源', ' | '.join(meta_parts), '1;36'))
            return _FormatResult(lines=lines)

        if payload.startswith('dictation_context_selected '):
            return _FormatResult()

        if payload.startswith('dictation_context_focus '):
            return _FormatResult()

        if payload.startswith('dictation_context_excerpt '):
            return _FormatResult()

        if payload.startswith('dictation_context_budget '):
            fields, _ = _parse_tokens(payload[len('dictation_context_budget ') :])
            utterance = fields.get('utterance_id', '?')
            state = self._state(utterance)
            state.context_wait_ms = _as_int(fields.get('waited_ms'))
            state.context_budget_state = fields.get('state', '-') or '-'
            state.context_overlap_ms = max(0, state.context_capture_ms - state.context_wait_ms)
            budget_state = fields.get('state', '-') or '-'
            if budget_state not in {'timeout', 'expired'}:
                return _FormatResult()
            waited_ms = _as_int(fields.get('waited_ms'))
            title = self._BUDGET_TITLES.get(budget_state, budget_state)
            return _FormatResult(
                lines=[
                    f'{self._stamp()} {self._badge(f"CTX WAIT #{utterance}", "1;33")} {title} | waited {waited_ms}ms'
                ]
            )

        if payload.startswith('dictation_partial_pipeline '):
            fields, _ = _parse_tokens(payload[len('dictation_partial_pipeline ') :])
            utterance = fields.get('utterance_id')
            state = self._state(utterance)
            pipeline_state = fields.get('state', '-')
            if pipeline_state == 'preview':
                state.partial_preview_count += 1
                state.partial_reused_chars = max(state.partial_reused_chars, _as_int(fields.get('reused_chars')))
                state.partial_stable_chars = max(state.partial_stable_chars, _as_int(fields.get('stable_chars')))
            elif pipeline_state == 'stable':
                advance_chars = _as_int(fields.get('advance_chars'))
                if advance_chars > 0:
                    state.partial_stable_advance_count += 1
                state.partial_stable_chars = max(state.partial_stable_chars, _as_int(fields.get('stable_chars')))
            elif pipeline_state == 'job_started':
                state.partial_jobs_started += 1
                state.partial_stable_chars = max(state.partial_stable_chars, _as_int(fields.get('stable_chars')))
                if not self._live_enabled:
                    parts = ['预跑启动']
                    if 'stable_chars' in fields:
                        parts.append(f'stable {fields["stable_chars"]}字')
                    if 'context_ready' in fields:
                        parts.append(f'ctx {"yes" if self._truthy(fields.get("context_ready")) else "no"}')
                    return _FormatResult(
                        lines=[
                            f'{self._stamp()} {self._badge(f"PRE #{utterance or '?'}", "1;35")} {" | ".join(parts)}'
                        ]
                    )
            elif pipeline_state == 'job_completed':
                state.partial_jobs_completed += 1
                state.partial_stable_chars = max(state.partial_stable_chars, _as_int(fields.get('stable_chars')))
                if not self._live_enabled:
                    parts = ['预跑完成']
                    if 'stable_chars' in fields:
                        parts.append(f'stable {fields["stable_chars"]}字')
                    if 'llm_ms' in fields and fields['llm_ms'] != '0':
                        parts.append(f'llm {fields["llm_ms"]}ms')
                    if 'changed' in fields:
                        parts.append(f'changed {"yes" if self._truthy(fields.get("changed")) else "no"}')
                    return _FormatResult(
                        lines=[
                            f'{self._stamp()} {self._badge(f"PRE #{utterance or '?'}", "1;35")} {" | ".join(parts)}'
                        ]
                    )
            elif pipeline_state == 'job_failed':
                if not self._live_enabled:
                    parts = ['预跑失败']
                    if fields.get('error'):
                        parts.append(str(fields['error']))
                    return _FormatResult(
                        lines=[
                            f'{self._stamp()} {self._badge(f"PRE #{utterance or '?'}", "1;31")} {" | ".join(parts)}'
                        ]
                    )
            elif pipeline_state == 'flush':
                state.partial_reused_chars = max(state.partial_reused_chars, _as_int(fields.get('reused_chars')))
                state.partial_stable_chars = max(state.partial_stable_chars, _as_int(fields.get('stable_chars')))
                reused_chars = _as_int(fields.get('reused_chars'))
                if reused_chars > 0 and not self._live_enabled:
                    parts = [f'预跑命中 {reused_chars}字']
                    if 'stable_chars' in fields:
                        parts.append(f'stable {fields["stable_chars"]}字')
                    return _FormatResult(
                        lines=[
                            f'{self._stamp()} {self._badge(f"PRE #{utterance or '?'}", "1;35")} {" | ".join(parts)}'
                        ]
                    )
            return _FormatResult()

        if payload.startswith('dictation_commit '):
            fields, _ = _parse_tokens(payload[len('dictation_commit ') :])
            utterance = fields.get('utterance_id')
            state = self._state(utterance)
            if fields.get('commit_mode'):
                state.commit_mode = fields['commit_mode']
            if 'guard_fallback' in fields:
                state.guard_fallback = self._truthy(fields.get('guard_fallback'))
            if fields.get('guard_reason'):
                state.guard_reason = str(fields['guard_reason'])
            return _FormatResult()

        if payload.startswith('dictation_postprocess_error '):
            fields, _ = _parse_tokens(payload[len('dictation_postprocess_error ') :])
            utterance = fields.get('utterance_id')
            if utterance:
                state = self._state(utterance)
                state.llm_used = False
                state.llm_ms = _as_int(fields.get('llm_ms'))
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
            return _FormatResult(
                lines=lines,
                log_events=[
                    _LogEvent(
                        event='postprocess_error',
                        fields={
                            'utterance_id': _as_int(fields.get('utterance_id')),
                            'llm_ms': _as_int(fields.get('llm_ms')),
                            'timeout_sec': _as_float(fields.get('timeout_sec')),
                            'provider': fields.get('provider'),
                            'model': fields.get('model'),
                            'llm_error': fields.get('llm_error'),
                        },
                    )
                ],
            )

        if payload.startswith('dictation_postprocess '):
            fields, _ = _parse_tokens(payload[len('dictation_postprocess ') :])
            utterance = fields.get('utterance_id')
            state = self._state(utterance)
            state.llm_used = self._truthy(fields.get('llm_used'))
            state.llm_ms = _as_int(fields.get('llm_ms'))
            state.llm_stream_used = self._truthy(fields.get('stream_used'))
            state.llm_stream_chunks = _as_int(fields.get('stream_chunks'))
            state.llm_first_token_ms = _as_int(fields.get('first_token_ms'))
            state.llm_timeout_sec = _as_float(fields.get('timeout_sec'))
            state.postprocess_ms = _as_int(fields.get('postprocess_ms'))
            state.raw_chars = _as_int(fields.get('raw_chars'))
            state.final_chars = _as_int(fields.get('final_chars'))
            if fields.get('provider') and fields.get('provider') != '-':
                state.llm_provider = fields['provider']
            if fields.get('model') and fields.get('model') != '-':
                state.llm_model = fields['model']
            if fields.get('context_source'):
                state.context_source = fields['context_source']
            if fields.get('context_chars'):
                state.context_chars = _as_int(fields.get('context_chars'))
            self._live_pipeline.llm_ms = state.llm_ms
            self._live_pipeline.llm_started_at = None
            if state.llm_used:
                self._live_pipeline.active_stage = 'llm_done'
            parts = [
                f'changed {"yes" if self._truthy(fields.get("changed")) else "no"}',
                f'llm {"yes" if self._truthy(fields.get("llm_used")) else "no"}',
            ]
            if 'postprocess_ms' in fields:
                parts.append(f'post {fields["postprocess_ms"]}ms')
            if 'llm_ms' in fields:
                parts.append(f'llm {fields["llm_ms"]}ms')
            if 'stream_requested' in fields:
                parts.append(f'stream {"on" if self._truthy(fields.get("stream_requested")) else "off"}')
            if 'stream_used' in fields:
                parts.append(f'used {"yes" if self._truthy(fields.get("stream_used")) else "no"}')
            if 'stream_chunks' in fields and fields['stream_chunks'] != '0':
                parts.append(f'chunks {fields["stream_chunks"]}')
            if 'first_token_ms' in fields:
                parts.append(f'first {fields["first_token_ms"]}ms')
            if 'raw_chars' in fields and 'final_chars' in fields:
                parts.append(f'{fields["raw_chars"]}->{fields["final_chars"]}字')
            if 'context_chars' in fields and fields['context_chars'] != '0':
                parts.append(f'context {fields["context_chars"]}字')
            lines = [
                f'{self._stamp()} {self._badge("POST", "1;32")} {" | ".join(parts)}'
            ]
            if self._live_enabled:
                return _FormatResult()
            if state.last_diff_summary and state.last_diff_summary != '无关键改动':
                lines.append(self._detail('改动', state.last_diff_summary, '1;32'))
            return _FormatResult(lines=lines)

        if payload.startswith('dictation_text '):
            fields, _ = _parse_tokens(payload[len('dictation_text ') :])
            stage = fields.get('stage', '-')
            text = fields.get('text', '')
            utterance = fields.get('utterance_id')
            state = self._state(utterance)
            label, code, _ = self._STAGE_META.get(stage, ('TEXT', '1;37', stage))
            if stage == 'llm_stream':
                state.last_stream_text = text
                if self._live_enabled:
                    self._live_pipeline.utterance_id = utterance or self._live_pipeline.utterance_id
                    self._live_pipeline.active_stage = 'llm'
                    return _FormatResult(live_line=self._pipeline_live_line())
                detail = f'首字 {state.llm_first_token_ms}ms | chunks {max(1, state.llm_stream_chunks)}'
                return _FormatResult(
                    live_line=self._live_text(
                        f"LLM #{utterance or '?'}",
                        code,
                        f'流式 {text}',
                        detail=detail,
                    )
                )
            if stage in {'hotwords_done', 'rules_done', 'llm_start', 'llm_done'}:
                return _FormatResult()
            if stage == 'final_ready':
                state.last_final_text = text
                if self._live_enabled:
                    return _FormatResult()
            return _FormatResult(lines=[self._detail(self._TEXT_LABELS.get(stage, label), text, code)])

        if payload.startswith('dictation_diff '):
            fields, _ = _parse_tokens(payload[len('dictation_diff ') :])
            stage = fields.get('stage', '-')
            diff = fields.get('diff', '')
            utterance = fields.get('utterance_id')
            state = self._state(utterance)
            state.last_diff_summary = _summarize_diff(diff)
            if self._live_enabled:
                return _FormatResult()
            _, code, _ = self._STAGE_META.get(stage, ('DIFF', '1;37', stage))
            return _FormatResult(lines=[self._detail('改动', state.last_diff_summary, code)])

        return _FormatResult(lines=[line])

    def _format_helper_line(self, line: str) -> _FormatResult:
        if not _should_echo_helper_line(line):
            return _FormatResult(lines=[line])

        payload = line[len('[vox-dictation]') :].strip()
        if payload == 'backend ready':
            if self._live_enabled:
                return _FormatResult()
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("BACKEND", "1;36")} 会话后端已就绪'])
        if payload == 'subtitle overlay enabled':
            if self._live_enabled:
                return _FormatResult()
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("HUD", "1;35")} 底部字幕预览已开启'])
        if payload.startswith('ready '):
            if self._live_enabled:
                return _FormatResult(
                    lines=[
                        f'{self._stamp()} {self._badge("READY", "1;36")} dictation 已就绪',
                        self._detail('KEYS', '按住 Right Command 开始, 松开后完成输入', '1;36'),
                    ]
                )
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("READY", "1;36")} {payload}'])
        if payload == 'recording started...':
            if self._live_enabled:
                self._reset_live_pipeline()
                self._live_pipeline.recording_started_at = time.monotonic()
                self._live_pipeline.asr_live_active = False
                self._live_pipeline.active_stage = 'recording'
                return _FormatResult(live_line=self._pipeline_live_line())
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("MIC", "1;36")} 开始录音'])
        if payload == 'recording stopped':
            if self._live_enabled:
                self._live_pipeline.recording_ms = self._elapsed_ms(self._live_pipeline.recording_started_at)
                self._live_pipeline.recording_started_at = None
                self._live_pipeline.asr_started_at = time.monotonic()
                self._live_pipeline.asr_live_active = True
                self._live_pipeline.active_stage = 'asr'
                return _FormatResult(live_line=self._pipeline_live_line())
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("MIC", "1;36")} 结束录音'])
        if payload == 'recording cancelled':
            if self._live_enabled:
                self._reset_live_pipeline()
                self._live_active = False
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("MIC", "1;33")} 已取消本次录音'])
        if payload.startswith('native sample rate: '):
            if self._live_enabled:
                return _FormatResult()
            return _FormatResult(lines=[self._detail('采样率', payload.split(': ', 1)[1], '1;36')])
        if payload.startswith('engine_start_ms='):
            if self._live_enabled:
                return _FormatResult()
            return _FormatResult(lines=[self._detail('启动', f'{payload.split("=", 1)[1]}ms', '1;36')])
        if payload.startswith('voice detected; sending '):
            fields, _ = _parse_tokens(payload.split('sending ', 1)[1])
            if self._live_enabled:
                self._live_pipeline.voice_detected = True
                if self._live_pipeline.active_stage == 'idle':
                    self._live_pipeline.recording_started_at = time.monotonic()
                    self._live_pipeline.active_stage = 'recording'
                return _FormatResult(live_line=self._pipeline_live_line())
            parts = []
            if 'preroll_ms' in fields:
                parts.append(f'preroll {fields["preroll_ms"]}ms')
            if 'peak' in fields:
                parts.append(f'peak {fields["peak"]}')
            if 'rms' in fields:
                parts.append(f'rms {fields["rms"]}')
            return _FormatResult(
                lines=[
                    f'{self._stamp()} {self._badge("MIC", "1;36")} 检测到语音'
                    + (f' | {" | ".join(parts)}' if parts else '')
                ]
            )
        if payload.startswith('backend_warmup '):
            fields, _ = _parse_tokens(payload[len('backend_warmup ') :])
            if self._live_enabled:
                return _FormatResult()
            parts = [f'status {fields.get("status", "-")}']
            if 'elapsed_ms' in fields:
                parts.append(f'{fields["elapsed_ms"]}ms')
            if 'reason' in fields:
                parts.append(fields['reason'])
            return _FormatResult(lines=[f'{self._stamp()} {self._badge("WARMUP", "1;36")} {" | ".join(parts)}'])
        if payload.startswith('partial: '):
            if self._live_enabled:
                if self._live_pipeline.active_stage == 'idle':
                    self._live_pipeline.recording_started_at = time.monotonic()
                    self._live_pipeline.active_stage = 'recording'
                self._live_pipeline.asr_live_active = True
                return _FormatResult(live_line=self._pipeline_live_line())
            text = payload.split(': ', 1)[1]
            return _FormatResult(live_line=self._live_text('PARTIAL', '1;34', f'局部 {text}'))
        if payload.startswith('partial_typed '):
            fields, _ = _parse_tokens(payload[len('partial_typed ') :])
            if self._live_enabled:
                return _FormatResult()
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
            return _FormatResult(
                lines=[
                    f'{self._stamp()} {self._badge("STREAM", "1;34")} 已同步局部文本'
                    + (f' | {" | ".join(parts)}' if parts else '')
                ]
            )
        if payload.startswith('final: '):
            if self._live_enabled:
                return _FormatResult()
            return _FormatResult(
                lines=[
                    f'{self._stamp()} {self._badge("TYPE", "1;32")} 开始输入最终文本',
                    self._detail('文本', payload.split(': ', 1)[1], '1;32'),
                ]
            )
        if payload.startswith('timings '):
            fields, _ = _parse_tokens(payload[len('timings ') :])
            utterance = fields.get('utterance_id', '?')
            state = self._state(utterance)
            state.capture_ms = _as_int(fields.get('capture_ms'))
            state.flush_roundtrip_ms = _as_int(fields.get('flush_roundtrip_ms'))
            state.audio_ms = _as_int(fields.get('audio_ms'))
            state.asr_infer_ms = _as_int(fields.get('infer_ms'))
            state.context_capture_ms = max(state.context_capture_ms, _as_int(fields.get('context_capture_ms')))
            if fields.get('context_source') and fields.get('context_source') != '-':
                state.context_source = fields['context_source']
            state.postprocess_ms = _as_int(fields.get('postprocess_ms'))
            state.llm_ms = _as_int(fields.get('llm_ms'))
            state.llm_used = self._truthy(fields.get('llm_used'))
            state.type_ms = _as_int(fields.get('type_ms'))
            state.backend_total_ms = _as_int(fields.get('backend_total_ms'))
            state.partial_sent_count = _as_int(fields.get('partial_sent'))
            state.partial_skipped_count = _as_int(fields.get('partial_skipped'))
            state.llm_timeout_sec = _as_float(fields.get('llm_timeout_sec')) or state.llm_timeout_sec
            if fields.get('llm_provider') and fields.get('llm_provider') != '-':
                state.llm_provider = fields['llm_provider']
            if fields.get('llm_model') and fields.get('llm_model') != '-':
                state.llm_model = fields['llm_model']
            state.context_overlap_ms = max(0, state.context_capture_ms - state.context_wait_ms)
            if state.llm_used and state.llm_stream_used and state.llm_ms > 0:
                state.llm_stream_ms = max(0, state.llm_ms - state.llm_first_token_ms)
            state.bottleneck = self._detect_bottleneck(state)
            lines = self._build_perf_lines(state)
            log_events: list[_LogEvent] = []
            if not state.summary_written:
                log_events.append(_LogEvent(event='utterance_summary', fields=self._summary_fields(state)))
                state.summary_written = True
            return _FormatResult(lines=lines, log_events=log_events)
        return _FormatResult(lines=[line])

    def _build_perf_lines(self, state: _UtteranceRenderState) -> list[str]:
        utterance = state.utterance_id
        context_part = (
            f'ctx {max(state.context_overlap_ms, state.context_capture_ms) / 1000:.2f}s overlap / {state.context_wait_ms}ms wait'
            if state.context_capture_ms or state.context_wait_ms or state.context_overlap_ms
            else 'ctx off'
        )
        llm_first = f'llm first {state.llm_first_token_ms}ms' if state.llm_used else 'llm off'
        if state.llm_used and state.llm_stream_used:
            llm_tail = f'llm stream {state.llm_stream_ms}ms / {max(1, state.llm_stream_chunks)} chunks'
        elif state.llm_used:
            llm_tail = f'llm {state.llm_ms}ms'
        else:
            llm_tail = 'llm skipped'
        head_parts = [
            f'audio {state.audio_ms / 1000:.2f}s',
            context_part,
            f'asr {state.asr_infer_ms}ms',
            llm_first,
            llm_tail,
            f'type {state.type_ms}ms',
            f'flush {state.flush_roundtrip_ms}ms',
        ]
        detail_parts = [
            f'post {state.postprocess_ms}ms',
            f'backend {state.backend_total_ms}ms',
            f'context {state.context_source if state.context_source != "-" else "n/a"}',
            f'llm {"yes" if state.llm_used else "no"}',
        ]
        if (
            state.partial_preview_count
            or state.partial_jobs_started
            or state.partial_reused_chars
            or state.partial_sent_count
            or state.partial_skipped_count
        ):
            partial_parts = [f'partial shown {state.partial_preview_count}x']
            if state.partial_sent_count > 0:
                partial_parts.append(f'sent {state.partial_sent_count}')
            if state.partial_skipped_count > 0:
                partial_parts.append(f'skipped {state.partial_skipped_count}')
            if state.partial_jobs_started or state.partial_jobs_completed:
                partial_parts.append(f'jobs {state.partial_jobs_completed}/{state.partial_jobs_started}')
            if state.partial_reused_chars > 0:
                partial_parts.append(f'hit {state.partial_reused_chars}字')
            elif state.partial_stable_chars > 0:
                partial_parts.append(f'stable {state.partial_stable_chars}字')
            detail_parts.append(' '.join(partial_parts))
        if state.llm_timeout_sec > 0:
            detail_parts.append(f'timeout {state.llm_timeout_sec:g}s')
        why_label = self._BOTTLENECK_LABELS.get(state.bottleneck, state.bottleneck)
        why_reason = self._bottleneck_reason(state)
        if self._live_enabled:
            headline_parts = [
                '完成',
                f'flush {state.flush_roundtrip_ms}ms',
                f'final {state.final_chars}字',
            ]
            lines = [
                f'{self._stamp()} {self._badge(f"RUN #{utterance}", "1;36")} {" | ".join(headline_parts)}'
            ]
            lines.append(self._detail('录音', f'{state.audio_ms / 1000:.2f}s', '1;36'))
            asr_value = f'{state.asr_infer_ms}ms'
            if state.raw_chars > 0:
                asr_value = f'{asr_value} | {state.raw_chars}字'
            lines.append(self._detail('转写', asr_value, '1;34'))
            if state.llm_used:
                llm_value = f'{state.llm_ms}ms'
                if state.llm_first_token_ms > 0:
                    llm_value = f'{llm_value} | 首字 {state.llm_first_token_ms}ms'
                if state.llm_stream_chunks > 0:
                    llm_value = f'{llm_value} | {state.llm_stream_chunks}块'
            else:
                llm_value = '未使用'
            lines.append(self._detail('润色', llm_value, '1;35'))
            if state.last_final_text:
                lines.append(self._detail('输出', state.last_final_text, '1;32'))
            if state.last_diff_summary and state.last_diff_summary != '无关键改动':
                lines.append(self._detail('改动', state.last_diff_summary, '1;33'))
            if state.bottleneck != 'balanced':
                lines.append(self._detail('判断', f'{why_label} | {why_reason}', '1;33'))
            self._reset_live_pipeline()
            self._live_active = False
            return lines
        lines = [f'{self._stamp()} {self._badge(f"PERF #{utterance}", "1;36")} {" | ".join(head_parts)}']
        if not self._live_enabled:
            lines.append(self._detail('细节', ' | '.join(detail_parts), '1;36'))
            lines.append(f'{self._stamp()} {self._badge(f"WHY #{utterance}", "1;33")} 瓶颈 {why_label} | {why_reason}')
            model_parts = [part for part in (state.llm_provider, state.llm_model) if part and part != '-']
            if model_parts:
                lines.append(self._detail('模型', ' / '.join(model_parts), '1;36'))
        elif state.bottleneck != 'balanced':
            lines.append(f'{self._stamp()} {self._badge(f"WHY #{utterance}", "1;33")} 瓶颈 {why_label} | {why_reason}')
        return lines

    def _detect_bottleneck(self, state: _UtteranceRenderState) -> str:
        if state.context_budget_state in {'timeout', 'expired'} and state.context_wait_ms >= 150:
            return 'context_budget'
        if state.llm_used and state.llm_first_token_ms >= max(800, state.asr_infer_ms * 2):
            return 'llm_first_token'
        if state.asr_infer_ms >= 800:
            return 'asr_infer'
        if state.llm_used and state.llm_stream_used and state.llm_stream_ms >= 600:
            return 'llm_stream_tail'
        if state.type_ms >= 150:
            return 'text_injection'
        return 'balanced'

    def _bottleneck_reason(self, state: _UtteranceRenderState) -> str:
        if state.bottleneck == 'context_budget':
            return f'上下文等待 {state.context_wait_ms}ms，预算状态 {state.context_budget_state}'
        if state.bottleneck == 'llm_first_token':
            return f'首字 {state.llm_first_token_ms}ms，明显高于 ASR {state.asr_infer_ms}ms'
        if state.bottleneck == 'asr_infer':
            return f'ASR 推理 {state.asr_infer_ms}ms'
        if state.bottleneck == 'llm_stream_tail':
            return f'持续生成 {state.llm_stream_ms}ms / {max(1, state.llm_stream_chunks)} chunks'
        if state.bottleneck == 'text_injection':
            return f'输入阶段 {state.type_ms}ms'
        return '各阶段耗时分布均衡'

    def _summary_fields(self, state: _UtteranceRenderState) -> dict[str, object]:
        return {
            'utterance_id': int(state.utterance_id or '0'),
            'audio_ms': state.audio_ms,
            'capture_ms': state.capture_ms,
            'flush_roundtrip_ms': state.flush_roundtrip_ms,
            'context_capture_ms': state.context_capture_ms,
            'context_wait_ms': state.context_wait_ms,
            'context_overlap_ms': state.context_overlap_ms,
            'context_status': state.context_status,
            'context_budget_state': state.context_budget_state,
            'context_source': None if state.context_source == '-' else state.context_source,
            'context_surface': None if state.context_surface == '-' else state.context_surface,
            'context_chars': state.context_chars,
            'asr_infer_ms': state.asr_infer_ms,
            'asr_total_ms': state.asr_total_ms,
            'llm_used': state.llm_used,
            'llm_stream_used': state.llm_stream_used,
            'llm_first_token_ms': state.llm_first_token_ms,
            'llm_ms': state.llm_ms,
            'llm_stream_ms': state.llm_stream_ms,
            'llm_stream_chunks': state.llm_stream_chunks,
            'type_ms': state.type_ms,
            'backend_total_ms': state.backend_total_ms,
            'postprocess_ms': state.postprocess_ms,
            'bottleneck': state.bottleneck,
            'final_chars': state.final_chars,
            'raw_chars': state.raw_chars,
            'partial_preview_count': state.partial_preview_count,
            'partial_stable_advance_count': state.partial_stable_advance_count,
            'partial_jobs_started': state.partial_jobs_started,
            'partial_jobs_completed': state.partial_jobs_completed,
            'partial_reused_chars': state.partial_reused_chars,
            'partial_stable_chars': state.partial_stable_chars,
            'partial_sent_count': state.partial_sent_count,
            'partial_skipped_count': state.partial_skipped_count,
            'commit_mode': state.commit_mode,
            'guard_fallback': state.guard_fallback,
            'guard_reason': state.guard_reason,
        }

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
    agent_log_handle: TextIO | None = None,
    source: str,
    echo: bool,
    lock: threading.Lock,
    formatter: _DictationLogFormatter | None = None,
) -> None:
    for line in iter(stream.readline, ''):
        result: _FormatResult | None = None
        should_process = (
            _should_echo_server_line(line) if source == 'server' else _should_echo_helper_line(line)
        )
        with lock:
            log_handle.write(line)
            if formatter is not None and should_process:
                result = formatter.format(source, line)
                for event in result.log_events:
                    log_handle.write(_serialize_log_event(event=event.event, **event.fields))
                    if agent_log_handle is not None:
                        compact = _serialize_agent_log_event(event=event.event, **event.fields)
                        if compact is not None:
                            agent_log_handle.write(compact)
            log_handle.flush()
            if agent_log_handle is not None:
                agent_log_handle.flush()
            if not echo:
                continue
            if not should_process:
                continue
            if formatter is None:
                result = _FormatResult(lines=[line.rstrip('\n')])
            if result is None:
                continue
            if result.finalize_live_before:
                sys.stderr.write('\n')
            if result.live_line is not None:
                sys.stderr.write(f'{_CLEAR_LINE}{result.live_line}')
                sys.stderr.flush()
                continue
            if not result.lines:
                continue
            for rendered in result.lines:
                sys.stderr.write(f'{rendered}\n')
            sys.stderr.flush()
        if not echo:
            continue


def _animate_live_output(
    formatter: _DictationLogFormatter,
    *,
    lock: threading.Lock,
    stop_event: threading.Event,
    interval_sec: float = 0.12,
) -> None:
    while not stop_event.wait(interval_sec):
        with lock:
            line = formatter.live_heartbeat()
            if line is None:
                continue
            sys.stderr.write(f'{_CLEAR_LINE}{line}')
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


def tail_agent_log(config: VoxConfig, lines: int = 80) -> str:
    path = dictation_agent_log_path(config)
    if not path.exists():
        return ''
    with path.open('r', encoding='utf-8', errors='ignore') as handle:
        return ''.join(deque(handle, maxlen=lines)).strip()


def _read_agent_log_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []

    events: list[dict[str, object]] = []
    with path.open('r', encoding='utf-8', errors='ignore') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _avg_int(values: list[int]) -> int:
    if not values:
        return 0
    return int(round(sum(values) / len(values)))


def _percentile_int(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((percentile / 100) * (len(ordered) - 1)))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def _metric_summary(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    return {
        'n': len(values),
        'avg': _avg_int(values),
        'p50': _percentile_int(values, 50),
        'p95': _percentile_int(values, 95),
        'max': max(values),
    }


def _int_field(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if value in (None, ''):
        return 0
    try:
        return int(value)
    except Exception:
        return 0


def _bool_field(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _metric_values(
    utterances: list[dict[str, object]],
    key: str,
    *,
    include_zero: bool = True,
    predicate: Callable[[dict[str, object]], bool] | None = None,
) -> list[int]:
    values: list[int] = []
    for utterance in utterances:
        if predicate is not None and not predicate(utterance):
            continue
        if key not in utterance:
            continue
        value = _int_field(utterance, key)
        if value == 0 and not include_zero:
            continue
        values.append(value)
    return values


def _metric_trend(
    utterances: list[dict[str, object]],
    key: str,
    *,
    include_zero: bool = True,
    predicate: Callable[[dict[str, object]], bool] | None = None,
) -> dict[str, int] | None:
    if len(utterances) < 4:
        return None
    midpoint = len(utterances) // 2
    first_values = _metric_values(
        utterances[:midpoint],
        key,
        include_zero=include_zero,
        predicate=predicate,
    )
    last_values = _metric_values(
        utterances[midpoint:],
        key,
        include_zero=include_zero,
        predicate=predicate,
    )
    if not first_values or not last_values:
        return None
    first_avg = _avg_int(first_values)
    last_avg = _avg_int(last_values)
    return {
        'first_avg': first_avg,
        'last_avg': last_avg,
        'delta': last_avg - first_avg,
    }


def _expand_agent_launch(payload: dict[str, object]) -> dict[str, object]:
    return {
        'session_id': payload.get('s'),
        'lang': payload.get('lang'),
        'asr_model': payload.get('am'),
        'partial_interval_ms': _int_field(payload, 'pi'),
        'type_partial': _bool_field(payload, 'tp'),
        'tty_verbose': _bool_field(payload, 'tv'),
        'helper_version': payload.get('hv'),
    }


def _expand_agent_config(payload: dict[str, object]) -> dict[str, object]:
    expanded = {
        'llm_enabled': _bool_field(payload, 'lu'),
        'llm_stream': _bool_field(payload, 'ls'),
        'provider': payload.get('lp'),
        'model': payload.get('lm'),
        'timeout_sec': _int_field(payload, 'lt'),
        'context_enabled': _bool_field(payload, 'ce'),
        'context_max_chars': _int_field(payload, 'cc'),
        'hotwords_enabled': _bool_field(payload, 'he'),
        'hotword_entries': _int_field(payload, 'hn'),
        'rewrite_aliases': _bool_field(payload, 'hr'),
        'case_sensitive': _bool_field(payload, 'cs'),
        'hints_enabled': _bool_field(payload, 'ie'),
        'hint_count': _int_field(payload, 'in'),
    }
    if 'dp' in payload:
        expanded['prompt_preset'] = payload.get('dp')
    if 'cp' in payload:
        expanded['custom_prompt_enabled'] = _bool_field(payload, 'cp')
    return expanded


def _expand_agent_utterance(payload: dict[str, object]) -> dict[str, object]:
    return {
        'utterance_id': _int_field(payload, 'u'),
        'audio_ms': _int_field(payload, 'aud'),
        'capture_ms': _int_field(payload, 'cap'),
        'flush_ms': _int_field(payload, 'fl'),
        'context_capture_ms': _int_field(payload, 'ctxc'),
        'context_wait_ms': _int_field(payload, 'ctxw'),
        'context_overlap_ms': _int_field(payload, 'ctxo'),
        'context_status': payload.get('ctxs'),
        'context_budget_state': payload.get('ctxb'),
        'context_source': payload.get('src'),
        'context_surface': payload.get('srf'),
        'context_chars': _int_field(payload, 'ctxr'),
        'asr_ms': _int_field(payload, 'asr'),
        'asr_total_ms': _int_field(payload, 'asrt'),
        'llm_used': _bool_field(payload, 'lu'),
        'llm_stream': _bool_field(payload, 'ls'),
        'llm_first_token_ms': _int_field(payload, 'ft'),
        'llm_ms': _int_field(payload, 'llm'),
        'llm_stream_tail_ms': _int_field(payload, 'lst'),
        'llm_stream_chunks': _int_field(payload, 'lsch'),
        'type_ms': _int_field(payload, 'ty'),
        'backend_ms': _int_field(payload, 'be'),
        'postprocess_ms': _int_field(payload, 'post'),
        'bottleneck': payload.get('bot'),
        'final_chars': _int_field(payload, 'fin'),
        'raw_chars': _int_field(payload, 'raw'),
        'partial_preview_count': _int_field(payload, 'pp'),
        'partial_stable_advance_count': _int_field(payload, 'psa'),
        'partial_jobs_started': _int_field(payload, 'pjs'),
        'partial_jobs_completed': _int_field(payload, 'pjc'),
        'partial_reused_chars': _int_field(payload, 'prc'),
        'partial_stable_chars': _int_field(payload, 'psc'),
        'partial_sent_count': _int_field(payload, 'psn'),
        'partial_skipped_count': _int_field(payload, 'psk'),
        'commit_mode': payload.get('cm'),
        'guard_fallback': _bool_field(payload, 'gf'),
        'guard_reason': payload.get('gr'),
    }


def _expand_agent_error(payload: dict[str, object]) -> dict[str, object]:
    event = payload.get('e')
    if event == 'pe':
        return {
            'event': 'postprocess_error',
            'utterance_id': _int_field(payload, 'u'),
            'llm_ms': _int_field(payload, 'llm'),
            'timeout_sec': _int_field(payload, 'lt'),
            'provider': payload.get('lp'),
            'model': payload.get('lm'),
            'error': payload.get('err'),
        }
    if event == 'sf':
        return {
            'event': 'server_failed',
            'session_id': payload.get('s'),
            'error': payload.get('err'),
        }
    return {'event': str(event or 'unknown')}


def _metric_stat(metrics: dict[str, dict[str, int]], metric: str, stat: str) -> int:
    return int(metrics.get(metric, {}).get(stat, 0) or 0)


def _build_partial_pipeline_summary(utterance_events: list[dict[str, object]]) -> dict[str, object]:
    analyzed = len(utterance_events)
    instrumented = any(
        any(key in event for key in ('pp', 'psa', 'pjs', 'pjc', 'prc', 'psc', 'psn', 'psk'))
        for event in utterance_events
    )
    preview_total = sum(_int_field(event, 'pp') for event in utterance_events)
    stable_total = sum(_int_field(event, 'psa') for event in utterance_events)
    jobs_started_total = sum(_int_field(event, 'pjs') for event in utterance_events)
    jobs_completed_total = sum(_int_field(event, 'pjc') for event in utterance_events)
    sent_total = sum(_int_field(event, 'psn') for event in utterance_events)
    skipped_total = sum(_int_field(event, 'psk') for event in utterance_events)
    reused_chars_values = [_int_field(event, 'prc') for event in utterance_events]
    stable_chars_values = [_int_field(event, 'psc') for event in utterance_events]
    hit_utterances = sum(1 for value in reused_chars_values if value > 0)
    active_utterances = sum(
        1
        for event in utterance_events
        if _int_field(event, 'pp') > 0
        or _int_field(event, 'psn') > 0
        or _int_field(event, 'pjs') > 0
        or _int_field(event, 'psa') > 0
    )
    hit_rate = int(round((hit_utterances / analyzed) * 100)) if analyzed else 0
    completion_rate = int(round((jobs_completed_total / jobs_started_total) * 100)) if jobs_started_total else 0
    attempted_total = sent_total + skipped_total
    skip_rate = int(round((skipped_total / attempted_total) * 100)) if attempted_total else 0
    reused_nonzero = [value for value in reused_chars_values if value > 0]

    return {
        'instrumented': instrumented,
        'active': active_utterances > 0,
        'analyzed_utterances': analyzed,
        'active_utterances': active_utterances,
        'preview_total': preview_total,
        'stable_advances_total': stable_total,
        'sent_total': sent_total,
        'skipped_total': skipped_total,
        'skip_rate': skip_rate,
        'jobs_started_total': jobs_started_total,
        'jobs_completed_total': jobs_completed_total,
        'jobs_completion_rate': completion_rate,
        'hit_utterances': hit_utterances,
        'hit_rate': hit_rate,
        'reused_chars_total': sum(reused_chars_values),
        'reused_chars_avg': _avg_int(reused_nonzero) if reused_nonzero else 0,
        'reused_chars_max': max(reused_chars_values) if reused_chars_values else 0,
        'stable_chars_max': max(stable_chars_values) if stable_chars_values else 0,
    }


def _build_digest_diagnosis(
    *,
    metrics: dict[str, dict[str, int]],
    bottlenecks: list[dict[str, object]],
    config: dict[str, object] | None,
    launch: dict[str, object] | None,
    partial_pipeline: dict[str, object],
) -> dict[str, object]:
    sample_count = _metric_stat(metrics, 'capture_ms', 'n')
    if sample_count <= 0:
        return {
            'status': 'no_data',
            'summary': '没有可分析的 utterance 样本',
            'signals': [],
            'evidence': {},
            'next_actions': [],
        }

    primary = str(bottlenecks[0]['name']) if bottlenecks else 'balanced'
    primary_count = int(bottlenecks[0]['count']) if bottlenecks else 0
    confidence = 'high' if primary_count * 2 >= sample_count else 'medium'

    ft_avg = _metric_stat(metrics, 'llm_first_token_ms', 'avg')
    ft_p95 = _metric_stat(metrics, 'llm_first_token_ms', 'p95')
    llm_p95 = _metric_stat(metrics, 'llm_ms', 'p95')
    llm_tail_p95 = _metric_stat(metrics, 'llm_stream_tail_ms', 'p95')
    ctxw_max = _metric_stat(metrics, 'context_wait_ms', 'max')
    asr_p95 = _metric_stat(metrics, 'asr_ms', 'p95')
    type_max = _metric_stat(metrics, 'type_ms', 'max')
    flush_p95 = _metric_stat(metrics, 'flush_ms', 'p95')
    backend_p95 = _metric_stat(metrics, 'backend_ms', 'p95')

    signals: list[str] = []
    if ctxw_max <= 50:
        signals.append('context_not_blocking')
    elif ctxw_max >= 150 or primary == 'context_budget':
        signals.append('context_wait_blocking')

    if type_max <= 80:
        signals.append('type_fast')
    elif type_max >= 150 or primary == 'text_injection':
        signals.append('type_slow')

    if asr_p95 <= 700:
        signals.append('asr_ok')
    elif asr_p95 >= 900 or primary == 'asr_infer':
        signals.append('asr_slow')

    if primary == 'llm_first_token' or ft_p95 >= 1200 or ft_avg >= max(800, asr_p95 * 2):
        signals.append('llm_first_token_slow')

    if primary == 'llm_stream_tail' or llm_tail_p95 >= 600:
        signals.append('llm_stream_tail_slow')

    if flush_p95 >= 1800:
        signals.append('flush_high')
    if backend_p95 >= 2200:
        signals.append('backend_high')

    context_enabled = bool(config and config.get('context_enabled'))
    llm_enabled = bool(config and config.get('llm_enabled'))
    partial_interval_ms = int((launch or {}).get('partial_interval_ms') or 0)
    partial_active = bool(partial_pipeline.get('active'))
    partial_preview_total = int(partial_pipeline.get('preview_total') or 0)
    partial_sent_total = int(partial_pipeline.get('sent_total') or 0)
    partial_skipped_total = int(partial_pipeline.get('skipped_total') or 0)
    partial_skip_rate = int(partial_pipeline.get('skip_rate') or 0)
    partial_hit_rate = int(partial_pipeline.get('hit_rate') or 0)
    partial_jobs_started = int(partial_pipeline.get('jobs_started_total') or 0)
    partial_jobs_completed = int(partial_pipeline.get('jobs_completed_total') or 0)
    partial_reused_chars_max = int(partial_pipeline.get('reused_chars_max') or 0)

    if partial_interval_ms > 0 and bool(partial_pipeline.get('instrumented')):
        if not partial_active or (partial_sent_total == 0 and partial_preview_total == 0):
            signals.append('partial_preview_idle')
        elif partial_skipped_total >= max(3, partial_sent_total):
            signals.append('partial_backpressure_high')
        elif partial_preview_total == 0 and partial_sent_total > 0:
            signals.append('partial_preview_no_response')
        else:
            signals.append('partial_preview_healthy')
        if partial_jobs_started > 0 and partial_jobs_completed < partial_jobs_started:
            signals.append('partial_pipeline_incomplete')

    if 'partial_backpressure_high' in signals and ('flush_high' in signals or 'backend_high' in signals):
        summary = '录音期 partial 背压明显，后续 flush 容易排队变慢'
    elif 'llm_first_token_slow' in signals:
        summary = 'LLM 首包慢；上下文未阻塞，ASR 与文本注入基本正常'
    elif 'partial_preview_no_response' in signals:
        summary = 'partial 预览请求发出了，但返回没有及时跟上'
    elif 'context_wait_blocking' in signals:
        summary = '上下文等待在阻塞最终输出'
    elif 'llm_stream_tail_slow' in signals:
        summary = 'LLM 持续生成偏慢，尾段输出拖长'
    elif 'asr_slow' in signals:
        summary = 'ASR 推理偏慢'
    elif 'type_slow' in signals:
        summary = '最终文本注入偏慢'
    elif 'flush_high' in signals or 'backend_high' in signals:
        summary = '整体 backend 往返偏高，但没有单一硬瓶颈'
    else:
        summary = '当前窗口内没有明显单一瓶颈'

    next_actions: list[str] = []
    if 'partial_backpressure_high' in signals:
        next_actions.append('先确认 partial 是否已经做成单飞；后端忙时要直接丢掉新的 partial，不要继续排队')
        next_actions.append('确认停录音时不再额外补发 partial，避免 final flush 被最后一条 partial 挤住')
    elif 'partial_preview_no_response' in signals:
        next_actions.append('核对 partial 请求与返回计数，确认预览没有被长音频推理拖住')
    elif llm_enabled and 'llm_first_token_slow' in signals:
        next_actions.append('先做一次关闭 LLM 的对照测试，确认尾延迟是否主要来自润色')
        next_actions.append('优先换更快的 LLM 或 provider，再复测首包时间')
        if context_enabled:
            next_actions.append('把 context_max_chars 下调后复测，确认 prompt 长度是否影响首包')
    elif 'context_wait_blocking' in signals:
        next_actions.append('先降低 context_max_chars，必要时临时关闭 context 复测')
    elif 'llm_stream_tail_slow' in signals:
        next_actions.append('优先缩短输出长度或换更快的流式模型')
    elif 'asr_slow' in signals:
        next_actions.append('先对照更轻的 ASR 模型或检查本机负载')
    elif 'type_slow' in signals:
        next_actions.append('重点检查目标应用的 Accessibility 注入链路')
    elif 'flush_high' in signals or 'backend_high' in signals:
        next_actions.append('重点对照 flush / backend 往返是否受 provider 抖动影响')

    evidence = {
        'samples': sample_count,
        'primary_bottleneck_count': primary_count,
        'llm_first_token_avg_ms': ft_avg,
        'llm_first_token_p95_ms': ft_p95,
        'llm_p95_ms': llm_p95,
        'llm_stream_tail_p95_ms': llm_tail_p95,
        'context_wait_max_ms': ctxw_max,
        'asr_p95_ms': asr_p95,
        'type_max_ms': type_max,
        'flush_p95_ms': flush_p95,
        'backend_p95_ms': backend_p95,
        'partial_preview_total': partial_preview_total,
        'partial_sent_total': partial_sent_total,
        'partial_skipped_total': partial_skipped_total,
        'partial_skip_rate': partial_skip_rate,
        'partial_hit_rate': partial_hit_rate,
        'partial_jobs_started_total': partial_jobs_started,
        'partial_jobs_completed_total': partial_jobs_completed,
        'partial_reused_chars_max': partial_reused_chars_max,
    }

    return {
        'status': 'ok',
        'primary': primary,
        'confidence': confidence,
        'summary': summary,
        'signals': signals,
        'evidence': evidence,
        'next_actions': next_actions,
    }


def build_dictation_agent_digest(
    config: VoxConfig,
    *,
    utterances: int = 20,
    slowest: int = 3,
    errors: int = 5,
) -> dict[str, object]:
    path = dictation_agent_log_path(config)
    events = _read_agent_log_events(path)
    cfg_events = [event for event in events if event.get('e') == 'cfg']
    launch_events = [event for event in events if event.get('e') == 'ls']
    utterance_events = [event for event in events if event.get('e') == 'u']
    error_events = [event for event in events if event.get('e') in {'pe', 'sf'}]

    requested_utterances = max(1, int(utterances))
    requested_slowest = max(1, int(slowest))
    requested_errors = max(0, int(errors))

    if requested_utterances:
        utterance_events = utterance_events[-requested_utterances:]
    launch = _expand_agent_launch(launch_events[-1]) if launch_events else None
    digest_config = _expand_agent_config(cfg_events[-1]) if cfg_events else None

    metrics: dict[str, dict[str, int]] = {}
    metric_specs: list[tuple[str, str, bool, Callable[[dict[str, object]], bool] | None]] = [
        ('audio_ms', 'aud', True, None),
        ('capture_ms', 'cap', True, None),
        ('flush_ms', 'fl', True, None),
        ('context_capture_ms', 'ctxc', True, None),
        ('context_wait_ms', 'ctxw', True, None),
        ('asr_ms', 'asr', True, None),
        ('asr_total_ms', 'asrt', True, None),
        ('llm_first_token_ms', 'ft', False, lambda event: _bool_field(event, 'lu')),
        ('llm_ms', 'llm', False, lambda event: _bool_field(event, 'lu')),
        ('llm_stream_tail_ms', 'lst', False, lambda event: _bool_field(event, 'ls')),
        ('type_ms', 'ty', True, None),
        ('backend_ms', 'be', True, None),
        ('postprocess_ms', 'post', True, None),
        ('final_chars', 'fin', True, None),
        ('raw_chars', 'raw', True, None),
        ('partial_preview_count', 'pp', True, None),
        ('partial_stable_advance_count', 'psa', True, None),
        ('partial_jobs_started', 'pjs', True, None),
        ('partial_jobs_completed', 'pjc', True, None),
        ('partial_reused_chars', 'prc', True, None),
        ('partial_stable_chars', 'psc', True, None),
        ('partial_sent_count', 'psn', True, None),
        ('partial_skipped_count', 'psk', True, None),
    ]
    for output_key, compact_key, include_zero, predicate in metric_specs:
        values = _metric_values(
            utterance_events,
            compact_key,
            include_zero=include_zero,
            predicate=predicate,
        )
        summary = _metric_summary(values)
        if summary is not None:
            metrics[output_key] = summary

    bottleneck_counts: dict[str, int] = {}
    for utterance_event in utterance_events:
        bottleneck = str(utterance_event.get('bot') or 'unknown')
        bottleneck_counts[bottleneck] = bottleneck_counts.get(bottleneck, 0) + 1
    bottlenecks = [
        {'name': name, 'count': count}
        for name, count in sorted(
            bottleneck_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    trends: dict[str, dict[str, int]] = {}
    trend_specs: list[tuple[str, str, bool, Callable[[dict[str, object]], bool] | None]] = [
        ('capture_ms', 'cap', True, None),
        ('flush_ms', 'fl', True, None),
        ('backend_ms', 'be', True, None),
        ('llm_ms', 'llm', False, lambda event: _bool_field(event, 'lu')),
        ('context_wait_ms', 'ctxw', True, None),
        ('partial_reused_chars', 'prc', True, None),
        ('partial_skipped_count', 'psk', True, None),
    ]
    for output_key, compact_key, include_zero, predicate in trend_specs:
        trend = _metric_trend(
            utterance_events,
            compact_key,
            include_zero=include_zero,
            predicate=predicate,
        )
        if trend is not None:
            trends[output_key] = trend

    slowest_utterances = [
        _expand_agent_utterance(payload)
        for payload in sorted(
            utterance_events,
            key=lambda event: (_int_field(event, 'cap'), _int_field(event, 'u')),
            reverse=True,
        )[:requested_slowest]
    ]

    recent_errors = [
        _expand_agent_error(payload)
        for payload in error_events[-requested_errors:]
    ]
    partial_pipeline = _build_partial_pipeline_summary(utterance_events)

    first_utterance_id = _int_field(utterance_events[0], 'u') if utterance_events else None
    last_utterance_id = _int_field(utterance_events[-1], 'u') if utterance_events else None

    return {
        'log_path': str(path),
        'exists': path.exists(),
        'total_events': len(events),
        'window': {
            'requested_utterances': requested_utterances,
            'analyzed_utterances': len(utterance_events),
            'first_utterance_id': first_utterance_id,
            'last_utterance_id': last_utterance_id,
        },
        'launch': launch,
        'config': digest_config,
        'metrics': metrics,
        'partial_pipeline': partial_pipeline,
        'bottlenecks': bottlenecks,
        'trends': trends,
        'diagnosis': _build_digest_diagnosis(
            metrics=metrics,
            bottlenecks=bottlenecks,
            config=digest_config,
            launch=launch,
            partial_pipeline=partial_pipeline,
        ),
        'slowest_utterances': slowest_utterances,
        'recent_errors': recent_errors,
    }


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
        background_partial_streaming=False,
    )
    ensure_dictation_dirs(config)
    port = port or pick_free_port(host)
    log_path = dictation_session_log_path(config)
    agent_log_path = dictation_agent_log_path(config)
    _prepare_dictation_log(log_path, config)
    _prepare_dictation_log(agent_log_path, config)
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
        '--verbose',
    ]
    if type_partial:
        helper_cmd.append('--type-partial')
    if subtitle_overlay:
        helper_cmd.append('--subtitle-overlay')

    _write_dual_log_event(
        log_path,
        agent_path=agent_log_path,
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
        tty_verbose=verbose,
        debug_log_enabled=True,
    )

    with log_path.open('a', encoding='utf-8') as log_handle, agent_log_path.open(
        'a',
        encoding='utf-8',
    ) as agent_log_handle:
        relay_threads: list[threading.Thread] = []
        relay_lock = threading.Lock()
        formatter = _DictationLogFormatter(sys.stderr)
        live_stop_event = threading.Event()
        live_thread: threading.Thread | None = None
        if verbose and formatter.live_updates_enabled():
            live_thread = threading.Thread(
                target=_animate_live_output,
                kwargs={
                    'formatter': formatter,
                    'lock': relay_lock,
                    'stop_event': live_stop_event,
                },
                daemon=True,
            )
            live_thread.start()
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
                'agent_log_handle': agent_log_handle,
                'source': 'server',
                'echo': verbose,
                'lock': relay_lock,
                'formatter': formatter,
            },
            daemon=True,
        )
        server_relay_thread.start()
        relay_threads.append(server_relay_thread)
        _write_dual_log_event(
            log_path,
            agent_path=agent_log_path,
            event='launch.server_started',
            session_id=session_id,
            server_pid=getattr(server_proc, 'pid', None),
            server_cmd=server_cmd,
        )
        try:
            wait_for_session_server(host, port, server_proc=server_proc)
        except Exception as error:
            _write_dual_log_event(
                log_path,
                agent_path=agent_log_path,
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
            _write_dual_log_event(
                log_path,
                agent_path=agent_log_path,
                event='launch.helper_started',
                session_id=session_id,
                helper_cmd=helper_cmd,
            )
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
                    'agent_log_handle': agent_log_handle,
                    'source': 'helper',
                    'echo': verbose,
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
            _write_dual_log_event(
                log_path,
                agent_path=agent_log_path,
                event='launch.helper_exited',
                session_id=session_id,
                helper_exit_code=exit_code,
            )
            return exit_code
        finally:
            live_stop_event.set()
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
            for relay_thread in relay_threads:
                relay_thread.join(timeout=1)
            if live_thread is not None:
                live_thread.join(timeout=1)
            _write_dual_log_event(
                log_path,
                agent_path=agent_log_path,
                event='launch.server_exited',
                session_id=session_id,
                server_exit_code=server_proc.returncode,
            )
