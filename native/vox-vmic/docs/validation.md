# Validation

## 已做

- `swift build` 可构建 helper
- `vox-vmicctl prime-sine` 与 `vox-vmicctl status` 可证明 ring buffer 在工作
- `driver/Makefile` 已接入 bundle 构建路径

## 待做

1. `make build-driver` 能稳定通过
2. 安装到 `/Library/Audio/Plug-Ins/HAL`
3. 重载 `coreaudiod`
4. 在 `Audio MIDI 设置` 中看到 `Vox Virtual Mic`
5. 在 QuickTime / 微信 / Zoom 中录到测试音

## 风险点

- `AudioServerPlugIn` 对对象属性很挑，缺字段时 HAL 可能直接忽略
- 当前共享通道更偏 MVP，长期仍建议升级成更严格的实时方案
- 第三方 App 对虚拟输入设备的兼容性需要实测
