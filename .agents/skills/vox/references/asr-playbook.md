# ASR Playbook

用于离线转写、流式转写和麦克风模式。

## 预处理

1. 先保证 ASR 模型可用：

```bash
bash scripts/ensure_model.sh asr-auto
```

2. 交付前要求 JSON/ndjson 优先。

## 离线转写

```bash
scripts/vox_cmd.sh asr transcribe \
  --audio <audio_file> \
  --lang <zh|en|auto> \
  --model auto \
  --json
```

## 流式转写（文件）

```bash
scripts/vox_cmd.sh asr stream \
  --input file \
  --source <audio_file> \
  --lang <zh|en|auto> \
  --model auto \
  --format ndjson
```

## 流式转写（麦克风）

```bash
scripts/vox_cmd.sh asr stream \
  --input mic \
  --source mic \
  --mic-seconds 12 \
  --lang zh \
  --format ndjson
```

## 校验点

1. 转写结果不为空。
2. 返回 `task_id`（若为 `--json`）。
3. 输出模式符合用户需求（文本或 NDJSON）。
