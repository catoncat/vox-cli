# Model Playbook

用于模型状态、下载、路径和缓存复用问题。

## 标准流程

1. 查看全量状态：

```bash
scripts/vox_cmd.sh model status --json
```

2. 解析目标模型：
   - ASR 默认：`asr-auto`
   - TTS 默认：`tts-default`
   - 指定模型：`qwen-asr-1.7b-4bit`、`qwen-tts-1.7b-customvoice-8bit`、`qwen-tts-1.7b-voicedesign-8bit` 等

3. 保证模型可用：

```bash
bash scripts/ensure_model.sh <model-id|asr-auto|tts-default>
```

4. 若用户需要缓存路径：

```bash
scripts/vox_cmd.sh model path --model <model-id>
```

## 判定规则

1. `verified=true` 才视为可用。
2. `downloaded=true` 但 `verified=false`，要重新 pull + verify。
3. 优先复用本地缓存，不主动重复下载。

## 常见问题

1. 下载失败：先看 `HF_ENDPOINT` 与网络连通。
2. 目录不可写：检查 `HF_HUB_CACHE` 路径权限。
3. 模型不匹配：确认命令使用的是 ASR/TTS 对应模型。
