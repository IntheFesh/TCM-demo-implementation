# R24 补丁报告：两个回归 + 一处兜底

**基线** `8ce1a4c`（R26 结束）。这不是新一轮，是回头修 R24 留下的两个回归——
两个都在 `docs/screenshots/r24_rings.png` 上肉眼可见，而 R24 那轮的 30 条纯函数
测试和 20 种 Playwright 状态**全绿**。

**这一条本身就是结论**：判据查的是"元素在不在、函数返回什么"，没有一条问
「按钮上有没有字」「标签压没压在一起」。这次把这两个问题补成判据。

（这一次的测量快照标签是 `R27`——标签只是**测量事件的编号**，不是新一轮；
R26 的快照 `rounds/R26.json` 原样不动，R26 报告引的还是它。）

---

## 一、做了什么（四项逐项 ✅/❌）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 0.1 | 自绘下拉 `refresh(sel)` + 两个 populate 调它 | ✅ | `web/ui/select.js` 的 `refreshSelect()`；`graph.js` 两处 |
| 0.2 | 两环展开上限 20 / 内环半径下限 / 扇形分布 | ✅ | `GB_EXPAND_CAP`、`gbInnerRadii`、`gbFanSlots`、`gbLayoutPositions` |
| 0.3 | 一次问诊成本改 ⏳ 并给算式 | ✅ | `README.md` 新表、`eval/RESULTS.md` #16、`docs/MATERIALS.md` |
| 0.4 | E3/E4 闸门没过的退路 + 段 9 建议提前跑 | ✅ | `scripts/run_onsite.sh` 段 9 的 `GATE_PY` 块 |

