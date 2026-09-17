# R33 报告：结构化推理链 S3′ —— 五家融合成**一份**诊断

**基线** `6f7dbb1`（R32 结束）。

这一轮把 §0.1 用户原话里最核心的一条变成代码：
「五位医家（叶天士、吴鞠通、张锡纯、李可、王云启）进行**综合分析，不要给出多个
答案**」「一个专家诊断，给出解决方案、药方」。此前的形状恰好相反——**一位**医家给
**2–3 个**候选方，三位医家并置成三列由人来比。

| # | 做了什么 | 实测 |
|---|---|---|
| 1 | `S3Structured` 五步链 schema（申报书 2.1 的「病变脏腑-证型-治法-方剂-药物组成」） | 六条校验，**9 种跳步输出被拒**（逐条验过） |
| 2 | `prompts/v1/s3_structured.yaml`（并列第二份，legacy 那份一字未动） | 169 行 / 5479 字符，5 个占位符 |
| 3 | `S3_MODE=structured\|legacy`，默认 **structured** | `results` 从 3 个元素变成 **1 个** |
| 4 | 五位医家参与综合（`in_synthesis` + `physicians_for_synthesis`） | 检索 **5 家各自 top-3 拼接**，refs 5 条 |
| 5 | `calls_per_consult()` 随模式变 | 五位 n=3：**17 → 5 次**（S3 本身 **15 → 3**，降 80%） |
| 6 | 顺带修了三个本轮量出来的缺陷（第六节） | 其中一个是 R31 留下的**测试污染版本控制文件** |

新增测试 **76** 条（R32 的 3209 → 3285），三个文件都超过要求条数：
`test_s3_structured.py` **34**（要求 ≥24）、`test_s3_structured_prompt.py` **17**（≥8）、
`test_s3_mode.py` **25**（≥10）。Playwright 从 20 种加到 **21 种**。

---

## 一、五步链：「不可跳步」怎么变成可执行的判据

判据**在 schema 层，不在 prompt 层**。prompt 只能*请求*模型别跳步；schema 能让跳了
步的输出**根本构造不出来**，于是 `generate()` 的两次重试会把具体的校验错误回灌给
模型（CLAUDE.md 那条重试约定）。

每一步显式声明自己的输入，校验器检查那个输入确实出现在上一步的输出里：

| 步 | 字段 | 声明的输入 | 校验 |
|---|---|---|---|
| 1 病变脏腑 | `organs[]` | 患者症状 | `supporting_symptoms` 非空 |
| 2 证型 | `syndrome` | `from_organs` | ⊆ 第 1 步的脏腑名 |
| 3 治法 | `method` | `from_syndrome` | **逐字** == `syndrome.name` |
| 4 方剂 | `formula` | `from_method` | **逐字** == `method.principle` |
| 5 药物组成 | `herb_choices[]` | `for_element` | ⊆ 脏腑 ∪ `method.targets` |

另加一条**双向**校验：`herb_choices` 的药名集合 == `formula.candidate.herb_items` 的
药名集合——方里有的药必须说得出理由，写了理由的药必须真的在方里。

**为什么 3/4 是逐字相等而不是"包含"**：允许包含的话模型可以把 `from_syndrome` 写成
「上述证型」，校验照样通过——而那正是跳步，这一步并没有真的接住上一步的结论，
只是提了一句。有一条测试拿「上述证型」「脾胃气虚」「脾胃气虚证（见上）」三种写法
逐个验它会被拒。

九种被拒的输出逐条验过（`tests/test_s3_structured.py`）：from_organs 不在 organs 里、
from_syndrome 写指代、from_method 不一致、方里有药没理由、给没开的药写理由、
for_element 凭空、influence 引未检索的医案、influences 为空、cited_case_ids 为空。

**双向不匹配时两个方向一起报**，不是先报一个：这条错误会被回灌给模型重试，
而重试只有两次，只报一半的话它改完一半再撞另一半，白花一次。

---

## 二、「只出一张方」与「五家综合」的防幻觉约束

`FormulaStep.candidate` 是**单个** `FormulaCandidate` 而不是 list
（`S3Syndrome.formula_candidates` 那个 `min_length=1, max_length=3` 的形状在这套
schema 里不存在）。挑哪张方由模型在第 3→4 步之间做完，并在 `rationale` 里说明。

