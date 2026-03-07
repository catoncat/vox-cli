from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path
import time

import pytest

from vox_cli.config import RuntimeConfig, VoxConfig
from vox_cli.runtime import RuntimeExecutionOptions, RuntimeLockBusyError, acquire_runtime_lock, acquire_runtime_lock_pool, acquire_runtime_locks


def _hold_lock(home_dir: str) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=home_dir, wait_for_lock=True, lock_wait_timeout_sec=5))
    options = RuntimeExecutionOptions(wait_for_lock=True, wait_timeout_sec=5)
    with acquire_runtime_lock(config, 'tts_infer', options=options, metadata={'model_id': 'demo'}):
        time.sleep(2)


@pytest.mark.skipif(not hasattr(__import__('fcntl'), 'flock'), reason='requires fcntl/flock')
def test_runtime_lock_no_wait_fails_when_resource_busy(tmp_path: Path) -> None:
    ctx = get_context('spawn')
    proc = ctx.Process(target=_hold_lock, args=(str(tmp_path),))
    proc.start()
    try:
        time.sleep(0.5)
        config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path), wait_for_lock=True, lock_wait_timeout_sec=5))
        options = RuntimeExecutionOptions(wait_for_lock=False, wait_timeout_sec=1)
        with pytest.raises(RuntimeLockBusyError):
            with acquire_runtime_lock(config, 'tts_infer', options=options):
                pass
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()


def _hold_lock_slot(home_dir: str, resource: str) -> None:
    config = VoxConfig(runtime=RuntimeConfig(home_dir=home_dir, wait_for_lock=True, lock_wait_timeout_sec=5))
    options = RuntimeExecutionOptions(wait_for_lock=True, wait_timeout_sec=5)
    with acquire_runtime_lock(config, resource, options=options, metadata={'model_id': 'demo'}):
        time.sleep(2)


@pytest.mark.skipif(not hasattr(__import__('fcntl'), 'flock'), reason='requires fcntl/flock')
def test_runtime_lock_pool_uses_second_slot_when_first_busy(tmp_path: Path) -> None:
    ctx = get_context('spawn')
    proc = ctx.Process(target=_hold_lock_slot, args=(str(tmp_path), 'tts_infer:small:0'))
    proc.start()
    try:
        time.sleep(0.5)
        config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path), wait_for_lock=True, lock_wait_timeout_sec=5))
        options = RuntimeExecutionOptions(wait_for_lock=False, wait_timeout_sec=1)
        with acquire_runtime_lock_pool(
            config,
            ['tts_infer:small:0', 'tts_infer:small:1'],
            options=options,
            metadata={'model_id': 'qwen-tts-0.6b-base-8bit'},
            display_resource='tts_infer',
        ) as handle:
            assert handle.resource == 'tts_infer:small:1'
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()


@pytest.mark.skipif(not hasattr(__import__('fcntl'), 'flock'), reason='requires fcntl/flock')
def test_runtime_multi_lock_no_wait_fails_when_any_slot_busy(tmp_path: Path) -> None:
    ctx = get_context('spawn')
    proc = ctx.Process(target=_hold_lock_slot, args=(str(tmp_path), 'tts_infer_slot:0'))
    proc.start()
    try:
        time.sleep(0.5)
        config = VoxConfig(runtime=RuntimeConfig(home_dir=str(tmp_path), wait_for_lock=True, lock_wait_timeout_sec=5))
        options = RuntimeExecutionOptions(wait_for_lock=False, wait_timeout_sec=1)
        with pytest.raises(RuntimeLockBusyError):
            with acquire_runtime_locks(
                config,
                ['tts_infer_slot:0', 'tts_infer_slot:1'],
                options=options,
                metadata={'model_id': 'qwen-tts-1.7b-base-8bit'},
            ):
                pass
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
