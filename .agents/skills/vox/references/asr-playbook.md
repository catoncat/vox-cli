# ASR Playbook

用于离线转写、流式转写和麦克风模式。

## 场景定位

这是重操作场景：先 `ensure_model`，再执行一次目标 ASR 命令，交付前再过门禁。

## 最小 happy path

1. 执行：

```bash
bash scripts/ensure_model.sh asr-auto
```

2. 执行一次目标 ASR 命令。
3. 交付前执行一次：

```bash
bash scripts/health_gate.sh
```

4. 仅当命令不可用、依赖缺失或环境错误时，才回退到 `bootstrap.sh --check` / `bootstrap.sh`。
5. 排查编排问题时，不要为了“确认一下”重复跑长音频、长时流式任务或压力测试。

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

## 交付要求

1. 转写结果不为空。
2. 返回 `task_id`（若为 `--json`）。
3. 输出模式符合用户需求（文本或 NDJSON）。
4. 交付前由 `health_gate` 或显式 `doctor` 覆盖到 `ok=true`；不要在主命令前重复跑。
