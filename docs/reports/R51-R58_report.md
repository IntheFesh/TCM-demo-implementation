# R51–R58 验收报告：机制级重构八轮

八轮的代码此前已经全部提交并推送（`dc80163` … `6ea907c`），但报告一份没写、
R57 没跑出实测数、R58 没跑出真机数——**代码写完不等于轮次完成**。本文件补
八份报告，每轮九节，"撞到的坑""发现但未动"两节不省。数字凡是能在这个沙盒
里现读现算的，都重新核实过，不照抄 commit message；commit message 里发现
的两处数字错误（R51"新增测试 66 条"应为 37 条、R54"新增测试 29 条"应为 22
条）已在对应小节订正并说明订正依据。

判据不在本文件里重复定义：R51–R57 的判据出处是各轮自己的模块文档字符串或
`eval/ablation/spec.py`；本文件只报实测数、对照基准、以及每条判据的通过/
未通过状态。

## 总览

| 轮 | 核心机制 | Commit | 净增测试 | 全量测试（当轮） |
|---|---|---|---:|---|
| R51 | 医理规则层：藏象/病机/治则/配伍四类规则 | `dc80163` | 37 | 4356 passed / 7 skipped |
| R52 | 第一相「演绎推导」：`S3_MODE=derived` 成为默认 | `f999ab7`+`1c7cc54` | 47+6=53 | 4403 passed / 7 skipped |
| R53 | 第二相：符号验证扩到医理一致性（+4 条规则） | `b210f6e` | 22 | 4426 passed / 7 skipped |
| R54 | 第三相：医案佐证，检索移到验证之后 | `e82f687` | 22 | 4461 passed / 7 skipped |
| R55 | 时延与渲染真机可用 | `8c4975e`+`ddbec39` | 46+1=47 | 4488 passed / 19 skipped |
| R56 | 产品面与交互收口 | `11eb42d` | 84 | 4613 passed（Playwright 研究模式 40 态+产品模式 45 态全过） |
| R57 | 四组消融 harness | `7104781` | — | 沙盒无真机数，见该轮五 |
| R58 | 真机验收脚本 | `6ea907c` | — | 沙盒无真机数，见该轮五 |
| （本次报数订正） | 两处 ruff check 告警 + 本文件 + SOURCES 第 156 条 | `02a13bb` | +2 修正 | **4655 passed / 7 skipped**（当前 HEAD，见文末「当前实测」） |

---

# R51　医理规则层

## 一、机制：给演绎推导一个地基

此前系统里只有"是什么"（证候定义、药性、方剂组成）与"谁做过什么"（医
案），没有一条"为什么/怎么推"——模型只能靠检索医案模仿，检索到案就换成
必填引用的 schema，这正是"投票整合"的根因。R51 补的是缺的这一环：
`data/standard/tcm_theory.jsonl`（171 条）+ `core/theory.py`（五个查询
接口：`organ_relations`/`transitions`/`principles_for`/`compatibility`/
`rule`），只建规则库和查询层，不碰 prompt、不碰 schema——那是 R52 的事。

## 二、怎么做的

四类规则：

- 藏象关系（ZX，43 条）：五行生克乘侮 + 表里 + 气血津液关系
- 病机传变（BJ，50 条）：单证素传变 + 脾胃门常见组合证素传变
- 治则推导（ZZ，53 条）：12 条经典元治则 + 41 条操作性/脏腑具体化治则 +
  10 条脏腑生理默认方向规则（这 10 条是覆盖率从 59.8% 补到 100% 的关键，
  见「四、坑」）
- 配伍理论（PW，25 条）：七情 + 君臣佐使 + 组方原则 + 药味加减 + 9 条
  药对经验

十八反十九畏**不复制**，只留一条指针规则指向 `core.safety_output`（CLAUDE.md
"同一概念的匹配逻辑只能有一处实现"）。

## 三、数据出处核实（防幻觉审计）

171 条按 `kind` × 出处交叉统计（本次重新现算，不依赖记忆）：

| kind（四类规则） | 条数 | curated/classic | `source` 字段原文 |
|---|---:|---|---|
| `organ_relation`（藏象） | 43 | curated | "人工整理（据中医基础理论藏象学说通用表述，本项目未持有该教材电子版）" |
| `pathomechanism`（病机） | 50 | curated | "人工整理（据中医基础理论病机学说通用表述，本项目未持有该教材电子版）" |
| `treatment_principle`（治则） | 53 | curated | "人工整理（据历代治则理论通用表述，本项目未持有可校验页码的教材版本）" |
| `compatibility`（配伍） | 25 | classic | 18 条出自 `books/中药学.md`、7 条出自 `books/方剂学.md`（总论段落） |
| **合计** | **171** | 146 curated / 25 classic | — |

**curated（146 条，A/B/C 三类）**：内容是学科公认的基础理论（任何一本《中
医基础理论》教材都会写同样的内容），但项目里没有一份可核对页码的教材电子
版（授权 clone `PanckooAI/TCM_Datasets` 未完成）——这是诚实标注，不是降级：
换到有教材原文时，这三类可以整批升级为 `classic` 并补 `span`，`core/theory.py`
的查询接口不用改。

**classic（25 条，D 类）**：`books/中药学.md`/`books/方剂学.md` 两本教材
总论确实在项目里（README 3.1 节说明的下载来源）。
`offline/extract_tcm_theory.py::_verify_span_in_book()` 在写文件**之前**
逐条断言 `span` 是对应书文件内容的真实子串，抽不到就让脚本崩，不静默放行
编造出处。

## 四、撞到的坑

**SOURCES.md 第 146 条**：`principles_for` 最初要求"必须给了病性（nature）"
才可能命中任何治则规则，脾胃门 174 个证候里 70 条（40%）`nature` 就是空
的，覆盖率卡在 59.8%（要求 ≥80%）。两种修法摆在面前——放宽验收阈值，或
补规则。选了后者：那 40% 覆盖不到的原因是规则库缺"脾宜升则健"这类只认
病位、不认病性的脏腑生理默认方向规则，不是这批证候真的没有对应治则。补
了 10 条这样的规则，覆盖率到 100%。**降验收线会把"规则库不全"这个事实
永久盖住，之后没人会再想起补那 10 条**——这是本轮唯一值得记的判断。

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 治则规则对脾胃门 174 个证型的覆盖率 | **100%** | 要求 ≥80%（§1.4 底线） |
| 净增测试（`git diff` 现算，非 commit message 的"66条"） | **37 条**（`test_tcm_theory.py` 29 + `test_theory_coverage.py` 8） | ≥22/≥8 的要求——两个数都超出 |
| 全量测试（当轮） | 4356 passed / 7 skipped | 无回归（0 failed） |
| ruff | clean | — |

