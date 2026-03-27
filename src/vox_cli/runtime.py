from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
import fcntl
import hashlib
import json
import os
import time

from .config import VoxConfig, get_locks_dir


LockLogger = Callable[[str], None]


@dataclass(frozen=True)
class RuntimeExecutionOptions:
    wait_for_lock: bool
    wait_timeout_sec: int
    task_id: str | None = None
    task_type: str | None = None
    command_summary: str | None = None
    log: LockLogger | None = None


@dataclass(frozen=True)
class RuntimeLockState:
    resource: str
    pid: int | None = None
    task_id: str | None = None
    task_type: str | None = None
    command_summary: str | None = None
    started_at: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class RuntimeLockError(RuntimeError):
    pass


class RuntimeLockBusyError(RuntimeLockError):
    pass


class RuntimeLockTimeoutError(RuntimeLockError):
    pass


@dataclass
class RuntimeLockHandle:
    resource: str
    path: Path
    file_obj: object


DEFAULT_WAIT_TIMEOUT_SEC = 1800
_WAIT_LOG_INTERVAL_SEC = 5.0
_LOCK_POLL_INTERVAL_SEC = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stringify_metadata(metadata: dict[str, object] | None) -> dict[str, str]:
    if not metadata:
        return {}
    return {key: str(value) for key, value in metadata.items() if value is not None}


def _lock_filename(resource: str) -> str:
    digest = hashlib.sha256(resource.encode('utf-8')).hexdigest()[:16]
    prefix = resource.split(':', 1)[0].replace('/', '_').replace(' ', '_')
    return f'{prefix}-{digest}.lock'


