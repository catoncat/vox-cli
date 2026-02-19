# Vox Preflight Checklist

在任何交付前执行以下检查，全部通过才可交付。

## 硬标准检查

1. 平台检查：
   - `uname -s` 必须是 `Darwin`
   - `uname -m` 必须是 `arm64`
2. 运行健康：
   - `scripts/vox_cmd.sh doctor --json` 返回 `ok=true`
3. 模型状态：
   - 重操作（ASR/TTS/Pipeline）前，先执行 `bash scripts/ensure_model.sh <...>`，并以此作为唯一模型校验路径
4. 命令结果：
   - 主命令退出码 `0`
5. 输出可用：
   - 需要输出文件时，文件路径存在

## 无副作用优先

1. 先跑：

```bash
bash scripts/bootstrap.sh --check
```

2. 若缺依赖，再跑：

```bash
bash scripts/bootstrap.sh
```

3. 交付前跑健康门禁（带缓存）：

```bash
bash scripts/health_gate.sh [--require-file <...>]
```

## 交付模板

1. 执行命令：
   - 列出完整命令（可复现）
2. 关键结果：
   - 模型 ID、任务 ID、输出路径
3. 失败重试：
   - 若失败，先给重试命令，再写入失败样本日志

## 禁止项

1. 禁止在未检查平台时直接下载模型。
2. 禁止在模型未 verify 时直接执行 TTS（clone/custom/design）或 pipeline。
3. 禁止只返回“失败了”，必须带可执行重试命令。
