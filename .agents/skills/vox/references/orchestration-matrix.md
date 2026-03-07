# Orchestration Matrix

这个文件定义 `vox` skill 的统一编排总则。所有请求先确定场景，再按本矩阵选择最小 happy path，然后才进入对应 playbook。

## 全局总则

1. 普通业务请求先走业务命令，不把 `bootstrap.sh --check`、`doctor`、安装流程当作主路径。
2. 只有重操作（`ASR` / `TTS` / `pipeline` / 显式模型准备）才走 `bash scripts/ensure_model.sh ...`。
3. `health_gate` 是交付门禁，不是默认前置预检；仅在交付前或显式体检请求时执行。
4. 名称、profile、模型不明确时，优先查一次本地状态（如 `profile list --json`、`model status --json`），不要猜。
5. 业务命令失败后，再按失败类型回退：`env -> bootstrap --check/install`、`model -> ensure_model`、`input -> 修参数/路径`、`runtime -> failure-loop`。
6. 排查 skill 编排问题时，默认静态审查 + 单次轻量复现；禁止擅自做压测、基准测试、并发重任务或重复重推理，除非用户明确要求。

## 决策矩阵

### 安装 / 体检

- 触发：安装、初始化、环境检查、命令不存在、明确要求自检
- 最小 happy path：`bash scripts/health_gate.sh`；若命令不可用或依赖缺失，再 `bash scripts/bootstrap.sh --check`
- 升级路径：`bootstrap --check` 失败后，才 `bash scripts/bootstrap.sh`
- 交付门禁：`health_gate`
- 禁止：普通业务请求一上来先走安装链路

### Model

- 触发：查看模型状态、路径、缓存；或显式要求 verify/pull/prepare model
- 最小 happy path：`model status/path` 直接执行；只有显式模型准备才 `ensure_model`
- 升级路径：命令不可用或环境错误时，再回退 `bootstrap`
- 交付门禁：默认无；若任务本身是体检/安装，再走 `health_gate`
- 禁止：把 `model status` 这类轻查询升级成完整预检

### Profile

- 触发：list/create/add-sample/管理 profile
- 最小 happy path：直接执行 `profile` 子命令；名字不明确时先 `profile list --json` 一次
- 升级路径：仅在命令不可用、依赖缺失或环境错误时回退 `bootstrap`
- 交付门禁：默认无；有明确产物/系统状态要求时按用户目标补充验证
- 禁止：先做外部搜索、硬编码别名，或无故触发模型校验

### ASR

- 触发：离线转写、流式转写、麦克风转写
- 最小 happy path：`ensure_model asr-auto` → `asr ...` → 交付前 `health_gate`
- 升级路径：仅在环境错误时回退 `bootstrap`
- 交付门禁：`health_gate`；若有输出文件，再额外校验文件存在
- 禁止：为排查编排重复跑长音频、长时流式任务或压力测试

### TTS

- 触发：`clone` / `custom` / `design`
- 最小 happy path：`clone` 且 profile 不明确时先 `profile list --json`；然后按模式选择 `ensure_model` alias（`tts-default` / `tts-custom-default` / `tts-design-default`）→ `tts ...` → `health_gate`
- 升级路径：仅在环境错误时回退 `bootstrap`
- 交付门禁：`health_gate --require-file <output.wav>`
- 禁止：为确认流程重复跑重推理、反复 benchmark，或把 `clone` 专属逻辑套到全部 TTS 场景

### Pipeline

- 触发：端到端“先 ASR 后 TTS”
- 最小 happy path：必要时先 `profile list --json`；然后 `ensure_model asr-auto` + `ensure_model tts-default` → `pipeline run` → `health_gate`
- 升级路径：仅在环境错误时回退 `bootstrap`
- 交付门禁：`health_gate`；若有音频输出，要求文件存在
- 禁止：为确认流程重复跑完整 pipeline 或并发多条重任务

### Troubleshooting

- 触发：为什么失败、如何重试、任务卡住、编排是否合理
- 最小 happy path：先静态读文档、命令帮助、失败日志、状态文件；必要时做单次最小复现
- 升级路径：按失败分类走 `failure-loop`，再回写可执行重试命令
- 交付门禁：给出根因、重试命令、是否需要修改 matrix/playbook
- 禁止：未经明确授权直接做压测、性能基准、长时重任务
