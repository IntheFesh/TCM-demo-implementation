# 录制回放 fixture

这个目录放 `LLM_MODE=replay` 要用的录制内容：一份 fixture = 一次
`generate()` 调用的 (schema, system, 模型原始输出) + 元信息。

**现在是空的**（除了这份说明和录制后生成的 `_baseline.json`）——录制需要真实
API key，开发沙盒里没有。录一次：

```bash
python -m scripts.record_fixtures --dry-run   # 先看清单和预估调用数（约 272 次，¥1.5）
python -m scripts.record_fixtures             # 真录
python -m scripts.verify_replay               # 退出码 0 = 回放跟录制逐字节一致
```

之后演示就用：

```bash
LLM_MODE=replay uvicorn api.main:app --port 8000
```

## 这些文件要进版本控制

零成本、断网可用这两条只有在 fixture 跟代码一起走的时候才成立——别人 clone 下来
就能跑，不需要 key。fixture 里**没有** API key（`REPLAY_RELEVANT_ENV` 只收会影响
prompt 的那几个环境变量，`LLM_API_KEY` 一律不记，有一条测试钉住这件事）。

## 文件名 = 索引

`{schema}_{sha12}.json`，`sha12` 是送进模型的那段 system 文本的 sha256 前 12 位。
文件名是给人看的，程序按文件里的 `meta.key` 装载；两者不一致会在未命中消息里
报出来。下划线开头的文件（`_baseline.json`）是录制附带的元文件，不是 fixture。

## 几条容易踩的

- **开 ReAct 和不开 ReAct 的 fixture 不共用**（prompt 模板不同），录制清单
  刻意各录一遍。
- **三种角色（patient/doctor/student）不需要各录一遍**：role 只在
  `api/main.py` 那一层裁剪字段，没有一次 LLM 调用跟它有关。
  `scripts/verify_replay.py` 会真的三种角色各跑一遍来证明这件事。
- **追问路径的回放有固有限制**：访问者的回答文本变了 → 下游 prompt 变了 →
  未命中。对外演示建议 `FAST_MODE=1`（追问 0 轮）；追问那条 fixture 的用途是
  让这条代码路径在回放下也能被验证，不是让任意回答都能命中。
- **未命中不会退回真实 API**，会抛 `LLMError` 并打印 schema / prompt 前 100 字 /
  sha12 / 环境差异。静默退化会让「这是录制的结果」这个声称变成假的。
