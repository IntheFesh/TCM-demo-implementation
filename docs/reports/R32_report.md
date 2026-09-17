# R32 报告：本体锚定层 + 知识块进**所有**检索模式

**基线** `4896084`（`cases.json` 入版本控制那一提交）。

这一轮建的是 §0.3 第四条实证（LLM-Modulo + 本体锚定，幻觉 63%→1.7%）所依赖的
那一层：把药理层的三元组读成**可查询的结构化本体**，并把按本次问诊裁剪的知识块
真正送进模型看得见的提示词。

| # | 做了什么 | 实测 |
|---|---|---|
| 1 | `core/ontology.py`：Herb/Formula/SourceRef + 九个查询接口，全部复用既有归一/安全表 | 516 行，`python -m core.ontology --stats` 退出码 **2**（数据不在，见第六节） |
| 2 | `data/standard/effect_synonyms.tsv` + `core/effect_synonyms.py` | **52** 行（47 textbook / 5 common），**180** 个功效词、**223** 个词元 |
| 3 | `build_focused_knowledge()` + `KNOWLEDGE_IN_PROMPT` 三档 | 四种 top3 模式全部进；off 档跟改前**逐字节相同** |
| 4 | manifest / bench 报告三字段 | `knowledge_in_prompt` / `knowledge_tokens` / `knowledge_entries` |
| 5 | 修了三个本轮**量出来**的缺陷（第五节） | 剂量查错 5 味、组成剂量静默丢失、冷启动测量静默失效 |

新增测试 **77** 条（R31 的 3130 → 3207），四个文件都超过要求条数：
`test_ontology.py` **27**（要求 ≥18）、`test_effect_synonyms.py` **16**（≥6）、
`test_knowledge_in_prompt.py` **19**（≥14）、`test_herb_naming_single_impl.py` **14**（≥4）。

---

## 一、这一轮真正要修的缺陷：知识块从未进过提示词

§0.4 第二个坑的原文是「`core/chain.py:710` 知识速查表**只在 full_context 下**
进提示词而演示跑 hybrid」。逐行核过，**属实**，而且比描述的还彻底：

`run_physician` 里是一个 if/else：

- `mode_eff == "full_context"` → `assemble(...)`，全量速查表进稳定前缀；
- 其余全部模式 → `render(s3_prompt["system"], refs=refs_text)`，
  `$refs` 里**只有医案块**。

所以 hybrid / dense / graph / bm25 四种模式下，本草 9776 条 + 方剂 3184 条
**一个 token 都没有进过模型**。

**为什么整套测试全绿。** 当时测了三件事，没有一件测到这里：

| 已有的测试 | 断言的是 | 漏掉的 |
|---|---|---|
| `test_context_prefix.py` | `assemble()` 的**返回值**对不对 | 这个配置下会不会调用它 |
| `test_chain.py` | `raw_excerpt` 有没有进 prompt | 知识块有没有进 prompt |
| `test_prefix_cache.py` | 前缀缓存命中率 0.989 | 前缀里有什么只在一种模式下成立 |

这跟 CLAUDE.md 第 31 条那三次是同一形状、但更难发现：那三次是**两处实现打架**
（单独测都对、放一起矛盾），这一次是**一处实现根本没被调用**——连矛盾都没有，
只有沉默。

**本轮的判据因此是"断言发给 LLM 的那个字符串"，不是"断言生成它的函数的返回值"。**
`tests/test_knowledge_in_prompt.py` 里每一条都从 `ReActFakeLLM.s3_systems[0]`
取出真正送出去的 system 文本再断言，并且对四种 top3 模式**逐个参数化**——
漏掉哪个模式就是漏掉一种运行配置。

实测（合成本体：3 味药 / 1 首方 / 1 条规律，hybrid 模式）：

```
off(=改前)      system 字符数=4518  knowledge={'mode':'off','available':False,'tokens':0}
focused(改后)   system 字符数=4787  knowledge={'mode':'focused','available':True,
                                              'n_herbs':2,'n_formulas':1,
                                              'n_patterns':1,'tokens':191}
```

