# TTS Playbook

用于 TTS 合成输出，支持 `clone / custom / design` 三种模式。

## 预处理

1. 先根据模式保证模型可用：

```bash
# clone：默认模型（通常是 qwen-tts-1.7b）
bash scripts/ensure_model.sh tts-default

# custom：建议用 CustomVoice 模型
bash scripts/ensure_model.sh qwen-tts-1.7b-customvoice-8bit

# design：建议用 VoiceDesign 模型
bash scripts/ensure_model.sh qwen-tts-1.7b-voicedesign-8bit
```

2. `clone` 模式需要 profile；若 profile 不存在，先创建并添加至少 1 个样本。

## 语音克隆（clone）

```bash
scripts/vox_cmd.sh tts clone \
  --profile <name-or-id> \
  --text "<target text>" \
  --out <output.wav> \
  --model qwen-tts-1.7b \
  --json
```

可选参数：

1. `--seed <int>`：固定随机性。
2. `--instruct "<style>"`：底层模型支持时生效。

## 预置说话人（custom）

```bash
scripts/vox_cmd.sh tts custom \
  --text "<target text>" \
  --speaker Vivian \
  --language auto \
  --instruct "开心，语速稍快" \
  --out <output.wav> \
  --model qwen-tts-1.7b-customvoice-8bit \
  --json
```

可选参数：

1. `--seed <int>`：模型支持时生效。

## 声音设计（design）

```bash
scripts/vox_cmd.sh tts design \
  --text "<target text>" \
  --instruct "低沉男声，播音腔，语气稳重" \
  --language auto \
  --out <output.wav> \
  --model qwen-tts-1.7b-voicedesign-8bit \
  --json
```

可选参数：

1. `--seed <int>`：模型支持时生效。

## 交付要求

1. 回报输出路径 `output.wav`。
2. 回报 `model_id`、`task_id`、`mode`。
3. 若文件未生成，必须给出重试命令。
