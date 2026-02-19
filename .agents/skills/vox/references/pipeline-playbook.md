# Pipeline Playbook

用于“先 ASR 后 TTS”一体化任务。

## 预处理

```bash
bash scripts/ensure_model.sh asr-auto
bash scripts/ensure_model.sh tts-default
```

## 一体化命令

```bash
scripts/vox_cmd.sh pipeline run \
  --profile <name-or-id> \
  --audio <input.wav> \
  --clone-text "<text>" \
  --lang zh \
  --json
```

## 适用场景

1. 用户明确要端到端结果（转写 + 合成）。
2. 用户只给输入音频与目标文案，不想拆命令。

## 交付要求

1. 返回转写结果摘要。
2. 返回克隆输出路径。
3. 返回任务 ID 和模型信息。
