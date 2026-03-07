# Shared Buffer

`vox-vmic` 当前使用一个 `mmap` 文件作为 helper 与 driver 的最小共享通道。

## 默认路径

通过 `vmic_default_shared_path()` 解析，默认落在：

- `$(TMPDIR)/com.envvar.vox.vmic/stream.bin`

## 协议

头部结构定义在 `shared/include/VMicBridge.h`，实现见 `shared/VMicBridge.c`。

关键字段：

- `version`
- `channels`
- `sampleRate`
- `capacityFrames`
- `writeIndex`
- `readIndex`
- `queuedFrames`
- `state`
- `generation`

状态值：

- `idle`
- `ready`
- `draining`

## 当前取舍

- 先只支持单声道 `float32`
- 先只支持单 producer / 单 consumer
- 当前主要用于 MVP 验证，不追求最终低延迟上限

## 未来演进

后续可以继续演进到：

- 真正实时 feeder
- 更强的背压/覆盖策略
- 控制面与音频面分离（XPC/Mach + shared memory）
