# R30 报告：R29 收尾的全仓库复查（先量再改）

**基线** `01ef077`（R29 结束）。这一份不是新一轮功能，是 R29 任务末尾那句
「对整个代码仓库进行详细的检查完善，所有功能不能简写简化，必须完全实现，
尽可能地去优化代码，以及运行和推理速度」的落地。

**做法：先量再改。** 逐个端点、逐个核心函数打表，改完逐位比对输出。
没量出问题的地方一个字没动——"看着可以优化"不是改代码的理由。

---

## 一、量出来的三件事

| # | 位置 | 实测 | 根因 | 改法 | 改后 |
|---|---|---|---|---|---|
| 1 | `/api/graph?limit=200` | **29.6 ms** | `ambiguous_syndrome_keys(store)`（扫全图 1312 个节点）写在推导式里 → 每个节点扫一遍全图，200 × 1312 = 26 万次 | 提到推导式外，三处调用点都提 | **13.9 ms（2.1×）** |
| 2 | `core.tools.question_candidates` | **119.2 ms** | (a) 两张索引各建两遍；(b) 内层循环为 1115 个候选各分配 4 条 177 元素的列表 | (a) 建一次传进 `syndrome_posterior`；(b) 两条后验分布、两个熵、两个 argmax 在同一遍里算完 | **60.9 ms（1.96×）** |
| 3 | `core.react.GRAPH_MISS_HINT` | — | 「国标的 **1282** 个症状节点」写死在**进 prompt 的文本**里，实际已是 1115 | `graph_miss_hint()` 现数；图谱取不到时只给不带数字的那句 | 不再会变成假话 |

第 1、2 两项**不许改任何输出**。第 2 项那个内层循环的浮点运算次序是逐位保留的：
`-sum(v·log2 v)` 展开成逐项 `-= v·log2(v)`，IEEE 下取负是精确运算，所以结果逐位相同
——这不是"误差可忽略"，是**一位都不差**。验证方式两道：

- 一次性对照：把 `01ef077` 的 `core/tools.py` 原样放回 `core/` 下当第二个模块导入，
  13 组输入（含 3 组 `k=2000` 的**全量 1115 条候选排名**）逐条比，`0` 处不一致；
- 长期判据：`tests/test_r29_review_fixes.py::test_the_fused_ig_loop_matches_the_textbook_formula_bit_for_bit`
  把教科书写法在测试里重写一遍，对 4 组证素逐条比 `ig` / `p_yes` / 两个分叉结论。
  **一次性对照不会在下一次改动时自动重跑，判据会。**