def _read_lock_state(lock_path: Path, resource: str) -> RuntimeLockState:
    try:
        raw = lock_path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return RuntimeLockState(resource=resource)
    except Exception:
        return RuntimeLockState(resource=resource)

    if not raw:
        return RuntimeLockState(resource=resource)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return RuntimeLockState(resource=resource)

    metadata = payload.get('metadata')
    return RuntimeLockState(
        resource=str(payload.get('resource') or resource),
        pid=payload.get('pid'),
        task_id=payload.get('task_id'),
        task_type=payload.get('task_type'),
        command_summary=payload.get('command_summary'),
        started_at=payload.get('started_at'),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _write_lock_state(
    file_obj,
    *,
    resource: str,
    options: RuntimeExecutionOptions,
    metadata: dict[str, object] | None,
) -> None:
    payload = {
        'resource': resource,
        'pid': os.getpid(),
        'task_id': options.task_id,
        'task_type': options.task_type,
        'command_summary': options.command_summary,
        'started_at': _utc_now(),
        'metadata': _stringify_metadata(metadata),
    }
    file_obj.seek(0)
    file_obj.truncate()
    json.dump(payload, file_obj, ensure_ascii=False)
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _clear_lock_state(file_obj) -> None:
    file_obj.seek(0)
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def format_lock_state(state: RuntimeLockState) -> str:
    parts: list[str] = []
    if state.task_type:
        parts.append(f'task={state.task_type}')
    if state.task_id:
        parts.append(f'id={state.task_id}')
    if state.pid:
        parts.append(f'pid={state.pid}')
    if state.started_at:
        parts.append(f'started={state.started_at}')
    for key in ('model_id', 'profile', 'audio', 'out'):
        value = state.metadata.get(key)
        if value:
            parts.append(f'{key}={value}')
    if state.command_summary:
        parts.append(f'cmd={state.command_summary}')
    return ', '.join(parts) if parts else 'unknown holder'


def build_lock_error_message(
    *,
    resource: str,
    state: RuntimeLockState,
    waited_sec: float | None = None,
    timed_out: bool = False,
) -> str:
    prefix = 'Timed out waiting for lock' if timed_out else 'Lock is busy'
    detail = format_lock_state(state)
    waited = '' if waited_sec is None else f' after {waited_sec:.1f}s'
    return f'{prefix} {resource}{waited}. Current holder: {detail}'


def _lock_path(config: VoxConfig, resource: str) -> Path:
    locks_dir = get_locks_dir(config)
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / _lock_filename(resource)


def read_runtime_lock_state(config: VoxConfig, resource: str) -> RuntimeLockState:
    return _read_lock_state(_lock_path(config, resource), resource)


def probe_runtime_lock(config: VoxConfig, resource: str) -> tuple[bool, RuntimeLockState]:
    lock_path = _lock_path(config, resource)
    file_obj = lock_path.open('a+', encoding='utf-8')
    locked = False
    try:
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            return (True, _read_lock_state(lock_path, resource))
        return (False, _read_lock_state(lock_path, resource))
    finally:
        if locked:
            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        file_obj.close()


@contextmanager
def acquire_runtime_lock_pool(
    config: VoxConfig,
    resources: list[str],
    *,
    options: RuntimeExecutionOptions,
    metadata: dict[str, object] | None = None,
    display_resource: str | None = None,
) -> Iterator[RuntimeLockHandle]:
    if not resources:
        raise ValueError('resources must not be empty')
    if len(resources) == 1:
        with acquire_runtime_lock(config, resources[0], options=options, metadata=metadata) as handle:
            yield handle
        return

    started = time.monotonic()
    last_logged = -_WAIT_LOG_INTERVAL_SEC
    pool_name = display_resource or resources[0]
    attempt_options = replace(options, wait_for_lock=False, log=None)

    while True:
        states: list[tuple[str, RuntimeLockState]] = []
        for resource in resources:
            manager = acquire_runtime_lock(config, resource, options=attempt_options, metadata=metadata)
            try:
                handle = manager.__enter__()
            except RuntimeLockBusyError:
                state = _read_lock_state(_lock_path(config, resource), resource)
                states.append((resource, state))
                continue
            except Exception:
                manager.__exit__(None, None, None)
                raise

            try:
                waited = time.monotonic() - started
                if options.log and waited >= 1.0:
                    options.log(f'[green]Acquired {pool_name}[/green] via {resource} after {waited:.1f}s wait')
                yield handle
                return
            finally:
                manager.__exit__(None, None, None)

        waited = time.monotonic() - started
        if not options.wait_for_lock:
            detail = '; '.join(f'{resource} -> {format_lock_state(state)}' for resource, state in states) or 'all slots busy'
            raise RuntimeLockBusyError(f'Lock pool is busy {pool_name}. Holders: {detail}')
        if waited >= options.wait_timeout_sec:
            detail = '; '.join(f'{resource} -> {format_lock_state(state)}' for resource, state in states) or 'all slots busy'
            raise RuntimeLockTimeoutError(
                f'Timed out waiting for lock pool {pool_name} after {waited:.1f}s. Holders: {detail}'
            )
        if options.log and waited - last_logged >= _WAIT_LOG_INTERVAL_SEC:
            detail = '; '.join(f'{resource} -> {format_lock_state(state)}' for resource, state in states) or 'all slots busy'
            options.log(f'[yellow]Waiting for {pool_name}[/yellow] ({waited:.1f}s). Holders: {detail}')
            last_logged = waited
        time.sleep(_LOCK_POLL_INTERVAL_SEC)


@contextmanager
def acquire_runtime_locks(
    config: VoxConfig,
    resources: list[str],
    *,
    options: RuntimeExecutionOptions,
    metadata: dict[str, object] | None = None,
) -> Iterator[list[RuntimeLockHandle]]:
    if not resources:
        raise ValueError('resources must not be empty')
    ordered_resources = list(dict.fromkeys(resources))
    with ExitStack() as stack:
        handles: list[RuntimeLockHandle] = []
        for resource in ordered_resources:
            handle = stack.enter_context(
                acquire_runtime_lock(config, resource, options=options, metadata=metadata)
            )
            handles.append(handle)
        yield handles


@contextmanager
def acquire_runtime_lock(
    config: VoxConfig,
    resource: str,
    *,
    options: RuntimeExecutionOptions,
    metadata: dict[str, object] | None = None,
) -> Iterator[RuntimeLockHandle]:
    locks_dir = get_locks_dir(config)
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / _lock_filename(resource)
    file_obj = lock_path.open('a+', encoding='utf-8')
    locked = False

    started = time.monotonic()
    last_logged = -_WAIT_LOG_INTERVAL_SEC

    try:
        while True:
            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                state = _read_lock_state(lock_path, resource)
                waited = time.monotonic() - started
                if not options.wait_for_lock:
                    raise RuntimeLockBusyError(
                        build_lock_error_message(resource=resource, state=state)
                    )
                if waited >= options.wait_timeout_sec:
                    raise RuntimeLockTimeoutError(
                        build_lock_error_message(
                            resource=resource,
                            state=state,
                            waited_sec=waited,
                            timed_out=True,
                        )
                    )
                if options.log and waited - last_logged >= _WAIT_LOG_INTERVAL_SEC:
                    options.log(
                        f'[yellow]Waiting for {resource}[/yellow] '
                        f'({waited:.1f}s). Holder: {format_lock_state(state)}'
                    )
                    last_logged = waited
                time.sleep(_LOCK_POLL_INTERVAL_SEC)

        _write_lock_state(file_obj, resource=resource, options=options, metadata=metadata)
        waited = time.monotonic() - started
        if options.log and waited >= 1.0:
            options.log(f'[green]Acquired {resource}[/green] after {waited:.1f}s wait')
        yield RuntimeLockHandle(resource=resource, path=lock_path, file_obj=file_obj)
    finally:
        if locked:
            try:
                _clear_lock_state(file_obj)
            except Exception:
                pass
            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        file_obj.close()
