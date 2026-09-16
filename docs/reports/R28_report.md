# R28 报告：上机顺序与检索模式钉死

**基线** `d617606`（R24 补丁结束）。这一轮修的是一条**会让人按错的配置花掉十倍钱、
并且让最贵那笔白花**的链子，三件事必须一起改：

1. `effective_mode()` 的默认值在 R21 被换成 `full_context`；
2. 剧本里 `RETRIEVER_MODE` 只在段 9 内部出现过一次，段 4/6/7/8 **没有任何一段钉模式**
   ——它们继承新默认，而两套单价差 **29 倍**（top3 ¥0.0055/次 vs full_context ¥0.16/次），
   段 7 的 1200 次于是从清单标的 ¥6.6 变成约 **¥192**；
3. fixture 的键是 `sha256(system)`，full_context 下 S3 的 system 含 18 万 token 前缀
   ——段 6 在新默认下录完，一旦段 9 闸门不过、按脚本提示退回 hybrid，
   **278 条 fixture 一条都命中不了**，演示保险归零。

**三处都不会报错。** 这一轮把它们变成会报错的东西。

---

## 一、做了什么（逐项 ✅/❌）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| A1 | 段表加 `order`，执行序与段号解耦（段号不动） | ✅ | `SEGMENTS` 七格、`segments_in_execution_order()` |
| A2 | `--dry-run` 同时打段号与执行序，段 9 标闸门 | ✅ | `print_plan()` |
| A3 | 闸门决定写进状态文件，后续段启动时打印当前口径 | ✅ | `record_mode_decision` / `mode_decision` |
| B4 | 每段钉死 `mode`，启动前 export（`n/a` 是 **unset**） | ✅ | `apply_retriever_mode()` |
| B5 | 成本按每段自己的模式取单价，分段列出 + 总计 | ✅ | `onsite_plan.unit_price_cny` / `segment_cost_cny` |
| B6 | `MATERIALS.md` 与上机清单的成本表按模式重算 | ✅ | 两套单价表 + 每段模式 |
| C7 | `fixtures/_manifest.json` 记录录制模式，回放启动即比对 | ✅ | `core/llm_replay.write_manifest` / `require_matching_mode` |
| C8 | 段 6 说明写明"产物与模式绑定、必须在段 9 之后"，`--dry-run` 黄字警告 | ✅ | 段表 note + `print_plan` |
| D9 | 报告里"几份文档"改成量出来的凭据记号 | ✅ | `bench.n_checked_docs` / `bench.check_exit_code` |
| 10 | 同名证候的病名限定（label + tooltip + 问诊图） | ✅ | `_display_label` / `describeNodeTooltip` |
| 11 | SOURCES 第 71、72 条 | ✅ | via_syndrome 聚合、默认值改动的涟漪 |
| — | 顺带修掉：`run_segment` 的 case 分支**没有段 10** | ✅ | 段 10 原来什么都不跑然后报退出码 0 |

**❌ 没有一项。**

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **3022** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R28` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **44**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑 |
| 凭据核对 | **12** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **20** 种全过 | `python -m scripts.screenshot_states` |

- 测试 passed **3022** —— `bench/rounds/R28.json:round.R28.pytest_passed=3022`
- 测试 skipped **10** —— `bench/rounds/R28.json:round.R28.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R28.json:round.R28.pytest_failed=0`
- 全量测试墙钟 **93.6** 秒 —— `bench/rounds/R28.json:round.R28.pytest_wall_s=93.6`
- Playwright **20** 种全过 —— `bench/rounds/R28.json:round.R28.playwright_states_passed=20`
- 凭据核对 **12** 份文档 —— `bench/rounds/R28.json:round.R28.n_checked_docs=12`

**「几份文档」这个数从这一轮起是量出来的。** 以前它手写在每轮报告里
（R25 写 9、R26 写 10、R24 补丁写 11——而当时实际已经是 12）：一个每轮都会变、
又没有任何东西盯着的数，手写必漂。现在它跟测试条数、Playwright 种数同一个待遇。

**12 这个数量在本报告落盘之前。** 本文件自己也被 `--check` 扫，所以此刻现量是 **13**
——跟 passed 数同一个口径（每轮的数量在自己那份报告写完之前），下一轮重量自动追上。
不是漂移：漂移是没人量，这里是量过、时点在报告之前。

**新增测试 51 条**（R24 补丁的 2971 → 3022）：`test_onsite_order_and_mode.py` **24** 条、
`test_fixture_mode_binding.py` **10** 条、`test_syndrome_disease_label.py` **14** 条，
另 3 条落在既有文件（resume 多一条执行序判据、collect_results 多一条、参数化多一份）。
`core/schemas.py` 一个字没动，44 处 `min_length=1` 原样。
跟 R25/R26/R24 补丁同一条规矩：**条数在它自己的报告落盘之前量**，写完这份之后是 **3023**（本文件给参数化的 `--check` 用例又添了一份），
所以 R29 的基线是 3023 不是 3022。

---

## 三、先红后绿：修复前的断言原文

两个新文件加起来 **29 条全红**（24 + 5），红法分三种：

```
# tests/test_onsite_order_and_mode.py（24 条里 23 红、1 过）
E   ImportError: cannot import name 'parse_segments' from 'scripts.onsite_plan'
E   ImportError: cannot import name 'SEGMENT_MODES' from 'scripts.onsite_plan'
E   assert 'segments_in_execution_order' in '#!/usr/bin/env bash\n# 上机剧本…'
E   AssertionError: assert '执行序' in '…段  名称   预估调用  人工卡点  说明…'
E   AssertionError: assert '闸门' in '9   R21~R24 的上机项   320  no   前缀规模 0 + …'

