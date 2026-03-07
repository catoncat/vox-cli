# Model Playbook

用于模型状态、路径、缓存复用，以及显式模型准备。

## 场景定位

1. `model status` / `model path` 是轻查询。
2. `verify / pull / prepare` 是模型准备。
3. 只有用户明确要求准备模型，或下游重操作明确需要时，才执行 `ensure_model`。

## 最小 happy path

### 查询状态

```bash
scripts/vox_cmd.sh model status --json
```

### 查询路径

```bash
scripts/vox_cmd.sh model path --model <model-id>
```

### 准备模型

```bash
bash scripts/ensure_model.sh <model-id|asr-auto|tts-default|tts-custom-default|tts-design-default>
```

## 规则

1. 纯查询型请求默认不跑 `ensure_model`。
2. 纯查询型请求默认不跑 `bootstrap.sh --check`。
3. `verified=true` 才视为模型可用。
4. `downloaded=true` 但 `verified=false` 时，重新按 `ensure_model` 处理。
5. 优先复用本地缓存，不主动重复下载。

## 常见问题

1. 下载失败：先检查网络与 `HF_ENDPOINT`。
2. 目录不可写：检查 `HF_HUB_CACHE` 权限。
3. 模型不匹配：确认命令使用的是 ASR/TTS 对应模型。