`n_herbs` 是 **2** 而不是 3：柴胡没有被这次问诊的证素/医案/规律选中，
说明知识块是**按本次问诊裁剪**的，不是把本体整个倒出来。

---

## 二、三档 `KNOWLEDGE_IN_PROMPT`

| 档 | 默认用在 | 语义 |
|---|---|---|
| `full` | `full_context` | 全量速查表进稳定前缀（改前就有，缓存命中 0.989，再塞一份裁剪版是纯浪费） |
| `focused` | hybrid / dense / graph / bm25 | 按本次问诊裁剪的知识块 |
| `off` | 只供对照实验 | **逐字节等于 R32 改动之前** |

`off` 那一档有两条测试钉着，因为它是 R38 消融实验的 A 组——多一个换行，
"知识块带来的差异"里就混进了"prompt 变了"这个额外变量：

1. `off` 的 system 里不出现 `## 参考医案` 这个小标题，也不出现任何药名；
2. `off` 的 system 与"本体压根不可用"那一跑的 system **逐字节相同**
   （`assert without == as_if_absent`）。

拼错档名（`focussed`）当场抛 `ValueError` 并列出三个可用值，不静默退化——
静默退化会让一份消融报告里的 A 组其实是 B 组。

**次序定死**：知识块在参考医案**之前**，`_format_case_block` 的输出与它在
`$refs` 里的相对位置一字未动（§0.6 明确不做那一条）。
`test_the_case_block_format_is_untouched` 断言医案块**逐字原样**出现在 system 里。

---

## 三、本体层怎么保证自己不是第二份实现

`core/ontology.py` **不持有任何一张自己的药物知识表**，四处判据全部 import：

| 判断 | 走谁 | 本轮钉的测试 |
|---|---|---|
| 药名归一 | `core.herbs.normalize_herb` | 四组写法 × 各 3–4 种拼法，从本体入口查到的正名必须等于它 |
| 十八反十九畏 | `core.safety_output.INCOMPATIBLE_PAIRS` | 24 对全过一遍，且对称（谁写在前面不改变结论） |
| 药典剂量上限 | `core.safety_output.dose_limit_entry` | **62 味全过，不抽样** |
| 功效对治法 | `core.effect_synonyms` | 223 个词元两两核对称性 |

`test_the_ontology_module_holds_no_herb_alias_table_of_its_own` 直接读 AST：
模块里不许出现第二张「写法 → 正名」的中文字典。判据是"这个判断此前有没有人做过"，
不是"我这个实现有没有 bug"。

### 功效同义词表为什么不并进 `syndrome_norm.SYNONYMS`

CLAUDE.md 第 31 条的例外条款要求把两个问题的区别写清楚：

- `syndrome_norm.SYNONYMS`：**这个词属于哪个证候门类**（腹泻/泄泻 → 泄泻门）
- `effect_synonyms.tsv`：**这个治法对应哪些功效表述**（疏肝理气 → 疏肝解郁/行气/开郁）

合并成一张，以后改一边看不出会不会连带影响另一边。区别写在模块 docstring
和 TSV 表头里，有一条测试专门断言这两段文字在（判据是"文档里写了区别"，
不是"两张表内容不重叠"——中医词汇就那么多，交集必然有）。

**同义展开是一跳，不是传递闭包。** 实测：匹配关系全表对称
（223 个词元两两核，**0** 对不对称），最大邻域 **26**（清热），
远小于 223 —— 没有塌成"什么都匹配什么"的一团。
闭包会顺着共享词把「清热」和「温里」连成一片，那时
R34 的 `effect_matches_method` 会变成**恒真**，跟恒假一样等于没有验证。

---

## 四、裁剪：放了什么、砍了什么都要可核

预算 `FOCUSED_KNOWLEDGE_MAX_TOKENS = 30000`。**这是取舍不是测量**，
理由写在常量旁边：S3 其余部分实测 4–6 千 token，而 top3 模式没有前缀缓存、
每个 token 都按未命中价计费。

