---
name: vox
description: "Vox 单入口语音编排技能。用于自然语言完成 profile 管理、样本添加、ASR 转写、语音克隆、pipeline 执行、模型管理与故障排查。缺依赖时再自动引导安装。用户只描述目标且未给具体命令时使用。"
---

# Vox

用一个入口覆盖 Vox CLI 全流程能力。默认优先执行业务操作，不把安装流程当作主流程。

先切到 skill 根目录（`SKILL.md` 所在目录）再运行下面脚本，避免相对路径错误。

## 总流程（业务优先）

1. 意图识别：
   - 先按 `references/intents.md` 映射到业务场景。
2. 预检：
   - 先运行无副作用检查。
   - 只有缺依赖时才运行真实安装。
3. 执行业务命令：
   - 按对应 playbook 执行（见下方 references）。
4. 交付前自检：
   - 强制执行 `health_gate`（优先用缓存状态，必要时才跑全量自检）。
5. 失败回写：
   - 记录失败样本到用户本地日志。

## 预检与安装

```bash
# 无副作用预检
bash scripts/bootstrap.sh --check

# 仅当缺依赖时执行
bash scripts/bootstrap.sh
```

## 模型确保

```bash
bash scripts/ensure_model.sh <model_id|asr-auto|tts-default>
```

## 交付前自检

```bash
# 推荐：带状态缓存（默认 24h TTL）
bash scripts/health_gate.sh [--require-model <...>] [--require-file <...>]

# 强制全量自检（忽略缓存）
bash scripts/health_gate.sh --force [--require-model <...>] [--require-file <...>]
```

## 失败回写

```bash
bash scripts/log_failure.sh --stage "<stage>" --command "<cmd>" --error "<msg>" [--retry "<retry-cmd>"]
bash scripts/failure_digest.sh
```

状态文件：

- 健康状态：`~/.vox/agent/state/health.json`
- 失败日志：`~/.vox/agent/failures.jsonl`
- 失败报告：`~/.vox/agent/state/failure_report.md`

## 必读 References（按场景加载）

1. 意图路由：`references/intents.md`
2. 模型管理：`references/model-playbook.md`
3. Profile/采样：`references/profile-playbook.md`
4. ASR 转写：`references/asr-playbook.md`
5. TTS 克隆：`references/tts-playbook.md`
6. Pipeline：`references/pipeline-playbook.md`
7. 输出模板：`references/response-contract.md`
8. 失败闭环：`references/failure-loop.md`

## 5 条硬标准（全部满足才交付）

1. 平台必须是 `Darwin + arm64`。
2. `vox doctor --json` 必须 `ok=true`。
3. 本次任务所需模型必须 `verified=true`。
4. 主命令退出码必须是 `0`。
5. 需要文件输出时，目标文件必须存在且路径已回报。

## 执行规则

1. 优先使用 JSON 输出（`--json` 或 `--format ndjson`）。
2. 执行前声明将要运行的命令；执行后回报关键结果。
3. 若命令失败，先给重试命令，再调用 `log_failure.sh`。
4. 只有预检失败或命令缺失时才进入安装流程。
5. 禁止跳过模型校验直接执行重操作（克隆、pipeline、流式转写）。

## 常用命令模板

```bash
# 健康检查
scripts/vox_cmd.sh doctor --json

# 模型状态
scripts/vox_cmd.sh model status --json

# ASR 离线
scripts/vox_cmd.sh asr transcribe --audio ./speech.wav --lang zh --model auto --json

# TTS 克隆
scripts/vox_cmd.sh tts clone --profile narrator --text "你好" --out ./out.wav --model qwen-tts-1.7b --json

# Pipeline
scripts/vox_cmd.sh pipeline run --profile narrator --audio ./input.wav --clone-text "把这段内容读出来" --lang zh --json
```

## 依赖与发布约定

1. 安装优先使用 `uv`。
2. 默认安装系统依赖：`ffmpeg`、`portaudio`。
3. CLI 来源：
   - 优先 `VOX_CLI_PACKAGE_SPEC`（显式指定包）。
   - 若设置 `VOX_CLI_GIT_URL`，使用 `git+<url>` 安装。
   - 默认回退到 `git+https://github.com/catoncat/vox-cli.git`。
