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

## Dictation Agent Logs
- 当排查 `vox dictation` 的速度、卡顿、超时、热词、上下文等待或 LLM 流式行为时，优先读取 `~/.vox/logs/dictation-session.agent.jsonl`，不要先通读 `dictation-session.log`。
- 优先执行 `vox dictation digest --json` 获取最近窗口的聚合结论；只有 digest 不足以解释问题时，再直接读取 `dictation-session.agent.jsonl`。
- `dictation-session.agent.jsonl` 是给 Agent 用的低 token 紧凑 JSONL；`dictation-session.log` 保留人类可读原始细节。
- 优先关注事件：
  - `{"e":"u", ...}`：单次 utterance 的紧凑 waterfall 摘要
  - `{"e":"cfg", ...}`：本次 dictation 配置快照
  - `{"e":"pe", ...}`：LLM 后处理失败
  - `{"e":"ls"|"ss"|"hs"|"hx"|"sx"|"sf", ...}`：启动与进程生命周期
- `u` 事件关键字段：
  - `aud` 音频时长 ms
  - `cap` 本次录音到最终完成的总采集耗时 ms
  - `fl` flush roundtrip ms
  - `ctxc` 上下文捕获耗时 ms
  - `ctxw` 上下文阻塞等待 ms
  - `ctxo` 上下文与其余阶段重叠耗时 ms
  - `asr` ASR 推理 ms
  - `asrt` ASR 总耗时 ms
  - `lu` 是否使用了 LLM，`1/0`
  - `ls` 是否使用了 LLM 流式，`1/0`
  - `ft` LLM 首 token ms
  - `llm` LLM 总耗时 ms
  - `lst` LLM 流式尾段耗时 ms
  - `lsch` LLM 流式 chunk 数
  - `ty` 最终文本注入耗时 ms
  - `be` 后端总耗时 ms
  - `post` 后处理总耗时 ms
  - `bot` 当前判定的瓶颈标签
  - `fin` 最终文本字数
  - `raw` 原始文本字数
  - `pp` partial preview 次数
  - `psa` stable prefix 前进次数
  - `psn` helper 实际发出的 partial 请求数
  - `psk` helper 因后端仍忙而主动丢掉的 partial 次数
  - `pjs` partial 预跑任务启动次数
  - `pjc` partial 预跑任务完成次数
  - `prc` 最终 flush 复用的字符数
  - `psc` 当前窗口内稳定前缀最大字符数
  - `cm` final commit 模式，当前正常应以 `full_final` 为主
  - `gf` / `gr` LLM 结构护栏是否触发以及原因
- `cfg` 事件关键字段：
  - `lu` LLM 开关，`ls` 流式开关
  - `lp` provider，`lm` model，`lt` timeout_sec
  - `dp` prompt preset，`cp` 是否是自定义 prompt
  - `ce` 上下文开关，`cc` context_max_chars
  - `he` 热词开关，`hn` 热词条目数，`hr` rewrite_aliases，`cs` case_sensitive
  - `ie` hints 开关，`in` hint 数量
- 分析速度问题时，优先按这个顺序判断：
  1. `ctxw` 是否异常偏大
  2. `asr` / `asrt` 是否上升
  3. `ft` 是否慢，说明首包慢
  4. `lst` / `lsch` 是否异常，说明流式尾段拖慢
  5. `ty` 是否异常，说明输入注入慢
  6. `fl` / `be` 是否持续走高，说明整体 backend 往返变慢
- `vox dictation digest --json` 当前会聚合：最近配置、最近 N 次 utterance 的 metrics、`partial_pipeline`、瓶颈分布、前后半窗趋势、最慢 utterance、最近错误，以及一段可直接消费的 `diagnosis`。
- 排查 partial 录音期背压时，先看这几个字段：
  1. `partial_pipeline.instrumented` 是否为 `true`
  2. `partial_pipeline.sent_total` 是否持续上升
  3. `partial_pipeline.skipped_total` / `partial_pipeline.skip_rate` 是否异常偏高
  4. `partial_pipeline.preview_total` 是否明显少于 `sent_total`
  5. 最后再结合 `diagnosis.signals` 判断是 `partial_preview_idle`、`partial_preview_no_response`、`partial_preview_healthy` 还是 `partial_backpressure_high`
- 给 Agent 排查 dictation 时，默认顺序是：
  1. 先跑 `vox dictation digest --json`
  2. 再看 `dictation-session.agent.jsonl`
  3. 只有需要逐行追根因时才看 `dictation-session.log`
- 只有在紧凑日志不足以解释问题时，再回看 `dictation-session.log` 里的原始行。
