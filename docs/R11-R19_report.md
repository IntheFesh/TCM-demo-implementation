# R11–R19 九轮总报告

这份文件是**九轮的横向账**：每轮的五个数、哪些判据已经过了、哪些只能上机跑。
每一轮自己的详细记录在 `data/SOURCES.md` 第 52–62 条，不在这里重复。

写下这份文件的理由：九轮的报告此前只存在于对话里，而对话不是交付物。
「R15 那轮测试是多少条」这种问题，去翻对话比去翻仓库贵得多。

---

## 一、九轮五个数

五个数的定义（每轮都用同一套口径）：

1. **全量测试** `python -m pytest -q` 的 passed / skipped / failed
2. **ruff** `ruff check .`
3. **防幻觉字段数** `grep -c "= Field(min_length=1" core/schemas.py`
   （**只数这一种写法**——不带 `= Field(` 前缀的 `grep -c "min_length=1"`
   会把注释和文档字符串也算进去，两种数法在这个文件里差 5 处以上）
4. **凭据核对** `cd scripts && python3 collect_results.py --check` 的退出码
5. **Playwright 前端状态** `python -m scripts.screenshot_states` 通过数

| 轮次 | commit | 测试（passed / skipped / failed） | ruff | `min_length=1` | `--check` | Playwright |
|---|---|---|---|---|---|---|
| R11 设计总纲进仓库 + 性能测量框架 | `33306fd` | 2279 / 6 / 0 | 干净 | 31 | 0 | —（前端还是单文件） |
| R12 三个性能杠杆 | `02741c2` | 2335 / 6 / 0 | 干净 | 31 | 0 | — |
| R13 前端拆四文件 + 设计令牌 | `c32e6de` | 2366 / 6 / 0 | 干净 | 31 | 0 | 1（r13_home） |
| R14 第 0 项 断循环依赖 | `c7f51ac` | 2373 / 6 / 0 | 干净 | 31 | 0 | 1 |
| R14 三列集注 + 用药对照带 | `b3080bf` | 2409 / 6 / 0 | 干净 | 31 | 0 | 6 |
| R15 患者/医生/学生三形态 | `5966980` | 2446 / 6 / 0 | 干净 | 31 | 0 | 10 |
| R16 图谱六层 + 图谱浏览器 | `9bcda7b` | 2471 / 6 / 0 | 干净 | 31 | 0 | 13 |
| R17 部署层收口（BYOK / 额度三档） | `4a8d135` | 2495 / 6 / 0 | 干净 | 31 | 0 | 15 |
| R18 之一 注册表扩五位 + 两套语料 | `a7a97c5` | 2526 / 6 / 0 | 干净 | 31 | 0 | 15 |
| R18 之二 立论层 / 教材 / 药理层 / 五源 / 参考医家 | `deae24c` | 2643 / 10 / 0 | 干净 | 36 | 0 | 16 |
| R19 上机剧本可续跑 + ε 按思考设置 + 性能对照 | 本轮 | **2693 / 10 / 0** | 干净 | 36 | 0 | 16 |

**R19 那一行的五个数，凭据在 git 历史里，不在当前的 `eval/bench/sandbox.json` 里。**
⚠ R21 发现并改正了这里的一个口径问题：`eval/bench/sandbox.json` 是**"当前这一轮的
测量"**，不是逐轮追加的日志——R21 重跑 `bench_sandbox --all` 之后它整份被覆盖成
R21 的数（2780 passed / 墙钟 66.8s），于是这份文件里挂在它上面的 R19 凭据当场
`--check` 报了 6 处不一致。**这不是文档写错了，是凭据挂错了对象**：一个会被下一轮
覆盖的文件，不能给一个历史轮次的数当凭据。改法是历史值指到 git 的那个 commit
（跟 `archive/2026-09-12/report_e3.json` 那批 📦 归档凭据同一个道理），当前值继续
挂 `sandbox.json`：

- 📦 R19 的五个数一次取全：`git show a22398a:eval/bench/sandbox.json`
  —— 里面 `pytest_passed` 2693、`pytest_skipped` 10、`pytest_failed` 0、
  `pytest_wall_s` 89.9、`playwright_states_passed` 16，跟上表 R19 那一行逐个对得上。
- ✅ **当前轮**（R21）那五个数仍然由 `collect_results --check` 逐个核，
  凭据记号在 `eval/RESULTS.md` 的性能表和 `docs/reports/R21_report.md` 里。

`ruff`、`min_length=1` 计数、`--check` 退出码这三个数**没有凭据记号也不该有**：
它们是命令的当场输出，不落任何文件。要核就当场跑那三条命令（第一节表头写了）。

**R14–R17 那四行的条数是怎么来的**：那几轮的 commit message 里没写条数，
所以不是从文字里抄的——用 `git worktree add --detach <commit>` 在那个版本上跑
`pytest --collect-only -q`，得到 collected 数，减去当时的 6 条 skip。
（R13 那一行可以对账：collected 2372 − 6 = 2366，跟它 commit message 里写的
2366 passed 一致，说明这个换算法对。）

