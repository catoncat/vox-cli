from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .audio import copy_as_wav
from .cache import inspect_cache
from .config import (
    VoxConfig,
    ensure_runtime_dirs,
    get_db_path,
    get_home_dir,
    get_hf_cache_dir,
    get_outputs_dir,
    get_profiles_dir,
    get_total_memory_gb,
    load_config,
    resolve_asr_model_id,
)
from .db import (
    add_profile_sample,
    complete_task,
    connect,
    create_profile,
    fail_task,
    get_task,
    init_db,
    list_profiles,
    list_tasks,
    resolve_profile,
    tracked_task,
)
from .models import MODEL_REGISTRY
from .services.asr_service import stream_to_ndjson, stream_transcribe_file, transcribe_file
from .services.model_service import ensure_model_downloaded, list_model_statuses, resolve_model
from .services.tts_service import clone_to_file, custom_to_file, design_to_file

console = Console()
err_console = Console(stderr=True)


@dataclass
class AppState:
    config: VoxConfig
    db_path: Path


def _print_json(payload: dict | list) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False))


def _fail(message: str, code: int = 1) -> None:
    err_console.print(f'[red]{message}[/red]')
    raise typer.Exit(code=code)


app = typer.Typer(help='Vox CLI: MLX Qwen ASR/TTS orchestration', no_args_is_help=True)
model_app = typer.Typer(help='Model operations')
profile_app = typer.Typer(help='Profile operations')
asr_app = typer.Typer(help='ASR operations')
tts_app = typer.Typer(help='TTS operations')
pipeline_app = typer.Typer(help='End-to-end pipelines')
task_app = typer.Typer(help='Task inspection')
config_app = typer.Typer(help='Config operations')

app.add_typer(model_app, name='model')
app.add_typer(profile_app, name='profile')
app.add_typer(asr_app, name='asr')
app.add_typer(tts_app, name='tts')
app.add_typer(pipeline_app, name='pipeline')
app.add_typer(task_app, name='task')
app.add_typer(config_app, name='config')


@app.callback()
def root_callback(ctx: typer.Context) -> None:
    cfg = load_config()
    ensure_runtime_dirs(cfg)
    db_path = get_db_path(cfg)
    init_db(db_path)
    ctx.obj = AppState(config=cfg, db_path=db_path)


@app.command('version')
def version_cmd() -> None:
    console.print(__version__)


@app.command('doctor')
def doctor_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, '--json')) -> None:
    state: AppState = ctx.obj
    cfg = state.config
    checks: dict[str, dict] = {}

    checks['platform'] = {
        'system': platform.system(),
        'machine': platform.machine(),
        'ok': platform.system() == 'Darwin' and platform.machine() == 'arm64',
    }

    try:
        import mlx  # noqa: F401

        checks['mlx'] = {'ok': True}
    except Exception as e:
        checks['mlx'] = {'ok': False, 'error': str(e)}

    try:
        import mlx_audio  # noqa: F401

        checks['mlx_audio'] = {'ok': True}
    except Exception as e:
        checks['mlx_audio'] = {'ok': False, 'error': str(e)}

    try:
        import huggingface_hub  # noqa: F401

        checks['huggingface_hub'] = {'ok': True}
    except Exception as e:
        checks['huggingface_hub'] = {'ok': False, 'error': str(e)}

    checks['hf_cache'] = {
        'path': str(get_hf_cache_dir(cfg)),
        'exists': get_hf_cache_dir(cfg).exists(),
    }
    checks['hf_endpoints'] = {'value': cfg.hf.endpoints}

    mem_gb = get_total_memory_gb()
    checks['memory_gb'] = {'value': round(mem_gb, 2) if mem_gb else None}
    checks['resolved_asr_model'] = {'value': resolve_asr_model_id(cfg)}

    ok = all(v.get('ok', True) for v in checks.values())

    payload = {'ok': ok, 'checks': checks}
    if as_json:
        _print_json(payload)
    else:
        table = Table(title='vox doctor')
        table.add_column('Check')
        table.add_column('Result')
        table.add_column('Details')
        for name, result in checks.items():
            status = result.get('ok', True)
            status_text = '[green]OK[/green]' if status else '[red]FAIL[/red]'
            table.add_row(name, status_text, json.dumps(result, ensure_ascii=False))
        console.print(table)

    if not ok:
        raise typer.Exit(code=1)


