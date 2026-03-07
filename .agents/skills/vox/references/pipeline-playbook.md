# Pipeline Playbook

用于“先 ASR 后 TTS”一体化任务。

## 场景定位

这是重操作场景，只在用户明确要端到端结果时使用，不拿它替代更窄的 ASR 或 TTS 请求。

## 最小 happy path

1. 若 `profile` 名不明确，先执行一次：

```bash
scripts/vox_cmd.sh profile list --json
```

2. 执行：

```bash
bash scripts/ensure_model.sh asr-auto
bash scripts/ensure_model.sh tts-default
```

3. 执行一次 `pipeline run`。
4. 交付前执行一次：

```bash
bash scripts/health_gate.sh
```

5. 若有音频输出，要求文件存在。
6. 仅在命令不可用、依赖缺失或环境错误时，才回退到 `bootstrap.sh --check` / `bootstrap.sh`。
7. 排查编排问题时，单次最小复现即可；禁止重复跑完整 pipeline 或并发多条重任务。

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
4. 若有输出音频，补充文件存在性验证。
