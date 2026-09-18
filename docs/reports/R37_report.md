# R37 报告：单链九段 —— 以及四个"判据全绿、人看不见"的洞

**基线** `efa926a`（R36 推理提速）。

这一轮把 structured 模式的结论从"三列集注里只剩一列"改成**一条九段的推理链**，
再加上点词看释义、单链图、三种分辨率验收。但这份报告真正值得读的是第四节：
**本轮新写的判据全部通过，而四张单链截图是空白的**——四个洞都是"代码跑过了、
人看不见"，全靠把数据真的喂进浏览器才露出来。

| # | 做了什么 | 实测 / 对照 |
|---|---|---|
| 1 | 单链九段（`renderChainFlow`，①主诉…⑨校验与出处） | 三种分辨率各验一次，横向溢出 **0**、正文字号 **≥12px** |
| 2 | 三列集注**保留**在 legacy 这一支 | R38 的消融要用它当对照组 |
| 3 | `GET /api/node_explain`：四节释义，**零 LLM** | 27 条测试；取不到时整块隐藏，不摆"暂无信息"的空壳 |
| 4 | 单链图（一个证型 → 一个方剂 → 药材是 compound 子节点） | Playwright 真渲染：层 2/3 各 1 个、层 4 全有 parent、两两不重叠 |
| 5 | 流式渲染节流 **≥50ms** | rAF 在 120Hz 上是 8ms，节流必须自己写 |
| 6 | label 宽度与字号**绑到槽位** | `--label-max-consult` 120px / `--label-max-browser` 84px |
| 7 | 重名证型追加证候编码 | 判断一处（`ambiguous_syndrome_pairs` + `syndrome_code_suffix`），两张图共用 |
| 8 | 取消按钮 + 300 秒空闲兜底 | 真浏览器验"跑起来之后可见、可点、不禁用" |
| 9 | 一次展开上限 14 → 20：**量了，不行，退回 14** | 见第五节（这一项是本轮唯一被实测否掉的计划项） |

Playwright 状态 **21 → 29**（+8）。新增测试 **47** 条：`test_node_explain.py` **27**、`test_chain_flow_frontend.py` **19**，
外加凭据核对多认一份文档（本报告自己）。全量 **3513 → 3560**。

---

## 一、九段是什么，为什么是九段

R33 把五位医家融合成一份结论之后，界面上那"一列"越来越像一个被撑坏的卡片：
证型、治法、方剂、药味、验证、归因全塞在一列里，而它们本来是**有先后的一条链**。
九段照申报书 2.1 的五步链排，前面补三段（主诉/证素/追问）、后面补一段（校验与出处）：

```
① 主诉与标准化症状   ② 证素   ③ 追问        ← S1 / S2
④ 病变脏腑   ⑤ 证型   ⑥ 治法   ⑦ 方剂   ⑧ 药物组成   ⑨ 校验与出处   ← S3
```

段与段之间那条竖线不是装饰：`S3Structured` 里 `from_organs` / `from_syndrome` /
`from_method` 这些字段就是"上一段的结论是下一段的输入"，界面把它显示出来。

**链顶必须写"这是谁的结论"**：三列有列头，单链没有——而「五家综合」和
「叶天士一家」是两种完全不同的东西（structured 给综合，legacy 单跑一家给那一家）。
旁边那行「本次引到 N 位医家的医案」按 `physicians_cited` **照实数**，
不拿"五家"这个名字当数（名字是配置，数是这次真跑出来的）。

---

## 二、点词看释义：零 LLM，四节，取不到就整块消失

`core/node_explain.py` 不调模型，只查已有的四个来源（本草/方剂本体、证候表、
医案三元组、当次结论），按固定四节拼：**是什么 / 出处原文 / 名医怎么用 / 注意**。

两条不许省的：

1. **每一节都要能回指**。没有出处的那一节不显示——一条查不到出处的解释在这个
   项目里等于幻觉。
2. **取不到时整块隐藏**，不显示"暂无信息"。空壳会让人以为系统"查过了、没有"，
   而实际是"这个节点压根没接这一层"。

节点 id 的形状是 `herb::{医家}::{方名}::{药名}` 这种，接口对 `node`/`name` 都有
**200 字上限**——不是怕慢，是怕错误信息里被塞长串（同 `MAX_COMPLAINT_CHARS`）。

---

## 三、这一轮真正的产出：四个"判据全绿、人看不见"的洞

按发现顺序。四个都不是想出来的，是**把真数据喂进真浏览器**之后掉出来的。

