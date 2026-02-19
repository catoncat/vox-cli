# Repository Guidelines

## Project Structure & Module Organization
- `src/vox_cli/`: core CLI package.
- `src/vox_cli/main.py`: Typer entrypoint that wires subcommands (`model`, `profile`, `asr`, `tts`, `pipeline`, `task`, `config`).
- `src/vox_cli/services/`: orchestration logic for ASR/TTS/model operations.
- `src/vox_cli/config.py`, `src/vox_cli/models.py`, `src/vox_cli/db.py`: configuration, model registry, and SQLite task/profile persistence.
- `config.example.toml`: local config template.
- `README.md`: operational reference and troubleshooting.
- `pyproject.toml` + `uv.lock`: dependencies and build metadata.

## Build, Test, and Development Commands
- `uv sync`: install all required dependencies.
- `uv sync --extra mic`: install optional microphone streaming support.
- `uv run vox doctor --json`: check platform/dependencies and effective runtime setup.
- `uv run vox model status --json`: inspect cache/download readiness for built-in models.
- `uv run vox asr transcribe --audio ./speech.wav --lang zh --model auto`: smoke test ASR flow.
- `uv run vox tts clone --profile narrator --text "hello" --out ./out.wav`: smoke test TTS cloning flow.

## Coding Style & Naming Conventions
- Python target is `>=3.10,<3.13`; use 4-space indentation and explicit type hints.
- Follow existing naming: `snake_case` for functions/modules, `PascalCase` for classes/dataclasses, command handlers ending with `_cmd`.
- Keep command parsing/output in `main.py`; move business logic to `services/` modules.
- Prefer small, composable helpers for path/config resolution and avoid duplicating endpoint/cache logic.

## Testing Guidelines
- This snapshot has no dedicated automated test suite yet.
- Minimum validation for contributions: run `uv run vox doctor --json` and execute at least one changed command path end-to-end.
- For new logic, add `pytest`-style tests under `tests/` using `test_*.py` naming (for future CI adoption).

## Commit & Pull Request Guidelines
- Git history is not available in this workspace export; use Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`) for consistency.
- Keep commits focused, and include related config/flag updates with behavior changes.
- PRs should include: objective, commands used for validation, representative CLI/JSON output, and linked issues.

## Security & Configuration Tips
- Never commit local runtime data from `~/.vox/` (profiles, outputs, `vox.db`) or Hugging Face cache paths.
- Keep machine-specific settings in env vars (`VOX_HOME`, `HF_ENDPOINT`, `HF_HUB_CACHE`) and share only sanitized examples via `config.example.toml`.
