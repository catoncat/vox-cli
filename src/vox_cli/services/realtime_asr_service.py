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

from ..config import VoxConfig
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock
from .asr_service import _extract_text, _map_language
from .dictation_postprocess_service import (
    DictationTextPostprocessor,
    build_dictation_postprocessor,
    has_dictation_hints,
    has_dictation_hotwords,
)
from .dictation_context_service import (
    DictationContext,
    DictationContextSnapshot,
    capture_dictation_context_snapshot,
)
from .model_service import ensure_model_downloaded, resolve_model

IDLE_WARMUP_AFTER_SEC = 45.0
IDLE_WARMUP_AUDIO_MS = 200


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

    _log_session(
        'dictation_config',
        llm_enabled=llm.enabled,
        llm_provider=llm.provider if llm.enabled else None,
        llm_model=llm.model if llm.enabled else None,
        llm_timeout_sec=llm.timeout_sec if llm.enabled else None,
        context_enabled=context.enabled,
        context_max_chars=context.max_chars if context.enabled else None,
        context_capture_budget_ms=context.capture_budget_ms if context.enabled else None,
        hotwords_enabled=hotwords.enabled,
        hotword_entries=hotword_count if hotwords.enabled else None,
        rewrite_aliases=hotwords.rewrite_aliases if hotwords.enabled else None,
        case_sensitive=hotwords.case_sensitive if hotwords.enabled else None,
        hints_enabled=hints.enabled,
        hint_count=hint_count if hints.enabled else None,
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
        role=context.element_role if context else None,
        url=context.page_url if context else None,
        capture_ms=snapshot.capture_ms if snapshot else None,
        selected_chars=len(context.selected_text or '') if context else 0,
        context_chars=len(context.context_text or '') if context else 0,
        error=snapshot.error if snapshot else None,
    )
    if context and context.selected_text:
        _log_session(
            'dictation_context_selected',
            utterance_id=utterance_id,
            text=context.selected_text,
        )
    if context and context.context_text:
        _log_session(
            'dictation_context_excerpt',
            utterance_id=utterance_id,
            text=context.context_text,
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


def _apply_dictation_postprocess(
    transcript: RealtimeTranscript,
    postprocessor: DictationTextPostprocessor | None,
    *,
    context_snapshot: DictationContextSnapshot | None = None,
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
        if 'context_source' in fields:
            stage_fields.append(('context_source', fields['context_source']))
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

    result = postprocessor.process(
        transcript.text,
        language=transcript.language,
        context=context,
        emit=emit_stage,
    )
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

    if result.metadata.get('llm_error'):
        _log_session(
            'dictation_postprocess_error',
            llm_error=result.metadata['llm_error'],
            llm_ms=int(result.metadata.get('llm_ms', 0)),
            timeout_sec=float(result.metadata.get('llm_timeout_sec', 0.0)),
            provider=result.metadata.get('provider', '-'),
            model=result.metadata.get('model', '-'),
        )

    _log_session(
        'dictation_postprocess',
        changed=bool(result.metadata.get('changed')),
        rules_changed=bool(result.metadata.get('rules_changed')),
        llm_used=bool(result.metadata.get('llm_used')),
        llm_ms=int(result.metadata.get('llm_ms', 0)),
        postprocess_ms=postprocess_ms,
        timeout_sec=float(result.metadata.get('llm_timeout_sec', 0.0)),
        provider=result.metadata.get('provider', '-'),
        model=result.metadata.get('model', '-'),
        raw_chars=int(result.metadata.get('original_chars', 0)),
        final_chars=int(result.metadata.get('final_chars', 0)),
        context_source=context.source if context else None,
        context_chars=len(context.context_text or '') if context else 0,
        hotword_matches=int(result.metadata.get('hotword_matches', 0)),
        hint_count=int(result.metadata.get('hint_count', 0)),
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
            _log_dictation_config(effective_config)
            context_capture_enabled = bool(
                postprocessor is not None
                and effective_config.dictation.llm.enabled
                and effective_config.dictation.context.enabled
            )
            context_capture_budget_ms = max(0, int(effective_config.dictation.context.capture_budget_ms))
            pending_context: PendingContextCapture | None = None

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
                        session.transcribe(partial=True, utterance_id=payload.get('utterance_id')),
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
                    context_snapshot = await clear_pending_context(
                        wait=context_capture_enabled,
                        utterance_id=transcript.utterance_id,
                    )
                    await _send_transcript(
                        websocket,
                        _apply_dictation_postprocess(
                            transcript,
                            postprocessor,
                            context_snapshot=context_snapshot if context_capture_enabled else None,
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