### 洞一：两个隐藏开关叠在一起，`!important` 那个永远赢

`index.html` 写的是 `class="is-hidden"`，`app.css` 里另有 `.show` 那一套。
`showChainFlow()` 加 `.show`，没人摘 `is-hidden`（`display:none !important`）。
于是：**四张分辨率截图全是空白，而判据全绿**——判据查的是 `classList.contains('show')`、
`.chain-sec` 有几个、`getComputedStyle(...).fontSize`，这三样在 `display:none`
的元素上全都返回正确值。

同一症状的第二种形状：这条链最初写在**默认折叠**的 `<details id="detail-zone">` 里，
而 structured 模式下三列是空的——折起来等于"跑完什么都看不见"。

改法治因也治判据：一个块只留一个开关、结论区移出折叠区；判据改成量几何——
`offsetParent !== null`、整块 ≥100px、每段 ≥20px、祖先不许有 `<details>`。

### 洞二：模式是猜的，条数是事实

`isSingleChain(manifest)` 在 manifest 没带 `s3_mode` 时回落到 `/health` 报的
服务端默认，而 `renderChainFlow()` 只读 `results[0]`。于是一份**三位医家**的响应
（回放的老 manifest、截图 fixture 都不带 `s3_mode`）在 structured 服务上会被画成
单链，另外两位的结论**静默消失**。

它是被全量截图**跑崩**才露出来的：29 种状态跑到第 14 种 `doctor_conflict` 时
`Page.evaluate` 抛 `Cannot read properties of null`——那条判据在找
`.col[data-physician="ye_tianshi"]`，而三列已经被清空了。

改法：形状判断（`isSingleChainResult(data)`，条数 >1 就一定不是单链）与模式判断
（`isSingleChain(manifest)`）分成两个函数，各答各的问题。

### 洞三：展示层漏 id——这次不是模型看不懂，是评审看不懂

第一版单链截图上同时印着 `ye_tianshi`、`modified`、`high`、`meridian_coverage`；
判据补上之后又从截图上抓出两处：`max_rounds`（追问停因）和 `formula`/`herbs`
（五家影响作用在哪一步）。**六处根因不同，形状一样**：中文名的来源跟用它的地方
之间隔着一次会落空的查表。改法一律是"每种 id 在边界上解析一次"：

| id | 原来 | 现在 |
|---|---|---|
| 医家 | 每次问诊清空重填的 `PHYSICIAN_NAMES`，structured 下只有 synthesis | `physicianName()` 两级回落（本次结果 → `/health` 注册表 → id） |
| 方剂来源 | 图谱 tooltip 里一个内联字面量 | `formulaSourceLabel()`，放在 graph.js（依赖方向只能 app→graph） |
| 置信度 | 直接印英文 | `confidenceLabel()` 一处 |
| 验证规则 / 结论 | 前端根本没有中文名 | 后端随结论下发（`RULE_LABELS` / `STATUS_LABELS`，跟 `ALL_RULES` 放一起） |
| 追问停因 | 结论区和进度日志两处都印 id | 后端下发 `stopped_by_label`（`STOP_LABELS` 跟那个 Literal 放一起），两处都用 |
| 影响作用在哪一步 | 印 `formula` / `herbs` | `influenceStepLabel()` **复用九段自己的段名**，不另建表 |

现在它是机器判据：截图判据里列出禁止出现的 id（医案号除外——那是要显示的凭据），
前端测试断言"查表只有一处"、"不许把停因 id 插进要显示的字符串"，
后端测试断言"每条规则、每种停因都有中文名，且跟枚举一一对应"。

### 洞四：手写的 fixture 跟真序列化漂了

洞三那条新判据先报的是 `partially_verified` ——而后端明明已经下发中文名了。
问题在 fixture：那份 `verification` 是**手写的 dict**，没有 `status_label` / `rule_label`。
判据没在验前端，它在验一份手写数据长什么样。改法：用真的
`VerificationResult(...).to_dict()` 现造（同一个文件里 `S3Structured`、`to_graph()`
本来就是这么做的）。

---

## 四、被实测否掉的那一项：一次展开上限回不到 20

计划里写着"标签收窄了，上限从 14 回到 20"。真浏览器逐对量包围盒（1280×800）：

| 一次展开 | 20 | 19 | 18 | 17 | 16 | **14** |
|---|---|---|---|---|---|---|
| 压字对数 | 2 | 1 | 4 | 7 | 6 | **0** |

两层根因：

