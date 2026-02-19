# Profile Playbook

用于 profile 生命周期和样本管理。

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
2. 用户未提供参考文本时，要求补充文本后再加样本。
3. 用户给多个样本时，按顺序逐个入库并回显每条结果。