# tests/test_fixture_mode_binding.py（10 条，收集期就炸）
E   ImportError: cannot import name 'MANIFEST_FILENAME' from 'core.llm_replay'
```

唯一一条修复前就绿的是 `test_the_zero_call_segments_still_run_first`
——段 0/1/2 本来就排在最前，这一轮没动它。**一条本来就绿的判据留在文件里是对的**：
它守的是"重排执行序时别把免费的段挪到后面去"。

---

## 四、`--dry-run` 的输出（验收物）

```
段号 执行序 模式          名称                     预估调用  人工卡点
0    0      n/a           环境自检                        0  no
1    1      n/a           零调用的验证                    0  no
2    2      n/a           本地模型                        2  no
9    3      full_context  R21~R24 的上机项              320  no     ← **闸门**
3    4      top3          R1 前提：role 填充率           60  YES
4    5      top3          R1 验收：噪声地板 ε           212  no
5    6      n/a           药理层抽取                   2181  YES
6    7      top3          录制回放                      278  no     ← 产物与模式绑定
7    8      top3          全套评测重跑                 1200  no
8    9      top3          性能基准                       38  no
10   10     n/a           R26 蒸馏（可选）              350  YES
--------------------------------------------------------------------------
**按模式分开算**（两套单价差 29 倍，加在一起的总数谁都对不上）：
  top3            1788 次 × ¥0.0055   ≈ ¥9.8
  full_context     320 次 × ¥0.1597   ≈ ¥51.1
  n/a             2533 次 × ¥0.0055   ≈ ¥13.9
  合计 4641 次 ≈ ¥74.8（高峰价；谷段五折）

执行序（不是段号）：0 1 2 9 3 4 5 6 7 8 10
⚠ 段 9 的闸门还没跑过：现在去跑段 6（录制）有白录的风险——
  fixture 的键含 system 全文，模式一变 278 条全部不命中。先跑段 9。
```

看得到的三件事：**执行序里段 9 在段 3 之前**、**每段的模式**、
**按模式分开的成本合计**。最后那两行黄字只在段 9 的闸门还没跑过时出现。

**顺带修正了一个一直偏低的总数**：R24 补丁那版清单说"全部跑完 ≈¥25.5"，
按模式重算是 **¥74.8**——差的那 ¥49 几乎全在段 9（full_context 的 320 次，
按旧均价算是 ¥1.8，按真实口径是 ¥51.1）。

---

## 五、每项改动的验证方式与结果

| 改动 | 怎么验 | 结果 |
|---|---|---|
| 执行序与段号解耦 | `order` 必须是 0..N 的**排列**（重复/跳号 = 有段永远不跑） | ✅ |
| 段 9 提前 | 断言它排在 3/4/5/6/7/8/10 **每一段**之前，不是只比段 3 早 | ✅ |
| 段号没动 | 段号仍是 0..10 连续；`--only`/`--from`/`--resume` 仍按段号寻址 | ✅ |
| 旧状态文件 | 写一份只有三列的旧状态文件，`--status` 照常读、`--resume` 照常算起点 | ✅ |
| `--from` 的语义 | 跳过判据从"段号 < FROM"改成"**执行序** < FROM 的执行序"——按段号跳会把已经跑过的段 9 再跑一遍 | ✅ |
| export 真的发生 | 把 `apply_retriever_mode` **抠出来单独跑**：top3→hybrid、full_context→full_context、n/a→**unset** | ✅ |
| 两套单价一处定义 | bash 的**代码行**里不许出现 `0.16` / `0.0055`（注释里解释可以） | ✅ |
| full_context 单价的来历 | 等于 `core.usage.cost_cny(hit_tokens=18万, out_tokens=3600, peak=True)` | ✅ |
| 段 7 换模式后跳变 | 断言 `dear/cheap == 单价之比` 且 > 20 倍——**不写死 ¥192** | ✅ |
| 闸门决定落盘 | 抠出 `record_mode_decision` 跑一遍，状态文件里出现 `#retriever_mode_decided` | ✅ |
| fixture 模式绑定 | 造一份 manifest 写 full_context、当前设 hybrid → `LLMError`，消息里两个模式都在 | ✅ |
| 回放后端启动即炸 | `ReplayBackend(d).fixtures` 直接抛，不是等某一条未命中 | ✅ |
| 缺 manifest 不拒绝 | 只往 stderr 提示一句 + 给 `--write-manifest` 命令 | ✅ |
| 段 10 真的被调 | `run_segment` 的 case 分支逐个查 `seg_0..seg_10` | ✅ |
| 病名限定 | 66 个重名节点的 label **两两不同**；搜 `name` 仍命中全部同名条目 | ✅ |