**订正说明**：commit `dc80163` 消息里写的是"新增测试 66 条"，但同一句话
后半截自己给出的拆分是"29+8"，加起来是 37 不是 66——这是当时写 commit
message 时的笔误（可能把某个中间统计口径的数字带错了）。本次用
`git diff dc80163^ dc80163 -- tests/ | grep -c '^+def test_'` 现算得到
37，与直接 `grep -c "^def test_"` 数两个新文件的当前内容完全一致（29+8=37），
以这个数为准。

## 六、发现但未动

- **十八反十九畏用指针规则而不是复制一份数据**：这是有意设计，不是未做
  完——CLAUDE.md"同一概念的匹配逻辑只能有一处实现"的直接应用，`core.theory.rule()`
  查到这条规则时转指 `core.safety_output`，两处数据源永远同步。
- **146 条 curated 规则待《中医基础理论》教材电子版到位后升级**：授权
  clone 未完成，这不是本轮能解决的——如果拿到教材，升级路径是重跑
  `offline/extract_tcm_theory.py` 并给这三类补 `span`，`core/theory.py`
  的接口签名不用改，下游（R52 的 `_format_theory_rules`）也不用改。

## 七、跟前后轮的边界

R51 只建规则库和五个查询接口，**不碰任何 prompt、不碰任何 schema**——
`core/theory.py` 在 R51 落地时没有任何调用方。R52 才开始消费它。这条边界
本身是刻意的：把"规则库对不对"和"规则库怎么被用"分成两轮验收，规则库
的覆盖率数字（100%）不会被 R52 引入的任何变量污染。

## 八、自查闸门

`git show dc80163 --stat` 确认改动范围限于 `data/standard/tcm_theory.jsonl`、
`core/theory.py`、`offline/extract_tcm_theory.py`、两个新测试文件——没有
触碰 `core/chain.py`/`core/schemas.py`/任何 prompt 文件，与「二、七」的
描述一致。`data/standard/tcm_theory.jsonl` 已确认在这个 commit 里入库
并推送（见文末「确认 tcm_theory.jsonl」一节）。

## 九、下一步

R52：把 `core/theory.py` 接进 S3 推导，建立不检索医案也能走完五步链的
`S3Derived`。

---

# R52　第一相「演绎推导」

## 一、机制：从 schema 层消除"必须先检索到医案"这条约束

R40–R47 那批改造把"五家综合"的产品面表述改成了"本次辨证"、把内部字段
从前端隐藏——但根因是 `S3Structured` 的 `physician_influences`/
`cited_case_ids` 两个字段是 `Field(min_length=1)`：只要这两个字段还在
schema 里、还是必填，模型就**必须**先检索到医案才能通过校验，不管界面
上那句话怎么改。R52 不是给 `S3Structured` 打补丁，是**新建**一套没有这
两个字段的 `S3Derived`（连同五个 `*Derived` 步骤类），防幻觉约束换成
`rule_refs`/`insufficient`——引用一条真实存在的医理规则，或者显式承认
"这一步依据不足"。

## 二、怎么做的

- `prompts/v1/s3_derived.yaml`：全文无 `$refs`、无参考医案块；占位符是
  `$theory_rules`（`_format_theory_rules` 按 S2 证素查出的医理规则文本）、
  `$knowledge`（本草/方剂本体，`physicians=[]`，**不带 R35 的名医用药
  规律统计**）、`$elements_summary`、`$symptoms`。
- `core/chain.py::run_derivation()`：不调用 `_search_cases`；`_as_s3_syndrome`
  从 `isinstance` 检查改成 `hasattr(to_s3_syndrome)` 鸭子类型，同时认
  `S3Structured`/`S3Derived`。
- `core/llm.py`：`S3_MODES` 扩到 `(derived, structured, legacy)`，默认改
  `derived`。
- `core/physicians.py`/`core/usage.py`：补上 `derived` 模式的专属分支
  （见「四、坑」第 148 条）。

## 三、数据/prompt 出处核实（本轮专项：s3_derived.yaml 三处命中）

`grep -n "参考医案\|医家的思路\|case_id" prompts/v1/s3_derived.yaml` 命中
3 处，全部在 `notes:` 字段（YAML 里的纯文档字符串，`core/llm.py::load_prompt`
只读取并 `render()` 字典里的 `system`/`user` 键，`notes` 从不被拼进发给
模型的文本——`core/chain.py:1548` 附近逐处调用点可核）：

1. 第 195 行："**全文没有参考医案块，也没有 `$refs` 占位符**"——否定式
   说明，确认这份 prompt 不含参考医案块，不是残留。
2. 第 211 行："…整体消失了（连带 `cited_case_ids`、`dose_evidence`）"——
   解释旧 schema 的字段为什么在新 schema 里不存在，`case_id` 是
   `cited_case_ids` 这个**已删除字段名**的子串命中，不是模板里真的有
   这个占位符。
3. 第 233 行："…全部通过，且不出现 `cited_case_ids`/`physician_influences`/
   `physician_source`/`dose_evidence` 中的任何一个字段名"——描述
   `tests/test_prompt_derived.py` 的验收判据本身，同样是 `case_id`
   命中已删除字段名的子串。

**结论：3 处全部是否定式说明或对已删除字段名的引用，没有一处是残留的
模板文本。** 这份 prompt 实际发给模型的正文（第 1–194 行）本身不含这三
个词组中的任何一个。

## 四、撞到的坑

- **SOURCES.md 第 147 条**：schema 编码机制——换掉字段比换措辞更重要。
  R40–R47 改了产品面表述、藏了字段，产出机制没变一个字；R52 新建
  `S3Derived` 而不是改 `S3Structured`，这是本轮唯一实测有效的教训：要
  改变模型的行为，改 schema 里"通过校验需要什么"这件事，不是改模型
  看到的措辞或界面上的标签。
- **SOURCES.md 第 148 条**：切换 `S3_MODE_DEFAULT` 默认值之后，全量测试
  第一遍红了两处，都不在新写的代码里——`physicians_for_mode("derived")`
  落进了 `else` 分支报成"3 位医家参与"（正确答案是 0）；`calls_per_consult`
  的 `if mode == "structured"` 没把 `derived` 也算进单次调用公式，落进
  按医家数相乘的公式。**改完默认值之后要跑一遍全量测试，不能只看新写
  的模块自己的测试。**

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 净增测试（f999ab7） | 47 条（`git diff` 现算：+48/-1） | 覆盖医案零可见性/阶段顺序/schema 防幻觉/prompt 内容四类 |
| 净增测试（1c7cc54 补测） | 6 条 | 五个 `*Derived` 步骤类各一条 + 整体一条 |
| 全量测试（当轮） | 4403 passed / 7 skipped | 较 R51 的 4356 净增 47（与净增测试数一致，无隐藏回归） |