**两处数字变化要单独说清楚**：

- **skipped 6 → 10**（R18 之二）。新增的 4 条是
  `tests/test_pharmacology_committed.py` 里那几条——`data/standard/materia_medica.jsonl`
  和 `formulary.jsonl` 不在沙盒里（要 AutoDL 上跑真实 LLM 抽取才有）。
  这 4 条 skip **不是静默跳过**：每条的 skip 理由里带着上机命令和产物路径。
- **`min_length=1` 31 → 36**（R18 之二）。纯新增：`RationaleRecord` 的
  `s` / `o` / `source_span` / `chapter` / `book` 五处。**没有动任何既有字段**，
  没有任何一处从 `Field(min_length=1)` 改成可选或 `min_length=0`。
  （这条铁律允许用更强约束替换后数字下降——`2f0d174` 那次 19 → 17 就是
  `str = Field(min_length=1)` 换成六值 `Literal`，是收紧。这一轮是上涨，
  上涨永远不需要辩解。）

## 二、九轮各自解决的那件事（一句话）

| 轮次 | 那件事 |
|---|---|
| R11 | 设计总纲进仓库并**订正六处**——总纲此前只在对话里，改一次就没人知道改了什么 |
| R12 | 三医家并发 / embedding 磁盘缓存 / 思考模式按步控制：**先分段量再优化**，不然说不清省下的是哪一段 |
| R13 | 前端 3574 行单文件拆成四个；设计令牌落地。依赖方向只允许 app.js → graph.js |
| R14 | 问诊页重排：三列集注、用药对照带（灰段=噪声地板、黑段=真实分歧）、五种状态 + 安全拦截整页替换 |
| R15 | 患者/医生/学生三种形态。患者模式是**独立形态不是三列的裁剪版** |
| R16 | 图谱六层 + 图谱浏览器以证素为枢纽；按类型定向展开（「肝」一展开拉 150 个节点那次） |
| R17 | 部署层：BYOK 收进顶栏一行小字、额度三档、降级不是错误、两个凭据盲区纳入注册表 |
| R18 | 注册表三位 → 五位；李可 57 例 / 王云启 77 例进抽取；《脾胃论》立论层；五本专科教材解析器；药理层进版本控制；五源合并；参考医家那一栏 |
| R19 | 上机剧本真可续跑（`--resume` 读状态文件）；ε 按思考设置分文件；性能对照表进凭据注册表 |

## 三、这九轮里 Playwright 抓到的、Python 测试抓不到的

这是 CLAUDE.md 那条「涉及结构变更时 Playwright 是必需验收环节」的实际战果。
**每一条的共同点都是：所有 Python 测试和所有前端纯函数测试都是绿的。**

| 轮次 | 症状 | 根因 |
|---|---|---|
| R14 | 页面点「辨证」毫无反应 | `#results` → `#columns` 改名漏了两处加载时的 `addEventListener`，整份 app.js 在那一行抛，后面全不执行 |
| R14 | 安全拦截页一片空白 | `growGraph(null)` 抛 `Cannot read properties of null` |
| R15 | 一条透明度断言**恒为真** | `cy.$('#' + CSS.escape(id))` 对含 `::` 和汉字的 id 返回空集合 → `.style()` 是 `undefined` → `parseFloat` 是 `NaN` → `Math.abs(NaN - 0.25) > 0.001` 恒 false |
| R16 | 展开「肝」拉进 150 个节点，图不可读 | 邻居 499 个、其中 61 个证型，没有按类型定向展开 |
| R16 | 20 个枢纽挤成中间一个点 | `minNodeSpacing: 28` 太小 |
| R18 | 非终态摆出**五列**，两列永远停在「辨证中」 | `physicianOrder()` 读 `/health` 下发的全部五位，而后端集注只跑 `physicians_enabled` 那三位 |

R18 那一条最能说明问题：`/health` 返回五位是**对的**（前端得知道李可存在才画得出
参考医家那一栏），后端返回三条结果也是**对的**，两边各自都没错——错在中间那层
没人测。判据因此应该从「改了 layer 数量」扩成「**改了任何前端按注册表循环渲染的东西**」。

## 四、上机 ⏳ 清单（按段序，每条带命令和判据）

沙盒里跑不了的全在这里。**每一条都有命令和判据**，没有「环境不支持」这种交代。

先跑一次看清单和预估成本：

```bash
bash scripts/run_onsite.sh --dry-run      # 九段、每段预估调用数、合计 ≈¥
bash scripts/run_onsite.sh                # 从段 0 开始
bash scripts/run_onsite.sh --status       # 看每段跑到哪了
bash scripts/run_onsite.sh --resume       # 从第一个没成功的段接着跑
```

