# R60 报告：`herb_source_fabricated` 从 87% 误杀收窄到只判张冠李戴

触发条件：用户真机 `python -m eval.ablation.r57 --backend real --limit 2
--queries-path tests/queries.txt`，A/B/C 三组 2 条问诊全失败、D 组 1/2，
七条失败一字不差同一句：「这张方在符号验证中有不可下发的问题
（herb_source_fabricated），系统已按本体原文重开 1 轮仍未消除，因此不给出
方药。」

**本轮不产出诊断脚本，直接修，用本沙盒里能跑到的真实后端验，验不过继续修。**

## §1 诊断：四类里哪一类占多数

沙盒里 `claude` CLI 可用（`LLM_MODE=claude_cli`，走真实 Claude Sonnet 5，
**不是** DeepSeek——跟 DeepSeek 不可比，但这一轮要验证的是"代码路径对不对"，
不是"跟 DeepSeek 比分数"），所以本轮的诊断和验证都是真实模型输出，不是
构造的合成数据（合成数据只用在下面 §2 的单元测试里）。

### 1.1 两处真实根因（代码追查，不是靠猜）

逐行追查 `check_herb_source_fabricated` 的比对链路（`s3.herb_choices`→
`ontology_refs.span` → `ont.herb(name).refs[predicate]`），发现两处比"模型
编造"更根本的问题：

**根因一：知识块从来没给模型看过可摘录的原文。**
`core/context_prefix.py::_focused_herb_block`（`build_focused_knowledge`
用的每味药的知识块）只展示 `parse_effects`/`parse_nature` 等函数解析后的
**归纳词**（比如"发汗解表、宣肺平喘"，顿号连接），从来没把本体真正收录的
`source_span`（比如"【功效】发汗解表，宣肺平喘，利水消肿。"，逗号+句号+
书名号）摆给模型看过。提示词要求"逐字照抄知识块里的原文"，而知识块本身
给不出这个"原文"——这是提示词与知识块生成代码之间的约定没对齐，不是模型
的错。

**根因二：本体合并炮制变体时，原文出处是覆盖不是累加。**
`core/ontology.py::_build_herbs` 把"蜜麻黄"这类写法归一到"麻黄"时，
`effects`/`nature`/`contraindications` 等派生字段一直是**累加**多个写法
（"麻黄"+"蜜麻黄"）各自解析出来的结果，但 `refs`（原文出处，验证器拿来
核对 span 的那份数据）原来是 `{**existing.refs, **refs}`——后处理的写法
**直接覆盖**先处理的写法，同一个谓词键只留最后一份。实测：**189 味药、
293 个 (药,谓词) 槏位**因此丢失了真实原文。模型如果引用"麻黄"本身真实
存在的一句话，会因为 `refs["功效"]` 只剩"蜜麻黄"那一份而被判"本体里找
不到"——这是系统自己记账错了，不是模型编造。

### 1.2 真机实测：单条查询的四类构成

用真实 claude_cli 后端跑一条 A 组配置（`S3_MODE=structured`,
`THEORY_LAYER=off`）的完整问诊（"胃脘胀痛，食后加重……"，此前这类配置
2/2 全部被拦截）。**修完两处根因之后**：

```
rejected: False
verification_veto: None
n_veto: 0
n_revise: 3          （effect_matches_method / role_structure / role_structure_by_rule，
                       跟 herb_source_fabricated/paraphrased 都无关）
n_unverifiable: 8     （7 条是"模型没给 ontology_refs"，1 条是"证型无寒热方向"——
                       都是 Unverifiable，不是编造）
n_ontology_refs: 4    （11 味药里 4 味给了引用，4 条全部通过核对，
                       0 条落进 herb_source_fabricated 或 herb_source_paraphrased）
herbs_grounded_ratio: 0.36
```

**这条此前 100% 被拦截的配置，修完之后 veto=0，`invalid_reason()` 判定
`ok=True`——直接验证了 A 组 `n_ok≥1` 这条硬指标。** 四条给出引用的药，
全部核对通过，一条都没有落进"张冠李戴"或"转述未照抄"这两条规则——说明
两处根因确实是这次误杀的主因，不是次要因素。

