# vox-vmic

`vox-vmic` 是 `vox-cli` 里的虚拟麦克风 MVP 子系统。

目标：

- 提供一个系统可见的虚拟输入设备 `Vox Virtual Mic`
- 把本地音频文件或后续实时 DSP 输出喂进去
- 为微信、会议软件、实时变声、ASR 联动打基础

## 现在已经能做什么

- 构建 helper：`vox-vmicctl`
- 构建并安装 HAL driver：`VoxVirtualMic.driver`
- 在系统音频设备中枚举出 `Vox Virtual Mic`
- 往虚拟麦克风写测试音或本地音频文件
- 用支持选择输入设备的软件把它当麦克风录入

## 一次跑通

```bash
cd native/vox-vmic
make install-driver
.build/debug/vox-vmicctl prime-sine --seconds 2 --frequency 660
make probe-driver
```

如果 `system_profiler` 或 `ffmpeg` 里能看到 `Vox Virtual Mic`，说明驱动已加载成功。

## 实际使用

### 1) 往虚拟麦克风写测试音

```bash
cd /Users/envvar/work/repos/vox-cli
uv run python -m vox_cli.main vmic prime-sine --seconds 2 --frequency 660
```

### 2) 往虚拟麦克风写本地音频文件

```bash
cd /Users/envvar/work/repos/vox-cli
uv run python -m vox_cli.main vmic enqueue --audio /path/to/input.wav
```

### 3) 在目标 App 里选输入设备

选择：`Vox Virtual Mic`

可用于：

- 微信 Mac
- QuickTime Player
- ffmpeg / OBS / 会议软件

## 常用命令

```bash
cd /Users/envvar/work/repos/vox-cli/native/vox-vmic
make build-helper
make build-driver
make install-driver
make uninstall-driver
make probe-driver
```

```bash
cd /Users/envvar/work/repos/vox-cli
uv run python -m vox_cli.main vmic status
uv run python -m vox_cli.main vmic prime-sine --seconds 2 --frequency 660
uv run python -m vox_cli.main vmic enqueue --audio /path/to/file.wav
uv run python -m vox_cli.main vmic build-driver
```

## 当前边界

还没做：

- 实时 feeder / 实时变声
- menubar helper app
- ASR tap / monitor output
- 更完整的 HAL 属性与控制项

但作为 MVP，已经可以把它当成一个可安装、可枚举、可喂音频的虚拟麦克风来用了。