裁剪顺序 `("formulary", "materia_detail", "materia_entries")`，
**规律永不裁**——砍掉它等于回到"只有教材、没有这五位医家"，
而"融合五家"正是这一轮要在知识层做到的事（§0.2 申报书「融合多流派」）。
`test_patterns_are_never_trimmed_even_at_a_tiny_budget` 把预算压到 1 token
验证：方剂被砍、详情被砍、本草条目减半，规律和它的 `case_ids` 全须全尾留着。

`trimmed_sections` 必须非空才算通过——**砍了什么要报出来**，
否则超预算时静默少放的那部分在 manifest 里看不出来。

manifest 里三个字段分开记，因为它们回答三个不同的问题：

- `knowledge_in_prompt`：这次**选**的是哪一档
- `knowledge_tokens`：实际放进去多少
- `knowledge_entries.available`：本体在不在

`tokens == 0` 且档位不是 `off`，只可能是本体不在——跟"放了 0 个 token"
是两件事。`_aggregate_knowledge` 取**最大值不是求和**：几位医家的知识块高度
重叠（同一批本草条目），求和会报出一个比实际多好几倍的数，而这个数要被引进报告。

---

## 五、顺带量出来并修掉的三个缺陷

这三条都不是 R32 的题目，是写测试时**被测试逼出来的**。

### 5.1 剂量上限查错 5 味（安全相关）

`Ontology.dose_limit()` 第一版写成"先 `normalize_for_incompat` 再 `normalize_herb`"。
62 味全过一遍，**5 味**查错：

| 写法 | 表里那条 | 归一后落到 | 查出来 |
|---|---|---|---|
| 巴豆霜 | **0.3g** | 巴豆 | 0.0g |
| 黑顺片 / 白附片 / 淡附片 / 熟附片 | 各 **15.0g** | 乌头 | 查不到 |

根因：`normalize_for_incompat` 回答的是"这两味算不算十八反的一对"，
它的**类目比剂量粗**。拿它查剂量 = 拿粗表的键索引细表。

项目里本来就有正确顺序（**先原始写法、再 `normalize_herb`**），
`check_dose_limits` 的 docstring 把理由写得很清楚，`herb_props` 也照着做了
——**但它是被"照着抄"的，不是被调用的**，所以第三处抄错了没人发现。
修法：抽成 `core/safety_output.py::dose_limit_entry()`，三处都调它；
有一条测试断言 `DOSE_LIMITS.get(` 在全仓库只出现在它自己的定义里那两次。

### 5.2 方剂组成的剂量被静默丢掉

`parse_composition("柴胡 12g、白芍9克")` 原来返回 `(('柴胡',''),('白芍','9克'))`
——药名与剂量之间是**空格**的写法（方剂书里最常见），
`_SPLIT_RE` 把空白也当分隔符，剂量被切成独立一段跟药名走散，然后归一成空、丢掉。
R34 的 `dose_exceeds` 规则读的正是这个数，丢了就是规则恒不触发。
修法：认出"整段只有剂量没有药名"的那一段，补回上一味药。
实测四种写法（`柴胡 12g、炙甘草六钱，白芍9克` / `柴胡12g 黄芩9g 半夏9g` /
`人参、白术、茯苓、甘草` / `大枣十二枚 生姜三片`）现在全部配对正确。

### 5.3 冷启动测量被进程级单例悄悄废掉

`cases.json` 进版本控制之后，`test_startup_bench_splits_the_four_segments`
**全量跑时红、单跑时绿**。根因不在这个测试：`get_retriever()` 是进程级单例，
全量跑时前面的测试已经用真 `cases.json` 建好了它
（失败输出里那句「1060 条医案」就是证据——合成语料只有 5×N 条），
于是 `construct` / `model_load` 两段**根本没发生**，量出来是 None。
而 None 在报告里打印成 `—`，跟"这台机器没装 sentence_transformers" **一模一样**。

修了三件事：`install_self_test()` 换路径后必须丢旧单例（否则整跑量的是旧语料
且一声不响）；`main()` 量之前先问 `retriever_is_built()`，热单例写进独立字段
`warm_singleton_note` 并让退出码非 0；单例的读写口只在 `core/retrieval.py`
实现一次（此前 5 个测试各自 monkeypatch 那个下划线开头的模块变量）。