`PhysicianInfluence.cited_case_ids` 是 `Field(min_length=1)`。理由写在 schema 里：

> 声称「综合了某位医家的经验」却指不出他哪一条医案，等于替他背书他没说过的话
> ——这是整份输出里最容易出现、也最难被看出来的一类编造，因为它读起来最像学术表述。

配套的 prompt 里有一句**明确许可**：「宁可只写两家真的影响了你的，不要凑五家」
——硬约束只能保证"引了 id"，凑数这件事要靠许可少写来减少。

**两道幻觉检查，管的是不同的事**：schema 的 `_influences_cite_retrieved_cases` 保证
influence 引的 id 在 `cited_case_ids` 里；`run_synthesis` 里的
`hallucinated` 再查 `cited_case_ids` 本身是不是真的检索到过。后者少一道，
模型可以把五条 influence 全指向一个编造的 id 而校验通过。

检索为空时用 `S3StructuredUnreferenced`——**没有 `cited_case_ids`，也没有
`physician_influences`**（后者一并去掉的理由跟前者同一条：一条"医家影响"必须指得出
医案，而这个场景下一条医案都没有）。这是 CLAUDE.md「新建一个不含该字段的 schema，
不是放松原来的约束」的第二处落地，形状照抄
`_S3Base` / `S3Syndrome` / `S3SyndromeUnreferenced`——项目里这个模式已经有一处，
用同一个形状而不是另发明一种。**五步链的校验一条没少：没有医案可引，不等于可以跳步。**

`Field(min_length=1)` 从 **44 → 67**（+23，全部纯新增，一处既有字段未动）。

---

## 三、下游一个调用方都不用改

`S3Structured.to_s3_syndrome()` 是**唯一**的转换点。api / 前端 / 分歧度 / 安全层 /
打分 / 病名校验 / 方剂建议读的都是 `S3Syndrome`，让每个调用方各自从结构化对象里取
字段等于把这一跳抄七遍（第 31 条）。

- `formula_candidates` 恰好一个元素，`selected` 恒 0；
- 引用为空时返回 `S3SyndromeUnreferenced` 而不是塞一个假 id——那会把「这次没有任何
  医案支撑」这个信号洗掉，而它正是前端要明示的东西；
- `reasoning` 里追加五步链条与五家影响的摘要：旧界面的证据链侧栏读的就是
  `reasoning`，不追加的话「融合了五家」这件事在 R37 之前完全看不见
  （新字段存在但没有界面读它，等于没做）。

结果字典的键**跟 legacy 逐一相同**，另加五个：`s3_structured`（五步链原件，
R34 验证器与 R37 单链界面读它）、`physician_influences`、`physicians_cited`、
`herbs_grounded_ratio`、`n_ontology_refs`。有一条测试把两种模式的键集合做差，
断言 `legacy - structured == ∅`。

`chain.py` 里另抽了两个共享函数，因为两条路径都要它们：
`_ref_row`（一条参考医案对外长什么样——前端证据链侧栏读的就是这些键）和
`_run_react_round`（G2 取证一轮）。后者抽出来尤其必要：中间那段「追问的回答必须
先过 `check_safety`」是 CLAUDE.md 点名的硬约束，抄两份意味着将来改一边会漏另一边，
而那正是「安全否决的后门」这条约束最怕的事。

---

## 四、五位医家：为什么**没有**把 `enabled` 翻成 True

任务书写的是「五位医家 `enabled=True`」。照着做之前先量了一遍代价：

| 量的是什么 | 值 |
|---|---|
| 五位里 `school` 为 None 的 | **2**（李可、王云启——两份语料前言里查不到，按项目惯例不编） |
| 五位两两配对总数 | **10** 对 |
| 其中学派判定会变成 `unknown` 的 | **7 对（70%）** |

而 λ2（学派层权重）与「跨学派分歧大于师承内」这条对照正是 §0.6 点名要**保留**的东西。

根因不是"该不该让五家参与"，是 **`enabled` 被要求同时回答两个问题**：谁算三列集注的
一员（legacy 用）、谁参与这一次综合分析（structured 用）。结构化模式取消了三列，
所以这两个问题**本来就不是同一个问题**——一个字段答两个问题正是第 31 条要防的形状
（此前三次撞的都是"匹配逻辑两处实现"，这次是"一个字段两种语义"）。

