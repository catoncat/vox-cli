# Vox Intents

把自然语言请求映射到可执行 CLI 命令。

## 1) 初始化与环境

- 触发词：
  - "安装 vox"
  - "环境检查"
  - "第一次使用"
- 执行：

```bash
bash scripts/bootstrap.sh
bash scripts/self_check.sh
```

## 2) 模型管理

- 触发词：
  - "下载模型"
  - "检查模型状态"
  - "模型路径"
- 执行：

```bash
scripts/vox_cmd.sh model status --json
bash scripts/ensure_model.sh <model-id|asr-auto|tts-default>
scripts/vox_cmd.sh model path --model <model-id>
```

## 3) ASR 离线/流式转写

- 触发词：
  - "转写音频"
  - "语音转文字"
  - "流式转写"
- 执行：

```bash
bash scripts/ensure_model.sh asr-auto
scripts/vox_cmd.sh asr transcribe --audio <audio> --lang <lang> --model auto --json
# 或
scripts/vox_cmd.sh asr stream --input file --source <audio> --lang <lang> --format ndjson
```

## 4) 语音克隆

- 触发词：
  - "克隆声音"
  - "按这个音色读"
  - "生成这段语音"
- 执行：

```bash
bash scripts/ensure_model.sh tts-default
scripts/vox_cmd.sh profile create --name <name> --lang zh --json
scripts/vox_cmd.sh profile add-sample --profile <name> --audio <ref.wav> --text "<ref text>" --json
scripts/vox_cmd.sh tts clone --profile <name> --text "<target text>" --out <out.wav> --model qwen-tts-1.7b --json
```

## 5) 一体化流程（ASR + TTS）

- 触发词：
  - "跑完整流程"
  - "先转写再克隆"
- 执行：

```bash
bash scripts/ensure_model.sh asr-auto
bash scripts/ensure_model.sh tts-default
scripts/vox_cmd.sh pipeline run --profile <name> --audio <in.wav> --clone-text "<text>" --lang zh --json
```

## 6) 任务排障

- 触发词：
  - "为什么失败"
  - "看任务详情"
- 执行：

```bash
scripts/vox_cmd.sh task list --json
scripts/vox_cmd.sh task show --id <task_id> --json
```
