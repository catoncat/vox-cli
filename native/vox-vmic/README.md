# vox-vmic

`vox-vmic` 是 `vox-cli` 里的虚拟麦克风 MVP 子系统。

目标：

- 先做一个系统可见的虚拟输入设备
- 再把本地音频文件 / 后续实时 DSP 输出喂进去
- 为微信、会议软件、实时变声、ASR 联动打基础

当前结构：

- `Package.swift` + `Sources/vmicctl`：helper CLI，负责准备测试音/文件并写入共享 ring buffer
- `shared/`：helper 与 driver 共享的 ring buffer 协议与实现
- `driver/`：`AudioServerPlugIn` skeleton，后续暴露成系统虚拟麦克风
- `docs/`：架构、协议、验证计划
- `scripts/`：构建/安装/卸载/重载系统音频服务脚本

## 当前状态

已经打通：

- `vox-vmicctl prime-sine`
- `vox-vmicctl enqueue <audio-file>`
- `vox-vmicctl status`
- `driver/` 可继续朝编译成功推进

还没做：

- 完整 HAL 对象属性覆盖
- 安装后在 `Audio MIDI 设置` 成功枚举
- 微信/会议软件真实验证
- 实时 feeder / 变声 / ASR tap

## 快速验证

```bash
cd native/vox-vmic
make build-helper
.build/debug/vox-vmicctl prime-sine --seconds 1.5 --frequency 440
.build/debug/vox-vmicctl status
```

构建 driver：

```bash
cd native/vox-vmic
make build-driver
```

## 高风险脚本

下面这些脚本会改系统音频环境，默认不自动执行：

- `scripts/install-driver.sh`
- `scripts/uninstall-driver.sh`
- `scripts/restart-coreaudiod.sh`