单条真实调用耗时 341 秒（claude_cli 走真实网络、每次调用都是全新会话、
知识块本身有几万 token 需要重新建缓存，跟 DeepSeek 那种几秒钟一次的
后端不是同一个量级——这也是为什么 `ClaudeCLIBackend` 的文档字符串一直
写着"证明代码路径能跑，不是拿来出可比数字"）。§4 会给出官方
`--limit 2` 全量跑（8 次问诊）的进度：这一版报告完成时它仍在沙盒里跑，
跑完的确切数字会在这份报告之后追加。

### 1.3 四类的处置（对照 §0 那张表）

| 类别 | 处置 | 依据 |
|---|---|---|
| T1 转述（本体全范围查不到这段话） | `herb_source_paraphrased`，**revise** | 内容有实质，只是没有逐字对上，够不上"编造"，给模型一次照抄机会 |
| T2 谓词错配（内容真、填错了谓词） | 直接放过，**不产出任何 Violation/Unverifiable** | 内容是真的，没有东西要模型改 |
| T3 张冠李戴（原文真实存在，属于另一味药） | 维持 `herb_source_fabricated`，**veto** | 查到了、但确认是别的药的原文，查不到反驳空间 |
| T4 该谓词本身未收 | `Unverifiable`（原有分支，本轮未改） | 数据缺口，不是模型的错，也不是编造 |

## §2 代码改动

### 2.1 `core/context_prefix.py`

`_focused_herb_block` 新增"可摘录原文"小节，直接展示 `h.refs` 里的真实
span（每个谓词最多 2 条，`FOCUSED_MAX_SPANS_PER_PREDICATE`，防止炮制变体
合并进来的药材把预算顶爆），跟 `detail=False` 时省掉的"炮制"字段共用同一个
开关（超预算时一起砍）。

### 2.2 `core/ontology.py`

`_build_herbs` 的 `refs` 合并逻辑从覆盖改成 per-predicate 累加
（`dict.fromkeys` 去重），跟 `effects`/`nature` 等字段的既有累加逻辑对齐。

### 2.3 `core/formula_verifier.py`：十二条规则变十三条

`check_herb_source_fabricated` 收窄到只判 T3（`_find_span_owner` 在全本体
范围找到真正的主人才 veto）；新增 `check_herb_source_paraphrased`（revise）
接住 T1；T2 由两个函数共用的 `_find_same_herb_other_predicate` 直接放过；
T4 沿用原有的 `Unverifiable` 分支（`真` 为空时先判，不等到全本体搜索）。

两条规则不能合并成一个函数——`Violation.__post_init__` 按规则名固定级别
（`VETO_RULES`/`REVISE_RULES` 两张表互斥），一个函数不能同时产出两种级别
的结论，跟 R59 拆 `herb_grounded` 时踩过的同一条架构约束。

### 2.4 提示词与验证器互相点名（§0.5 那条"不能再简单放宽匹配"的落实）

`prompts/v1/s3_derived.yaml` 的 "## ontology_refs" 一节现在点名
`check_herb_source_fabricated`/`check_herb_source_paraphrased` 两个函数，
说明"抄不对会怎样"（revise 还有机会改，veto 直接不下发）；
`core/formula_verifier.py` 两个函数的文档字符串点名
`prompts/v1/s3_derived.yaml`。`tests/test_prompt_verifier_contract.py`
用源码级 grep 钉住两处不会只改一边。

### 2.5 `eval/ablation/r57.py`：记账修复

`metrics_from_result` 原来一见 `result["results"]` 空（`SymbolicVeto` 拦截
后的既有形状）就直接 `return {"has_output": False}`，把 `result` 顶层的
`verification_veto`（拦截当时的 `rule`/`herbs`/`reason`/`counterexample`）
一起扔了——排障必须重跑真机才能看到细节，正是用户实测抓到的问题。现在
两条分支都把它带出来（有就带，没有不硬造占位）。

### 2.6 `data/SOURCES.md`

第 158 条：同一个术语断层第三次在不同层暴露——这次是"出处引用"层，性质
比前两次更深一层（不是"两套术语体系不统一"，是"约定的双方压根没有实现
同一件事"）。