做法：新开 `in_synthesis`（默认 True，五位全有）+ `physicians_for_synthesis()`，
`enabled` 一个字没动；再加 `physicians_for_mode(mode)` 作为**唯一的分派点**。
**功能上完全满足要求**：structured 模式下五位医家的医案全部参与检索、全部可以在
`physician_influences` 里出现；legacy 的三列集注与 λ2 对照原样保留。

有一条测试把那 10 对 / 7 对当场算一遍，别让报告里的数字变成一句没人核过的话。

---

## 五、成本：这一轮最大的一笔省

`calls_per_consult()` 的公式随模式变：

| 模式 | 公式 | n=1 | n=3 |
|---|---|---|---|
| legacy（三位） | `2 + 3 × N` | 5 | 11 |
| legacy（若五位） | `2 + 5 × N` | 7 | 17 |
| **structured（五位）** | `2 + N` | **3** | **5** |

**S3 这一步本身：五位 × N=3 = 15 次 → 1 × 3 = 3 次（降 80%）。**
医家数不进 structured 的公式——五家在**同一次**调用里融合。
`best_of_n` 仍然乘上来：采样是"同一份产出采几次挑最好的"，跟"几位医家"是两件不同的事。

对照要说清楚：**这个省是调用数的省，不是墙钟的省**。一次 structured 的 S3 输入更长
（五家医案 + 知识块）、输出更结构化，单次耗时会比 legacy 的单次长。墙钟归 R36。

---

## 六、顺带量出并修掉的三个缺陷

### 6.1 一条测试原地改版本控制里的文件（R31 留下的）

`test_the_verifier_catches_a_stale_committed_file` 为了验"指纹对不上要报错"，把真的
`data/standard/syndromes_manifest.json` 的 `syndromes_sha256` 改成 64 个 0，
在 `finally` 里恢复。这一轮有一次全量跑被超时杀掉，`finally` 没来得及执行。

**代价不是"要 git checkout 一下"，是诊断被带向错误方向。** 下一次跑报的是：

```
✗ 落盘 jsonl：记录 000000000000 / 现算 ccc7a2b8111f
AssertionError: 落盘的 syndromes.jsonl 跟 manifest 记的指纹对不上。
                要么它被手改过，要么重新生成之后 manifest 没跟着写。
```

两句话都是对的，都指向"生成物"这条线索——而真正的原因是**测试污染**，跟生成物、
跟这一轮的改动都毫无关系。实际花了十几分钟才看出来（一开始怀疑的是自己的 R33 改动）。

修法不是"加更多 try/finally"（打断就是打断，finally 不保证跑），而是让脚本能对着
副本工作：`scripts/verify_generated_data.py` 加 `--manifest` / `--jsonl`，测试指向
`tmp_path` 里的副本，真文件一个字节不碰。另加一条源码级判据：这个文件里不许出现
`MANIFEST_PATH.write_text` 这类调用。**判据是源码里没有这种调用，不是"跑一遍看文件
变没变"**——后者只在正好被打断的那一次才看得出来，而那正是这个缺陷难查的原因。

### 6.2 改默认值之后那 115 条红测试

默认从"只有 legacy 一条路"改成 `structured` 之后，全量测试立刻 **115 failed**。
三种改法只有一种对：

| 改法 | 后果 |
|---|---|
| 把默认改回 legacy | 新模式永远跑不到，等于没做（R32 那个坑原样重演） |
| 逐条改那 115 条去断言新形状 | 它们本来要测的是 legacy 那一支，改完就不测了 |
| **钉住模式，让它们继续测 legacy** | 对——它们的答案本来就是 legacy |

`tests/conftest.py` 把 `S3_MODE` 钉成 `legacy`（跟 `_pin_two_physicians` 钉住两位医家
是同一个手法）。**钉住之后必须补上那个被钉掉的判据**：
`test_the_product_default_is_structured` 显式 `delenv` 之后断言默认是 structured，
这个钉子改不掉它；R33 三个新文件全部显式 `S3_MODE=structured` 走真路径；
其中一条断言**发给 LLM 的 system 出自 s3_structured.yaml**（R32 那条教训的判据形式）。