@config_app.command('show')
def config_show_cmd(ctx: typer.Context, as_json: bool = typer.Option(True, '--json/--pretty')) -> None:
    state: AppState = ctx.obj
    payload = state.config.model_dump()
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@model_app.command('list')
def model_list_cmd() -> None:
    table = Table(title='Supported models')
    table.add_column('Model ID')
    table.add_column('Kind')
    table.add_column('Repo ID')
    table.add_column('Quantization')
    for spec in MODEL_REGISTRY.values():
        table.add_row(spec.model_id, spec.kind, spec.repo_id, spec.quantization or '-')
    console.print(table)


@model_app.command('status')
def model_status_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, '--json')) -> None:
    state: AppState = ctx.obj
    statuses = list_model_statuses(state.config)

    if as_json:
        _print_json(statuses)
        return

    table = Table(title='Model status')
    table.add_column('Model')
    table.add_column('Downloaded')
    table.add_column('Verified')
    table.add_column('Incomplete')
    table.add_column('Cache Dir')
    for row in statuses:
        table.add_row(
            row['model_id'],
            'yes' if row['downloaded'] else 'no',
            'yes' if row['verified'] else 'no',
            'yes' if row['has_incomplete'] else 'no',
            row['cache_dir'],
        )
    console.print(table)


@model_app.command('verify')
def model_verify_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., '--model'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj
    spec = resolve_model(state.config, model)
    cache = inspect_cache(spec, get_hf_cache_dir(state.config))
    payload = {
        'model_id': spec.model_id,
        'repo_id': spec.repo_id,
        'downloaded': cache.downloaded,
        'verified': cache.verified,
        'has_incomplete': cache.has_incomplete,
        'has_weights': cache.has_weights,
        'revision': cache.revision,
        'cache_dir': str(cache.cache_dir),
    }
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)
    if not cache.verified:
        raise typer.Exit(code=1)


@model_app.command('path')
def model_path_cmd(ctx: typer.Context, model: str = typer.Option(..., '--model')) -> None:
    state: AppState = ctx.obj
    spec = resolve_model(state.config, model)
    cache = inspect_cache(spec, get_hf_cache_dir(state.config))
    if cache.revision:
        p = cache.cache_dir / 'snapshots' / cache.revision
    else:
        p = cache.cache_dir
    console.print(str(p))


@model_app.command('pull')
def model_pull_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., '--model'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj
    spec = resolve_model(state.config, model)

    with connect(state.db_path) as conn:
        with tracked_task(conn, 'model_pull', spec.model_id, {'repo_id': spec.repo_id}) as task:
            try:
                result = ensure_model_downloaded(state.config, spec, allow_download=True)
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, 'result': result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@profile_app.command('create')
def profile_create_cmd(
    ctx: typer.Context,
    name: str = typer.Option(..., '--name'),
    lang: str = typer.Option('zh', '--lang'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj
    with connect(state.db_path) as conn:
        try:
            row = create_profile(conn, name=name, language=lang)
        except Exception as e:
            _fail(f'Create profile failed: {e}')

    payload = dict(row)
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@profile_app.command('list')
def profile_list_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, '--json')) -> None:
    state: AppState = ctx.obj
    with connect(state.db_path) as conn:
        rows = list_profiles(conn)

    payload = [dict(r) for r in rows]
    if as_json:
        _print_json(payload)
        return

    table = Table(title='Profiles')
    table.add_column('ID')
    table.add_column('Name')
    table.add_column('Language')
    table.add_column('Samples')
    for row in payload:
        table.add_row(row['id'], row['name'], row['language'], str(row['sample_count']))
    console.print(table)


