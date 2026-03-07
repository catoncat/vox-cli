# Profile Playbook

用于 profile 生命周期和样本管理。

## 场景定位

这是轻操作场景，继承 `references/orchestration-matrix.md` 的全局总则：默认直接执行业务命令，不做模型校验，不把预检/安装当作主流程。

## 最小 happy path

1. `list/create/add-sample` 直接执行对应 `profile` 子命令。
2. 用户给了可能记错或不够确定的 profile 名时，先执行一次 `scripts/vox_cmd.sh profile list --json`，再在现有 profile 中确认最接近项。
3. 仅在命令不可用、依赖缺失或环境错误时，才回退到 `bootstrap.sh --check` / `bootstrap.sh`。
4. 不做外部搜索、不硬编码别名、不无故触发 `ensure_model`。

## 创建与查看

```bash
scripts/vox_cmd.sh profile create --name <name> --lang zh --json
scripts/vox_cmd.sh profile list --json
```

## 添加样本

```bash
scripts/vox_cmd.sh profile add-sample \
  --profile <name-or-id> \
  --audio <sample.wav> \
  --text "<reference text>" \
  --json
```

## 样本质量硬约束

1. 时长必须 `2~30` 秒。
2. RMS 必须 `>= 0.005`。
3. 音频需可读；失败时返回明确路径和原因。

## 对话策略

1. 用户未提供 profile 名称时，先建议 `narrator` 并说明可改名。
2. 用户给了可能记错或不够确定的 profile 名时，先执行一次 `scripts/vox_cmd.sh profile list --json`，在现有 profile 里确认最接近项；不要先做外部搜索或硬编码别名。
3. 用户未提供参考文本时，要求补充文本后再加样本。
4. 用户给多个样本时，按顺序逐个入库并回显每条结果。
