# Vox Intents

把自然语言请求先映射到场景，再由 `references/orchestration-matrix.md` 选择最小 happy path。默认先执行业务，缺依赖才安装。

## 路由表

1. 安装/体检类请求：
   - 触发词：安装、初始化、环境检查、跑不起来
   - 先读：`references/checklist.md`
   - 再读：`references/response-contract.md`
   - 优先命令：体检优先 `bash scripts/health_gate.sh`；仅在命令不可用、依赖缺失或用户明确要求安装检查时才用 `bash scripts/bootstrap.sh --check`
2. 模型请求：
   - 触发词：下载模型、看缓存、模型路径、模型是否完整
   - 先读：`references/model-playbook.md`
3. Profile/采样请求：
   - 触发词：新建角色、添加样本、管理 profile
   - 先读：`references/profile-playbook.md`
4. ASR 请求：
   - 触发词：转写、语音转文字、流式输出、麦克风转写
   - 先读：`references/asr-playbook.md`
5. TTS 合成请求：
   - 触发词：克隆声音、生成语音、用某个 profile 读文本、指定 speaker 说话、按描述设计声音
   - 先读：`references/tts-playbook.md`
6. 一体化请求：
   - 触发词：完整流程、先转写再合成、端到端
   - 先读：`references/pipeline-playbook.md`
7. 报错/排障请求：
   - 触发词：为什么失败、重试、查看任务
   - 先读：`references/failure-loop.md`

## 路由规则

1. 先判定场景，再按 `references/orchestration-matrix.md` 选择该场景的最小 happy path。
2. 优先选择最窄场景（例如“转写”优先走 ASR playbook，不走 pipeline）。
3. 用户没明确输出格式时，默认选 JSON（或 `ndjson`）。
4. 用户只给目标不提供参数时，先给默认参数并明确回显。
5. 普通业务请求先走最小业务路径，不先运行 `bootstrap.sh --check`；仅在业务命令报依赖缺失、命令不存在或用户明确要求体检时，才进入预检/安装流程。
6. 重操作场景只通过 `ensure_model` 处理模型准备，不把 `doctor` 或安装链路塞进主流程。
7. 名称、profile、模型不明确时，先查本地状态一次，再做匹配；不要硬编码别名。
8. 排查 skill 编排问题时，默认使用静态审查和轻量命令；禁止擅自做压测、基准测试或重复重推理，除非用户明确要求。
9. 任一命令失败时，执行失败回写并给重试命令。
