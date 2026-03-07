# vox-cli

独立于任何现有业务项目的本地语音编排 CLI，面向 Apple Silicon + MLX：

- ASR：`Qwen3-ASR-1.7B`（`4bit/8bit`）
- TTS：`Qwen3-TTS`（`clone / custom / design`，含 `1.7B/0.6B` 变体）
- 下载策略：默认国内镜像 `hf-mirror`，失败自动回退官方 Hugging Face
- 缓存策略：优先复用本地 Hugging Face 缓存，避免重复下载
- Dictation：原生 macOS 按住说话输入，前端参考 `picc`，后端复用本地 Qwen ASR 流式会话

---

## 0. Skills 单入口安装（推荐）

如果你希望通过自然语言使用 `vox` 全能力（安装、模型管理、ASR/TTS、pipeline），推荐安装本仓库内置 skill：

```bash
npx skills add catoncat/vox-cli --skill vox -g -y
```

说明：

- skill 路径在仓库内：`.agents/skills/vox`
- 首次执行会自动做平台守卫（仅 `macOS arm64`）并默认安装依赖：`uv`、`ffmpeg`、`portaudio`
- CLI 安装优先 `uv`（可通过环境变量覆盖安装源）

---

## 1. 设计目标

1. 完全独立：不依赖某个 Web/App 服务。
2. 可复现：所有动作都通过 CLI 命令完成。
3. 可观测：下载、转写、克隆均记录任务状态。
4. 中国网络友好：镜像优先 + 官方回退。

---

## 2. 运行要求

- 系统：`macOS`（Apple Silicon，`arm64`）
- Python：`>=3.10,<3.13`
- 包管理：`uv`
- 推理后端：`mlx` + `mlx-audio`

> 说明：当前版本只支持 Apple Silicon + MLX。

---

## 3. 安装与初始化

### 3.1 安装依赖

```bash
cd /Users/envvar/work/repos/vox-cli
uv sync
```

如果你要用麦克风流式转写（`asr stream --input mic`），再安装可选依赖：

```bash
uv sync --extra mic
```

如果你要用 `vox dictation`，还需要本机有 Rust 工具链（`cargo`），因为首次运行会编译原生前端 helper。

### 3.2 基础自检

```bash
uv run vox doctor
```

JSON 输出：

```bash
uv run vox doctor --json
```

### 3.3 更新全局命令

如果你在本地开发 `vox-cli`，推荐用内置更新命令把当前仓库安装到全局：

```bash
cd /Users/envvar/work/repos/vox-cli
uv run vox self update --repo .
```

只想先看会执行什么命令：

```bash
uv run vox self update --repo . --dry-run
```

如果全局 `vox` 还是旧版，无法识别 `self update`，用手动兜底：

```bash
cd /Users/envvar/work/repos/vox-cli
uv build
uv tool install --force --prerelease=allow dist/vox_cli-0.1.0-py3-none-any.whl
```

> 推荐用 `wheel` 安装全局命令，不建议长期依赖 `uv tool install .`。

---

## 4. 核心能力

### 4.1 模型注册

内置模型：

1. TTS：
   - `qwen-tts-1.7b` -> `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16`（默认 clone）
   - `qwen-tts-1.7b-base-8bit` -> `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit`
   - `qwen-tts-1.7b-customvoice-8bit` -> `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit`
   - `qwen-tts-1.7b-voicedesign-8bit` -> `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit`
   - `qwen-tts-0.6b-base-8bit` -> `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit`
   - `qwen-tts-0.6b-customvoice-8bit` -> `mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit`
   - `qwen-tts-0.6b-voicedesign-8bit` -> `mlx-community/Qwen3-TTS-12Hz-0.6B-VoiceDesign-8bit`
2. ASR：
   - `qwen-asr-1.7b-8bit` -> `mlx-community/Qwen3-ASR-1.7B-8bit`
   - `qwen-asr-1.7b-4bit` -> `mlx-community/Qwen3-ASR-1.7B-4bit`