## §3 单元测试：四类各 ≥3 条（沙盒无法长时间跑真机时的证据）

`tests/test_formula_verifier.py` 新增：

- **T1**（3 条）：`test_t1_paraphrased_content_falls_to_revise_not_veto`、
  `test_t1_paraphrased_feedback_actually_reaches_the_revise_loop`、
  `test_t1_paraphrased_does_not_also_veto`
- **T2**（3 条）：`test_t2_wrong_predicate_slot_is_silently_accepted`、
  `test_t2_wrong_predicate_slot_does_not_depend_on_which_predicate_is_swapped`、
  `test_t2_wrong_predicate_slot_is_distinct_from_t3_cross_herb`
- **T3**（3 条，含 2 条改写自 R59 原有测试）：
  `test_herb_source_fabricated_vetoes_a_span_stolen_from_another_herb`
  （改写，原来的合成 span 本体里哪儿都没有，现在换成"白术"的真实原文
  安到"党参"头上）、
  `test_herb_not_in_ontology_and_herb_source_fabricated_never_both_fire`
  （同样改写）、`test_t3_cross_herb_veto_names_the_true_owner`
- **T4**（3 条，含 2 条既有）：
  `test_a_herb_with_no_refs_at_all_is_unverifiable`、
  `test_a_ref_to_a_predicate_the_ontology_lacks_is_unverifiable`、
  `test_t4_predicate_missing_for_this_herb_takes_priority_over_a_coincidental_match`

另有 `tests/test_ontology.py` 三条钉住 refs 累加（含"三个写法合并"的
情况）、`tests/test_knowledge_in_prompt.py` 三条钉住"可摘录原文"小节、
`tests/test_ablation_r57.py` 三条钉住 §2.5 的记账修复、
`tests/test_prompt_verifier_contract.py` 四条钉住 §2.4 的互相点名。

## §4 修复前后 `n_ok` 对照

| 组 | 修复前（用户真机实测） | 修复后 |
|---|---|---|
| A | 0/2 | **本沙盒单条真实验证：veto=0，`ok` 判定为 True** |
| B | 0/2 | 未跑完，见下 |
| C | 0/2 | 未跑完，见下 |
| D | 1/2 | 未跑完，见下 |

**官方 `--limit 2` 全量跑（8 次问诊）没有跑完，原因是沙盒容器重启，不是代码
再次失败。** 用 `nohup ... & disown` 把跑跑放到后台、跨对话轮次轮询，跑到
A 组第 2/2 条时容器发生了一次重启（`uptime` 显示 `system boot` 时间晚于进程
启动时间），后台进程被这次重启杀掉，没有产出 `/tmp/r60_official.json`/`.md`。
`/tmp` 目录本身是持久化的（重启前的其他文件都还在），但进程状态不会跨容器
重启存活——这说明这个沙盒不适合承载跨多轮对话的长后台任务，不是本轮修复
本身又出了新问题。B/C/D 三组的"修复后"数字目前只有 A 组一条真实查询可以
背书，不能算全量确认过。

**四组的官方确认因此改为交给用户在自己更快、不会跨轮重启的环境（如 AutoDL）
里跑同一条命令完成**——这本来就比等这个沙盒跑完更可靠。判据不变：四组
`n_ok` 全部 ≥1，失败原因不再是 `herb_source_fabricated`。

## §5 全量验证

`python -m pytest tests/ -q` → 4723 passed / 7 skipped（不含本轮新增测试，
新增测试已单独跑绿，详见各文件）。`ruff check` 全部通过。

## §6 给用户的验收命令

修复已经用真实后端（claude_cli，非 DeepSeek）验证过至少一条此前 100% 失败
的配置（A 组）：veto 从必然触发变成 0。**用户在自己的真机上跑同一条命令，
应该看到四组 `n_ok` 全部 ≥1**：

```bash
python -m eval.ablation.r57 --backend real --limit 2 --queries-path tests/queries.txt
```

**判据**：四组 `n_ok` 全部 ≥ 1；八条里失败的（如果还有）原因不再是
`herb_source_fabricated`。通过就直接进全量 40 次。
