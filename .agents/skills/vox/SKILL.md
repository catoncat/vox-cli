---
name: vox
description: "Vox 单入口语音编排技能。用于自然语言完成环境守卫、CLI 安装、模型按需下载、ASR 转写、语音克隆、pipeline 执行和任务排障。用户只描述目标且未给具体命令时使用。"
---

# Vox

使用一个入口覆盖 Vox CLI 全流程能力。始终通过 CLI 执行，不直接写推理代码。

先切到 skill 根目录（`SKILL.md` 所在目录）再运行下面脚本，避免相对路径错误。

## 固定流程

1. 先执行初始化与守卫：

```bash
# 无副作用预检（推荐先跑）
bash scripts/bootstrap.sh --check

# 真实安装与修复
bash scripts/bootstrap.sh
```

2. 根据需求匹配意图（见 `references/intents.md`）。
3. 需要模型时先确保模型可用：

```bash
bash scripts/ensure_model.sh <model_id|asr-auto|tts-default>
```

4. 执行对应 `vox` 命令。
5. 交付前强制自检：

```bash
bash scripts/self_check.sh [--require-model <...>] [--require-file <...>]
```

6. 失败时记录样本：

```bash
bash scripts/log_failure.sh --stage "<stage>" --command "<cmd>" --error "<msg>" [--retry "<retry-cmd>"]
```

## 5 条硬标准（必须全部满足）

1. 平台必须是 `Darwin + arm64`。
2. `vox doctor --json` 必须 `ok=true`。
3. 本次任务所需模型必须 `verified=true`。
4. 主命令退出码必须是 `0`。
5. 需要文件输出时，目标文件必须存在且路径已回报。

## 执行规则

1. 优先使用 JSON 输出（`--json` 或 `--format ndjson`）。
2. 执行前先声明将要运行的命令；执行后回报关键结果。
3. 若命令失败，先给重试命令，再调用 `log_failure.sh` 记录失败样本。
4. 禁止跳过守卫直接执行重操作（下载模型、克隆、pipeline）。

## 常用命令模板

```bash
# 环境检查
scripts/vox_cmd.sh doctor --json

# 查看模型状态
scripts/vox_cmd.sh model status --json

# 离线转写
scripts/vox_cmd.sh asr transcribe --audio ./speech.wav --lang zh --model auto --json

# 语音克隆
scripts/vox_cmd.sh tts clone --profile narrator --text "你好" --out ./out.wav --model qwen-tts-1.7b --json

# 一体化流程
scripts/vox_cmd.sh pipeline run --profile narrator --audio ./input.wav --clone-text "把这段内容读出来" --lang zh --json
```

## 依赖与发布约定

1. 安装优先使用 `uv`。
2. 默认安装系统依赖：`ffmpeg`、`portaudio`。
3. CLI 来源：
   - 优先 `VOX_CLI_PACKAGE_SPEC`（显式指定包）。
   - 若设置 `VOX_CLI_GIT_URL`，使用 `git+<url>` 安装。
   - 默认回退到 `git+https://github.com/catoncat/vox-cli.git`。