### 4.2 ASR 自动选型

当 `--model auto` 或配置中 `asr.default_model = "auto"` 时：

- 内存 `>= 32GB`：默认 `8bit`
- 内存 `< 32GB`：默认 `4bit`

阈值可配置（见后文配置章节）。

### 4.3 下载与缓存

- 先检查本地缓存完整性（`refs/main`、`snapshots`、权重文件、无 `.incomplete`）
- 校验通过则直接复用
- 校验不通过才触发下载
- 下载端点默认顺序：
  1. `https://hf-mirror.com`
  2. `https://huggingface.co`

---

## 5. 配置

默认配置文件：`~/.vox/config.toml`

参考模板：`config.example.toml`

```toml
[runtime]
home_dir = "~/.vox"

[hf]
endpoints = ["https://hf-mirror.com", "https://huggingface.co"]
# cache_dir = "~/.cache/huggingface/hub"

[asr]
default_model = "auto" # auto | qwen-asr-1.7b-8bit | qwen-asr-1.7b-4bit
memory_threshold_gb = 32

[tts]
default_model = "qwen-tts-1.7b"
```

### 5.1 环境变量优先级

从高到低：

1. 环境变量
2. `~/.vox/config.toml`
3. 内置默认值

关键环境变量：

- `VOX_HOME`：修改运行目录（默认 `~/.vox`）
- `VOX_HF_ENDPOINTS`：覆盖下载端点列表，逗号分隔
- `HF_ENDPOINT`：强制优先某个单端点（会放到端点列表首位）
- `HF_HUB_CACHE`：覆盖 Hugging Face 缓存目录
- `VOX_ASR_DEFAULT_MODEL`：覆盖 ASR 默认模型
- `VOX_ASR_MEMORY_THRESHOLD_GB`：覆盖自动选型阈值

查看当前生效配置：

```bash
uv run vox config show
uv run vox config show --pretty
```

---

## 6. 命令总览

```bash
vox version
vox doctor
vox model ...
vox profile ...
vox asr ...
vox tts ...
vox pipeline ...
vox task ...
vox config ...
vox dictation ...
vox self update ...
```

---

## 7. 命令手册

## 7.1 `model`

### 列出支持模型

```bash
uv run vox model list
```

### 查看缓存与校验状态

```bash
uv run vox model status
uv run vox model status --json
```

### 校验指定模型是否完整

```bash
uv run vox model verify --model qwen-tts-1.7b
```

### 下载指定模型（若已完整则直接复用）

```bash
uv run vox model pull --model qwen-asr-1.7b-4bit
```

### 输出模型快照路径

```bash
uv run vox model path --model qwen-tts-1.7b
```

## 7.2 `profile`

### 创建 profile

```bash
uv run vox profile create --name narrator --lang zh
```

### 查看 profile 列表

```bash
uv run vox profile list
uv run vox profile list --json
```

### 添加参考样本

```bash
uv run vox profile add-sample \
  --profile narrator \
  --audio ./ref.wav \
  --text "这是参考文本"
```

样本硬约束：

- 时长 `2~30` 秒
- RMS >= `0.005`
- 会统一转存为 WAV 到 `~/.vox/profiles/<profile_id>/`

## 7.3 `asr`

### 离线转写

```bash
uv run vox asr transcribe \
  --audio ./speech.wav \
  --lang zh \
  --model auto
```

`--model` 可选：

- `auto`
- `qwen-asr-1.7b-8bit`
- `qwen-asr-1.7b-4bit`

### 流式转写（文件输入）

```bash
uv run vox asr stream \
  --input file \
  --source ./speech.wav \
  --lang zh \
  --model auto \
  --format ndjson
```

`--format` 可选：

- `text`：纯文本连续输出
- `ndjson`：每行一个 JSON chunk，便于程序消费

### 流式转写（麦克风输入，实验）

```bash
uv run vox asr stream \
  --input mic \
  --source mic \
  --mic-seconds 12 \
  --lang zh
```

> 需要 `uv sync --extra mic`。