---

## 六、无法完成项（⏳ 上机）

| # | 项 | 为什么沙盒做不了 | 上机命令 |
|---|---|---|---|
| 1 | 闸门兜底真的触发一次 | 要真实 API 跑 E3/E4 | `bash scripts/run_onsite.sh --only 9` |
| 2 | 按新执行序真跑一遍 | 每一段都要真实 key / GPU / 语料 | `bash scripts/run_onsite.sh`（会先跑 0/1/2，再跑段 9） |
| 3 | fixture 在 hybrid 下重录 + manifest | 要真实 API（278 次） | 段 9 定下模式之后 `bash scripts/run_onsite.sh --only 6` |
| 4 | 段 7 在 top3 下的真实花费（验 ¥9.8 那个口径） | 要真实 key | 跑完看 `data/usage.jsonl` |

---

## 七、发现但未动 / 发现并当场修了

1. **当场修了：段 10 从来没被执行过。** `run_segment` 的 `case "$n"` 只到 9，
   段 10 落进空分支、退出码 0——**一个"跑完了"的假绿**。读代码时发现的，
   不是任何判据发现的（那时的判据只查"段表里有没有段 10"）。现在逐个查 `seg_0..seg_10`。
2. **当场修了：`n/a` 段的单价一开始写成 0。** `--dry-run` 一跑就看出来了：
   段 5（药理层抽取 **2181** 次）的检索模式确实是 n/a，按 0 算清单会说
   "全剧本最贵的那一段不要钱"。**"不走检索层"和"不花钱"是两件事**——
   前者说输入怎么拼，后者说调不调模型。价格因此按"带不带知识前缀"分档，
   不按检索模式分档。
3. **当场修了：模式钉不上时早退会让状态文件缺一行。** 第一版写的是
   `apply_retriever_mode … || return 0`，而那条路径跳过了 `record_segment`
   ——下次 `--resume` 会把这一段当成"跑过了"。现在钉不上就记一个非 0 退出码。
4. **发现并当场处理：病名限定让图谱浏览器压字。** 加了病名之后证型标签最宽
   **169px**（原来约 100），rings 判据当场红。两步处理：病名**另起一行**
   （宽度回到 124px），展开上限从 **20 降到 14**——标签变大，同一块画布放得下的
   就是变少了。**宁可少画也不压字**，少画的那些状态栏照旧报"还有 47 个，搜索直达"。