## 六、发现但未动

- **R35 名医用药规律统计故意不摆进 derived prompt**：那批数据本身是从
  医案挖出来的，摆进这一相的 prompt 等于换了个位置重新把"模仿哪位医家
  的用药习惯"带回来——跟 R52 的核心主张矛盾。这是设计决策，不是遗漏。
- **`legacy`/`structured` 两份旧 prompt 保留不删**：R1~R51 的全部历史
  数字都是在这两份 prompt 下跑出来的，删掉即丢基线，三份 prompt 并存
  到 R57 消融实验里各自对应 A/B 组与历史对照。

## 七、跟前后轮的边界

R52 只新建了一条并行路径（`derived` 模式），`structured`/`legacy` 两条
路径原样保留——R53 的符号验证、R54 的事后佐证都是在 `derived` 这条新
路径上继续加，不改前两条路径的行为。这条边界让 R38（老四组消融）在
R51–R58 期间始终可复现。

## 八、自查闸门

`tests/test_prompt_derived.py` 按字符串扫描 `s3_derived.yaml` 本身，任
何一处不小心抄回 `s3_structured.yaml` 的措辞（`cited_case_ids` 等四个
字段名出现在**发给模型的正文**里）都会被当场抓到——这条测试跟「三」的
人工核实是同一件事的两层保证：一层是本次人工读 `notes:` 字段确认语义，
一层是机器持续对正文（不含 `notes`）做字符串扫描。

## 九、下一步

R53：把符号验证扩到医理一致性，不然"演绎推导"这四个字没有验证器背书。

---

# R53　第二相「符号验证扩到医理一致性」

## 一、机制：验证器要认得出医理规则，不能只认本体

R51 建了规则库，R52 让推导链引用它，但符号验证器（R34 的七条本体规则）
从来没问过"这一步的证型跟脏腑对不对得上、治法跟证型矛不矛盾"这类医理
层面的问题——只查配伍/剂量这类本体层面的事。R53 新增四条 revise 级规则：
`principle_matches_syndrome`/`method_not_contraindicated`/
`pathomechanism_consistent`/`role_structure_by_rule`，数据源是
`core/theory.py`（R51 医理规则层），不是 `core/ontology.py`（本草/方剂
本体）——两者是独立开关。

## 二、怎么做的

- `core/formula_verifier.py`：`ALL_RULES` 从七条扩到十一条，拆成
  `ONTOLOGY_RULES`（七条，问 `ont.available`）/`THEORY_RULES`（四条，问
  `load_theory()`）两张表；`VerificationResult` 加 `theory_available` 字段。
- `MAX_REVISE_ROUNDS` 产品默认从 3 降到 1（规则变多之后，多留的第二三轮
  改的多半是同一类没改对的地方）。
- `core/node_explain.py`：新增四条规则不按单味药判（跟证型/治法/脏腑
  相关），herb 节点「验证结果」区块补上"按什么判"的说明。

## 三、数据出处核实

四条新规则的数据源是 R51 的 `core/theory.py`，不引入新数据文件——`span`/
`source` 的防幻觉约束沿用 R51 已核实的那份，本轮不新增需要核实的数据
出处。

## 四、撞到的坑

- **SOURCES.md 第 149 条**：两个数据源缺一个，不该把另一个能判的规则
  也判成"没跑"。`verify_formula` 原来只有一个"数据够不够"判据：
  `ont.available`——本体不可用就把 `ALL_RULES` 全部标成 unverifiable。
  R53 加了四条依赖医理规则层的新规则后，继续沿用这条老逻辑会出现假象：
  药理层数据没部署的机器上，新加的四条医理一致性规则明明能跑，却被本体
  那个开关一起误判成"一条都没验"。修法：每接入一个新的外部数据源，
  "这个数据源在不在"就该是它自己的独立开关。
- **SOURCES.md 第 150 条**：给一张"规则名→中文名"表加条目，要找出所有
  遍历那张表的调用方，不能只改产出规则的那一侧。`RULE_LABELS` 从七条
  扩到十一条那一刻，`test_the_rule_names_come_from_the_backend_label_table`
  就红了——`_herb_verifiability` 原来完全没提新加的四条（它们是链级判据，
  不是单味药判据）。修法不是删测试断言范围，是给这四条各加一行"说明判据
  落在哪"，不留空。

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 符号验证规则总数 | 11 条（7 本体 + 4 医理） | R34 落地时 7 条 |
| 净增测试 | **22 条**（`test_theory_consistency_rules.py`，`git diff` 现算 +22/-0；`test_formula_verifier.py` 的 +3/-2 是既有测试的钉子修正，不计入"新增"） | 与 commit message 一致，无需订正 |
| `MAX_REVISE_ROUNDS` | 3 → 1 | 产品默认（规则变多后多留的轮次边际收益低） |
| 全量测试（当轮） | 4426 passed / 7 skipped | 较 R52 的 4403 净增 23（22 条新测试 + 1 条本轮其它改动带来的净增，量级吻合） |

## 六、发现但未动

- **本体与医理规则层同时缺失的组合场景**：`test_theory_consistency_rules.py`
  与既有 `test_formula_verifier.py` 分别覆盖了"只缺本体"和"只缺医理规则
  层"两种单一缺失场景，**没有找到一条显式覆盖"两者都缺"的测试**——按
  `verify_formula` 的实现（两个开关独立判断、独立跳过），这种组合场景
  理论上是两条独立跳过逻辑的简单叠加，行为可预测，但目前没有一条测试
  把它坐实。留给 R58 真机验收或后续一轮补。

## 七、跟前后轮的边界

R53 只扩验证器，不碰 `run_derivation()` 本身的生成逻辑——生成出什么样
的候选结论，R53 完全不管，只负责"生成出来的结论过不过得了医理一致性
这一关"。这条边界保证 R54 加事后佐证时，"验证先跑完"这件事的语义不会
被 R53 的改动影响。

## 八、自查闸门

`tests/test_revise_loop.py` 的三处硬编码七条/三轮钉子已同步更新为十一条/
一轮；`tests/test_node_explain_sections.py` 遍历 `RULE_LABELS.items()` 的
那条测试重新跑过，四条新规则各自有"按什么判"的说明。

## 九、下一步

R54：验证跑完之后，加一层事后医案佐证，但绝不能回头改已经验证通过的
推导结论——不然"演绎推导"就变回了变相的"先编后凑证据"。

---

# R54　第三相「医案佐证」

## 一、机制：佐证只读不改，调用顺序本身是第一道保证

`core/corroboration.py::corroborate()` 在 R53 的符号验证闭环跑完、`s3`
已经定型之后才被调用，拿最终结论去检索全部医家的医案库，按用药集合
Jaccard 距离分成 `concordant`/`divergent`/`no_precedent` 三桶。**只读
`s3`，不修改、不重新生成任何一步**——这是 R54 唯一的产品承诺，也是最
容易被后续改动无意破坏的一条不变式。

