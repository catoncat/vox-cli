from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from vox_cli.services.tts_service import _replace_output, _run_generation_to_temp_file


@dataclass
class FakeChunk:
    audio: np.ndarray
    sample_rate: int


def test_run_generation_to_temp_file_streams_audio(tmp_path: Path) -> None:
    output_path = tmp_path / 'demo.wav'

    def fake_generate(*, text: str):
        assert text == 'hello'
        yield FakeChunk(audio=np.ones(1600, dtype=np.float32), sample_rate=16000)
        yield FakeChunk(audio=np.ones(800, dtype=np.float32), sample_rate=16000)

    temp_path, sample_rate, duration_sec = _run_generation_to_temp_file(
        fake_generate,
        output_path=output_path,
        text='hello',
    )
    _replace_output(temp_path, output_path)

    assert sample_rate == 16000
    assert duration_sec == 0.15
    assert output_path.exists()

    data, sr = sf.read(str(output_path), dtype='float32')
    assert sr == 16000
    assert data.shape[0] == 2400
