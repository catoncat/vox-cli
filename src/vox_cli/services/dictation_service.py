from __future__ import annotations

import asyncio
import importlib.resources as resources
import json
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import websockets

from ..config import VoxConfig, get_home_dir, resolve_asr_model_id


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


async def _probe_session_server(ws_url: str) -> None:
    async with websockets.connect(ws_url, open_timeout=1.0, max_size=None) as websocket:
        message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
        payload = json.loads(message)
        if payload.get('status') != 'ready':
            raise RuntimeError(f'unexpected session-server hello: {payload}')


def wait_for_session_server(host: str, port: int, timeout: float = 15.0) -> None:
    ws_url = f'ws://{host}:{port}'
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            asyncio.run(_probe_session_server(ws_url))
            return
        except Exception as error:
            last_error = error
            time.sleep(0.15)
    raise RuntimeError(
        f'Timed out waiting for dictation session server on {host}:{port}: {last_error}'
    )


def ensure_native_binary(*, rebuild: bool = False) -> Path:
    manifest = native_manifest_path()
    binary = native_binary_path()

    if not manifest.exists():
        raise RuntimeError(f'Native dictation manifest not found: {manifest}')

    if rebuild or not binary.exists():
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
    partial_interval_ms: int = 0,
    verbose: bool = False,
) -> int:
    resolved_model = resolve_asr_model_id(config, None if model == 'auto' else model)
    binary = ensure_native_binary(rebuild=rebuild_native)
    ensure_dictation_dirs(config)
    port = port or pick_free_port(host)
    log_path = dictation_session_log_path(config)

    server_cmd = [
        sys.executable,
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
    ]
    helper_cmd = [
        str(binary),
        '--server-url',
        f'ws://{host}:{port}',
        '--partial-interval-ms',
        str(partial_interval_ms),
    ]
    if verbose:
        helper_cmd.append('--verbose')

    with log_path.open('a', encoding='utf-8') as log_handle:
        server_proc = subprocess.Popen(
            server_cmd,
            cwd=repo_root(),
            stdout=log_handle,
            stderr=log_handle,
        )
        try:
            wait_for_session_server(host, port)
        except Exception as error:
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
            return subprocess.call(helper_cmd, cwd=native_project_dir())
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