5. **发现但未动：15 组证候在加了病名之后仍然重名，而它们是数据缺陷。**
   TB-114「热证（胃痛）」的定义是「肝郁化火，横逆犯胃」（那是肝胃郁热证），
   TB-115 同名同病的定义却是「脾胃虚寒，胃失和降」——**脾胃虚寒挂在"热证"名下**。
   显示层能做的是不装作它们一样：这 15 组再补一个 code（`热证（胃痛 TB-114）`）。
   `test_the_still_ambiguous_pairs_are_a_data_defect_not_a_display_bug` 钉住"15"这个数
   ——有人修好解析器之后它会红，那时要更新的是这一节和那条注释，不是删判据。

   > **R29 更正（2026-09-17）。** 这一条原来把根因写成"教材解析时名字列跟定义列
   > 错位"。R29 逐条打出来之后，那个描述**既不完整也不准确**——不是两列错位，
   > 而是四类不同的东西，而且其中两类本来就该被 OCR 修正表修掉：
   >
   > | 类别 | 例子 | 机制 | R29 的处理 |
   > |---|---|---|---|
   > | 编号标题掉了行首 `#` | TB-114/115/116 都叫「热证」 | `_SYN_HEADING_RE` 锚在 `#` 上，认不出原文第 3001 行的 `7.痰火扰心`，上一条的名字被沿用（实测：带 `#` 的编号标题 237 个、不带的编号行 934 个） | 报进「可疑条目」，**没改解析器**（934 行里绝大多数是正文段落，放宽锚点要另有判据） |
   > | OCR 形近字 | `disease=黄疽`、`name=温疤证` | 扫描误识。全文「疸」0 次「疽」109 次、「疤」153 次「瘢」0 次 | **已修**：`ocr_fixes.tsv` 加 `scope` 列，「疽→疸」限 disease、「疤→疟」限 name |
   > | 教材原文自己掉字 | `disease=逆`（第 5968 行）、`name=阻心脉证`（第 2981 行）、`disease=闭`（第 8737 行） | 原文就没有那个字，不是切分吃掉的 | 报进「可疑条目」，**不自动改**——补哪个字要靠语义猜 |
   > | 名称错切/合并 | `（6）哮喘脱证`（第 1781 行） | 同上 | 同上 |
   >
   > R29 重新生成证候表与图谱之后这个数**仍然是 15**（jsonl 层的重名组数从 30
   > 降到 28，但那两组落在图里已经合并过的节点上）。判据照旧钉 15，
   > 措辞改成「只能降不能升」。详见 `docs/reports/R29_report.md`。
6. **发现但未动：`_baseline.json` 和 `_manifest.json` 各写各的。** 两份都是录制附带的
   元文件，都放 fixture 目录、都以下划线开头、都被 `_load` 跳过。合并成一份更"干净"，
   但它们回答的不是同一个问题（一份是"录出来的结论长什么样"、一份是"这批能不能在
   当前配置下放"），而且合并要动 R10 那套逐字节比对。留着，各自的注释里写清区别。
7. **发现但未动：`--only` 仍然可以绕过执行序。** `--only 6` 会在段 9 没跑过时
   直接录 fixture。没有拦——`--only` 的存在意义就是"我知道我在干什么"。
   但 `--dry-run` 的黄字警告会在那时出现，而且录完的 manifest 会记下当时的模式，
   模式不对时回放启动就炸。**三道提示，不加第四道锁。**

---

## 八、13 条自查

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template` | ✅ 本轮没动任何 prompt |
| 2 | LLM 输出全部用 pydantic 承接 | ✅ 新增的 `FixtureSetManifest` 也是 pydantic（跨进程读回来的数据） |
| 3 | `Field(min_length=1)` 没有放松 | ✅ **44** 处，`core/schemas.py` 一个字没改 |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新数据文件（`_manifest.json` 是 fixture 目录的元文件，跟 `_baseline.json` 同类） |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ `require_matching_mode` 在 `_load` 里调，manifest 读在用的时候 |
| 6 | S1 全局只跑一次 | ✅ 没动推理链 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 成本按两套单价分开列并各自写明来历；「几份文档」改成量出来的 |
| 8 | 同一概念只有一处实现 | ✅ 段表解析（`parse_segments`，四个测试文件共用）、模式映射（`retriever_mode_for`）、两套单价（`unit_price_cny`）、闸门阈值（`GATE_OUTPUT_CHANGE_RATE`）、当前模式（`effective_mode`）各一处 |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ 没有新的标识符入口 |
| 10 | 安全否决在 S2 之前 | ✅ 没动 |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ 48 条新判据：bash 片段单独跑、假 DOM、临时目录，零网络 |
| 13 | 改了按注册表/图层循环渲染的东西 → 跑 Playwright | ✅ **这一轮它第三次抓到东西**：病名限定让标签变宽、20 个摆不下（压字 2~4 对、字缩到 11.9px），展开上限因此降到 14 |

---

## 九、下一步

这一轮是**跑 `run_onsite.sh` 之前的前置**，做完了。上机清单本身没变，
变的是它的顺序和每段的口径：

- **A 组（不花钱）之后紧接着跑段 9**（`bash scripts/run_onsite.sh --only 9`）：
  它决定 full_context 还能不能当默认，闸门不过要 `export RETRIEVER_MODE=hybrid`，
  而那之后每一段的成本口径、段 6 录的 fixture 全都跟着变。
- **段 6（录制）必须在段 9 之后**：fixture 的键含 system 全文，模式一变全不命中。
  现在录完会写 `_manifest.json`，回放启动时模式对不上会当场报错而不是演示到一半。
- 其余清单项（教材五本、药理层、LoRA、MES 盲评）见 `docs/reports/R26_report.md`
  第六节与 `docs/R11-R19_report.md` 第四节。