@profile_app.command('add-sample')
def profile_add_sample_cmd(
    ctx: typer.Context,
    profile: str = typer.Option(..., '--profile'),
    audio: Path = typer.Option(..., '--audio'),
    text: str = typer.Option(..., '--text'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj

    if not audio.exists():
        _fail(f'Audio file not found: {audio}')

    profiles_dir = get_profiles_dir(state.config)

    with connect(state.db_path) as conn:
        profile_row = resolve_profile(conn, profile)
        if profile_row is None:
            _fail(f'Profile not found: {profile}')

        sample_id = str(uuid.uuid4())
        dst = profiles_dir / str(profile_row['id']) / f'{sample_id}.wav'
        metrics = copy_as_wav(audio, dst)

        # Hard constraints aligned with cloning quality.
        if metrics.duration_sec < 2 or metrics.duration_sec > 30:
            dst.unlink(missing_ok=True)
            _fail(f'Invalid sample duration {metrics.duration_sec:.2f}s (expected 2-30s)')

        if metrics.rms < 0.005:
            dst.unlink(missing_ok=True)
            _fail(f'Audio RMS too low ({metrics.rms:.4f}); please provide clearer audio')

        row = add_profile_sample(
            conn,
            profile_id=str(profile_row['id']),
            audio_path=str(dst),
            reference_text=text,
            duration_sec=metrics.duration_sec,
            rms=metrics.rms,
        )

    payload = dict(row)
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@asr_app.command('transcribe')
def asr_transcribe_cmd(
    ctx: typer.Context,
    audio: Path = typer.Option(..., '--audio'),
    lang: str = typer.Option('auto', '--lang'),
    model: str = typer.Option('auto', '--model'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj
    if not audio.exists():
        _fail(f'Audio file not found: {audio}')

    model_arg = None if model == 'auto' else model
    resolved_model = resolve_asr_model_id(state.config, model_arg)

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'asr_transcribe',
            resolved_model,
            {'audio': str(audio), 'lang': lang},
        ) as task:
            try:
                result = transcribe_file(state.config, audio, resolved_model, lang)
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, **result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload['text'])


def _capture_microphone_to_file(path: Path, seconds: int) -> None:
    try:
        import sounddevice as sd
        import soundfile as sf
    except Exception as e:
        raise RuntimeError(f'sounddevice is required for --input mic: {e}') from e

    sample_rate = 16000
    frames = int(seconds * sample_rate)
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, dtype='float32')
    sd.wait()
    sf.write(str(path), recording, sample_rate)


@asr_app.command('stream')
def asr_stream_cmd(
    ctx: typer.Context,
    source: str = typer.Option('', '--source', help='Audio file path when input=file'),
    input_mode: str = typer.Option('file', '--input', help='file|mic'),
    lang: str = typer.Option('auto', '--lang'),
    model: str = typer.Option('auto', '--model'),
    format: str = typer.Option('text', '--format', help='text|ndjson'),
    mic_seconds: int = typer.Option(8, '--mic-seconds', min=2, max=120),
) -> None:
    state: AppState = ctx.obj
    model_arg = None if model == 'auto' else model
    resolved_model = resolve_asr_model_id(state.config, model_arg)

    if input_mode not in {'file', 'mic'}:
        _fail('--input must be file or mic')

    if format not in {'text', 'ndjson'}:
        _fail('--format must be text or ndjson')

    temp_audio: Path | None = None
    if input_mode == 'file':
        if not source:
            _fail('--source is required when --input file')
        audio_path = Path(source)
        if not audio_path.exists():
            _fail(f'Audio file not found: {audio_path}')
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix='vox-mic-'))
        temp_audio = temp_dir / 'mic_capture.wav'
        _capture_microphone_to_file(temp_audio, mic_seconds)
        audio_path = temp_audio

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'asr_stream',
            resolved_model,
            {'audio': str(audio_path), 'lang': lang, 'input': input_mode},
        ) as task:
            try:
                chunks = list(stream_transcribe_file(state.config, audio_path, resolved_model, lang))
                if format == 'ndjson':
                    for row in stream_to_ndjson(chunks, session_id=task.id):
                        console.print(row)
                else:
                    for chunk in chunks:
                        console.print(chunk, end='')
                    console.print()
                complete_task(conn, task.id, {'chunks': len(chunks)})
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    if temp_audio:
        shutil.rmtree(temp_audio.parent, ignore_errors=True)


