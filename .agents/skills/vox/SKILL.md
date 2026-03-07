---
name: vox
description: "Vox 语音工作流编排技能。用于 profile 管理、模型查询与准备、ASR 转写、TTS（clone/custom/design）、pipeline 执行、安装体检与故障排查。用户只描述目标且未给具体命令时使用。"
---

# Vox

把 `vox` 当成一个编排入口，而不是单一 TTS skill。

先切到 skill 根目录（`SKILL.md` 所在目录）再执行脚本，避免相对路径错误。

## 使用方式

1. 先读 `references/intents.md`，判断请求属于哪个场景。
2. 再读 `references/orchestration-matrix.md`，选择该场景的最小 happy path。
3. 只加载当前场景需要的 1 个 playbook；不要把所有 playbook 都读进上下文。
4. 按场景执行：
   - 轻操作：`profile`、`model status/path` 直接执行业务命令。
   - 重操作：`ASR`、`TTS`、`pipeline` 先走 `bash scripts/ensure_model.sh ...`，再执行业务命令。
   - 安装/体检：优先 `bash scripts/health_gate.sh`；只有命令不可用或环境报错时才 `bash scripts/bootstrap.sh --check`。
5. 名称、profile、模型不明确时，先查一次本地状态，不要猜。
6. 失败后按 `env / model / input / runtime` 分类回退，不要把广谱预检塞回主流程。
7. 交付前只按矩阵和 `references/checklist.md` 要求做门禁；普通轻操作不要额外跑 `doctor` 或 `health_gate`。

## 必读文件

- 场景判断：`references/intents.md`
- 统一编排：`references/orchestration-matrix.md`
- 交付门禁：`references/checklist.md`
- 输出模板：`references/response-contract.md`

## 场景文件

- `references/model-playbook.md`：模型查询、路径、缓存、显式模型准备
- `references/profile-playbook.md`：profile list/create/add-sample
- `references/asr-playbook.md`：离线/流式/麦克风转写
- `references/tts-playbook.md`：`clone / custom / design`
- `references/pipeline-playbook.md`：端到端 ASR + TTS
- `references/failure-loop.md`：失败分类、重试、规则回写

## 执行硬规则

1. 默认先业务，后预检。
2. 重操作的模型准备只走 `scripts/ensure_model.sh`。
3. 只有环境问题才进入 `bootstrap.sh --check` / `bootstrap.sh`。
4. 排查 skill 编排问题时，优先静态审查和单次轻量复现。
5. 禁止擅自做压测、重复重推理、并发重任务，除非用户明确要求。
6. 运行示例或回归测试时，输出文件必须写到 `/tmp` 或 `~/.vox/outputs`，不要写到仓库目录。
7. 失败时先给可执行重试命令，再写失败日志。

## 常用脚本

```bash
bash scripts/ensure_model.sh <model-id|asr-auto|tts-default|tts-custom-default|tts-design-default>
bash scripts/health_gate.sh [--require-file <...>]
bash scripts/bootstrap.sh --check
bash scripts/bootstrap.sh
bash scripts/log_failure.sh --stage "<stage>" --command "<cmd>" --error "<msg>" [--retry "<retry-cmd>"]
bash scripts/failure_digest.sh
```
