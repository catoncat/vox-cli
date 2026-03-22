from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import websockets
from websockets.server import WebSocketServerProtocol

from ..config import VoxConfig, resolve_dictation_prompt_selection
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock
from .asr_service import _extract_text, _map_language
from .dictation_postprocess_service import (
    DictationPostprocessResult,
    DictationTextPostprocessor,
    apply_dictation_transforms,
    build_dictation_postprocessor,
    has_dictation_hints,
    has_dictation_hotwords,
    has_dictation_transforms,
)
from .dictation_context_service import (
    DictationContext,
    DictationContextSnapshot,
    capture_dictation_context_snapshot,
)
from .model_service import ensure_model_downloaded, resolve_model

IDLE_WARMUP_AFTER_SEC = 45.0
IDLE_WARMUP_AUDIO_MS = 200
PARTIAL_STABLE_GUARD_CHARS = 6
PARTIAL_STABLE_MIN_CHARS = 8
PARTIAL_STABLE_MIN_ADVANCE_CHARS = 4
STABLE_BREAK_CHARS = frozenset(' \t\r\n,.;:!?)]}>，。；：！？、》】）』」')
TRIVIAL_COMMIT_SUFFIX_CHARS = frozenset(' \t\r\n,.;:!?)]}>，。；：！？、》】）』」…')


@dataclass
class RealtimeTranscript:
    text: str
    is_partial: bool
    language: str | None
    segments: list[dict[str, Any]] | None = None
    utterance_id: int | None = None
    timings: dict[str, Any] | None = None


@dataclass
class PendingContextCapture:
    task: asyncio.Task[DictationContextSnapshot]
    started_at: float


@dataclass
class IncrementalDictationState:
    epoch: int = 0
    last_partial_text: str = ''
    stable_raw_text: str = ''
    submitted_raw_text: str = ''
    completed_raw_text: str = ''
    completed_text: str = ''
    queued_raw_text: str | None = None
    queued_language: str | None = None
    task: asyncio.Task[tuple[int, str, DictationPostprocessResult]] | None = None
    context_snapshot: DictationContextSnapshot | None = None
    completed_candidate: IncrementalPostprocessCandidate | None = None


@dataclass
class IncrementalPostprocessCandidate:
    raw_text: str
    result: DictationPostprocessResult
    language: str | None = None
    context_snapshot: DictationContextSnapshot | None = None


