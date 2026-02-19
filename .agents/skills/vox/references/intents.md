# Vox Intents

把自然语言请求映射到业务 playbook。默认先执行业务，缺依赖才安装。

## 路由表

1. 安装/体检类请求：
   - 触发词：安装、初始化、环境检查、跑不起来
   - 先读：`references/checklist.md`
   - 再读：`references/response-contract.md`
   - 优先命令：`bash scripts/bootstrap.sh --check`、`bash scripts/health_gate.sh`
2. 模型请求：
   - 触发词：下载模型、看缓存、模型路径、模型是否完整
   - 先读：`references/model-playbook.md`
3. Profile/采样请求：
   - 触发词：新建角色、添加样本、管理 profile
   - 先读：`references/profile-playbook.md`
4. ASR 请求：
   - 触发词：转写、语音转文字、流式输出、麦克风转写
   - 先读：`references/asr-playbook.md`
5. TTS 克隆请求：
   - 触发词：克隆声音、生成语音、用某个 profile 读文本
   - 先读：`references/tts-playbook.md`
6. 一体化请求：
   - 触发词：完整流程、先转写再合成、端到端
   - 先读：`references/pipeline-playbook.md`
7. 报错/排障请求：
   - 触发词：为什么失败、重试、查看任务
   - 先读：`references/failure-loop.md`

## 路由规则

1. 优先选择最窄场景（例如“转写”优先走 ASR playbook，不走 pipeline）。
2. 用户没明确输出格式时，默认选 JSON（或 `ndjson`）。
3. 用户只给目标不提供参数时，先给默认参数并明确回显。
4. 任一命令失败时，执行失败回写并给重试命令。