### 常驻会话服务（`session-server`）

```bash
uv run vox asr session-server --lang zh --model auto --port 8765
```

协议说明：

- 二进制帧：`PCM16LE` 单声道音频块
- 控制消息：`partial`、`flush`、`reset`、`close`、`ping`
- 结果消息：`text` + `is_partial`，用于原生 dictation 前端消费

## 7.4 `dictation`

```bash
uv run vox dictation --lang zh --model auto
```

特点：

- 原生 macOS 前端，参考 `picc` 的 push-to-talk 体验
- 默认监听右侧 `Command`：按下开始录音，松开提交当前语音段
- `dictation` 前台运行时，后端会话日志直接输出到当前终端
- 首次运行会自动用 `cargo build --release` 编译 `native/vox-dictation`
- 启动时会打印 helper 版本指纹，例如：`v0.1.0 (<git-hash>, build <timestamp>)`

常用参数：

- `--host` / `--port`：自定义本地会话服务地址
- `--rebuild-native`：强制重编原生 helper
- `--partial-interval-ms`：录音期间周期性请求 partial 结果

推荐用法：

```bash
# 开发态验证当前仓库代码
cd /Users/envvar/work/repos/vox-cli
uv run vox dictation --lang zh --rebuild-native

# 更新全局命令后日常使用
vox dictation --lang zh
```

如果 `start/stop` 正常，但没有文字输出，重点检查这几类日志：

- `[vox-dictation][audio] sending ...`：前端是否真的送出音频块
- `[session-server] append_pcm16 ...`：后端是否真的收到音频块
- `[session-server] transcript ...`：后端是否真的返回识别结果

### 来源与致谢

当前 `vox dictation` 的原生 macOS 前端实现，明确参考并部分移植自 `andelf/picc` 仓库中的 `dictation` 工具实现：

- 仓库：`https://github.com/andelf/picc`
- 作者：`Andelf <andelf@gmail.com>`

感谢原作者在 `objc2`、`AppKit`、`AVFAudio`、全局热键与 push-to-talk dictation 这条链路上的探索与开源实现。

## 7.5 `tts`

### 语音克隆（`clone`）

```bash
uv run vox tts clone \
  --profile narrator \
  --text "你好，这是克隆后的语音" \
  --out ./out.wav \
  --model qwen-tts-1.7b
```

可选参数：

- `--seed`
- `--instruct`（仅当底层模型接口支持时生效）

### 预置说话人合成（`custom`）

```bash
uv run vox tts custom \
  --text "你好，这是 Vivian 的示例语音" \
  --speaker Vivian \
  --language auto \
  --instruct "开心，语速自然" \
  --out ./custom.wav \
  --model qwen-tts-1.7b-customvoice-8bit
```

可选参数：

- `--seed`（仅当底层模型接口支持时生效）

### 声音设计合成（`design`）

```bash
uv run vox tts design \
  --text "你好，这是按描述设计出来的声音" \
  --instruct "低沉男声，播音腔，语气稳重" \
  --language auto \
  --out ./design.wav \
  --model qwen-tts-1.7b-voicedesign-8bit
```

可选参数：

- `--seed`（仅当底层模型接口支持时生效）

## 7.6 `pipeline`

### 一体化流程（先 ASR 再 TTS 克隆）

```bash
uv run vox pipeline run \
  --profile narrator \
  --audio ./input.wav \
  --clone-text "把这段内容读出来" \
  --lang zh
```

可选：

- `--asr-model auto|qwen-asr-1.7b-8bit|qwen-asr-1.7b-4bit`
- `--tts-model qwen-tts-1.7b`
- `--out ./result.wav`

## 7.7 `task`

所有关键操作会写入 SQLite 任务表。

### 查看任务列表

```bash
uv run vox task list
uv run vox task list --json
```

### 查看单个任务详情

```bash
uv run vox task show --id <task_id>
```

---

## 8. 数据目录布局

默认 `VOX_HOME=~/.vox`，目录结构：

