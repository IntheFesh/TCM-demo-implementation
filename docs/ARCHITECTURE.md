# 架构约定

这份文档说明本项目的代码组织原则，以及几条贯穿全项目的设计约束。
每条约束都附带它要防止的问题——改动相关代码之前，先确认改动不会破坏这些约束。

## 1. 实现可替换

每个组件都通过一个稳定的接口被调用，替换实现时只改一个类，不改调用方：

- LLM 后端都实现 `core/llm.py::LLMBackend`（`generate()` / `model_name()` /
  `backend_id()`），由 `LLM_MODE` 选择：云端 OpenAI 兼容 API、本地 vLLM server、
  进程内 vLLM、录制回放。调用方只调 `get_llm()`。
- 检索器由 `core/retrieval.py::get_retriever()` 提供，五种模式
  （`dense` / `bm25` / `graph` / `hybrid` / `full_context`）对调用方是同一个接口。
- 知识图谱的读写由 `core/graph/store.py::NetworkXStore` 封装，在线路径
  （工具层、检索层、API）只通过它访问图谱。

依赖保持最少：不引入数据库、依赖注入容器或日志框架。需要持久化的运行时数据
（审计日志、问诊记录）用追加写的 JSONL 文件；唯一的磁盘缓存是语料向量缓存
（`data/cache/`，文件名带输入的 sha256，删除后可完全重建）。

## 2. LLM 调用

**prompt 模板一律用 `string.Template`（`$var` 占位），不用 `str.format()`。**
prompt 里有 JSON 示例，示例的花括号会让 `.format()` 抛异常。模板在
`prompts/v1/*.yaml`，由 `core/llm.py::load_prompt()` / `render()` 读取与填充。

**所有 LLM 输出都由 pydantic 模型承接，不裸用 dict。** 模型定义集中在
`core/schemas.py`。`LLMBackend.generate()` 统一负责：

- 解析前剥离 markdown 围栏（` ```json ... ``` `）；
- 校验失败时重试：一次调用最多 3 次尝试（首次 + 2 次重试），重试时把上一次的
  校验错误回灌给模型；
- 识别被 `max_tokens` 截断的输出，直接报错而不是当作格式错误重试；
- 两层超时：HTTP 各相位的读超时，加一层整次调用的墙钟兜底
  （`core/llm.py::CallTimeouts`）。

**防幻觉约束不放松。** `core/schemas.py` 里所有 `Field(min_length=1)`（例如结论
必须引用的医案 id、药名、出处原文）都不能改成可选或 `min_length=0`。新场景确实
不需要某个字段时，新建一个不含该字段的 schema，而不是放松原有约束。用更强的类型
替换（`Literal`、枚举、更具体的子类型）属于收紧，是允许的——判断标准是替换后
能接受的值集合变大还是变小。统计当前约束数的口径：

```bash
grep -c "= Field(min_length=1" core/schemas.py
```

**加载模型或大文件的对象一律惰性初始化**，不在模块顶层实例化：embedding 模型、
语料向量、图谱、本草/方剂本体都在第一次使用时加载。这让 `import` 保持轻量，
也让不需要这些资源的测试和脚本不必准备它们。

## 3. 推理链的顺序约束

推理链在 `core/chain.py::consult()`：S1 症状标准化 → 安全检查 → S2 证素推断 →
（可选）追问 → S3 按医家推导证型、治法、方药 → 输出侧安全检查与方剂验证。

**S1 全局只跑一次，所有医家共用结果。** 如果对每位医家各跑一次 S1，两次输出的
症状列表会不同，构图时症状节点 id 对不上，边会指向不存在的节点。

**安全否决在 S2 之前。** 危重症状（`core/safety.py::check_safety`）在证素推断之前
检出。`consult()` 默认在命中时中止整条链路，被拦截的请求不产出任何方药——这是结构上
的保证，不是在结果备注里提一句转诊。HTTP 接口按角色分流
（`core/safety.py::role_sees_full_reasoning_on_red_flag`）：患者角色整页拦截、只给就医
指引；医师 / 学生 / 研究者角色有专业判断力，拿到完整推理，同时附带醒目的危重提示
（顶部红色警示条、方剂区水印、导出前二次确认、病历中的「危重提示」段）。两种情形复用
同一个开关（`safety_bypassed()` / `consult(eval_mode=...)`），检测本身始终执行、命中
原因始终记录在 `safety_flag` 里。

**追问的回答同样先过 `check_safety`。** 追问是安全否决的另一个入口：如果追问问出
了危重症状（「有没有黑便」→「有」），回答直接进证素推断会绕过前面的拦截。
所以回答命中即按拦截处理。`core/tools.py::question_candidates` 给每个候选问题带
`safety_relevant` 标记（判据复用 `core/safety.py` 的表），提问方据此决定回答要不要
先送安全层。

**输出侧检查在模型之外。** 处方的配伍禁忌、剂量上限、煎法等由
`core/safety_output.py::assess_formula_safety()` 按规则表判定，不接受模型或客户端
自己声称"安全"。

