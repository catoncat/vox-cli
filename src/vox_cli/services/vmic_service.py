from __future__ import annotations

import importlib.resources as resources
import shutil
import subprocess
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def native_project_dir() -> Path:
    dev_path = repo_root() / 'native' / 'vox-vmic'
    if dev_path.exists():
        return dev_path
    packaged = resources.files('vox_cli').joinpath('native/vox-vmic')
    return Path(str(packaged))


def helper_manifest_path() -> Path:
    return native_project_dir() / 'Package.swift'


def helper_binary_path(configuration: str = 'release') -> Path:
    return native_project_dir() / '.build' / configuration / 'vox-vmicctl'


def ensure_helper_binary(*, rebuild: bool = False, configuration: str = 'release') -> Path:
    manifest = helper_manifest_path()
    binary = helper_binary_path(configuration)

    if not manifest.exists():
        raise RuntimeError(f'Native vmic manifest not found: {manifest}')

    if rebuild or not binary.exists():
        swift = shutil.which('swift')
        if not swift:
            raise RuntimeError('`swift` not found; install Xcode Command Line Tools first')
        subprocess.run(
            [swift, 'build', '-c', configuration, '--product', 'vox-vmicctl'],
            cwd=native_project_dir(),
            check=True,
        )

    if not binary.exists():
        raise RuntimeError(f'Native vmic binary missing after build: {binary}')
    return binary


def run_helper(
    args: list[str],
    *,
    rebuild_native: bool = False,
    configuration: str = 'release',
) -> str:
    binary = ensure_helper_binary(rebuild=rebuild_native, configuration=configuration)
    completed = subprocess.run(
        [str(binary), *args],
        cwd=native_project_dir(),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.rstrip()


def build_driver() -> Path:
    script = native_project_dir() / 'scripts' / 'build-driver.sh'
    if not script.exists():
        raise RuntimeError(f'Native vmic build script not found: {script}')

    subprocess.run(['bash', str(script)], cwd=native_project_dir(), check=True)

    candidates = [
        native_project_dir() / 'driver' / 'build' / 'VoxVirtualMic.driver',
        native_project_dir() / 'driver' / 'build' / 'VirtualMic.driver',
        native_project_dir() / 'build' / 'VoxVirtualMic.driver',
        native_project_dir() / 'build' / 'VirtualMic.driver',
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError('Driver build finished but bundle path could not be determined')
