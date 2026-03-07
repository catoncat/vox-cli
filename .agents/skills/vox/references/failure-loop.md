# Failure Loop

把“事后吐槽”变成“前置改进”。

## 记录失败

每次失败都写入本地日志：

```bash
bash scripts/log_failure.sh \
  --stage "<bootstrap|model|profile|asr|tts|pipeline|task>" \
  --command "<failed command>" \
  --error "<error message>" \
  --retry "<retry command>"
```

默认会对 token/key/password 做基础脱敏；如需保留原始文本可加 `--no-sanitize`。

## 聚类复盘

定期聚类失败样本，产出可维护的规则候选：

```bash
bash scripts/failure_digest.sh
```

输出：

- `~/.vox/agent/state/failure_report.md`
- `~/.vox/agent/state/failure_report.json`

## 失败分类与回退

1. `env`：平台或依赖缺失 → 才回退 `bootstrap.sh --check` / `bootstrap.sh`
2. `model`：未下载、未校验、下载失败 → 先走 `ensure_model`
3. `input`：路径错误、音频不合法、参数缺失 → 优先修参数/路径
4. `runtime`：执行异常、输出缺失 → 看日志、状态文件、命令帮助，再决定是否最小复现

## 闭环要求

1. 每次失败都要给可执行重试命令。
2. 环境/安装类重复失败，再补 `checklist.md`。
3. 场景业务类重复失败，优先补对应 `playbook`、`intents.md` 或 `orchestration-matrix.md`。
4. 若失败根因是场景误判、过度预检或编排混乱，优先修编排文件；不要把广谱预检塞回主流程。