### 6.3 bench 的 `check_exit_code` 自指，一轮里跑多次不收敛

`bench_sandbox --all` 的顺序是：跑 pytest → 跑 Playwright → 跑 `--check` →
**最后**才写 `sandbox.json`。所以它的 pytest 与 `--check` 比对的是**上一次**的
`sandbox.json`。正确用法是**一轮只跑一次 bench，跑完再对齐文档**，下一轮自然收敛。

这一轮我跑了四次，于是踩进一个自指循环：文档钉 `check_exit_code=0`，而
`sandbox.json` 记的是 1（那一次跑时文档还没对齐），于是 `--check` 恒红、
恒记 1。中途试过把凭据改指 `bench/rounds/R33.json`（"不可变快照"），
**那样更糟**：`--round R33` 每跑一次就重写这个快照，它在轮内并不是不可变的。

收敛办法（已做到）：先把文档对齐到当前 `sandbox.json`，再跑最后一次 bench——
这一次它的 pytest 与 `--check` 看到的是一致的文档，于是双双干净，
写出 `pytest_failed=0` / `check_exit_code=0` 的快照；然后把文档对齐到这一份，**停手**。
最终态：文档、`sandbox.json`、独立跑的 pytest 与 `--check` 四者一致。

**留给下一轮的判据**：一轮只跑一次 `bench_sandbox --all`，跑完对齐文档，不要回头再跑。

---

## 七、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **3285** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R33` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **67**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑 |
| 凭据核对 | **17** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **21** 种全过 | `python -m scripts.screenshot_states` |

- 测试 passed **3285** —— `bench/rounds/R33.json:round.R33.pytest_passed=3285`
- 测试 skipped **10** —— `bench/rounds/R33.json:round.R33.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R33.json:round.R33.pytest_failed=0`
- 全量测试墙钟 **117.8** 秒 —— `bench/rounds/R33.json:round.R33.pytest_wall_s=117.8`
- Playwright **21** 种全过 —— `bench/rounds/R33.json:round.R33.playwright_states_passed=21`
- 凭据核对 **17** 份文档 —— `bench/rounds/R33.json:round.R33.n_checked_docs=17`

**新增测试 76 条**（R32 的 3209 → 3285）。跟前几轮同一条规矩：**条数在它自己的报告
落盘之前量**，写完这份之后是 3286，所以下一轮的基线是 **3286**。

### 每个数的对照

| 数 | 对照 |
|---|---|
| `results` 1 个元素 | 对照组 legacy = **3** 个（同一条主诉、同一份代码，只改 `S3_MODE`） |
| S3 调用 3 次 | 对照组 legacy 五位 × N=3 = **15** 次 |
| 一次问诊 5 次调用 | 对照组 legacy 三位 **11** 次 / 若五位 **17** 次 |
| `min_length=1` 67 处 | 基线 44 处（R32），**+23 全部新增，0 处放松** |
| 防跳步校验拒绝 9 种输出 | 对照：同一份 payload 只改一处就构造成功（每条测试都是这个形状） |
| `herbs_grounded_ratio` 0.0 | 对照：`knowledge_entries.available=False`——**本体不在**，不是模型没引 |
| 拒绝的学派配对 7/10 | 对照：翻 `enabled` 之前是 **1/3**（三位里只有两个学派，全部可判） |

---

## 八、也核过了、但这一轮**没动**

1. **药理层的两份 jsonl 仍然不在这台机器上。** 用户说 `0c2a21c` 已经提交，
   但核到本轮结束时 `origin/claude/tcm-demo-multi-round-hjp4q1` 的 HEAD 仍是
   `6f7dbb1`（R32），`git cat-file -t 0c2a21c` 报 `Not a valid object name`，
   GitHub API 列出的分支也只有这一条。所以 `herbs_grounded_ratio` 在沙盒里恒 **0.0**
   ——那个 0 的含义是「本体不在」，靠 manifest 的 `knowledge_entries.available`
   跟「本体在但模型没引」分开。**这两件事的区分已经实现并测到，等数据到位就有真数。**