顺带同一条判据修掉 `scripts/bench_consult.py`：它用 `CASES_PATH.exists()` 判断
"要不要装合成医案"，而检索器的路径绑在构造函数默认值上——`cases.json` 进版本控制
之后两者给出相反答案，合成医案没装上、检索真的不可用。改走
`core.retrieval.cases_available()`（同一个问题只有一处实现）。

---

## 六、诚实交代：药理层数据在这台机器上**不存在**

§0 的基线描述里写着「药理层入库」。**核过了，不成立**：

```
$ python -c "from core.context_prefix import pharmacology_read_path; \
             print(pharmacology_read_path('materia_medica'), pharmacology_read_path('formulary'))"
None None

$ python -m core.ontology --stats
药理层数据不在（data/standard/materia_medica.jsonl 与 formulary.jsonl）。
…
$ echo $?
2
```

`data/standard/materia_medica.jsonl`（9776 条）与 `formulary.jsonl`（3184 条）
在仓库、工作树、`data/` 与 `data/standard/` 两个解析目录里都**没有**。
它们由 `offline/extract_reference_triples.py` 在有真实 LLM 的机器上抽取产出
（上机剧本段 5，2181 块）。已入库的是 `cases.json`（1075 诊次）和
李可 57 案 / 王云启 77 案——那是**医案层**，不是药理层。

所以本轮的实际状态：

- 本体层代码完整，`available=False`，所有查询返回空/None **而不是抛异常**；
- 知识块在本沙盒里是空字符串，prompt 逐字节等于改前，manifest 里
  `knowledge_entries.available=False` 如实记录；
- 全部 27 条本体测试用**就地构造的合成本体**跑通，不依赖那两份文件；
- `python -m core.ontology --stats` 退出码 **2** —— "没核"，不是"核过了没问题"。

**这对后面几轮的影响要现在说清楚**：R34 的符号验证器（`herb_grounded`
需要本体原文作反例）、R38 的四组消融（B/C/D 组都要本体在），
都要等段 5 跑完拿到这两份 jsonl 之后才有真数可报。
R33/R35/R36/R37 不依赖它，可以照常推进。

---

## 七、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **3207** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R32` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **44**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑 |
| 凭据核对 | **16** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **20** 种全过 | `python -m scripts.screenshot_states` |

- 测试 passed **3207** —— `bench/rounds/R32.json:round.R32.pytest_passed=3207`
- 测试 skipped **10** —— `bench/rounds/R32.json:round.R32.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R32.json:round.R32.pytest_failed=0`
- 全量测试墙钟 **115.4** 秒 —— `bench/rounds/R32.json:round.R32.pytest_wall_s=115.4`
- Playwright **20** 种全过 —— `bench/rounds/R32.json:round.R32.playwright_states_passed=20`
- 凭据核对 **16** 份文档 —— `bench/rounds/R32.json:round.R32.n_checked_docs=16`

**新增测试 77 条**（R31 的 3130 → 3207）。跟前几轮同一条规矩：**条数在它自己的
报告落盘之前量**，写完这份之后是 3208，所以下一轮的基线是 **3208**。
`core/schemas.py` 一个字没动，44 处 `min_length=1` 原样——R33 新增
`S3Structured` 时只会增不会减。

### 每个数的对照

| 数 | 对照 |
|---|---|
| 知识块 tokens 191 | 对照组 `off` 档 = **0**（且 prompt 逐字节相同） |
| 本体查询接口 9 个 | 对照：改前药理层只有 2 个消费者（`query_materia_medica` 一次一味、速查表全量拼文本） |
| 功效表 52 行 / 180 词 | 对照：不走表时 `herbs_by_effect("疏肝理气")` 命中 **0** 味（柴胡的功效写的是「疏肝解郁」） |
| 剂量查错 5 味 | 对照：62 味全表，修后 **0** 味 |
| 邻域最大 26 | 对照：全表 223 个词元（塌成一团的话这两个数会接近） |

