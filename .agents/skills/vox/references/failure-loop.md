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

## 失败分类

1. `env`：平台或依赖缺失。
2. `model`：未下载、未校验、下载失败。
3. `input`：路径错误、音频不合法、参数缺失。
4. `runtime`：执行异常、输出缺失。

## 闭环要求

1. 每次失败都要给可执行重试命令。
2. 相同失败重复出现时，把对应预检规则补进 `checklist.md`。
3. 新场景稳定后，把固定流程补进对应 playbook。