@tts_app.command('clone')
def tts_clone_cmd(
    ctx: typer.Context,
    profile: str = typer.Option(..., '--profile'),
    text: str = typer.Option(..., '--text'),
    out: Path = typer.Option(..., '--out'),
    model: str = typer.Option('qwen-tts-1.7b', '--model'),
    seed: int | None = typer.Option(None, '--seed'),
    instruct: str | None = typer.Option(None, '--instruct'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'tts_clone',
            model,
            {'profile': profile, 'text_preview': text[:50], 'out': str(out)},
        ) as task:
            try:
                result = clone_to_file(
                    config=state.config,
                    conn=conn,
                    profile_id_or_name=profile,
                    text=text,
                    output_path=out,
                    model_id=model,
                    seed=seed,
                    instruct=instruct,
                )
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, **result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@tts_app.command('custom')
def tts_custom_cmd(
    ctx: typer.Context,
    text: str = typer.Option(..., '--text'),
    out: Path = typer.Option(..., '--out'),
    speaker: str = typer.Option('Vivian', '--speaker'),
    language: str = typer.Option('auto', '--language'),
    instruct: str | None = typer.Option(None, '--instruct'),
    model: str = typer.Option('qwen-tts-1.7b-customvoice-8bit', '--model'),
    seed: int | None = typer.Option(None, '--seed'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'tts_custom',
            model,
            {
                'text_preview': text[:50],
                'out': str(out),
                'speaker': speaker,
                'language': language,
            },
        ) as task:
            try:
                result = custom_to_file(
                    config=state.config,
                    text=text,
                    output_path=out,
                    model_id=model,
                    speaker=speaker,
                    language=language,
                    instruct=instruct,
                    seed=seed,
                )
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, **result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@tts_app.command('design')
def tts_design_cmd(
    ctx: typer.Context,
    text: str = typer.Option(..., '--text'),
    instruct: str = typer.Option(..., '--instruct'),
    out: Path = typer.Option(..., '--out'),
    language: str = typer.Option('auto', '--language'),
    model: str = typer.Option('qwen-tts-1.7b-voicedesign-8bit', '--model'),
    seed: int | None = typer.Option(None, '--seed'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'tts_design',
            model,
            {
                'text_preview': text[:50],
                'instruct_preview': instruct[:50],
                'out': str(out),
                'language': language,
            },
        ) as task:
            try:
                result = design_to_file(
                    config=state.config,
                    text=text,
                    output_path=out,
                    model_id=model,
                    instruct=instruct,
                    language=language,
                    seed=seed,
                )
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, **result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@pipeline_app.command('run')
def pipeline_run_cmd(
    ctx: typer.Context,
    profile: str = typer.Option(..., '--profile'),
    audio: Path = typer.Option(..., '--audio'),
    clone_text: str = typer.Option(..., '--clone-text'),
    out: Path | None = typer.Option(None, '--out'),
    lang: str = typer.Option('auto', '--lang'),
    asr_model: str = typer.Option('auto', '--asr-model'),
    tts_model: str = typer.Option('qwen-tts-1.7b', '--tts-model'),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj

    if not audio.exists():
        _fail(f'Audio file not found: {audio}')

    if out is None:
        out = get_outputs_dir(state.config) / f'pipeline-{uuid.uuid4().hex[:8]}.wav'

    resolved_asr_model = resolve_asr_model_id(state.config, None if asr_model == 'auto' else asr_model)

    with connect(state.db_path) as conn:
        with tracked_task(
            conn,
            'pipeline_run',
            None,
            {
                'profile': profile,
                'audio': str(audio),
                'clone_text_preview': clone_text[:50],
                'out': str(out),
            },
        ) as task:
            try:
                asr_result = transcribe_file(state.config, audio, resolved_asr_model, lang)
                clone_result = clone_to_file(
                    config=state.config,
                    conn=conn,
                    profile_id_or_name=profile,
                    text=clone_text,
                    output_path=out,
                    model_id=tts_model,
                    seed=None,
                    instruct=None,
                )
                result = {
                    'transcription': asr_result,
                    'clone': clone_result,
                }
                complete_task(conn, task.id, result)
            except Exception as e:
                fail_task(conn, task.id, str(e))
                _fail(str(e))

    payload = {'task_id': task.id, **result}
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


@task_app.command('list')
def task_list_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(50, '--limit', min=1, max=200),
    as_json: bool = typer.Option(False, '--json'),
) -> None:
    state: AppState = ctx.obj
    with connect(state.db_path) as conn:
        rows = list_tasks(conn, limit=limit)

    payload = [dict(r) for r in rows]
    if as_json:
        _print_json(payload)
        return

    table = Table(title='Tasks')
    table.add_column('ID')
    table.add_column('Type')
    table.add_column('Status')
    table.add_column('Model')
    table.add_column('Started')
    for row in payload:
        table.add_row(
            row['id'],
            row['task_type'],
            row['status'],
            row.get('model_id') or '-',
            row['started_at'],
        )
    console.print(table)


@task_app.command('show')
def task_show_cmd(
    ctx: typer.Context,
    task_id: str = typer.Option(..., '--id'),
    as_json: bool = typer.Option(True, '--json/--pretty'),
) -> None:
    state: AppState = ctx.obj
    with connect(state.db_path) as conn:
        row = get_task(conn, task_id)

    if row is None:
        _fail(f'Task not found: {task_id}')

    payload = dict(row)
    if as_json:
        _print_json(payload)
    else:
        console.print(payload)


def main() -> None:
    app()


if __name__ == '__main__':
    main()
