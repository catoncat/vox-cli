# TTS Playbook

用于语音克隆与合成输出。

## 预处理

1. 先保证 TTS 模型可用：

```bash
bash scripts/ensure_model.sh tts-default
```

2. 若 profile 不存在，先创建并添加至少 1 个样本。

## 语音克隆命令

```bash
scripts/vox_cmd.sh tts clone \
  --profile <name-or-id> \
  --text "<target text>" \
  --out <output.wav> \
  --model qwen-tts-1.7b \
  --json
```

## 可选参数

1. `--seed <int>`：固定随机性。
2. `--instruct "<style>"`：底层模型支持时生效。

## 交付要求

1. 回报输出路径 `output.wav`。
2. 回报 `model_id` 与 `task_id`。
3. 若文件未生成，必须给出重试命令。