## 二、怎么做的

- 调用顺序本身是第一道保证：`corroborate()` 排在 `run_derivation()` 的
  验证闭环之后。
- `tests/test_corroboration.py` 逐字节 sha256 比对调用前后的 `s3` 是第
  二道、机器可验证的保证——这条测试比"人工审代码确认没有改动"更强，它
  在每次 CI 都会跑。
- `CORROBORATION=on|off` 开关（R57 消融：A/B/C 组关，D 组开），默认 on。
- 彻底去掉 `physician_influences` 键（不是留空列表——那个字段说的是
  "检索到的医案影响了推导过程"，这一相设计上没有这件事，留空列表会让
  读者误以为"查过但没查到"）。
- 延迟 import 破循环：`core/corroboration.py` 延迟 import
  `core.chain._search_cases`，跟 `core/context_prefix.py::_default_case_formatter`
  同一个既有模式。

## 三、数据出处核实

不引入新数据文件，检索的是既有医案库（`data/ye_tianshi/`、
`data/wu_jutong/` 抽取出的 `cases.json`），出处沿用项目一开始就有的
`data/SOURCES.md` 核实结论，本轮不新增核实项。

## 四、撞到的坑

**SOURCES.md 第 151 条**：预留的 canary 测试要在真的落地时被看见，不能
因为实现选了另一种数据形状就悄悄失效。R52 写过一条
`test_future_corroboration_field_is_not_yet_present`，注释明说"R54 落地
会变红，提醒作者更新"。R54 落地后跑了一遍，**它没有变红**——不是因为
R54 没实现，是它断言的是 `concordant`/`divergent`/`no_precedent`/
`physicians_with_precedent` 不是 `results[0]` 的顶层键，而 R54 的真实
实现把这四个桶嵌进了 `results[0]["corroboration"]` 里。canary 测试的
价值只在首次触发时被人看见——发现后手动改成对真实形状的断言，不是删掉
重写（删掉重写会丢失"这条测试曾经是哪一轮的占位符"这条历史线索）。

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 净增测试（现算，非 commit message 的"29 条"） | **22 条**（`test_corroboration.py`，`git diff` 现算 +22/-0；`test_derived_no_cases.py`/`test_phase_order.py` 的 +1/-1、+2/-2 是既有假设更新，不计入"新增"） | — |
| 全量测试（当轮） | 4461 passed / 7 skipped | 较 R53 的 4426 净增 35（22 条新测试 + 既有测试隔离改造带来的额外用例，量级吻合） |
| sha256 一致性校验 | 100%（调用前后 `s3` 逐字节相同） | 唯一判据：0 处不同即通过，没有"部分相同"这种中间态 |

**订正说明**：commit `e82f687` 消息写"新增 29 条测试（tests/test_corroboration.py）"，
现算该文件的新增测试函数数为 22（`git diff e82f687^ e82f687 -- tests/test_corroboration.py`
显示 `+22/-0`，当前文件 `grep -c "^def test_"` 同样是 22），以 22 为准。

## 六、发现但未动

- **`divergent`/`concordant` 判定阈值本身没有做敏感性分析**：Jaccard 距
  离多大算"分歧"、多小算"一致"，用的是既有 CLAUDE.md 里"改用药物集合
  的 Jaccard 距离"这条既定判据的默认阈值，R54 没有针对"事后佐证"这个
  新场景重新校准或做敏感性分析——如果 R57 的 C vs D 一致率闸门（≥0.9）
  测出问题，第一个要查的可能不是医理规则覆盖面，而是这个阈值本身在
  "零医案推导 + 事后一次性佐证"这个新场景下是否仍然合适。
- **既有假设更新（`test_derived_no_cases.py`/`test_phase_order.py`）**：
  这两个文件原本断言"`consult()` 全程零检索"，R54 后这个假设只在
  `CORROBORATION=off` 时成立。已经加了开关隔离测试范围（见五、净增测试
  订正说明），但没有反向新增一条"`CORROBORATION=on` 时事后确实检索了"
  的正面测试独立于 `test_corroboration.py` 本身——目前这件事只被
  `test_corroboration.py` 一份测试文件覆盖，单点覆盖，不是本轮遗漏，
  是记录下来供 R58 真机验收时留意。

## 七、跟前后轮的边界

R54 的承诺是"绝不回头改推导"——这条边界比"不碰前一轮代码"更强：R54
甚至不允许**读到**验证结果之后再去动 `s3` 的内容，`corroborate()` 的
返回值只追加进 `results[0]["corroboration"]`，不覆写任何既有键。R57 的
C vs D 一致率闸门验的正是这条边界有没有被守住。

## 八、自查闸门

`tests/test_corroboration.py` 的 sha256 比对每次 CI 必跑；
canary 测试已按「四、坑」的做法改成对真实形状的断言，不是删掉重写。

## 九、下一步

R55：前三相加起来的调用链变长了，时延与前端渲染要跟上——不然"演绎推导
+验证+佐证"跑起来用户看到的是一个转圈转很久说不出理由的页面。

---

# R55　时延与渲染真机可用

## 一、机制：两类问题分开修——记账断链是后端 bug，状态卡死是前端 bug

R55 修的是两条独立的链路，不是同一个 bug 的两种表现：`reasoning_tokens`
恒 `None` 是 usage 记账函数一个"看起来无害"的早退条件；九段状态骨架⑨
卡在"推理中"是前端状态桶粒度太粗。两者都符合同一条更抽象的判据（见
「四、坑」第 152 条），但触发场景、修法、验收方式完全不同。

## 二、怎么做的

**5.1 时延**：

- `core/llm.py::record_usage()` 根因修复：早退判据从"看到就整条跳过"改
  成三类信号（缓存/completion/reasoning）各自独立判断、独立计入
  `n_reported`。
- `S3_REASONING_EFFORT_TOP3` high→low、`FULL_CONTEXT` max→medium：
  2026-09-17 真机三档墙钟实测 264.6/276.3/208.9 秒，顺序落在测量噪声内，
  "档位越高越慢"假设不成立。
- `MAX_ASK_ROUNDS` 按角色分（`core/followup.py::max_ask_rounds_for_role`）：
  医师 0、患者 1、学生/研究者不限。

**5.2 渲染断链**：

- 新增 `agent_step` 事件：`AgentTrace` 加 `on_step` 回调，四能力决策实时
  广播。
- 九段渲染骨架第⑨段（校验与出处）改成独立状态机 `chainChecksState`，只
  认 `verify_revise`/`done` 两个事件，不再从 `reached` 反推。
