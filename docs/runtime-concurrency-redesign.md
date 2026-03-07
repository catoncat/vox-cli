# Vox CLI 并发安全与运行时治理整改

## 1. 背景与问题清单

当前 `vox-cli` 的重操作以同步 CLI 方式直接执行，但缺少统一的运行时资源治理，导致多个命令同时触发时会把模型下载、模型加载、推理、音频写入同时叠加到同一台机器上。

### P0

1. 重任务没有进程级并发门禁
   - 受影响：`tts clone/custom/design`、`pipeline run`、`asr transcribe/stream/session-server`、`model pull`
   - 现状：`tracked_task` 只负责 SQLite 记账，不负责任务排队、互斥或资源治理。
2. 每次推理都独立加载模型
   - 受影响：TTS/ASR 全部推理路径
   - 现状：每个进程都会重新 `load_model/load`，没有本机单飞与串行化。
3. `ensure_model_downloaded` 后仍把 repo id 传回加载层
   - 受影响：TTS/ASR 全部推理路径
   - 现状：会再次进入 `mlx_audio` 的 `get_model_path/snapshot_download` 路径解析链路。

### P1

4. 默认 TTS 模型过重
   - 现状：默认是 `qwen-tts-1.7b`（`1.7B bf16`），误并发时爆炸半径过大。
5. prompt 音频生成不是原子操作
   - 现状：仅做 `exists()` 判断，并发命中同一 profile 时可能重复构建。
6. 输出音频写入不是原子操作
   - 现状：直接写最终路径，并发或中断时可能覆盖/写坏。
7. TTS 结果先全量攒内存再拼接
   - 现状：长文本和并发时会额外放大 CPU/内存压力。

### P2

8. 运行态缓存校验过重
   - 现状：之前的校验会递归扫描 `.incomplete` 和权重目录，运行态开销偏大。
9. `task` 语义容易误导
   - 现状：当前是任务记录，不是后台调度队列。
10. README 对缓存复用的表述过强
   - 现状：容易让用户误以为多个命令会共享模型实例。

## 2. 本次整改目标

### Phase 1：同步 CLI 并发治理

本次只改同步 CLI，不引入后台 worker，但把它做成：

- 有统一的进程级锁
- 默认支持等待，并给出清晰交互
- 所有重任务通过同一套运行时选项传递等待策略与日志
- 模型下载、TTS 推理、ASR 推理、prompt 构建、输出写入都走独立资源锁
- 模型加载统一改为本地 snapshot path，避免重复进入 Hub 路径解析
- TTS 输出改为流式落盘 + 原子替换

### 非目标

- 不做后台任务队列与 worker 常驻进程
- 不做跨进程模型实例复用
- 不做 TTS 自动按内存选型

## 3. Phase 1 设计

### 3.1 运行时资源模型

统一使用 `~/.vox/locks/` 下的锁文件管理资源，占用中的命令默认等待，超时后失败。

资源划分：

- `tts_infer`
- `asr_infer`
- `model_download:<model_id>`
- `prompt_build:<profile_id>:<prompt_key>`
- `output_write:<abs_output_path>`

每把锁都记录：

- `task_type`
- `task_id`
- `pid`
- `command_summary`
- `started_at`
- 关键元数据（`model_id/profile/audio/out`）

等待中的命令会周期性打印：

- 正在等待的资源名
- 已等待时长
- 当前持有者摘要

### 3.2 配置与 CLI

新增配置：

```toml
[runtime]
home_dir = "~/.vox"
wait_for_lock = true
lock_wait_timeout_sec = 1800
```

新增 CLI 选项到重命令：

- `--wait/--no-wait`
- `--wait-timeout <sec>`

行为规则：

- CLI 未显式传参时，使用配置值
- `--no-wait` 时，发现占用立即失败
- `--wait` 或默认配置为等待时，进入阻塞等待并输出持有者信息

### 3.3 模型获取与加载

统一链路：

1. `resolve_model`
2. `ensure_model_downloaded`
3. 返回本地 `snapshot_path`
4. TTS/ASR 加载层只接收本地路径

这样避免 `ensure_model_downloaded` 之后再次把 repo id 传给 `mlx_audio`，从而避免重复进入 `snapshot_download` 解析链路。

### 3.4 原子写入与流式落盘

- prompt wav：先写临时文件，完成后 `os.replace`
- 最终输出 wav：先写同目录临时文件，完成后 `os.replace`
- TTS 输出：边生成边写入 `SoundFile`，不再先把所有 chunk 收集到内存后 `np.concatenate`

## 4. 用户可见行为变更

1. 默认 TTS 模型改为 `qwen-tts-0.6b-base-8bit`
2. 重命令默认会等待锁，而不是直接并发冲击机器
3. `task` 仍是审计记录，不是后台任务队列
4. “复用缓存”指复用已下载 snapshot，不代表复用同一模型实例

## 5. Phase 2 演进方向

后续如需进一步提升吞吐和稳定性，再演进到真正任务队列：

- CLI 负责提交任务
- 本地 worker 常驻持有模型实例
- `task` 由审计表升级为真实队列表
- TTS/ASR 可以按 worker 池和设备策略做更精细的调度

这一步不在本次实现范围内，但本次的资源命名、等待策略和锁元数据设计，都是为了给后续队列化保留兼容空间。