```text
~/.vox/
├── config.toml
├── vox.db
├── cache/
│   └── voice_prompt/
├── profiles/
│   └── <profile_id>/
└── outputs/
```

Hugging Face 模型缓存默认不在 `~/.vox`，而在：

```text
~/.cache/huggingface/hub
```

可通过 `HF_HUB_CACHE` 覆盖。

---

## 9. 典型工作流

## 9.1 第一次启动（推荐）

```bash
cd /Users/envvar/work/repos/vox-cli
uv sync
uv run vox doctor --json
uv run vox model status --json
```

如果 ASR 未下载：

```bash
uv run vox model pull --model qwen-asr-1.7b-4bit
```

## 9.2 复用已下载缓存（不重复下载）

```bash
uv run vox model verify --model qwen-tts-1.7b
uv run vox model status --json
```

当 `verified=true` 时，推理会直接复用缓存。

## 9.3 从 0 到语音克隆

```bash
uv run vox profile create --name narrator --lang zh
uv run vox profile add-sample --profile narrator --audio ./ref.wav --text "这是参考文本"
uv run vox tts clone --profile narrator --text "这是目标文本" --out ./out.wav
```

## 9.4 更新全局命令

```bash
cd /Users/envvar/work/repos/vox-cli
uv run vox self update --repo .
```

---

## 10. 故障排查

### 10.1 `doctor` 失败

优先看：

```bash
uv run vox doctor --json
```

常见问题：

1. 不是 Apple Silicon：当前版本不支持。
2. `mlx` / `mlx-audio` 未安装：先 `uv sync`。
3. 缓存路径不可写：检查 `HF_HUB_CACHE` 或目录权限。

### 10.2 模型下载失败

1. 检查端点顺序：
   `uv run vox config show --pretty`
2. 临时强制镜像：

```bash
HF_ENDPOINT=https://hf-mirror.com uv run vox model pull --model qwen-asr-1.7b-4bit
```

3. 临时强制官方：

```bash
HF_ENDPOINT=https://huggingface.co uv run vox model pull --model qwen-asr-1.7b-4bit
```

### 10.3 流式麦克风不可用

1. 确认安装了 `mic` extra：`uv sync --extra mic`
2. macOS 麦克风权限授权终端
3. 先用文件流式排除模型问题：
   `uv run vox asr stream --input file --source ./speech.wav`

### 10.4 Dictation 启动失败

1. `cargo` 不存在：先安装 Rust toolchain
2. `native/vox-dictation/Cargo.toml` 缺失：确认仓库完整
3. 端口占用：换一个 `--port`
4. 没有输入回填：检查 macOS 的 Accessibility 权限
5. 如果全局 `vox` 看起来还是旧版，先执行：`uv run vox self update --repo .`

### 10.5 Dictation 没有输出文字

重点观察当前终端里的这些日志：

- `[vox-dictation] recording started...` / `recording stopped`
- `[vox-dictation][audio] sending ...`
- `[session-server] append_pcm16 ...`
- `[session-server] transcript ...`

如果热键能开始/停止，但没有 `audio sending`，优先检查前端音频链路。
如果有 `audio sending` 但没有 `append_pcm16`，优先检查本地 websocket 链路。
如果有 `append_pcm16` 但没有 `transcript`，优先检查模型/音频格式。

### 10.6 样本添加失败

- 时长超限（<2s 或 >30s）
- 音量过低（RMS < 0.005）
- 音频文件路径错误

---

## 11. 输出与集成建议

- 推荐上游系统优先使用 `--json` / `ndjson` 输出，避免解析富文本表格。
- 如果你做自动化编排，优先使用：
  - `model status --json`
  - `asr transcribe --json`
  - `task show --id ... --json`

---

## 12. 路线图（建议）

1. 增加 `model delete` 与缓存清理命令
2. 增加 `profile sample list/remove`
3. 把 `dictation` 的 partial UI 做到更平滑（当前以最终提交为主）
4. 增加结构化日志与 Prometheus 指标导出