2. **前端仍然是三列布局的代码在渲染一列。** 新加的第 21 种 Playwright 状态
   （`structured_single`）在真浏览器里验过：一列、`data-physician="synthesis"`、
   铺满容器宽度、列头显示「五家综合」、`divergence` 为 null 时不摆空的处方对照表。
   **但这只是"没塌"，不是 R37 要做的单栏问诊流程**——九段流程、节点释义、
   流式渲染都在 R37。
3. **`full_context` + structured 走的是 `s3_structured.yaml`，没有稳定前缀。**
   `assemble()` 是按**单个医家**的全量医案组装的，五家综合没有"哪一位医家的全量
   医案"这个概念。这条如实记在结果的 `knowledge.prefix_assembled=False` 里
   ——别让人看到 `mode="full"` 就以为缓存命中了。要不要给五家做一个共享前缀
   是 R36 的事（它要真实的缓存命中率数据才能判断值不值）。
4. **ReAct 在 structured 下传 `physician=None`**（不按医家过滤医案）。这是"五家一起
   看"的正确表达，但它让 ReAct 的医案层工具返回的是全库结果而不是某一家的——
   检索面变宽，相关性可能下降。**没有量**：要真实 LLM 跑 ReAct 才看得出来，归 R38。
5. **`physicians_cited` 可能只有一两家。** 这不是缺陷，是要被看见的结果：
   manifest 的 `synthesis` 同时记 `physicians_available`（5）和
   `n_physicians_cited`，只报前者就是拿接入数冒充生效数。真机上这个比值是 R38 的
   一个指标。

---

## 九、自查

1. ✅ `S3Structured` 五步链，**六条校验**，9 种跳步输出逐条验过被拒
2. ✅ 逐字相等而不是包含（「上述证型」这类指代被拒，三种写法各验一条）
3. ✅ 双向不匹配**一次报全**，不浪费重试
4. ✅ 只出一张方（`candidate` 是单个，不是 list——有一条 type hint 级断言）
5. ✅ `PhysicianInfluence.cited_case_ids` 必填 + prompt 里明确许可「可以少写」
6. ✅ 检索为空时新建不含引用字段的 schema，**五步链校验一条没少**
7. ✅ `to_s3_syndrome()` 只此一处，下游键集合 `legacy - structured == ∅`
8. ✅ 五位医家参与综合；**`enabled` 未动**，代价（7/10 对学派 unknown）当场算过
9. ✅ `calls_per_consult` 随模式变，S3 调用 15 → 3
10. ✅ 默认是 structured，且有一条**不受 conftest 钉子影响**的测试断言它
11. ✅ 断言的是**发给 LLM 的 system 字符串**，不是生成它的函数的返回值
12. ✅ Playwright 加第 21 种，真浏览器验过一列布局（CLAUDE.md 那条必需环节）
13. ✅ `s3_syndrome.yaml` 与 `S3Syndrome`/`S3SyndromeUnreferenced` 一个字没动（各有一条测试钉住）
14. ✅ 全量 **3285** passed / 10 skipped / **0** failed；ruff 干净；`--check` 0（17 份文档）；Playwright **21** 种全过
15. ✅ `data/SOURCES.md` 第 **82/83/84** 条；`min_length=1` **44 → 67**，纯新增
16. ✅ 每个数都带对照（见第七节末表）

---

## 十、下一步（R34）

`core/formula_verifier.py` 七条规则 + `MAX_REVISE_ROUNDS=3` 闭环。本轮已经为它铺好三件事：
`S3Structured.ontology_refs`（方级 + 药级去重保序的引用列表，`Violation.counterexample`
要引的本体原文就在那些 `span` 里）、`herbs_grounded_ratio()`（三指标之一，
连"0 有两种原因"这件事都已经在文档字符串里写清并测到）、以及
`results[0]["s3_structured"]` 这个入口（验证器读它，不用再认识 `S3Syndrome`）。

**R34 的 veto 级规则要拿真实本体数据才能验反例**，而药理层的两份 jsonl 到本轮结束时
仍不在这台机器上（第八节第 1 条）。届时若仍缺，R34 的做法跟 R32 一样：
代码与测试用就地构造的合成本体跑通，`available=False` 的降级路径照常可用，
**报告里如实写"veto 规则的真实反例没核"**，不拿合成数据冒充实测。
