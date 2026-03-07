from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import websockets
from websockets.server import WebSocketServerProtocol

from ..config import VoxConfig
from ..runtime import RuntimeExecutionOptions, acquire_runtime_lock
from .asr_service import _extract_text, _map_language
from .model_service import ensure_model_downloaded, resolve_model


@dataclass
class RealtimeTranscript:
    text: str
    is_partial: bool
    language: str | None
    segments: list[dict[str, Any]] | None = None


class RealtimeASRSession:
    def __init__(self, model: Any, language: str | None, sample_rate: int = 16_000) -> None:
        self.model = model
        self.language = _map_language(language)
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []

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

    def transcribe(self, *, partial: bool) -> RealtimeTranscript:
        audio = self._concat_audio()
        if audio is None or audio.size == 0:
            return RealtimeTranscript(text='', is_partial=partial, language=self.language)

        decode_options: dict[str, object] = {}
        if self.language:
            decode_options['language'] = self.language

        result = self.model.generate(audio, **decode_options)
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
            },
            ensure_ascii=False,
        )
    )


async def serve_realtime_session(
    config: VoxConfig,
    model_id: str | None,
    language: str | None,
    host: str,
    port: int,
    sample_rate: int = 16_000,
    runtime_options: RuntimeExecutionOptions | None = None,
) -> None:
    spec = resolve_model(config, model_id, kind='asr')
    options = _build_runtime_options(config, runtime_options)
    ensure_result = ensure_model_downloaded(
        config,
        spec,
        allow_download=True,
        runtime_options=options,
    )
    model_path = Path(str(ensure_result['snapshot_path']))

    with acquire_runtime_lock(
        config,
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
                    await _send_transcript(websocket, session.transcribe(partial=True))
                elif action == 'flush':
                    await _send_transcript(websocket, session.transcribe(partial=False))
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

        async with websockets.serve(handler, host, port, max_size=None):
            await asyncio.Future()


def run_realtime_session_server(
    config: VoxConfig,
    model_id: str | None,
    language: str | None,
    host: str,
    port: int,
    sample_rate: int = 16_000,
    runtime_options: RuntimeExecutionOptions | None = None,
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
            )
        )
    except KeyboardInterrupt:
        pass
