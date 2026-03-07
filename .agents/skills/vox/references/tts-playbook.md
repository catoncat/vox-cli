# TTS Playbook

用于 TTS 合成输出，支持 `clone / custom / design` 三种模式。

## 场景定位

这是重操作场景，继承 `references/orchestration-matrix.md`：按具体模式选最小 happy path，但统一遵守“先业务、后预检、交付前门禁”。

## 预处理

1. `clone` 模式若 profile 名不够明确，先看一次现有 profile：

```bash
scripts/vox_cmd.sh profile list --json
```

2. 先根据模式保证模型可用：

```bash
# clone：按配置中的 `tts.default_model`
bash scripts/ensure_model.sh tts-default

# custom：按配置中的 `tts.default_custom_model`
bash scripts/ensure_model.sh tts-custom-default

# design：按配置中的 `tts.default_design_model`
bash scripts/ensure_model.sh tts-design-default
```

3. `clone` 模式需要 profile；若 profile 不存在，先创建并添加至少 1 个样本。

## 最小 happy path

1. 仅在 profile 名不够明确时，执行一次 `scripts/vox_cmd.sh profile list --json`。
2. 执行一次 `bash scripts/ensure_model.sh <...>`。
3. 直接执行目标 TTS 命令。
4. 交付前执行一次 `bash scripts/health_gate.sh --require-file <output.wav>`。
5. 仅当业务命令报命令不存在、依赖缺失或导入失败时，才回退到 `bootstrap.sh --check` / `bootstrap.sh`。
6. 排查 skill 编排时，不要为“测速度”重复执行 TTS 或做压测；单次最小复现即可。

## 模式差异

1. `clone`：可能需要先确认 `profile`，因此仅在 profile 名不明确时额外执行 `profile list --json`。
2. `custom`：不依赖本地 profile，直接 `ensure_model` → `tts custom` → `health_gate`。
3. `design`：不依赖本地 profile，直接 `ensure_model` → `tts design` → `health_gate`。

## 语音克隆（clone）

```bash
scripts/vox_cmd.sh tts clone \
  --profile <name-or-id> \
  --text "<target text>" \
  --out <output.wav> \
  --json
```

可选参数：

1. `--seed <int>`：固定随机性。
2. `--instruct "<style>"`：底层模型支持时生效。
3. `--model <model-id>`：仅在需要覆盖配置默认时传入。

## 预置说话人（custom）

```bash
scripts/vox_cmd.sh tts custom \
  --text "<target text>" \
  --speaker Vivian \
  --language auto \
  --instruct "开心，语速稍快" \
  --out <output.wav> \
  --json
```

可选参数：

1. `--seed <int>`：模型支持时生效。
2. `--model <model-id>`：仅在需要覆盖配置默认时传入。

## 声音设计（design）

```bash
scripts/vox_cmd.sh tts design \
  --text "<target text>" \
  --instruct "低沉男声，播音腔，语气稳重" \
  --language auto \
  --out <output.wav> \
  --json
```

可选参数：

1. `--seed <int>`：模型支持时生效。
2. `--model <model-id>`：仅在需要覆盖配置默认时传入。

## 交付要求

1. 回报输出路径 `output.wav`。
2. 回报 `model_id`、`task_id`、`mode`。
3. 若文件未生成，必须给出重试命令。