1. **`text-max-width` 断不了中文。** cytoscape 的 `text-wrap: wrap` 只在空白/换行
   处断行，「疫毒炽盛（急黄）证」里没有断行机会——令牌从 124px 收到 84px 之后，
   量出来的最宽标签**仍然是 125px**。那个令牌真正收窄的只是重名时补的
   `（病名 编码）` 那一行（里面有空格）。
2. **14 个以上会从两排扇面变三排**，而三排的径向间距放不下 125×41 的标签——
   所以"少画几个"反而更差（16、17 比 20 压得更多）。面积也对得上：这个扇面约
   11.2 万 px²，一个位置要 124×62≈7,700 px²，理想排布也就 14 个上下。

结论：**上限留在 14，把这五组数写进常量的注释里**。`--label-max-browser` 这个
令牌保留（它确实收得住带空格的那一行），但注释里写清"不要拿它当'标签变窄了'的
依据"。

---

## 五、每个数都带对照

| 数 | 本轮 | 对照 |
|---|---|---|
| Playwright 状态 | **29** | R36 的 21（+8：chain_flow×4 分辨率、chain_running、cancel_button、node_explain、single_chain_graph） |
| 单链最宽标签 | **125** px | 令牌写的是 84px——差的这 41px 就是"CJK 断不了行"那件事 |
| 一次展开上限 | **14** | R24 补丁 20 → R28 14 → R37 试 20 实测 2 对压字，退回 14 |
| 释义接口 LLM 调用 | **0** 次 | 对照是 S3 那一步的 1 次（同样是"给人看的解释"，一个要付钱一个不用） |
| 防幻觉字段数 | **72** 处 | 口径 `grep -c "= Field(min_length=1" core/schemas.py`；本轮 **0 增 0 减** |

---

## 六、工程数（bench 落盘，可核）

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **3560** passed / **7** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R37` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **72**（口径：`grep -c "= Field(min_length=1" core/schemas.py`；本轮 0 增 0 减） | 当场跑 |
| 凭据核对 | **22** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **29** 种全过 | `python -m scripts.screenshot_states` |

- 测试 passed **3560** —— `bench/rounds/R37.json:round.R37.pytest_passed=3560`
- 测试 skipped **7** —— `bench/rounds/R37.json:round.R37.pytest_skipped=7`
- 测试 failed **0** —— `bench/rounds/R37.json:round.R37.pytest_failed=0`
- 全量测试墙钟 **143.5** 秒 —— `bench/rounds/R37.json:round.R37.pytest_wall_s=143.5`
- Playwright **29** 种全过 —— `bench/rounds/R37.json:round.R37.playwright_states_passed=29`
- 凭据核对 **22** 份文档 —— `bench/rounds/R37.json:round.R37.n_checked_docs=22`

---

## 七、自查

1. ✅ 单链九段在三种分辨率下真的**看得见**（量高度与 offsetParent，不查类名）
2. ✅ 两种形态互斥：structured 下 `#columns` 是空的，legacy 下 `#chain-flow` 不显示
3. ✅ 多份结论永远不会被画成单链（形状判断与模式判断分开，各有测试）
4. ✅ 释义接口零 LLM；取不到整块隐藏；节点 id/名字有 200 字上限
5. ✅ 单链图跑了 Playwright——**这是 CLAUDE.md 点名的那条**（改了图的层结构就必须真渲染）
6. ✅ 重名证型补编码的判断只有一处，两张图共用；排版各自决定
7. ✅ 页面上不出现 id（医案号除外，那是凭据）；查表各只有一处
8. ✅ 规则中文名跟 `ALL_RULES` 放在一起，漏配会被测试当场抓住
9. ✅ `Field(min_length=1)` 计数 **72 处**，本轮一处未动
10. ✅ 安全否决仍在 S2 之前（本轮没碰推理链的次序）
11. ✅ 每个数都带对照（第五节）；被否掉的那一项如实写在第四节，不悄悄删掉
12. ✅ ruff 干净；Playwright 29 种全过

---

## 八、下一步（R38）

内部三指标、四组消融（A/B/C/D）、外部基准接入（TCM-BEST4SDT、MTCMB TCM-PR）、
与开源中医模型的对比（BianCang / ShizhenGPT）。

三条**必须带进去**的已知局限：R34 的本体只有 235 首方（方名有 OCR 截断）、
R35 的用药规律只覆盖五位医家里的三位（证型档实际只有「悬饮」够 support）、
以及消融对照要用 `S3_BEST_OF_N=3` / `S1S2_MERGED=1` / `S3_MODE=legacy` 三个开关。
