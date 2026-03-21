from __future__ import annotations

import numpy as np

from vox_cli.services.dictation_context_service import DictationContext, DictationContextSnapshot
from vox_cli.services.dictation_postprocess_service import DictationPostprocessResult
from vox_cli.services.realtime_asr_service import (
    RealtimeASRSession,
    RealtimeTranscript,
    _apply_dictation_postprocess,
    _remaining_context_budget_ms,
)


class _FakeResult:
    def __init__(self, text: str = 'ok', language: str | None = None) -> None:
        self.text = text
        self.language = language


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[np.ndarray, dict[str, object]]] = []

    def generate(self, audio, **kwargs):
        self.calls.append((np.array(audio, copy=True), dict(kwargs)))
        return _FakeResult(language=kwargs.get('language'))


def _pcm16(values: list[int]) -> bytes:
    return np.asarray(values, dtype=np.int16).tobytes()


def test_session_warmup_on_first_use_when_requested() -> None:
    now = [0.0]
    model = _FakeModel()
    session = RealtimeASRSession(
        model=model,
        language='zh',
        sample_rate=16_000,
        idle_warmup_after_sec=45.0,
        warmup_audio_ms=200,
        clock=lambda: now[0],
    )

    warmed = session.warmup(allow_first_use=True)

    assert warmed is not None
    assert warmed['elapsed_ms'] == 0
    assert len(model.calls) == 1
    assert model.calls[0][0].shape[0] == 3200
    assert model.calls[0][1]['language'] == 'Chinese'


def test_session_transcribe_warms_after_idle_gap() -> None:
    now = [0.0]
    model = _FakeModel()
    session = RealtimeASRSession(
        model=model,
        language='zh',
        sample_rate=16_000,
        idle_warmup_after_sec=30.0,
        warmup_audio_ms=200,
        clock=lambda: now[0],
    )

    session.append_pcm16(_pcm16([1000, -1000, 2000, -2000]))
    first = session.transcribe(partial=False)
    assert first.text == 'ok'
    assert len(model.calls) == 1
    assert first.timings is not None
    assert first.timings['warmup_ms'] == 0
    assert first.timings['infer_ms'] == 0

    now[0] = 31.0
    session.append_pcm16(_pcm16([1000, -1000, 2000, -2000]))
    second = session.transcribe(partial=False)

    assert second.text == 'ok'
    assert len(model.calls) == 3
    assert second.timings is not None
    assert second.timings['warmup_ms'] == 0
    assert second.timings['total_ms'] == 0
    warmup_audio, warmup_kwargs = model.calls[1]
    assert warmup_audio.shape[0] == 3200
    assert warmup_kwargs['language'] == 'Chinese'


def test_session_force_warmup_ignores_idle_threshold() -> None:
    now = [0.0]
    model = _FakeModel()
    session = RealtimeASRSession(
        model=model,
        language='zh',
        sample_rate=16_000,
        idle_warmup_after_sec=300.0,
        warmup_audio_ms=200,
        clock=lambda: now[0],
    )

    session.append_pcm16(_pcm16([1000, -1000]))
    session.transcribe(partial=False)
    assert len(model.calls) == 1

    now[0] = 5.0
    warmed = session.warmup(force=True)

    assert warmed is not None
    assert len(model.calls) == 2
    assert model.calls[1][0].shape[0] == 3200


class _FakePostprocessor:
    def __init__(self) -> None:
        self.context = None

    def process(
        self,
        text: str,
        *,
        language: str | None = None,
        context=None,
        emit=None,
    ) -> DictationPostprocessResult:
        self.context = context
        return DictationPostprocessResult(
            text='polished',
            metadata={
                'changed': True,
                'postprocess_ms': 12,
                'llm_used': True,
                'llm_ms': 9,
                'provider': 'openrouter',
                'rules_changed': True,
            },
        )


def test_apply_dictation_postprocess_updates_text_and_timings() -> None:
    postprocessor = _FakePostprocessor()
    transcript = RealtimeTranscript(
        text='raw',
        is_partial=False,
        language='Chinese',
        timings={'total_ms': 20, 'infer_ms': 8},
    )

    result = _apply_dictation_postprocess(
        transcript,
        postprocessor,
        context_snapshot=DictationContextSnapshot(
            context=DictationContext(
                source='ghostty',
                app_name='Ghostty',
                window_title='codex',
                element_role='AXTextArea',
                context_text='context text',
            ),
            capture_ms=18,
        ),
    )

    assert result.text == 'polished'
    assert result.timings is not None
    assert result.timings['total_ms'] == 32
    assert result.timings['postprocess_ms'] == 12
    assert result.timings['llm_ms'] == 9
    assert result.timings['llm_used'] is True
    assert result.timings['rules_changed'] is True
    assert result.timings['llm_provider'] == 'openrouter'
    assert result.timings['context_capture_ms'] == 18
    assert result.timings['context_available'] is True
    assert result.timings['context_source'] == 'ghostty'
    assert postprocessor.context is not None
    assert postprocessor.context.app_name == 'Ghostty'


def test_remaining_context_budget_ms_uses_elapsed_capture_window() -> None:
    now = [10.0]

    remaining = _remaining_context_budget_ms(
        started_at=9.4,
        budget_ms=1200,
        clock=lambda: now[0],
    )

    assert remaining == 600

    now[0] = 11.0
    exhausted = _remaining_context_budget_ms(
        started_at=9.4,
        budget_ms=1200,
        clock=lambda: now[0],
    )

    assert exhausted == 0