- `s3_done` 后 120 秒无 `done` 的兜底：`_cache_finished_result`/
  `_get_finished_result` + `GET /api/consult/stream/{id}/result`。

## 三、数据出处核实

不涉及新数据文件，本轮不新增核实项。

## 四、撞到的坑

**SOURCES.md 第 152 条**（同一条判据的两个实例）：

一、`record_usage()` 的 `if hit is None and miss is None: return`——原意
是"DeepSeek 专属的两个缓存字段都没报，这次 usage 就不算命中过缓存统计"，
但因为写在函数最前面，效果变成"只要没报缓存字段，这条 usage 整个不算
数"。任何不报 DeepSeek 缓存字段但报了 `reasoning_tokens` 的后端，
`reasoning_tokens` 被永远记成 0。**判据：一个函数里"提前 return"的条件，
要问它守的是"这条数据整体没东西可记"还是"这条数据里某一类字段没报"。**

二、前端九段骨架⑨原来跟④-⑧共用 `sec.step === "s3"` 这一个粗粒度状态桶，
`s3_done` 之后进入验证/佐证/个体化调整期间⑨显示跟④-⑧一样的"推理中"，
真机上表现为"已用 286 秒仍在转，底部日志却已经写着输出完成"。**一个复用
别处状态的字段，要问它复用的粒度是不是真的对得上这个场景要回答的问题。**

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 三档墙钟实测（R55 commit 原话"三档"，具体对应哪三种配置本次核实**核实不到**，见下方订正） | 264.6 / 276.3 / 208.9 秒 | 三档顺序落在测量噪声内，不支持"档位越高越慢" |
| 净增测试（8c4975e） | 46 条（`git diff` 现算 +47/-1） | `test_event_contract.py`(7) + `test_chain_progressive_render.py`(15) + `test_done_fallback.py`(13) 三个新文件共 35，另外 ~11 条来自既有文件的补充/修正 |
| 净增测试（ddbec39 补充） | 1 条 | `test_doctor_role_asks_zero_followup_rounds_in_the_real_event_stream` |
| 全量测试（当轮） | 4488 passed / 19 skipped | 较 R54 的 4461 净增 27（47 净增 - 一批既有超时测试因环境差异改为 skip，skip 数从 7 升到 19 属已知环境相关，非本轮引入回归） |

## 六、发现但未动（commit message 原话"无法完成项"，逐条移到这里）

- **三档真机质量对照表**（证型/主方/验证器一次通过率/rule_refs 完整率）：
  沙盒没有真实 LLM 后端，跑不出这份数字。**订正**：commit `8c4975e` 消息
  写"工具在 `scripts/compare_reasoning_tiers.py`"，本次核实该文件**不
  存在**于仓库任何位置（`find`/`grep` 均无命中）——工具没有真的落地，
  只是在 commit message 里承诺了。连带的后果是"264.6/276.3/208.9 秒"
  这三个数具体对应哪三种配置（是 `S3_REASONING_EFFORT` 的三个档位、还是
  三种检索模式、还是别的组合）本次**核实不到**——没有工具能重现这份
  实验设计，唯一留下的痕迹是这三个数字本身和"顺序落在测量噪声内"这句
  结论。**这是本次报数时才发现的新坑，不在 SOURCES.md 第 152 条范围
  内**（那一条讲的是记账/渲染两处 bug，不是这份工具缺失）。`README.md`
  另一处（「四组消融（R57）」一节）写的"top3 档约 45 秒、full_context
  档约 75 秒"是一个**独立的、量级小得多的数字**，跟这里的 264.6~276.3
  秒（约 4.5 分钟）不是同一份测量、也没有任何文档说明两者的关系——两处
  都自称是"R55 记录"，互相之间存在数量级差异，本次核实同样核实不到哪个
  更准。R57/R58 的时间成本估算不应该再直接引用这两个互相矛盾的数字中
  的任何一个，应该让用户自己先跑一次小样本（`--limit 2`）实测，
  详见 `docs/ONSITE_R57_R58.md`。
- **真机验收阈值**（p50≤60s/首字≤3s/思考≤4000字/调用≤4）：本轮不虚构
  这些数字，留给 R58 的 `scripts/acceptance_r58.py` 做真机闸门。
- **CLAUDE.md 要求的 Playwright 复核**：本轮改动的是⑨段状态机（非节点/
  边结构），沙盒内跑了 15 条 node 直测，真实浏览器的 Playwright 复核留
  待与 R56（产品面）一起做——已在 R56 完成（见 R56 报告「八」）。

## 七、跟前后轮的边界

R55 不碰任何推理逻辑（S1–S3、验证、佐证的产出内容一个字不改），只改
"这些结果怎么被记账、怎么被渲染"。这条边界保证 R55 引入的全部改动理论
上不会影响 R57 消融实验要测的任何内容指标。

## 八、自查闸门

`tests/test_event_contract.py` 做源码级后端 `emit` 与前端 `case`/`if`
的双向核对——防止"后端发了、前端没处理"这类契约缺口再次出现而不被
发现。

## 九、下一步

R56：产品面与交互收口——R55 修完时延与渲染断链，R56 处理截图里逐条列出
的产品面问题。

---

# R56　产品面与交互收口

## 一、机制：截图逐条销号，零推理逻辑改动

R56 不碰 S1–S3、验证、佐证的产出内容，只改"用户看到什么、看得到多少"——
危重信号按角色分流、图谱页改聚焦当前证型的"本例知识地图"、新增鉴别诊断
一节、释义面板改吸顶侧栏、追问改聊天气泡、医师标识加格式校验。

## 二、十条清单逐条销号

| # | 项目 | 状态 | 实现 |
|---|---|---|---|
| 1 | 原始 JSON | 已消除 | `web/index.html` `class="internal-only"` 盖住相关技术注记 |
| 2 | λ1 说明 | 已消除 | 同上，与原始 JSON 共用一个 gate |
| 3 | 帧流式遥测 | 已消除 | `web/app.js` `if (!isProductMode())` 包住"N 帧流式/首字 Xs"文案 |
| 4 | 「样本（0/3）」 | 已消除 | 全仓剩余"样本"出现处全部在 `//` 注释里，不在渲染文本里 |
| 5 | `core/elements.py` 出处 | 已消除 | `core/node_explain.py::_SOURCE_LABELS` 翻译表 + `_PATH_LEAK_RE` 兜底 |
| 6 | 循证指南未渲染 markdown | 已消除 | `_section()` 统一 `.replace("**","")`，全项目单一出口 |
| 7 | SP-01 编码混进正文 | 已消除 | 编码只进独立 `codes=` 字段（tooltip 用），从不进 `lines` |
| 8 | 「证候表里有 61 条」 | 已消除 | 改写为 `f"常见于 {len(rows)} 种证候"` |
| 9 | 追问三轮调试格式 | 已消除 | `followupChatHtml(f)` + 聊天气泡样式替代调试格式 |
| 10 | 医师标识校验 | 已消除 | `DOCTOR_ID_FORMAT_RE` 提交前 + 实时两处校验 |

