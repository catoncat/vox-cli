# Architecture

## 目标

先做一个最小可验证的虚拟麦克风闭环：

`audio file / test tone -> vox-vmicctl -> shared ring buffer -> AudioServerPlugIn -> App 读取输入设备`

## 模块

### 1. helper CLI (`Sources/vmicctl`)

职责：

- 生成测试音或读取本地音频文件
- 解码、下混、重采样到统一 PCM
- 把样本写入共享 ring buffer
- 输出 JSON 状态，方便 Python 层和调试脚本消费

### 2. shared ring buffer (`shared/`)

职责：

- 统一 helper/driver 之间的数据协议
- 用 `mmap` 文件做最小共享内存通道
- 先支持单 producer / 单 consumer

当前默认：

- sample rate: `48000`
- channels: `1`
- sample format: `float32`

### 3. virtual mic driver (`driver/`)

职责：

- 向 Core Audio HAL 暴露一个虚拟输入设备
- 在 `ReadInput` 阶段从 ring buffer 拉取样本
- 数据不足时补静音

当前状态：

- 已有 `AudioServerPlugIn` skeleton
- 已接入 shared ring buffer reader
- 仍需继续补齐 HAL 属性与真实枚举验证

## 为什么先用 mmap ring buffer

因为 MVP 第一目标是：

- 尽快证明“系统可见虚拟麦克风”这条链路能通
- 不把复杂度浪费在 GUI、网络传输、通用编排上

后续如果做实时变声 / ASR / 监听 / 多路混音，再升级为：

- 真正的 lock-free ring buffer
- Mach service / XPC 控制面
- helper app + menubar + session orchestration

## 下一步

1. helper 与 driver 都能稳定构建
2. 安装后在 `Audio MIDI 设置` 看见 `Vox Virtual Mic`
3. QuickTime / 微信 / 会议软件选中并录到测试音
4. 再接实时音频管线