| # | 要跑的 | 命令 | 判据 |
|---|---|---|---|
| 1 | 环境自检（零调用） | `bash scripts/run_onsite.sh --only 0` | 全量测试两段都过（`-m "not real_embedding"` + `-m real_embedding`，中间 sleep 20 防 2GB OOM） |
| 2 | 本地模型 | `bash scripts/start_vllm.sh` 然后 `python -m scripts.verify_local_backend` | 起得来、能出一次合法 S3 |
| 3 | role 填充率**闸门** | `python -m scripts.verify_role_fill` | ≥ 90%。**不过就停**——分层 ε 三个数建立在它上面 |
| 4 | ε 两套设置 | 段 4 会跑两次：默认那次 + `S3_THINKING=disabled` 那次 | 判据 ε_core < ε_online < ε_adjunct（不成立就如实报，不调参去凑）。两套数**不可比、并列报** |
| 5 | 药理层抽取**闸门** | `python -m scripts.run_pharmacology_extraction --limit-blocks 5` 人工核质量，过了再 `--crosscheck` | `s`（药名）对得上原文、`o` 和 `source_span` 也对得上 |
| 5b | 药理层两个文件进版本控制 | 把产物放 `data/standard/` 下提交（R18-F 已把落盘路径改到那儿） | `tests/test_pharmacology_committed.py` 那 4 条 skip 转为 pass；`pharmacology.n_materia_medica` / `n_formulary` 两个凭据键自动生效 |
| 6 | 录制 + 回放 | `python -m scripts.record_fixtures`（≈278 次调用）然后 `python -m scripts.verify_replay` | **23/23 全部命中且逐字节一致**，退出码 0 |
| 7 | 全套评测重跑 | `python -m eval.run_eval --e3 --e4 --e8 --e9`，再 `python -m eval.sdt.run --split Test --solver chain` | 回写 `eval/RESULTS.md` 的凭据记号，然后 `collect_results --check` 退出码 0 |
| 8 | 性能 | `python -m scripts.bench_startup` 跑**两次进程**；`python -m scripts.bench_consult --backend real --repeat 3 --no-react`，再 `--repeat 1 --react` | 热启动 ≤ 20s；一次问诊 ≤ 90s（不开 ReAct）/ ≤ 240s（开） |
| 9 | 五本专科教材扩表 | `git clone --depth 1 https://github.com/PanckooAI/TCM_Datasets.git /tmp/tcmds`，然后每本跑一次 `python -m offline.build_syndrome_textbook --md-path … --layout {waike,fuke,erke,yanke,tuina} --append`；抽出 0 条时先 `--detect` | 证候表从 337 条扩到 ≈1444 条 |
| 10 | LoRA 训练 | `pip install -r requirements-train.txt`（沙盒缺 peft/accelerate，`python -m scripts.train_lora --check-deps` 退出码 1），然后 `python -m scripts.train_lora` | 2 个基座 × 5 位医家 = 10 个 adapter；计划里不该有 `train 为 0` 的警示 |
| 11 | MES 盲评 | `python -m eval.mes.collect --all-pairs` | 20 条评完、三对 McNemar p 值。**这是训练的第三个前提**，没有任何脚本能替人评分 |

第 9 项在沙盒里**试过但被拦住了**：沙盒的网络策略不允许 `git clone` 第三方仓库
（分类器按「未经审查的外部代码」拒绝）。五个解析器和 OCR 修正表已经写好、用合成
fixture 测过 31 条；缺的只是那五个 markdown 文件。要在沙盒里做完这一项，需要用户
授权一次 clone，或者把那五个 `.md` 直接放进 `books/`。

## 五、这九轮建立的、后面每轮都要遵守的东西

- **任何数字旁边必须有它的对照基准**。这一轮的 P2/P3 就是个活例子：
  `/health` 五位 vs 三位两次测量方向相反，正确的结论是「这个开销量不出来」，
  不是「涨了 1ms」。挑一次合意的方向写进文档，是这个项目在 E2 那条
  （师承内 vs 跨学派两轮结论相反）上已经学过的教训。
- **凭据记号**：文档里的数写成 `文件名:键=值`，`collect_results --check` 逐个核。
  真机数据不存在时标 ⏳ 并说明「不是文件丢了，是这个数不存在」。
  R19 把沙盒性能数也纳进来了（`eval/bench/sandbox.json`），因为手抄的数漂过三次。
- **同一概念只能有一处实现**，写死的常量也算一处实现。这九轮里新踩的两次：
  R18-D 的剂量判据（复用 `pharmacology_sources.has_classic_dose`，没另写正则）、
  R18-I 的参考医家名单（读 `/health` 的 `enabled`，前端不列名单）。
- **改前端按注册表循环渲染的东西 → 跑 Playwright**（见第三节）。
- **上机剧本的状态要落盘**：跑几小时、会换终端、容器可能被回收，
  「我记得是段 5 挂的」本身就是故障点。