三项交互改造：释义面板改**桌面端吸顶右侧栏**（`@media (min-width: 769px)`
下 `position: fixed`）；危重拦截**按角色分形态**（`role_sees_full_reasoning_on_red_flag`，
医师有完整推理+警示条，患者只看警示条）；图谱浏览器改「本例知识地图」
（`gbFocusOnNodeId` 自动聚焦当前证型，不再回退到脾胃门首屏）。

## 三、数据出处核实

不涉及新数据文件；违禁词扫描表从 23 词扩到 36 词（`tests/test_no_demo_artifacts.py::BANNED`，
现算 `len(BANNED) == 36`，与 commit message 一致）。

## 四、撞到的坑（本次补记，见 SOURCES.md 第 156 条）

`scripts/screenshot_states.py` 两处判据在释义面板改吸顶侧栏之后失效，
都是"判据依赖的信号已经不再指向它原本想测的那件事"：

一、`box.offsetParent === null` 曾经是"有 `.show` 类却没真的显示"的可靠
信号，前提是正常文档流定位。改成 `position: fixed` 之后该属性按 CSS
规范恒为 `null`，判据从"类名对但没显示"变成"永远为真"。改用
`getComputedStyle(box).display`/`.visibility` 直接查计算样式。

二、循证对照口径声明的字面量判据断言页面含"中医药循证临床实践指南"这个
书名，而 `GUIDELINE_GAP_NOTE` 的措辞早就改成不提书名的表述——判据没跟
着这次措辞修订一起改，直到这次真的跑 Playwright 才第一次报错。

**本条目原本在落地时（`11eb42d`）只写进了 commit message 一行摘要，没
有单独进 `data/SOURCES.md`——这是本轮报数核实时发现的真实遗漏，已补记
为第 156 条并说明补记原因。**

## 五、这一轮的数，每个带对照

| 数字 | 值 | 对照基准 |
|---|---|---|
| 违禁词表 | 36 词 | R56 之前 23 词 |
| 净增测试（现算 `git diff` +87/-3） | 净 84 | 十个专项测试文件（`test_ui_r56_*.py` 九个 + `test_hover_tooltip.py` 等）覆盖十条清单逐项 |
| 全量测试（当轮） | 4613 passed / 7 skipped | 较 R55 的 4488 净增 125（新增 84 + 既有测试因十条清单改动而更新的用例） |
| Playwright | 研究模式 40 态、产品模式 45 态全过 | CLAUDE.md 要求的图层/渲染结构变更必经真实浏览器验收 |

## 六、发现但未动

- **医师工号格式校验只是通用正则**（`/^[A-Za-z0-9]{3,}$/`），不对应任何
  真实医院 HIS 系统的工号规范——因为没有真实 HIS 系统可对接，这条校验
  目前只能挡住"明显不像工号的输入"，不能验证"这是不是这家医院真实存在
  的工号"。真正的校验要等 R46 已经搭好接口的 HIS 集成（`POST /api/integration/consult`）
  真正对接一家医院时才能做实。
- **R48（合规与安全）未做**：R56 把 veto 文案去 demo 化，但这只是产品面
  措辞的一次修订，不是系统性的法务/合规审查——全部对外文案的合规审查是
  R48 的独立范围，本轮故意不越界去做。

## 七、跟前后轮的边界

R56 不改变任何一步推导的产出内容——十条清单里的每一条都是"同样的数据，
换一种呈现方式"或"同样的判断，换一个触发时机（危重信号拦截时机不变，
变的是拦截后给谁看什么）"。这条边界保证 R57 的四组消融实验测出来的内容
指标不会被 R56 的任何改动污染。

## 八、自查闸门

全量 pytest（4613 通过）+ Playwright 两种模式共 85 态全部过——这是
CLAUDE.md"图层结构变更需 Playwright 真实渲染验收"这条纪律在本轮的落地，
不是可选项。

## 九、下一步

R57：前六轮做的全部机制性改动（医理规则层、三相演绎、时延与渲染、产品
面收口）要用一次消融实验证明"不靠模仿也能推"——这是整个 R51-R58 改造
成败的唯一判据。

---

# R57　四组消融：证明不靠模仿也能推

## 一、机制：四组沿三个布尔展开，唯一定义出处是 `eval/ablation/spec.py`

A=无医理层+医案进推导（现状模仿机制）；B=无医理层+无医案；C=有医理层+
无医案（核心组）；D=有医理层+第三相事后佐证（最终形态）。**这是本轮成败
的唯一判据**：C 组一次通过率 ≥ A 组；C 组 `rule_refs` 完整率 ≥ 0.9；C 与
D 的证型/治法/主方一致率 ≥ 0.9。

## 二、怎么做的

- `core/theory.py` 新增 `THEORY_LAYER` 开关（默认 on），B 组关掉时
  `run_derivation` 不把医理规则摆进 prompt。
- 修正 `core/corroboration.py` 一条跟这份定义矛盾的旧注释（"A/B 关、C/D
  开"错了，应为"A/B/C 关，只有 D 开"，见「四、坑」第 153 条）。
- `eval/ablation.py` 拆成 `eval/ablation/` 包：`r38.py`（原 R38 四组不变，
  测的是完全不同的另一条轴——生成流水线的工程旋钮）+ `spec.py`（R57
  分组定义）+ `r57.py`（20 条 SDT Train 脾胃门主诉的选取/跑组/五指标
  聚合/三条硬指标判定）。
- `scripts/bench_consult.py` 修复两处假设（见「四、坑」第 154 条）。

## 三、数据出处核实

20 条主诉从 SDT Train 按"证型第一个词含脾/胃"挑选，病案号
35/53/117/118/135/191/205/279/286/316/346/349/6/41/47/49/85/104/106/150，
确定性可复现（`select_pi_wei_men_complaints` 文档字符串）。

## 四、撞到的坑

- **SOURCES.md 第 153 条**：上下文压缩会把"任务书原文的分组定义"这类只
  出现过一次的关键文字彻底丢掉，而自己事后写的两处说明可能已经互相矛
  盾——`core/corroboration.py` 一条早前写的注释说"A/B 组关掉事后佐证、
  C/D 组开着"，而 `docs/reports/R55_followup_checkpoint.md` 说"C 组关
  掉事后佐证"，两处出自同一个会话却直接矛盾。没有直接猜一个更像对的，
  而是把矛盾摆给用户确认——用户给出的权威定义确认了"C 关 D 开"，同时
  指出 `corroboration.py` 那条注释是错的。**判据：一个只会被说一次的
  关键定义，一旦要在多处引用，就必须找一个唯一出处，不能让每处引用各
  自复述一遍。**
