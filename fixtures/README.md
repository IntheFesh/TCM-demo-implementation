# 录制回放 fixture

这个目录存放 `LLM_MODE=replay` 使用的录制内容：一份 fixture 对应一次 `generate()` 调用的
(schema, system, 模型原始输出) 及元信息。录制需要真实 API key：

```bash
python -m scripts.record_fixtures --dry-run   # 查看录制清单与预估调用数
python -m scripts.record_fixtures             # 录制
python -m scripts.verify_replay               # 退出码 0 = 回放与录制一致
```

之后即可离线回放：

```bash
LLM_MODE=replay uvicorn api.main:app --port 8000
```

## 与代码一起提交

录制好的 fixture 可以与代码一起提交，这样任何人 clone 之后不需要 key 就能离线运行。
fixture 中不包含 API key：`REPLAY_RELEVANT_ENV` 只记录会影响 prompt 的环境变量，
`LLM_API_KEY` 不在其中，并有测试保证这一点。

## 文件名即索引

文件名为 `{schema}_{sha12}.json`，`sha12` 是发给模型的 system 文本的 sha256 前 12 位。
文件名方便人查找，程序按文件内的 `meta.key` 装载；两者不一致时会在未命中信息里报出。
下划线开头的文件（如 `_baseline.json`）是录制附带的元文件，不是 fixture。

## 使用须知

- **开启与关闭 ReAct 的 fixture 不共用**（prompt 模板不同），录制清单会各录一遍。
- **不同角色不需要分别录制**：角色只影响 `api/main.py` 对响应字段的裁剪，与 LLM 调用无关；
  `scripts/verify_replay.py` 会用三种角色分别回放来验证这一点。
- **追问路径的回放有固有限制**：回答文本变化会改变后续 prompt，导致未命中。对外展示建议
  `FAST_MODE=1`（不追问）；追问的 fixture 用于在回放下验证这条代码路径。
- **未命中不会退回真实 API**，而是抛出 `LLMError`，并给出 schema、prompt 前 100 字、sha12 与
  环境差异，避免把实时调用的结果误当成录制结果。