`question_candidates` 剩下的 60.9 ms 拆开是：打分循环 77→约 30 ms、
`is_safety_relevant × 1115` 15.6 ms、`_symptom_index` 6.6 ms、其余约 10 ms。
**没有继续优化**：再快就要动浮点算法（用"未列出的证候一律取同一个值"把内层
循环从 O(177) 降到 O(该症状实际指向的证候数)，前缀和展开），那会让 `ig` 在
`MIN_INFORMATION_GAIN` 门槛上的边界候选跳变。一次追问 60 ms，而它旁边是一次
几秒的 LLM 调用——**这个取舍不值得**。记在第五节。

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **3071** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R30` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **44**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑 |
| 凭据核对 | **14** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **20** 种全过 | `python -m scripts.screenshot_states` |

- 测试 passed **3071** —— `bench/rounds/R30.json:round.R30.pytest_passed=3071`
- 测试 skipped **10** —— `bench/rounds/R30.json:round.R30.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R30.json:round.R30.pytest_failed=0`
- 全量测试墙钟 **95.9** 秒 —— `bench/rounds/R30.json:round.R30.pytest_wall_s=95.9`
- Playwright **20** 种全过 —— `bench/rounds/R30.json:round.R30.playwright_states_passed=20`
- 凭据核对 **14** 份文档 —— `bench/rounds/R30.json:round.R30.n_checked_docs=14`

**新增测试 21 条**（R29 的 3050 → 3071）：`test_r29_review_fixes.py` **18** 条、
`test_distill_from_v4.py` 补 **3** 条。跟前几轮同一条规矩：
**条数在它自己的报告落盘之前量**，写完这份之后是 **3072**（本文件给参数化的
`--check` 用例又添了一份），所以下一轮的基线是 3072。
`core/schemas.py` 一个字没动。

---

## 三、扫过但**没有**改的地方（列出来才算扫过）

| 扫的是什么 | 结果 |
|---|---|
| `NotImplementedError` / `TODO` / `FIXME` / `XXX` / `HACK` | 全仓库 **12 处**，全部是抽象基类方法或 `Neo4jStore` 那个**有意的第二实现占位**（架构原则：接口写好、主实现落地、第二实现留占位）。没有一处是"写了一半忘了" |
| 「简化 / 简写 / 暂不 / 占位 / 未实现」中文标记 | 逐条读完，没有一处是被简化的功能；`core/safety_output.py:534` 那条明确写的是"硬凑一个带假 rationale 的占位比收窄参数类型更违反禁止占位实现" |
| 可变默认参数（`def f(x=[])`） | **0 处** |
| `except Exception: pass` | 3 处，全部在有注释解释的位置（进度条清理、LLM 重试计数） |
| 生产代码里的 `assert` | 1 处，已改（见第四节） |
| 零引用的模块级函数 | 10 个，9 个是 FastAPI 路由处理器（靠装饰器注册，不靠名字引用）；**1 个是真死代码**，已接上（见第四节） |
| 前端 `innerHTML +=` / 循环里的 `querySelector` | **0 处** |
| 数据加载函数是否惰性 + 单次 | `get_graph_store` / `_load_standard` / `_load_case_triples` 都是双重检查锁的惰性单例；`load_diseases` 有 `lru_cache(maxsize=1)`。没有一处在模块顶层读文件 |
| `import api.main` 0.379s 花在哪 | `python -X importtime`：fastapi 154ms + pydantic 建模 26ms + `core.schemas` 26ms。**没有一处是本项目的数据或模型在 import 时被加载**——这 0.38 秒是框架成本，改不动也不该改 |

---

## 四、另外三处改动

1. **`core/chain.py` 的 `assert last_exc is not None` → 显式分支。**
   `python -O` 会把 assert 整行删掉，那时 `raise last_exc` 变成 `raise None`,
   抛的是「exceptions must derive from BaseException」，把真正的根因
   （429 / 超时 / 校验失败）盖掉。现在 `last_exc is None` 时抛一条写明
   "这个状态不该出现，请连同 `s3_best_of_n()` 的取值一起报告"的 `LLMError`。
2. **`offline/distill_from_v4.estimate_as_dict` 是死代码，而它的文档字符串
   声明了一个不存在的消费方**（"给 `--json` 之外的调用方读的形状"——当时既没有
   `--json` 也没有任何调用方）。**这正是"功能只做了一半"的形状**，所以接上而不是删：
   加 `--estimate --json`，把估算按机器可读形状打**最后一行**，
   报告里的 ¥ 数字从这里取而不是手抄。`main()` 里不许再出现第二处 `asdict(est)`，
   有判据盯着。
3. **四处过期数字**：`core/tools.py` 两处「93 个标准症状」、`api/main.py`
   与 `web/graph.js` 里的「178 个证候 66 个重名」（现在是 177/67）、
   `api/main.py` 里 `ambiguous_syndrome_keys` 的文档字符串仍在复述 R28 那个
   **说错了机制**的归因（"名字列跟定义列错位"），一并按 R29 查清的根因改写。

---

## 五、发现但未动

1. **`question_candidates` 还能再快一个量级，但不值得。**
   1115 个症状里绝大多数只指向一两个证候，所以内层那 177 次里 99% 取的是同一个
   常量 `P_UNLISTED`。用前缀和（`Σ pr`、`Σ pr·log2 pr`）把 O(177) 降成
   O(该症状实际指向的证候数)，数学上等价，**浮点上不等价**——`ig` 会在
   `MIN_INFORMATION_GAIN` 门槛上跳变，`round(ig, 4)` 偶尔变，近似并列的排序会换位。
   一次追问 60 ms，旁边是一次几秒的 LLM 调用。**换算法要单独一轮**，
   带"改前改后 1115 条候选排名差几条"这个数。
2. **`check_residual` 14.7 ms，`_match_graph_symptoms` 对每个患者症状扫 1115 个
   症状节点，而 `_symptom_fragments(name)` 对同一个 `name` 被反复重算。**
   memo 能省 26%，但那是加缓存（CLAUDE.md：demo 阶段不加）；
   结构改法是把匹配器改成"一次匹配多个患者症状"，那会动它的签名，
   而它是**全模块唯一的症状文本匹配器**（第 31 条），签名一动四个调用方都要跟。
   一次问诊它跑不到十次，先不动。
3. **`data/element_index.json` 在这台机器上不存在**，`build_graph --all` 的第三步
   因此退出码非 0。它要 `cases.json`（真实 LLM 抽取，不在版本控制里）。
   前两步（建图 + `graph_stats` 写回 `weight_by_physician`）都跑完了。
   **不是这一轮弄坏的**，上机时跟段 5 之后一起跑。
4. **`_symptom_index` 里 `len(store.find_nodes("symptom"))` 这类"扫一遍图数个数"
   的调用在 `graph_miss_hint()` 里每次提示都重算一遍。** 提示一次问诊最多出现
   几次，0.3 ms 一次，不值得为它加状态。
5. **`api/main.py` 的 `_symptom_counts_by_syndrome_code(store)` 每次请求扫一遍
   3765 条边。** 已经在循环外了（不是第一节那个问题），一次 1.5 ms。
   要再省就得给图建持久索引，那是 walking skeleton 之后的事。

---

## 六、自查

1. ✅ 先量再改：三处都有改前改后的实测数
2. ✅ 两处性能优化**不改任何输出**：一次性 13 组逐位对照 + 长期判据各一道
3. ✅ 每个数都带对照（29.6→13.9、119.2→60.9、1282→现数）
4. ✅ 扫过但没改的地方列了 9 类，不是"看了一遍没问题"
5. ✅ 死代码接上而不是删掉（`--estimate --json`），文档字符串不再声明不存在的消费方
6. ✅ `-O` 下不会 `raise None`
7. ✅ 过期数字四处清掉，prompt 里那个改成现数
8. ✅ 新增 21 条判据；全量 **3071** passed / 10 skipped / **0** failed
9. ✅ ruff 干净；`--check` 退出码 0（14 份文档）；Playwright **20** 种全过
10. ✅ `data/SOURCES.md` 第 74、75 条（循环不变量、prompt 里的写死数字）
11. ✅ `core/schemas.py` 没动，44 处 `min_length=1` 原样
12. ✅ 没有加任何缓存（CLAUDE.md），两处重复计算都用传参解决

---

## 七、下一步

- 放宽编号标题的锚点（R29 报告第七节第 2 条）——那是唯一能把 61 条重名真正解开的改动。
- 给生成物加一道"跟当前代码一致"的判据（R29 报告第七节第 1 条）。
- `question_candidates` 换算法（第五节第 1 条）：要单独一轮，带 1115 条排名的前后对照。