- **SOURCES.md 第 154 条**：`--backend fake` 的通用 schema 填充器只看
  得到 JSON Schema 表达得出的约束，看不到 Python 模型校验器。
  `S3Derived` 五个步骤类用 `@model_validator` 要求 `rule_refs` 非空或
  `insufficient` 非空，这条约束不在 JSON Schema 的 `required` 列表里，
  假后端填不出满足它的数据——`S3_MODE=derived` 这条路径直到 R57 第一次
  真的用假后端跑演绎推导才把这个盲点跑出来。同一轮还带出第二处：
  `invalid_reason()` 把"derived 模式 0 位医家参与检索"（故意如此）误判
  成"没产出结论"——`physicians_for_mode("derived")` 恒返回空字典是正确
  行为，但被现成的分支悄悄接住、答非所问。修法是给 `invalid_reason()`
  加一档 `mode == "derived"` 的显式分支。

## 五、这一轮的数——沙盒只能验管道，真机数需要用户跑

本次用 `--backend fake` 跑了一遍完整四组管道（`--sdt-dir` 指向本次会话
临时克隆的 TCMEval-SDT checkout），确认四组各自设对了环境变量、跑通、
不崩、报告格式正确：

| 组 | 医理层/医案/事后佐证 | n_ok/20 | rule_refs 适用性 | 内容指标 |
|---|---|---:|---|---|
| A 现状模仿机制 | 关/进/关 | 19（1 例触发危重信号被安全否决，正确行为，不是失败） | 不适用（`structured` 模式无此键） | ⏳ 假后端不出数 |
| B 既无医理也无医案 | 关/不进/关 | 19（同上） | 适用 | ⏳ 假后端不出数 |
| C 纯演绎（核心组） | 开/不进/关 | 19（同上） | 适用 | ⏳ 假后端不出数 |
| D 演绎+事后佐证 | 开/不进/开 | 19（同上） | 适用 | ⏳ 假后端不出数 |

**这份表只证明管道能跑，不代表任何真实推理质量——五个内容指标（带本体
出处占比/验证器一次过率/`rule_refs` 完整率/幻觉率/C-D 一致率）在假后端
下不出数，是 harness 自己的设计（`content_metrics_valid: false`），不是
本次没跑全。** 每组唯一的"失败"是同一例触发黑便（危重信号）被安全否决，
四组都在同一条主诉上正确拦截，符合"安全否决在 S2 之前"的设计，不是
bug（详见「六」）。

三条硬指标**本轮无法判定**——不是"跑了没过"，是"没有真机数可判"，三分
返回值意义上属于第一种（未跑），不是第二种（跑了不达标）。

**真机命令**（需要在用户 AutoDL 机器上跑，已在 README 披露）：

```bash
git clone https://github.com/zhuyan166/TCMEval.git   # 若尚未有本地 checkout
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT
```

**成本量级——订正**：README 这一节原写"约 1~1.5 小时机器时间"，依据是
"top3 档约 45 秒、full_context 档约 75 秒"；但 R55 commit 里另一处记录的
是"三档墙钟实测 264.6/276.3/208.9 秒"（约 3.5~4.6 分钟）——两个数字量级
差 3~4 倍，且量它们的工具（`scripts/compare_reasoning_tiers.py`）不存在
于仓库，核实不出哪个更准（详见 R55 报告「六」的订正）。**本报告不再
沿用任一个数字做时间估算**，按两者的量级给一个更宽的区间：单次问诊
45 秒~5 分钟，80 次串行**保守估计 1~6.7 小时**，实际很可能在区间靠下的
位置（R56 commit 记录过一次单次问诊「已用 286 秒」，落在 5 分钟这一档
附近，不是 45 秒那一档）。**正确做法是先用 `--limit 2` 跑 2 条 × 4 组 = 8
次问诊拿到真实单次耗时，再据此估算完整 80 次的时长**——具体命令与费用
估算方法见 `docs/ONSITE_R57_R58.md`「一、R57」，不在这里重复。跑完后
`eval/report_ablation_r57.json` 的 `gates`/`all_gates_passed` 字段（或直接
`cat eval/report_ablation_r57.md`）就是三条硬指标的判定结果，不用翻
JSON。

## 六、发现但未动

- **R38 与 R57 两套 A/B/C/D 定义同名不同义，尚无跨文档消歧提示**：R38
  的 A/B/C/D 测的是生成流水线的工程旋钮（产品默认/三列集注/best-of-3/
  S1+S2 合一），R57 的 A/B/C/D 测的是要不要检索医案的架构轴——两套定义
  除了都用字母 A-D 之外没有任何关系，`eval/ablation/__main__.py` 的
  docstring 里解释了两者用不同 CLI 入口以避免误用，但**任何一份未来的
  报告或 README 段落，如果只写"C 组怎样怎样"而不注明是 R38-C 还是
  R57-C，读者会读错**。这不是代码 bug，是文档风险，本轮没有引入任何
  强制消歧机制（比如统一改名成 `R57-A`/`R57-B`），留给未来一轮或写
  报告时人工留意（本报告全篇统一在 R57 上下文里省略前缀，R38 上下文
  单独成节，不会混用）。
- **80 次真机问诊本轮不跑**：如「五」所述，需要用户在自己机器上跑，本
  轮只交付了管道 + 命令 + 成本量级。

## 七、跟前后轮的边界

R57 不修改任何推导/验证/佐证逻辑，只加了一个新开关（`THEORY_LAYER`）和
一套消融脚本——这条开关本身默认 on，不影响 R51-R56 已经验收过的产品
默认路径。

## 八、自查闸门

`--backend fake` 全跑通（0 崩溃）；`content_metrics_valid` 字段在假后端
下正确报告为 `false`，没有拿假数据冒充真实内容指标——这是本轮唯一能在
沙盒里核实的闸门。

## 九、下一步

R58：等 R57 真机数出来、C 组过闸门之后，跑真机验收脚本，验证"这套已经
证明能不靠模仿推理的系统，端到端跑起来是否可用"。

---

# R58　真机验收

## 一、机制：验四件事，全部真实端到端，前置门槛是 R57 先过闸门

`scripts/acceptance_r58.py` 是这一轮改造交付前的最后一道闸门，读
`eval/report_ablation_r57.json` 的 `all_gates_passed`，读不到或者是
`false` 都不放行（除非显式加 `--skip-gate-check` 调试脚本本身）——R57
没过就没有 R58，这条脚本不是另一条独立判据。三分返回值：没跑 R57（找
不到报告文件）/ 跑了没过（`all_gates_passed: false`）/ 跑了过了，不是
裸 `False`。

## 二、怎么做的