class RealtimeASRSession:
    def __init__(
        self,
        model: Any,
        language: str | None,
        sample_rate: int = 16_000,
        *,
        idle_warmup_after_sec: float = IDLE_WARMUP_AFTER_SEC,
        warmup_audio_ms: int = IDLE_WARMUP_AUDIO_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.model = model
        self.language = _map_language(language)
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []
        self.idle_warmup_after_sec = max(0.0, idle_warmup_after_sec)
        self.warmup_audio_ms = max(0, warmup_audio_ms)
        self._clock = clock or time.monotonic
        self._last_generate_at: float | None = None

    def append_pcm16(self, payload: bytes) -> None:
        if not payload:
            return
        chunk = np.frombuffer(payload, dtype=np.int16)
        if chunk.size == 0:
            return
        self._chunks.append(chunk.astype(np.float32) / 32768.0)

    def reset(self) -> None:
        self._chunks.clear()

    def has_audio(self) -> bool:
        return any(chunk.size for chunk in self._chunks)

    def _concat_audio(self) -> np.ndarray | None:
        if not self.has_audio():
            return None
        if len(self._chunks) == 1:
            return self._chunks[0]
        return np.concatenate(self._chunks)

    def _build_decode_options(self) -> dict[str, object]:
        decode_options: dict[str, object] = {}
        if self.language:
            decode_options['language'] = self.language
        return decode_options

    def _mark_generated(self) -> None:
        self._last_generate_at = self._clock()

    def _warmup_audio(self) -> np.ndarray:
        warmup_samples = max(1, int(self.sample_rate * self.warmup_audio_ms / 1000))
        return np.zeros(warmup_samples, dtype=np.float32)

    def warmup(
        self,
        *,
        force: bool = False,
        allow_first_use: bool = False,
    ) -> dict[str, Any] | None:
        if self.warmup_audio_ms <= 0:
            return None
        if force:
            needs_warmup = True
            if self._last_generate_at is None:
                reason = 'forced:first-use'
            else:
                reason = f'forced:idle={self._clock() - self._last_generate_at:.1f}s'
        elif self._last_generate_at is None:
            needs_warmup = allow_first_use
            reason = 'first-use'
        else:
            idle_for = self._clock() - self._last_generate_at
            needs_warmup = idle_for >= self.idle_warmup_after_sec
            reason = f'idle={idle_for:.1f}s'

        if not needs_warmup:
            return None

        started_at = self._clock()
        self.model.generate(self._warmup_audio(), **self._build_decode_options())
        elapsed_ms = int((self._clock() - started_at) * 1000)
        self._mark_generated()
        print(f'[session-server] warmup completed reason={reason} elapsed_ms={elapsed_ms}', flush=True)
        return {'elapsed_ms': elapsed_ms, 'reason': reason}

    def transcribe(self, *, partial: bool, utterance_id: int | None = None) -> RealtimeTranscript:
        audio = self._concat_audio()
        if audio is None or audio.size == 0:
            return RealtimeTranscript(
                text='',
                is_partial=partial,
                language=self.language,
                utterance_id=utterance_id,
            )

        warmup_stats = self.warmup()
        decode_options = self._build_decode_options()
        infer_started_at = self._clock()
        result = self.model.generate(audio, **decode_options)
        infer_elapsed_ms = int((self._clock() - infer_started_at) * 1000)
        self._mark_generated()
        text = _extract_text(result)
        segments = None
        if hasattr(result, 'segments'):
            raw_segments = getattr(result, 'segments')
            try:
                segments = [
                    {
                        'start': float(seg['start']),
                        'end': float(seg['end']),
                        'text': str(seg['text']).strip(),
                    }
                    for seg in raw_segments
                ]
            except Exception:
                segments = None

        transcript = RealtimeTranscript(
            text=text,
            is_partial=partial,
            language=(getattr(result, 'language', None) or self.language),
            segments=segments,
            utterance_id=utterance_id,
            timings={
                'audio_ms': int((audio.size / self.sample_rate) * 1000),
                'warmup_ms': int(warmup_stats['elapsed_ms']) if warmup_stats else 0,
                'warmup_reason': warmup_stats['reason'] if warmup_stats else None,
                'infer_ms': infer_elapsed_ms,
                'total_ms': int((warmup_stats['elapsed_ms']) if warmup_stats else 0) + infer_elapsed_ms,
            },
        )
        print(
            '[session-server] '
            f'transcribe utterance_id={utterance_id or 0} '
            f'partial={partial} '
            f'audio_ms={transcript.timings["audio_ms"]} '
            f'warmup_ms={transcript.timings["warmup_ms"]} '
            f'infer_ms={transcript.timings["infer_ms"]} '
            f'total_ms={transcript.timings["total_ms"]}',
            flush=True,
        )
        if not partial:
            self.reset()
        return transcript


def _build_runtime_options(config: VoxConfig, runtime_options: RuntimeExecutionOptions | None) -> RuntimeExecutionOptions:
    if runtime_options is not None:
        return runtime_options
    return RuntimeExecutionOptions(
        wait_for_lock=config.runtime.wait_for_lock,
        wait_timeout_sec=max(1, config.runtime.lock_wait_timeout_sec),
    )


async def _send_transcript(
    websocket: WebSocketServerProtocol,
    transcript: RealtimeTranscript,
) -> None:
    await websocket.send(
        json.dumps(
            {
                'text': transcript.text,
                'is_partial': transcript.is_partial,
                'language': transcript.language,
                'segments': transcript.segments,
                'utterance_id': transcript.utterance_id,
                'timings': transcript.timings,
            },
            ensure_ascii=False,
        )
    )


def _format_log_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _log_session(event: str, **fields: Any) -> None:
    parts = [f'{key}={_format_log_value(value)}' for key, value in fields.items() if value not in (None, '')]
    suffix = f' | {" | ".join(parts)}' if parts else ''
    print(f'[session-server] {event}{suffix}', flush=True)


def _summarize_hotword_entries(config: VoxConfig, *, max_items: int = 8) -> str:
    parts: list[str] = []
    for entry in config.dictation.hotwords.entries[:max_items]:
        value = entry.value.strip()
        if not value:
            continue
        aliases = [alias.strip() for alias in entry.aliases if alias.strip()]
        if aliases:
            parts.append(f'{value} <- {", ".join(aliases)}')
        else:
            parts.append(value)
    return ' | '.join(parts)


def _summarize_hints(config: VoxConfig, *, max_items: int = 6) -> str:
    items = [item.strip() for item in config.dictation.hints.items if item.strip()]
    return ' | '.join(items[:max_items])


def _log_dictation_config(config: VoxConfig) -> None:
    llm = config.dictation.llm
    context = config.dictation.context
    hotwords = config.dictation.hotwords
    hints = config.dictation.hints
    hotword_count = len([entry for entry in hotwords.entries if entry.value.strip()])
    hint_count = len([item for item in hints.items if item.strip()])
    prompt_preset, custom_prompt_enabled, _, _ = resolve_dictation_prompt_selection(llm)

    _log_session(
        'dictation_config',
        llm_enabled=llm.enabled,
        llm_provider=llm.provider if llm.enabled else None,
        llm_model=llm.model if llm.enabled else None,
        llm_timeout_sec=llm.timeout_sec if llm.enabled else None,
        llm_stream=llm.stream if llm.enabled else None,
        prompt_preset=prompt_preset if llm.enabled else None,
        custom_prompt_enabled=custom_prompt_enabled if llm.enabled else None,
        context_enabled=context.enabled,
        context_max_chars=context.max_chars if context.enabled else None,
        context_capture_budget_ms=context.capture_budget_ms if context.enabled else None,
        hotwords_enabled=hotwords.enabled,
        hotword_entries=hotword_count if hotwords.enabled else None,
        rewrite_aliases=hotwords.rewrite_aliases if hotwords.enabled else None,
        case_sensitive=hotwords.case_sensitive if hotwords.enabled else None,
        hints_enabled=hints.enabled,
        hint_count=hint_count if hints.enabled else None,
        incremental_llm=False if llm.enabled else None,
    )
    if has_dictation_hotwords(hotwords):
        _log_session('dictation_config_hotwords', text=_summarize_hotword_entries(config))
    if has_dictation_hints(hints):
        _log_session('dictation_config_hints', text=_summarize_hints(config))


def _log_dictation_context(
    utterance_id: int,
    *,
    snapshot: DictationContextSnapshot | None = None,
    context: DictationContext | None = None,
    state: str,
) -> None:
    _log_session(
        'dictation_context',
        utterance_id=utterance_id,
        state=state,
        source=context.source if context else None,
        app=context.app_name if context else None,
        window=context.window_title if context else None,
        surface=context.surface if context else None,
        role=context.element_role if context else None,
        url=context.page_url if context else None,
        capture_ms=snapshot.capture_ms if snapshot else None,
        selected_chars=len(context.selected_text or '') if context else 0,
        focus_chars=len(context.focus_text or '') if context else 0,
        context_chars=len(context.context_text or '') if context else 0,
        error=snapshot.error if snapshot else None,
    )
    if context and context.selected_text:
        _log_session(
            'dictation_context_selected',
            utterance_id=utterance_id,
            text=context.selected_text,
        )
    if context and context.focus_text and context.focus_text != context.selected_text:
        _log_session(
            'dictation_context_focus',
            utterance_id=utterance_id,
            text=context.focus_text,
        )
    if context and context.context_text:
        _log_session(
            'dictation_context_excerpt',
            utterance_id=utterance_id,
            text=context.context_text,
        )


def _log_partial_pipeline(
    utterance_id: int | None,
    *,
    state: str,
    **fields: Any,
) -> None:
    _log_session(
        'dictation_partial_pipeline',
        utterance_id=utterance_id or 0,
        state=state,
        **fields,
    )


def _log_context_prefetch(
    utterance_id: int | None,
    snapshot: DictationContextSnapshot,
) -> None:
    context = snapshot.context
    _log_session(
        'dictation_context_prefetch',
        utterance_id=utterance_id or 0,
        source=context.source if context else None,
        app=context.app_name if context else None,
        window=context.window_title if context else None,
        surface=context.surface if context else None,
        role=context.element_role if context else None,
        url=context.page_url if context else None,
        capture_ms=snapshot.capture_ms,
        context_chars=len(context.context_text or '') if context else 0,
        selected_chars=len(context.selected_text or '') if context else 0,
        focus_chars=len(context.focus_text or '') if context else 0,
        error=snapshot.error,
    )


def _longest_common_prefix(left: str, right: str) -> str:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def _truncate_stable_prefix(text: str, *, floor: int = 0) -> str:
    if not text or len(text) <= floor:
        return text[:floor]
    for index in range(len(text), floor, -1):
        if text[index - 1] in STABLE_BREAK_CHARS:
            return text[:index]
    return text


def _shrink_incremental_state_to_prefix(
    state: IncrementalDictationState,
    raw_text: str,
) -> None:
    if state.stable_raw_text and not raw_text.startswith(state.stable_raw_text):
        state.stable_raw_text = _longest_common_prefix(state.stable_raw_text, raw_text)
    if state.completed_raw_text and not raw_text.startswith(state.completed_raw_text):
        state.completed_raw_text = ''
        state.completed_text = ''
        state.completed_candidate = None
    if state.submitted_raw_text and not raw_text.startswith(state.submitted_raw_text):
        state.submitted_raw_text = ''
    if state.queued_raw_text and not raw_text.startswith(state.queued_raw_text):
        state.queued_raw_text = None


def _compute_incremental_stable_prefix(
    previous_text: str,
    current_text: str,
    *,
    committed_text: str = '',
) -> str:
    if not previous_text or not current_text:
        return committed_text if current_text.startswith(committed_text) else ''

    common = _longest_common_prefix(previous_text, current_text)
    if committed_text and not common.startswith(committed_text):
        return _longest_common_prefix(common, committed_text)
    if len(common) < PARTIAL_STABLE_MIN_CHARS:
        return committed_text

    safe_end = len(common) - PARTIAL_STABLE_GUARD_CHARS
    if safe_end <= len(committed_text):
        return committed_text

    candidate = common[:safe_end]
    boundary = _truncate_stable_prefix(candidate, floor=len(committed_text))
    if len(boundary) >= len(committed_text) + PARTIAL_STABLE_MIN_ADVANCE_CHARS:
        return boundary
    if len(candidate) >= len(committed_text) + PARTIAL_STABLE_MIN_ADVANCE_CHARS:
        return candidate
    return committed_text


def _apply_local_partial_preview(
    transcript: RealtimeTranscript,
    postprocessor: DictationTextPostprocessor | None,
    *,
    stable_raw_text: str = '',
    completed_raw_text: str = '',
    completed_text: str = '',
    context_snapshot: DictationContextSnapshot | None = None,
) -> RealtimeTranscript:
    if postprocessor is None or not transcript.text.strip():
        return transcript

    base_text = transcript.text
    if completed_raw_text and completed_text and base_text.startswith(completed_raw_text):
        base_text = f'{completed_text}{base_text[len(completed_raw_text):]}'
    elif stable_raw_text and base_text.startswith(stable_raw_text):
        base_text = f'{stable_raw_text}{base_text[len(stable_raw_text):]}'

    context = context_snapshot.context if context_snapshot else None
    preview = postprocessor.process(
        base_text,
        language=transcript.language,
        context=context,
        allow_llm=False,
    )
    timings = dict(transcript.timings or {})
    preview_ms = int(preview.metadata.get('postprocess_ms', 0))
    timings['preview_postprocess_ms'] = preview_ms
    timings['preview_changed'] = bool(preview.metadata.get('changed'))
    if completed_raw_text and completed_text:
        timings['preview_completed_chars'] = len(completed_text)
    return RealtimeTranscript(
        text=preview.text,
        is_partial=True,
        language=transcript.language,
        segments=transcript.segments,
        utterance_id=transcript.utterance_id,
        timings=timings,
    )


def _remaining_context_budget_ms(
    *,
    started_at: float,
    budget_ms: int,
    clock: Callable[[], float] | None = None,
) -> int:
    if budget_ms <= 0:
        return 0
    now = (clock or time.monotonic)()
    elapsed_ms = max(0, round((now - started_at) * 1000))
    return max(0, budget_ms - elapsed_ms)


def _is_trivial_commit_suffix(suffix: str) -> bool:
    return bool(suffix) and all(char in TRIVIAL_COMMIT_SUFFIX_CHARS for char in suffix)


def _build_trivial_suffix_reuse_result(
    postprocessor: DictationTextPostprocessor,
    candidate: IncrementalPostprocessCandidate,
    suffix: str,
) -> DictationPostprocessResult:
    metadata = dict(candidate.result.metadata)
    rules_input = str(
        metadata.get('rules_input_text')
        or metadata.get('llm_output_text')
        or metadata.get('final_text')
        or candidate.result.text
    )
    merged_rules_input = f'{rules_input}{suffix}'
    if has_dictation_transforms(postprocessor.transforms):
        merged_text = apply_dictation_transforms(merged_rules_input, postprocessor.transforms)
    else:
        merged_text = merged_rules_input.strip()
    metadata['rules_input_text'] = merged_rules_input
    metadata['rules_input_chars'] = len(merged_rules_input)
    metadata['rules_text'] = merged_text
    metadata['rules_chars'] = len(merged_text)
    metadata['rules_changed'] = merged_text != merged_rules_input
    metadata['final_text'] = merged_text
    metadata['final_chars'] = len(merged_text)
    metadata['changed'] = True
    return DictationPostprocessResult(text=merged_text, metadata=metadata)


def _select_final_commit_reuse(
    transcript: RealtimeTranscript,
    postprocessor: DictationTextPostprocessor | None,
    state: IncrementalDictationState,
) -> tuple[str, DictationPostprocessResult, DictationContextSnapshot | None, int] | None:
    candidate = state.completed_candidate
    if candidate is None or postprocessor is None:
        return None

    raw_text = transcript.text.strip()
    if not raw_text or not candidate.raw_text or not raw_text.startswith(candidate.raw_text):
        return None

    if raw_text == candidate.raw_text:
        return ('reuse_exact', candidate.result, candidate.context_snapshot, len(candidate.raw_text))

    suffix = raw_text[len(candidate.raw_text) :]
    if _is_trivial_commit_suffix(suffix):
        return (
            'reuse_punct',
            _build_trivial_suffix_reuse_result(postprocessor, candidate, suffix),
            candidate.context_snapshot,
            len(candidate.raw_text),
        )
    return None


def _apply_dictation_postprocess(
    transcript: RealtimeTranscript,
    postprocessor: DictationTextPostprocessor | None,
    *,
    context_snapshot: DictationContextSnapshot | None = None,
    reused_result: DictationPostprocessResult | None = None,
    commit_mode: str = 'full_final',
    commit_reused_chars: int = 0,
) -> RealtimeTranscript:
    if postprocessor is None or transcript.is_partial:
        return transcript

    utterance_id = transcript.utterance_id or 0
    asr_infer_ms = int((transcript.timings or {}).get('infer_ms', 0))
    asr_total_ms = int((transcript.timings or {}).get('total_ms', 0))
    context = context_snapshot.context if context_snapshot else None

    def emit_stage(stage: str, fields: dict[str, Any]) -> None:
        stage_fields = [
            ('utterance_id', utterance_id),
            ('stage', stage),
            ('t_rel_ms', int(fields.get('t_rel_ms', 0))),
        ]
        if 'stage_ms' in fields:
            stage_fields.append(('stage_ms', int(fields['stage_ms'])))
        if 'postprocess_ms' in fields:
            stage_fields.append(('postprocess_ms', int(fields['postprocess_ms'])))
        if 'asr_infer_ms' in fields:
            stage_fields.append(('asr_infer_ms', int(fields['asr_infer_ms'])))
        if 'asr_total_ms' in fields:
            stage_fields.append(('asr_total_ms', int(fields['asr_total_ms'])))
        if 'timeout_sec' in fields:
            stage_fields.append(('timeout_sec', float(fields['timeout_sec'])))
        if 'stream_requested' in fields:
            stage_fields.append(('stream_requested', bool(fields['stream_requested'])))
        if 'stream_used' in fields:
            stage_fields.append(('stream_used', bool(fields['stream_used'])))
        if 'stream_chunks' in fields:
            stage_fields.append(('stream_chunks', int(fields['stream_chunks'])))
        if 'first_token_ms' in fields and fields['first_token_ms'] is not None:
            stage_fields.append(('first_token_ms', int(fields['first_token_ms'])))
        if 'provider' in fields:
            stage_fields.append(('provider', fields['provider']))
        if 'model' in fields:
            stage_fields.append(('model', fields['model']))
        if 'chars' in fields:
            stage_fields.append(('chars', int(fields['chars'])))
        if 'input_chars' in fields:
            stage_fields.append(('input_chars', int(fields['input_chars'])))
        if 'context_chars' in fields:
            stage_fields.append(('context_chars', int(fields['context_chars'])))
        if 'context_selected_chars' in fields:
            stage_fields.append(('context_selected_chars', int(fields['context_selected_chars'])))
        if 'context_focus_chars' in fields:
            stage_fields.append(('context_focus_chars', int(fields['context_focus_chars'])))
        if 'context_source' in fields:
            stage_fields.append(('context_source', fields['context_source']))
        if 'context_surface' in fields:
            stage_fields.append(('context_surface', fields['context_surface']))
        if 'matches' in fields:
            stage_fields.append(('matches', int(fields['matches'])))
        if 'replacements' in fields:
            stage_fields.append(('replacements', fields['replacements']))
        if 'hotword_entries' in fields:
            stage_fields.append(('hotword_entries', int(fields['hotword_entries'])))
        if 'hotword_matches' in fields:
            stage_fields.append(('hotword_matches', int(fields['hotword_matches'])))
        if 'hint_count' in fields:
            stage_fields.append(('hint_count', int(fields['hint_count'])))
        if 'changed' in fields:
            stage_fields.append(('changed', bool(fields['changed'])))
        if 'llm_used' in fields:
            stage_fields.append(('llm_used', bool(fields['llm_used'])))
        if 'error' in fields:
            stage_fields.append(('error', str(fields['error'])))
        if 'reason' in fields:
            stage_fields.append(('reason', str(fields['reason'])))
        if 'fallback' in fields:
            stage_fields.append(('fallback', str(fields['fallback'])))
        _log_session('dictation_stage', **dict(stage_fields))

        if 'text' in fields:
            _log_session(
                'dictation_text',
                utterance_id=utterance_id,
                stage=stage,
                text=str(fields['text']),
            )
        if 'diff' in fields:
            _log_session(
                'dictation_diff',
                utterance_id=utterance_id,
                stage=stage,
                diff=str(fields['diff']),
            )

    emit_stage(
        'asr_final',
        {
            't_rel_ms': 0,
            'asr_infer_ms': asr_infer_ms,
            'asr_total_ms': asr_total_ms,
            'chars': len(transcript.text),
            'text': transcript.text,
        },
    )

    if context_snapshot is None:
        _log_dictation_context(utterance_id, state='disabled')
    elif context is None and context_snapshot.error:
        _log_dictation_context(utterance_id, snapshot=context_snapshot, state='error')
    elif context is None:
        _log_dictation_context(utterance_id, snapshot=context_snapshot, state='empty')
    else:
        _log_dictation_context(utterance_id, snapshot=context_snapshot, context=context, state='ready')

    if reused_result is None:
        result = postprocessor.process(
            transcript.text,
            language=transcript.language,
            context=context,
            emit=emit_stage,
        )
    else:
        result = reused_result
        result.metadata['original_text'] = transcript.text
        result.metadata['original_chars'] = len(transcript.text)

    timings = dict(transcript.timings or {})
    postprocess_ms = int(result.metadata.get('postprocess_ms', 0))
    if postprocess_ms:
        timings['postprocess_ms'] = postprocess_ms
        timings['total_ms'] = int(timings.get('total_ms', 0)) + postprocess_ms
    if 'llm_ms' in result.metadata:
        timings['llm_ms'] = int(result.metadata['llm_ms'])
    if 'llm_timeout_sec' in result.metadata:
        timings['llm_timeout_sec'] = float(result.metadata['llm_timeout_sec'])
    if 'llm_used' in result.metadata:
        timings['llm_used'] = bool(result.metadata['llm_used'])
    if 'llm_stream_requested' in result.metadata:
        timings['llm_stream_requested'] = bool(result.metadata['llm_stream_requested'])
    if 'llm_stream_used' in result.metadata:
        timings['llm_stream_used'] = bool(result.metadata['llm_stream_used'])
    if 'llm_stream_chunks' in result.metadata:
        timings['llm_stream_chunks'] = int(result.metadata['llm_stream_chunks'])
    if result.metadata.get('llm_first_token_ms') is not None:
        timings['llm_first_token_ms'] = int(result.metadata['llm_first_token_ms'])
    if 'rules_changed' in result.metadata:
        timings['rules_changed'] = bool(result.metadata['rules_changed'])
    if result.metadata.get('provider'):
        timings['llm_provider'] = str(result.metadata['provider'])
    if result.metadata.get('model'):
        timings['llm_model'] = str(result.metadata['model'])
    if 'original_chars' in result.metadata:
        timings['original_chars'] = int(result.metadata['original_chars'])
    if 'final_chars' in result.metadata:
        timings['final_chars'] = int(result.metadata['final_chars'])
    if context_snapshot is not None:
        timings['context_capture_ms'] = int(context_snapshot.capture_ms)
        timings['context_available'] = context is not None
    if context and context.source:
        timings['context_source'] = context.source
    if context and context.surface:
        timings['context_surface'] = context.surface
    timings['commit_mode'] = commit_mode
    timings['commit_reused_chars'] = int(commit_reused_chars)
    timings['llm_guard_fallback'] = bool(result.metadata.get('llm_guard_fallback'))
    if result.metadata.get('llm_guard_reason'):
        timings['llm_guard_reason'] = str(result.metadata.get('llm_guard_reason'))

    _log_session(
        'dictation_commit',
        utterance_id=utterance_id,
        commit_mode=commit_mode,
        reused_chars=commit_reused_chars,
        llm_used=bool(result.metadata.get('llm_used')),
        guard_fallback=bool(result.metadata.get('llm_guard_fallback')),
        guard_reason=result.metadata.get('llm_guard_reason'),
    )

    if result.metadata.get('llm_error'):
        _log_session(
            'dictation_postprocess_error',
            utterance_id=utterance_id,
            llm_error=result.metadata['llm_error'],
            llm_ms=int(result.metadata.get('llm_ms', 0)),
            timeout_sec=float(result.metadata.get('llm_timeout_sec', 0.0)),
            provider=result.metadata.get('provider', '-'),
            model=result.metadata.get('model', '-'),
        )

    _log_session(
        'dictation_postprocess',
        utterance_id=utterance_id,
        changed=bool(result.metadata.get('changed')),
        rules_changed=bool(result.metadata.get('rules_changed')),
        llm_used=bool(result.metadata.get('llm_used')),
        llm_ms=int(result.metadata.get('llm_ms', 0)),
        postprocess_ms=postprocess_ms,
        timeout_sec=float(result.metadata.get('llm_timeout_sec', 0.0)),
        stream_requested=bool(result.metadata.get('llm_stream_requested')),
        stream_used=bool(result.metadata.get('llm_stream_used')),
        stream_chunks=int(result.metadata.get('llm_stream_chunks', 0)),
        first_token_ms=result.metadata.get('llm_first_token_ms'),
        provider=result.metadata.get('provider', '-'),
        model=result.metadata.get('model', '-'),
        raw_chars=int(result.metadata.get('original_chars', 0)),
        final_chars=int(result.metadata.get('final_chars', 0)),
        context_source=context.source if context else None,
        context_surface=context.surface if context else None,
        context_chars=len(context.context_text or '') if context else 0,
        hotword_matches=int(result.metadata.get('hotword_matches', 0)),
        hint_count=int(result.metadata.get('hint_count', 0)),
        commit_mode=commit_mode,
        reused_chars=commit_reused_chars,
        guard_fallback=bool(result.metadata.get('llm_guard_fallback')),
        guard_reason=result.metadata.get('llm_guard_reason'),
    )

    return RealtimeTranscript(
        text=result.text,
        is_partial=transcript.is_partial,
        language=transcript.language,
        segments=transcript.segments,
        utterance_id=transcript.utterance_id,
        timings=timings,
    )


async def serve_realtime_session(
    config: VoxConfig,
    model_id: str | None,
    language: str | None,
    host: str,
    port: int,
    sample_rate: int = 16_000,
    runtime_options: RuntimeExecutionOptions | None = None,
    apply_dictation_postprocess: bool = False,
    dictation_llm_timeout_sec: float | None = None,
) -> None:
    effective_config = config
    if dictation_llm_timeout_sec is not None:
        effective_config = config.model_copy(deep=True)
        effective_config.dictation.llm.timeout_sec = max(0.1, float(dictation_llm_timeout_sec))

    spec = resolve_model(effective_config, model_id, kind='asr')
    options = _build_runtime_options(effective_config, runtime_options)
    ensure_result = ensure_model_downloaded(
        effective_config,
        spec,
        allow_download=True,
        runtime_options=options,
    )
    model_path = Path(str(ensure_result['snapshot_path']))
    postprocessor = build_dictation_postprocessor(effective_config) if apply_dictation_postprocess else None

    with acquire_runtime_lock(
        effective_config,
        'asr_infer',
        options=options,
        metadata={
            'task_type': 'asr_session_server',
            'model_id': spec.model_id,
            'out': f'{host}:{port}',
        },
    ):
        from mlx_audio.stt import load

        model = load(model_path)

        async def handler(websocket: WebSocketServerProtocol) -> None:
            session = RealtimeASRSession(model=model, language=language, sample_rate=sample_rate)
            context_capture_enabled = bool(
                postprocessor is not None
                and effective_config.dictation.llm.enabled
                and effective_config.dictation.context.enabled
            )
            context_capture_budget_ms = max(0, int(effective_config.dictation.context.capture_budget_ms))
            pending_context: PendingContextCapture | None = None
            logged_dictation_config = False
            incremental_enabled = postprocessor is not None
            # 录音期间只保留本地 deterministic preview，避免增量 LLM 任务把后续 flush 挤住。
            incremental_llm_enabled = False
            incremental_state = IncrementalDictationState()

            def reset_incremental_state(*, clear_context: bool = True) -> None:
                incremental_state.epoch += 1
                incremental_state.last_partial_text = ''
                incremental_state.stable_raw_text = ''
                incremental_state.submitted_raw_text = ''
                incremental_state.completed_raw_text = ''
                incremental_state.completed_text = ''
                incremental_state.completed_candidate = None
                incremental_state.queued_raw_text = None
                incremental_state.queued_language = None
                task = incremental_state.task
                incremental_state.task = None
                if clear_context:
                    incremental_state.context_snapshot = None
                if task is not None:
                    task.cancel()

            async def cancel_pending_context() -> None:
                nonlocal pending_context
                if pending_context is None:
                    return
                pending_context.task.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_context.task
                pending_context = None

            async def clear_pending_context(
                *,
                wait: bool,
                utterance_id: int | None = None,
            ) -> DictationContextSnapshot | None:
                nonlocal pending_context
                if pending_context is None:
                    return None
                if not wait and not pending_context.task.done():
                    return None
                task = pending_context.task
                started_at = pending_context.started_at
                pending_context = None
                try:
                    if wait and not task.done():
                        remaining_budget_ms = _remaining_context_budget_ms(
                            started_at=started_at,
                            budget_ms=context_capture_budget_ms,
                        )
                        if remaining_budget_ms <= 0:
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
                            _log_session(
                                'dictation_context_budget',
                                utterance_id=utterance_id or 0,
                                budget_ms=context_capture_budget_ms,
                                waited_ms=0,
                                state='expired',
                            )
                            return None
                        wait_started_at = time.monotonic()
                        try:
                            snapshot = await asyncio.wait_for(task, timeout=remaining_budget_ms / 1000)
                        except asyncio.TimeoutError:
                            waited_ms = int((time.monotonic() - wait_started_at) * 1000)
                            _log_session(
                                'dictation_context_budget',
                                utterance_id=utterance_id or 0,
                                budget_ms=context_capture_budget_ms,
                                waited_ms=waited_ms,
                                state='timeout',
                            )
                            return None
                        waited_ms = int((time.monotonic() - wait_started_at) * 1000)
                        _log_session(
                            'dictation_context_budget',
                            utterance_id=utterance_id or 0,
                            budget_ms=context_capture_budget_ms,
                            waited_ms=waited_ms,
                            state='ready',
                        )
                        return snapshot
                    return await task
                except asyncio.CancelledError:
                    return None

            async def harvest_context_snapshot(utterance_id: int | None) -> None:
                snapshot = await clear_pending_context(wait=False)
                if snapshot is not None:
                    incremental_state.context_snapshot = snapshot
                    _log_context_prefetch(utterance_id, snapshot)

            def start_incremental_postprocess(
                target_raw_text: str,
                *,
                utterance_id: int | None,
                language_value: str | None,
            ) -> None:
                if (
                    not incremental_llm_enabled
                    or postprocessor is None
                    or not target_raw_text.strip()
                    or incremental_state.task is not None
                ):
                    return

                epoch = incremental_state.epoch
                context = incremental_state.context_snapshot.context if incremental_state.context_snapshot else None
                context_snapshot = incremental_state.context_snapshot
                incremental_state.submitted_raw_text = target_raw_text
                _log_partial_pipeline(
                    utterance_id,
                    state='job_started',
                    stable_chars=len(target_raw_text),
                    completed_chars=len(incremental_state.completed_text),
                    context_ready=context is not None,
                )

                async def runner() -> tuple[int, str, DictationPostprocessResult]:
                    result = await asyncio.to_thread(
                        postprocessor.process,
                        target_raw_text,
                        language=language_value,
                        context=context,
                    )
                    return epoch, target_raw_text, result

                task = asyncio.create_task(runner())
                incremental_state.task = task

                def on_done(done_task: asyncio.Task[tuple[int, str, DictationPostprocessResult]]) -> None:
                    if incremental_state.task is done_task:
                        incremental_state.task = None

                    queued_raw_text = incremental_state.queued_raw_text
                    queued_language = incremental_state.queued_language
                    incremental_state.queued_raw_text = None
                    incremental_state.queued_language = None

                    try:
                        result_epoch, source_text, result = done_task.result()
                    except asyncio.CancelledError:
                        result_epoch = None
                        source_text = ''
                    except Exception as error:
                        _log_partial_pipeline(
                            utterance_id,
                            state='job_failed',
                            stable_chars=len(target_raw_text),
                            error=str(error),
                        )
                        result_epoch = None
                        source_text = ''
                    if (
                        result_epoch == incremental_state.epoch
                        and source_text
                        and incremental_state.last_partial_text.startswith(source_text)
                    ):
                        incremental_state.completed_raw_text = source_text
                        incremental_state.completed_text = result.text
                        incremental_state.completed_candidate = IncrementalPostprocessCandidate(
                            raw_text=source_text,
                            result=result,
                            language=language_value,
                            context_snapshot=context_snapshot,
                        )
                        _log_partial_pipeline(
                            utterance_id,
                            state='job_completed',
                            stable_chars=len(source_text),
                            output_chars=len(result.text),
                            changed=bool(result.metadata.get('changed')),
                            llm_used=bool(result.metadata.get('llm_used')),
                            llm_ms=int(result.metadata.get('llm_ms', 0)),
                        )

                    if queued_raw_text and queued_raw_text != source_text:
                        start_incremental_postprocess(
                            queued_raw_text,
                            utterance_id=utterance_id,
                            language_value=queued_language,
                        )

                task.add_done_callback(on_done)

            async def build_partial_transcript(
                transcript: RealtimeTranscript,
            ) -> RealtimeTranscript:
                if not incremental_enabled or not transcript.text.strip():
                    incremental_state.last_partial_text = transcript.text.strip()
                    return transcript

                await harvest_context_snapshot(transcript.utterance_id)
                raw_text = transcript.text.strip()
                utterance_id = transcript.utterance_id
                _shrink_incremental_state_to_prefix(incremental_state, raw_text)
                next_stable = _compute_incremental_stable_prefix(
                    incremental_state.last_partial_text,
                    raw_text,
                    committed_text=incremental_state.stable_raw_text,
                )
                if len(next_stable) > len(incremental_state.stable_raw_text):
                    _log_partial_pipeline(
                        utterance_id,
                        state='stable',
                        stable_chars=len(next_stable),
                        advance_chars=len(next_stable) - len(incremental_state.stable_raw_text),
                        partial_chars=len(raw_text),
                    )
                    incremental_state.stable_raw_text = next_stable
                if (
                    incremental_llm_enabled
                    and incremental_state.stable_raw_text
                    and incremental_state.stable_raw_text != incremental_state.completed_raw_text
                    and incremental_state.stable_raw_text != incremental_state.submitted_raw_text
                ):
                    if incremental_state.task is None:
                        start_incremental_postprocess(
                            incremental_state.stable_raw_text,
                            utterance_id=utterance_id,
                            language_value=transcript.language,
                        )
                    else:
                        incremental_state.queued_raw_text = incremental_state.stable_raw_text
                        incremental_state.queued_language = transcript.language

                preview_transcript = _apply_local_partial_preview(
                    transcript,
                    postprocessor,
                    completed_raw_text=incremental_state.completed_raw_text,
                    completed_text=incremental_state.completed_text,
                    context_snapshot=incremental_state.context_snapshot,
                )
                reused_chars = (
                    len(incremental_state.completed_text)
                    if incremental_state.completed_raw_text
                    and raw_text.startswith(incremental_state.completed_raw_text)
                    else 0
                )
                _log_partial_pipeline(
                    utterance_id,
                    state='preview',
                    partial_chars=len(raw_text),
                    preview_chars=len(preview_transcript.text),
                    stable_chars=len(incremental_state.stable_raw_text),
                    reused_chars=reused_chars,
                )
                incremental_state.last_partial_text = raw_text
                return preview_transcript

            await websocket.send(
                json.dumps(
                    {
                        'status': 'ready',
                        'model_id': spec.model_id,
                        'repo_id': spec.repo_id,
                        'sample_rate': sample_rate,
                    },
                    ensure_ascii=False,
                )
            )

            async for message in websocket:
                if not logged_dictation_config:
                    _log_dictation_config(effective_config)
                    logged_dictation_config = True
                if isinstance(message, bytes):
                    session.append_pcm16(message)
                    continue

                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps({'error': 'invalid JSON control message'}, ensure_ascii=False)
                    )
                    continue

                action = payload.get('action')
                if action == 'partial':
                    await _send_transcript(
                        websocket,
                        await build_partial_transcript(
                            session.transcribe(partial=True, utterance_id=payload.get('utterance_id'))
                        ),
                    )
                elif action == 'capture_context':
                    if not context_capture_enabled:
                        continue
                    if payload.get('reason') == 'start' and pending_context is not None:
                        pending_context.task.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending_context.task
                        pending_context = None
                    await clear_pending_context(wait=False)
                    if pending_context is None:
                        pending_context = PendingContextCapture(
                            task=asyncio.create_task(
                                asyncio.to_thread(
                                    capture_dictation_context_snapshot,
                                    effective_config,
                                )
                            ),
                            started_at=time.monotonic(),
                        )
                elif action == 'flush':
                    transcript = session.transcribe(
                        partial=False,
                        utterance_id=payload.get('utterance_id'),
                    )
                    commit_mode = 'full_final'
                    reused_result: DictationPostprocessResult | None = None
                    reused_context_snapshot: DictationContextSnapshot | None = None
                    reused_chars = (
                        len(incremental_state.completed_text)
                        if incremental_state.completed_raw_text
                        and transcript.text.startswith(incremental_state.completed_raw_text)
                        else 0
                    )
                    if (
                        postprocessor is not None
                        and (reuse := _select_final_commit_reuse(transcript, postprocessor, incremental_state)) is not None
                    ):
                        commit_mode, reused_result, reused_context_snapshot, reused_chars = reuse
                    _log_partial_pipeline(
                        transcript.utterance_id,
                        state='flush',
                        reused_chars=reused_chars,
                        stable_chars=len(incremental_state.stable_raw_text),
                        completed_chars=len(incremental_state.completed_text),
                        commit_mode=commit_mode,
                    )
                    context_snapshot_from_partial = incremental_state.context_snapshot
                    reset_incremental_state(clear_context=True)
                    if reused_result is not None:
                        await cancel_pending_context()
                        context_snapshot = reused_context_snapshot or context_snapshot_from_partial
                    else:
                        context_snapshot = await clear_pending_context(
                            wait=context_capture_enabled,
                            utterance_id=transcript.utterance_id,
                        )
                        if context_snapshot is None:
                            context_snapshot = context_snapshot_from_partial
                    await _send_transcript(
                        websocket,
                        _apply_dictation_postprocess(
                            transcript,
                            postprocessor,
                            context_snapshot=context_snapshot if context_capture_enabled else None,
                            reused_result=reused_result,
                            commit_mode=commit_mode,
                            commit_reused_chars=reused_chars,
                        ),
                    )
                elif action == 'warmup':
                    warmed = session.warmup(
                        force=bool(payload.get('force')),
                        allow_first_use=True,
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                'status': 'warmed' if warmed else 'noop',
                                'timings': warmed,
                            },
                            ensure_ascii=False,
                        )
                    )
                elif action == 'reset':
                    session.reset()
                    reset_incremental_state(clear_context=True)
                    await websocket.send(json.dumps({'status': 'reset'}, ensure_ascii=False))
                elif action == 'close':
                    break
                elif action == 'ping':
                    await websocket.send(json.dumps({'status': 'pong'}, ensure_ascii=False))
                else:
                    await websocket.send(
                        json.dumps({'error': f'unknown action: {action}'}, ensure_ascii=False)
                    )

            if pending_context is not None:
                pending_context.task.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_context.task
            if incremental_state.task is not None:
                incremental_state.task.cancel()
                with suppress(asyncio.CancelledError):
                    await incremental_state.task

        async with websockets.serve(
            handler,
            host,
            port,
            max_size=None,
            ping_interval=None,
            ping_timeout=None,
        ):
            await asyncio.Future()


def run_realtime_session_server(
    config: VoxConfig,
    model_id: str | None,
    language: str | None,
    host: str,
    port: int,
    sample_rate: int = 16_000,
    runtime_options: RuntimeExecutionOptions | None = None,
    apply_dictation_postprocess: bool = False,
    dictation_llm_timeout_sec: float | None = None,
) -> None:
    try:
        asyncio.run(
            serve_realtime_session(
                config=config,
                model_id=model_id,
                language=language,
                host=host,
                port=port,
                sample_rate=sample_rate,
                runtime_options=runtime_options,
                apply_dictation_postprocess=apply_dictation_postprocess,
                dictation_llm_timeout_sec=dictation_llm_timeout_sec,
            )
        )
    except KeyboardInterrupt:
        pass