---

## 八、也核过了、但这一轮**没动**

1. **`HERB_ALIASES` 有 21 种描述性写法没收（81 次出现）。** 本轮审计
   `cases.json` 698 种药名写法，51 种 / 311 次"去掉首字就是已知正名"。
   其中 **30 种 / 230 次是「生」前缀，绝不能补**——表里同时收录
   生附子 0.0g / 附子 15.0g、生川乌 0.0g / 川乌 3.0g、生草乌 0.0g / 草乌 3.0g、
   生半夏 3.0g / 半夏 9.0g 四对，补进去等于删掉四条严格限量
   （有一条测试专门钉住这件事）。剩下 21 种 / 81 次（白/大/鲜/嫩/老/小 前缀，
   如「大杏仁」「鲜荷叶」「嫩钩藤」）是真的描述性修饰，但改 `HERB_ALIASES`
   会动 **Jaccard 分歧度**这个已测量的数，要单独一轮带前后对照。
   R38 要用分歧度做对照，在那之前改它会让两轮的数不可比。
2. **`data/standard/prescribing_patterns.jsonl` 不存在**，`patterns_for()` 恒返回
   空列表。那是 R35 的产物，本轮只把消费端和路径判据（必须落
   `data/standard/`，坏行跳过不吞整表）写好并测到。
3. **李可 / 王云启仍是 `enabled: False`。** R33 §「五位医家 `enabled=True`」
   是下一轮的事，本轮 `build_focused_knowledge` 的 `physicians` 参数已经
   按 `physicians_enabled()` 传进去，扩到五位时不用改这里。
4. **知识块目前不参与 ReAct 那一支的追加文本。** ReAct 的取证结果是追加在
   S3 prompt 之后的，知识块在 `$refs` 里、位置在前，两者不冲突；
   但"ReAct 查到的药理"和"知识块里的药理"会重复一部分内容。
   要去重得先有真实本体数据才量得出重复率，留到段 5 之后。

---

## 九、自查

1. ✅ `core/ontology.py`：九个查询接口 + Herb/Formula/SourceRef，**不持任何自己的药物表**（AST 测试钉住）
2. ✅ `effect_synonyms.tsv` **52** 条全部带来源（47 textbook / 5 common），表头写清跟 `SYNONYMS` 的区别
3. ✅ 同义展开一跳不闭包：全表 **223** 词元对称性 **0** 例外，最大邻域 **26**
4. ✅ 知识块进**四种** top3 模式，断言的是**发给 LLM 的 system 字符串**，逐模式参数化
5. ✅ `off` 档与改前**逐字节相同**（且与"本体不可用"那一跑逐字节相同）
6. ✅ `_format_case_block` 一字未动，医案块逐字原样出现在 system 里
7. ✅ 规律永不裁；裁了什么写进 `trimmed_sections`，不静默少放
8. ✅ manifest 三字段 + bench 报告三字段，`available` 与 `tokens=0` 分开记
9. ✅ 药理层数据不在这件事**写在第六节正文**，`--stats` 退出码 2，不是 0
10. ✅ 顺带量出并修掉三个缺陷，每个都带前后对照的数（5 味 / 4 种写法 / 1060 vs 5×N）
11. ✅ 全量 **3207** passed / 10 skipped / **0** failed；ruff 干净；`--check` 0（**16** 份文档）；Playwright **20** 种全过
12. ✅ `data/SOURCES.md` 第 **79/80/81** 条；`core/schemas.py` 没动，**44** 处 `min_length=1` 原样
13. ✅ 每个数都带对照（见第七节末表）

---

## 十、下一步（R33）

`S3Structured` schema + `prompts/v1/s3_structured.yaml` + `S3_MODE=structured|legacy`。
本轮已经为它铺好两件事：知识块进了提示词（S3′ 的「不可跳步」要求模型引用本体，
引用不到的东西没法要求它引用），以及 `OntologyRef` 要指向的那些结构
（`Herb.refs` / `Formula.refs` 里的 `SourceRef(book, source, span)`）已经在了。