1. 3 条主诉（`tests/queries.txt` 前三条）× 3 个角色（patient/doctor/
   researcher）= 9 次真实问诊——耗时/token/追问轮数/验证轮数，走
   `scripts/bench_consult.py::run_once` 同一套后端封装。
2. 按角色断言响应形状（`assert_response_shape`，逐字对照
   `api/main.py::_filter_response_by_role` 的实现，不猜它的行为）。
3. `cases.json` 原文摘录溯源（`audit_case_excerpt_grounding`）——扫真实
   发给模型的 system prompt，凡是引用医案的地方，摘录必须是那条医案
   `raw_excerpt` 的真实子串。
4. 5 张 1920×1080 真机截图 + 每张都用 `document.body.innerText` 扫一遍
   违禁词（词表唯一出处 `tests/test_no_demo_artifacts.py::BANNED`，不
   在这里另抄一份）。

## 三、数据出处核实：审计工具自身的正确性（本轮专项）

`audit_case_excerpt_grounding` 本身是一条防幻觉审计，它的正确性因此格外
要紧——判据本身错了，会把"系统没有问题"错判成"系统在编造"，也可能把
真的编造判成"过了"。这条工具第一次真的用真实 `cases.json` 跑
（`S3_MODE=structured`，1060 处医案引用，零 LLM 调用，`_format_case_block`
拼出的 prompt 直接送进审计函数，不需要真实模型作答）测出两个自己的
bug（见「四」）。修完两处之后：**1060 处引用、0 处编造**——这是审计工具
在真实数据上跑出的结果，不是任何一次真实问诊的结果（本轮没有真实 LLM
调用，`S3_MODE=structured` 只是被拿来构造出带医案引用的 prompt 样本）。

## 四、撞到的坑

**SOURCES.md 第 155 条**：一份"审计别的代码有没有幻觉"的新工具，第一次
拿真实数据跑之前，它自己的判据也可能是错的。

一、`CASE_BLOCK_RE` 最初写的是"原文：后面那一行"（`[^\n]*`，不跨行）。
但 `case.raw_excerpt` 天然可能带换行（方药另起一行是常见格式），正则只
抓第一行，天然比真实摘录短，1060 处引用**全部**被判成"摘录跟原文对不
上"——不是系统在编造，是审计工具自己把"我只抓了一半"读成了"抓全了、
对不上"。改成非贪婪匹配到下一个"结构化："为止。

二、修完第一处又暴露第二处：`raw_excerpt_by_case_id` 这份查表字典最初
只塞"有摘录的医案"，`_format_case_block` 对没有摘录的医案会诚实地写
"（原文缺失）"——这是正确行为，但审计工具的字典里找不到这条 id（不是
因为它不存在，是因为被过滤条件筛掉了），于是被判成"cases.json 里没有
这条医案 id"——44 条诚实的"没有摘录"被误判成 44 条"编造的 id"。修法是
字典覆盖全部医案 id（值允许是 `None`），审计函数内部把"id 不存在"和
"id 存在但没摘录"拆成两条独立分支——CLAUDE.md「三分返回值」纪律在这个
新场景下的又一个实例。

## 五、这一轮的数——沙盒只能验管道，真机数需要用户跑

沙盒无真实 LLM 后端，本轮在此环境下能核实的只有：

| 项 | 结果 |
|---|---|
| 审计工具自身正确性（真实 `cases.json`，零 LLM） | 1060 处引用、0 处编造（两处审计工具自身 bug 已修复，见「四」） |
| `--backend fake --skip-gate-check` 管道冒烟 | 脚本本身能跑通，9 次问诊全走通，角色裁剪判据对；截图这一步在假后端下会跳过（截不出有意义的内容） |
| 真机 9 次问诊 + 5 张截图 | **本轮未跑，需要用户在自己机器上跑** |

## 六、发现但未动

- **真机验收本身**：这是本节的主体内容，见文末「R58 真机验收：最小命令
  序列」一节——命令、环境变量、预计时长费用、输出位置、怎么看通过没
  通过，全部写清楚，不留占位符。
- **`test_theory_consistency_rules.py` 与本体/医理层组合场景**（R53 遗留，
  见 R53「六」）：R58 真机验收如果覆盖到这类组合场景会顺带验证，但本轮
  脚本的四件事里没有专门针对这一点设计断言。

## 七、跟前后轮的边界

R58 不引入任何新的产品逻辑，纯粹是"把前面七轮的成果接起来跑一次真实
端到端"的验收脚本——它发现的两个 bug（「四」）都在审计脚本自己身上，
不在被审计的生产代码里。

## 八、自查闸门

`tests/test_acceptance_r58.py` 用真实 `cases.json` 数据回归审计函数本身，
两处新发现的 bug 各自有专门的回归测试防止再犯。

## 九、下一步

八轮全部完成后，剩下的是用户在真机上跑出 R57 的 80 次问诊与 R58 的 9 次
问诊，产出这份报告目前唯二缺失的真机数字。

---

# 当前实测（撰写本报告时，HEAD=`02a13bb`）

- `git log --oneline -3`：`02a13bb` → `6ea907c`(R58) → `7104781`(R57)
- 全量测试：**4655 passed / 7 skipped**（两次独立全量跑数字一致）
- `ruff check .`：**0**（本次修复 2 处，见 `02a13bb`）
- `ruff format --check .`：331/342 会被重排——项目 `ruff.toml` 刻意只启用
  `E4/E7/E9/F`，不启用风格规则（配置里有注释说明原因），抽查一个本轮未
  改动的文件（`core/schemas.py`，最后改动是 R52）同样"Would reformat"，
  确认是仓库既有基线，不是本轮回归。

# 确认 `data/standard/tcm_theory.jsonl` 已 commit + push

```
$ git ls-files data/standard/tcm_theory.jsonl
data/standard/tcm_theory.jsonl
$ git log --oneline -1 -- data/standard/tcm_theory.jsonl
dc80163 R51：医理规则层——藏象/病机/治则/配伍四类规则，给演绎推导一个地基
$ git show origin/claude/tcm-demo-multi-round-hjp4q1:data/standard/tcm_theory.jsonl | wc -l
171
```

R51 落地时提交并推送，171 条与本地一致，落在 `.gitignore` 的
`!data/standard/*.jsonl` 例外范围内，`git pull` 即有，不需要重新生成。

# R58 真机验收：最小命令序列

见 `scripts/acceptance_r58.py` 与本报告 R57/R58 两节；完整可粘贴命令、
环境变量清单、预计时长费用、输出位置与判读方式已在聊天回复里给出（篇幅
原因不重复贴在本文件——本文件是逐轮机制与数字的记录，命令序列是操作
手册，两者读者不同，放在一起会让本文件失去"一轮一份记录"的结构）。