**❌ 没有一项。**

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **2971** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R27` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **44**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑 |
| 凭据核对 | 退出码 **0**（这里原写"11 份文档"是**错的**，当时实际 12 份——R28 把这个数改成量出来的、带凭据记号，见 `docs/reports/R28_report.md`） | `python -m scripts.collect_results --check` |
| Playwright | **20** 种全过（rings 在 **1280×800** 下重验） | `python -m scripts.screenshot_states` |

- 测试 passed **2971** —— `bench/rounds/R27.json:round.R27.pytest_passed=2971`
- 测试 skipped **10** —— `bench/rounds/R27.json:round.R27.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R27.json:round.R27.pytest_failed=0`
- 全量测试墙钟 **77.5** 秒 —— `bench/rounds/R27.json:round.R27.pytest_wall_s=77.5`
- Playwright 通过 **20** 种 —— `bench/rounds/R27.json:round.R27.playwright_states_passed=20`

**新增测试 21 条**（R26 的 2950 → 2971）：`tests/test_ui_r24_patch.py` **16** 条、
`tests/test_run_onsite.py` **4** 条（闸门兜底），另 1 条来自既有文件的参数化。
`core/schemas.py` 一个字没动，44 处 `min_length=1` 原样。

**2971 是在这份报告存在之前量的**，写完它之后是 2973（+1 是这份报告进了
`--check` 清单带来的参数化用例，+1 是为"补丁报告也要被核"补的那条判据）。
跟前几轮同一条规矩：**每一次的条数都在它自己的报告落盘之前量**。

---

## 三、先红后绿：修复前的断言长什么样

**0.1 的五条判据在修复前全红**，其中四条红在同一个地方——`refreshSelect` 根本
不存在：

```
$ python -m pytest tests/test_ui_r24_patch.py -q     # 修复前
E   node 执行失败：
E     /tmp/tmph89mpvjc.js:1771
E     refreshSelect(sel);
E     ^
E     ReferenceError: refreshSelect is not defined
...
E   AssertionError: populateGbPhysicianSelect 填完选项没有刷新自绘层
E   assert 'refreshSelect(sel)' in 'function populateGbPhysicianSelect(physicians) {…}'
5 failed in 4.99s
```

修复后 `16 passed`（后 11 条是 0.2 的布局判据，一起写在同一个文件里）。

**这一组判据用的是新的 `DOM_FAKE`，不是原来的 `DOM_STUB`。** 这一点是补丁的关键：
`DOM_STUB` 是个什么都接住的 Proxy（回答"没有 DOM 时会不会炸"），在它上面断言
"按钮文本非空"**恒真**——而恒真的断言正是这个 bug 当初没被发现的原因。
`DOM_FAKE` 记录父子关系、属性、文本和事件，所以自绘层渲染成了什么可以被断言。
两个桩回答的不是同一个问题，所以并存（CLAUDE.md 第 31 条的例外，理由写在
`tests/web_harness.py` 里）。

---

## 四、0.2 的每个数都是量出来的，不是估的

第一版按"一排 7 个、三排、±40°"写完，真浏览器一量：**10 对标签压在一起**。
后面每一步都是"改一个参数 → 在 1280×800 的真浏览器里量 40 个节点的
`renderedBoundingBox` → 看还剩几对"，一共量了八轮：

| 改动 | 压字对数 | 这一步学到的 |
|---|---|---|
| concentric → preset + 扇形（初版） | 10 | ±40° 的扇面**几何上放不下** 20 个标签：那个扇区约 4.8 万 px²，20 个标签要 10 万 |
| 扇面按需张开（40°→75°） | 8 | 张开之后压字的从"扇内"变成"扇内 vs 内环" |
| 每排按弧长定容量（内排少放） | 2 | 每排一样多，外排还宽松、内排已经压字 |
| 位置留余量 62×112px、枢纽错开 0.78 | 1 | 余量是量出来的：一个证型标签 wrap 后约 100×50px |
| 枢纽改为**往外**错开 1.22 | 3 | 往内错让一半枢纽掉到 0.18 规格线以下（实测 0.148） |
| 内环收到 0.181×短边 | 1 | 内环每多占 10px，外圈就少 10px |
| 余数给最外排 + 相邻排错开半格 | 0 | 余数给最内排 = 把人塞进最短的那条弧 |
| 外圈 0.44→0.45→定 0.45 | 0 | 0.42 时 zoom=1、0.45 时 zoom=0.965（字 12.55px，仍 ≥ 12） |

**最终实测（1280×800，40 个节点）**：压字 **0 对**、最小渲染字号 **12.55px**、
内环最小半径 **0.21×短边**（规格 ≥ 0.18）、外圈角度跨度 < 180°。

三个新的 Playwright 判据（都是这一轮加的）：

1. **字号 ≥ 12px**：`renderedStyle('font-size')` 取最小值。抓的是"布局其实摆不下、
   被 `fit` 缩成 0.7 倍"这种情况——节点没少、字看不清。
2. **两两比包围盒**（含标签），留 2px 容差：容差不是放水，`renderedBoundingBox`
   把标签的抗锯齿边也算进去，两个框贴着但没压字时会报 1px 级相交。
3. **内环不许塌**：从**枢纽自己的重心**量半径，不是从画布 extent 的中心量——
   扇面只在一侧，extent 的中心被它拽偏，同一张图量出来是 0.068 vs 0.21。

还有一处 API 改动：`/api/graph/neighbors` 的每个节点多一个 `n_symptoms`
（"取哪 20 个"的依据）。它**必须服务端算**——证型的症状数记在 `indicates` 边的
`via_syndrome` 属性上，不是节点邻居；第一版按数 symptom 邻居写，61 个证型
**全部返回 0**，一个恒为 0 的排序键跟没有排序一样，而它不报错。

---

## 五、改了哪些测试断言

| 测试 | 原断言 | 现断言 | 为什么不是放宽 |
|---|---|---|---|
| `test_graph_browser.py::test_the_layout_is_concentric_with_hubs_inside` | `name: "concentric"` | `name: "preset"` + 位置由 `gbLayoutPositions` 算 | 规格（内圈枢纽、外圈展开）没变，换的是实现——concentric 不接受半径下限/扇形范围/椭圆，而这三样正是那张糊图的直接原因 |
| `test_ui_r24.py::test_the_browser_has_exactly_two_rings` | `gbHubIds.has(ele.id()) ? 2 : 1` | `hubIds = visible.filter(…gbHubIds.has(id))` | 原断言钉的是 concentric 的参数写法。两环**真的分得开**改由纯函数判据 + Playwright 判据管，那两条比数一个字面量强 |

**没有删掉任何一条判据。**

---

## 六、无法完成项（⏳ 上机）

| # | 项 | 为什么沙盒做不了 | 上机命令 |
|---|---|---|---|
| 1 | 一次问诊的真实花费（替掉 ¥1.44 那个估算） | 要真实 key | 跑一次问诊后看 `/api/usage` 的 `tokens_today` |
| 2 | E3/E4 闸门兜底真的触发一次 | 要真实 API 跑 E3/E4 | `bash scripts/run_onsite.sh --only 9`；闸门不过时脚本退出码 1 并打印退回 hybrid 的命令（**兜底逻辑本身已经在沙盒里跑过**，见第七节） |
| 3 | 两环在 1920×1080 投影仪上的表现 | 判据只验了 1280×800 | 接上投影仪开一次图谱浏览器；更大的屏只会更宽松 |

---

## 七、发现但未动 / 发现并当场修了

1. **当场修了：兜底代码要能被跑到。** 段 9 的闸门兜底是一段 heredoc 里的 python，
   `tests/test_run_onsite.py` 把它**抠出来单独跑**（四条：阈值不写死、通过、
   不通过退 1 并给退路、`change_rate` 为 null 按不通过处理）。
   只断言"源码里有这几个字"等于没测——R9 那轮「静默不再正常」是同一个形状。
2. **当场修了：`null` 不等于通过。** `change_rate` 为 null 表示闸门无法判定
   （两侧检索全为空）。按通过处理会让一次什么都没测到的跑变成绿灯。
3. **发现但未动：`text-max-width: 90px` 是全局样式**，问诊图和浏览器共用。
   浏览器的证型标签 wrap 成两行正是布局最挤的原因，把它调小能省出空间——
   但那会同时改问诊图的观感，属于另一轮的事。现在的做法是让布局去适应标签，
   不是让标签去适应布局。
4. **发现但未动：`GB_SLOT_V` / `GB_SLOT_H` 是在 13px 字号下量的。** 换字号或
   换语言（标签变长）要重量一次。判据会红（压字），所以不会静默失效。
5. **发现但未动：段 9 排在最后但建议提前跑。** 段号是依赖顺序，执行顺序是另一回事
   ——②③只要 `cases.json` + 真实 key。剧本里写了这句，但没有真的把它挪到前面：
   挪段号会牵动 `SEGMENTS` / `--resume` 的状态文件语义，不值当。

---

## 八、13 条自查

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template` | ✅ 没动 prompt |
| 2 | LLM 输出全部用 pydantic 承接 | ✅ 没动 schema |
| 3 | `Field(min_length=1)` 没有放松 | ✅ **44** 处，`core/schemas.py` 一个字没改 |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新数据文件 |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ 没有这类对象 |
| 6 | S1 全局只跑一次 | ✅ 没动推理链 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 第四节每一步都带"改前几对压字→改后几对"；成本那三个数各自标了口径和 ⏳ |
| 8 | 同一概念只有一处实现 | ✅ 闸门阈值问 `GATE_OUTPUT_CHANGE_RATE`（判据钉住剧本里没有字面 0.4）；`gbFetchInto` 仍是唯一的取数入口（加的是 `pick` 钩子，不是第二条路径）；症状数只在服务端算一处 |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ 没有新入口 |
| 10 | 安全否决在 S2 之前 | ✅ 没动 |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ 16 条新前端判据走 node + 假 DOM；4 条闸门判据只跑一段 python |
| 13 | 改了按注册表/图层循环渲染的东西 → 跑 Playwright | ✅ **这一轮就是靠它验收的**：20 种全过，rings 在 1280×800 下重验并重截 |

---

## 九、下一步

四项做完，全量 0 failed、ruff 0、`--check` 0。剩下的仍然是那份合并上机清单
（`docs/reports/R26_report.md` 第六节 + R25 报告第六节 + R11–R19 报告第四节），
其中第 15/16 项（full_context 的命中率与 E3/E4）**建议紧跟在不花钱的 A 组之后跑**
——它们决定默认配置成不成立，闸门没过要退回 `RETRIEVER_MODE=hybrid`，
这件事越早知道越好。