## 4. 同一概念只有一处实现

新增工具或判断之前，先查有没有现成的匹配器或判定函数，有就复用。同一个判断在
两处各实现一遍，单独测试时两边都正常，放进同一条推理链里就会给出相反的答案。
当前的统一实现：

- 症状、证候门类的等价写法（「水肿」与「肿胀」）统一走 `core/syndrome_norm.py`
  的 `SYNONYMS` / `canonical()`；
- 两位医家的结论分歧用药物集合的 Jaccard 距离衡量，不用证型名的字符串相等；
- 图谱上的症状匹配统一走 `core/tools.py::_match_graph_symptoms`，
  `query_graph` 与 `check_residual` 共用它；
- 医家集合的筛选只走 `core/physicians.py` 的 `physicians_enabled()` /
  `physicians_all()` / `physicians_for_mode()`。

判断标准是「这个判断此前有没有人做过」，而不是「我这个实现有没有 bug」。

例外只有一种：两处回答的**不是同一个问题**。例如 `core/safety.py` 的危重关键词表
不并入 `SYNONYMS`——后者回答「这个词属于哪个证候门类」，前者回答「要不要在辨证
开始前拦截整个请求」；`core/safety_output.py`（能不能发出这张方）与
`core/formula_check.py`（这张方拟得好不好）也是两个问题。走这条例外时，代码里要
写清楚两个问题的区别。

## 5. 标识符在边界统一解析

数据文件里存 id（医家 `ye_tianshi`、证候 `SP-01`），界面上显示中文名。任何接收
外部输入的边界（模型输出、HTTP 请求、命令行参数）都通过统一的解析函数把输入转成
规范 id，例如 `core/physicians.py::resolve_physician_id()`，不在过滤处直接比较字符串。
给模型看的 prompt 里同时给出 id 和中文名，并说明参数该填哪一个。

工具返回空结果时必须区分三种情况：

| 情况 | 返回 |
|---|---|
| 参数错了 | `error`，并列出可用值，让模型自我纠正 |
| 数据文件不存在 | `available: false` |
| 数据在、确实没有匹配 | 正常结果，`note` 里写明「已查 N 条」 |

## 6. 数据约定

- 医案不是单诊快照：`CaseRecord` 带 `case_group_id` / `visit_index` /
  `prev_case_id`，同一病人的多次复诊构成一个序列。检索、训练数据导出、图谱构建
  都按序列处理（例如训练集/留出集按 `case_group_id` 划分）。
- 医家 id 一律小写下划线：`ye_tianshi` / `wu_jutong` / `zhang_xichun`。
- 人工整理的静态参考表（本草、方剂、证候、疾病参考表等）放 `data/standard/`。
  `.gitignore` 整体忽略 `*.jsonl`，只对 `data/standard/*.jsonl` 开了例外，
  放在别处的 `.jsonl` 不会进入版本控制。
- 医家注册表 `core/physicians.py` 是医家元数据（名称、出处、学派、配色）的唯一来源，
  前端配色也由 `/health` 下发，不在 CSS 里另写一份。

## 7. 数字必须带对照

「分歧度」「准确率」「幻觉率」这类数字不单独出现在文档、前端或报告里，旁边必须有
它的对照基准：对照组的数，或噪声地板 ε（同一输入重复采样的天然分歧）。
没有基准的数字无法判断好坏。评测数字只维护在 `eval/RESULTS.md` 一处，并由
`python -m scripts.collect_results --check` 与报告文件逐个核对。

## 8. 测试与评测分离

- `tests/`：不需要网络、不需要 API key。LLM 调用由各测试模块自带的假后端承接；
  embedding 模型由 `tests/conftest.py` 统一换成确定性的假编码器，需要真实
  sentence-transformers 模型的少数用例标记为 `real_embedding`，可用
  `pytest -m "not real_embedding"` 排除。`tests/conftest.py` 还把运行时数据（问诊历史、
  病历文书、偏好、审计日志）指到每条用例自己的临时目录，测试不写仓库的 `data/`。
- `eval/`：需要真实 LLM 调用的评测（噪声地板、消融、外部基准、患者模拟），单独运行，
  不进 pytest。
- 前端的结构性变更（图谱的层编号与层含义、compound 父子节点、推导链分段）需要在
  真实浏览器里渲染验证：`python -m scripts.screenshot_states` 用 Playwright 把一组
  构造好的响应喂给真实页面并截图、检查 DOM。后端 JSON 结构测试不会调用前端渲染代码，
  覆盖不到这类问题。

## 9. 代码风格

- Python 3.10+，可选类型写 `X | None`，不用 `Optional[X]`。
- 注释用中文，只写「为什么」，不复述代码在做什么。
- lint 基线见 `ruff.toml`（只启用语法级规则 E4/E7/E9 与 F），CI 在
  `.github/workflows/ci.yml` 中运行 `ruff check .` 与 `pytest`。
