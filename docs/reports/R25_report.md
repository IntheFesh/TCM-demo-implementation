# R25 报告：演示保险、材料索引、上机第 9 段

**基线** `144bf81`（R24 结束）。这一轮**不改任何业务代码**：三件事全部落在
「人在演示前五分钟能不能靠自己发现问题」和「把这些话讲给别人听时每句有没有凭据」上。

判据因此也换了形状——没有新的推理路径要测，要测的是**一个脚本会不会漏报**、
**一份对外材料里有没有裸数字**。前者靠 20 条逐项判据（每条 fail 必须带修法），
后者靠已有的凭据核对器（`MATERIALS.md` 进 `DEFAULT_CHECK_PATHS`，和 README /
RESULTS.md / DEMO.md 同一个核对器）。

---

## 一、做了什么（逐项 ✅/❌）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 1 | **`scripts/demo_preflight.py`**：十二项演示前自检 | ✅ 新文件 **311** 行；退出码 0/1；`--strict` / `--json` |
| 2 | 七个环境残留变量各带一句后果 | ✅ `LEFTOVER_ENV`；`RETRIEVER_MODE` 残留是 **fail** 不是 warn |
| 3 | 额度折算复用 `calls_per_consult()` | ✅ 不写死 11；判据钉住"脚本里没有字面 11" |
| 4 | **`docs/MATERIALS.md`**：对外材料索引 | ✅ 新文件 **112** 行；七件能讲的事 + 工程规模表 + **八行「不能讲」** |
| 5 | `MATERIALS.md` 进凭据核对器 | ✅ `DEFAULT_CHECK_PATHS` 加 `docs/MATERIALS.md`；`--check` 退出码 0 |
| 6 | **`run_onsite.sh` 第 9 段**：R21~R24 的上机项 | ✅ `seg_9()` 四件事，估 **320** 次；进 `COSTLY_SEGMENTS` |
| 7 | 第 9 段与段 6/7/8 不可比这句写进两处 | ✅ 段说明里（`top3` 测的 vs `full_context` 测的）+ 本报告第四节 |
| 8 | `tests/test_demo_preflight.py` | ✅ **20** 条；含「不许有 skipped 状态」「每条 fail 必须带 fix」 |
| 9 | SOURCES 第 68 条 | ✅ 六小节，含「清单内容对、形式错」和「静默跳过比没有检查更坏」 |

**❌ 没有一项。** 第 9 段里的四件事本身是 ⏳ 上机项（要真实 API / 要联网取字体），
这一轮交付的是**脚本 + 估算 + 上机命令**，按 §0.5 第 3 条不停流程，进第六节清单。

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **2906** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R25` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **37**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑，不落文件 |
| 凭据核对 | 退出码 **0**（9 份文档） | `python -m scripts.collect_results --check` |
| Playwright | **20** 种全过 | `python -m scripts.screenshot_states` |

带凭据记号的那几个，引这一轮的不可变快照 `eval/bench/rounds/R25.json`：

- 测试 passed **2906** —— `bench/rounds/R25.json:round.R25.pytest_passed=2906`
- 测试 skipped **10** —— `bench/rounds/R25.json:round.R25.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R25.json:round.R25.pytest_failed=0`
- 全量测试墙钟 **97.8** 秒 —— `bench/rounds/R25.json:round.R25.pytest_wall_s=97.8`
- Playwright 通过 **20** 种 —— `bench/rounds/R25.json:round.R25.playwright_states_passed=20`

**`min_length=1` 的口径说明**：CLAUDE.md 写的基线是 31 处，当前实测 **37** 处
（口径 `grep -c "= Field(min_length=1" core/schemas.py`，不是 `grep -c "min_length=1"`
——后者会把注释和文档字符串里提到这个词的行也算进来）。31 → 37 全部是纯新增
（M1 之后各轮的药理层与建议层字段，最近一处是 R23 的 `Advice.reason`），
**本轮 `core/schemas.py` 一个字没改**，37 处一处没动。

**新增测试 21 条**（R24 的 2885 → 本轮 2906）：`tests/test_demo_preflight.py` **20** 条，
第 21 条是 `tests/test_docs_numbers.py::test_each_checked_doc_passes` 这个参数化用例
因为 `MATERIALS.md` 进了检查清单而多出来的一份（`[MATERIALS.md]`）。

**2906 这个数是在这份报告存在之前量的**，写完它之后全量测试是 **2907**
——`DEFAULT_CHECK_PATHS` 用 glob 收 `docs/reports/R*_report.md`，所以每一轮的报告
自己会给参数化用例加一份。这不是漏测，是这套凭据机制的固有形状（R23 那轮把
"当前轮报告条数"这条循环判据换成文件对文件的比较，就是为了不在这里绕圈）：
**每一轮的条数都在它自己的报告落盘之前量**，所以 R26 的基线是 2907 不是 2906。

---

## 三、每项改动的验证方式与结果

### 3.1 `demo_preflight` 十二项——在沙盒里真的跑了一次，并且**故意让它红**

```
$ python -m scripts.demo_preflight ; echo $?
× 医案语料：cases.json 不在——检索层一开口就 RetrievalUnavailable，三位医家的 S3 一次都跑不了
    修：python -m offline.extract_cases
! 演示设置 LLM_MODE：LLM_MODE=None，建议 replay：…
! 演示设置 FAST_MODE：FAST_MODE=None，建议 1：…
! 录制 fixture：fixtures/ 下没有任何录制——回放模式会当场失败
! 离线字体：还在用 CDN 字体：断网演示会掉到系统字体（宋/黑两族的区分没了）
! 前缀缓存预热：远端缓存的死活这台机器查不到（那是 DeepSeek 的内部状态）…
✓ 环境残留：七个会改变演示行为的变量都没设
✓ 知识图谱：data/graph.json 在（1667 KB）
✓ 医家注册表：启用 3 位 / 注册表共 5 位：叶天士、吴鞠通、张锡纯
✓ 额度折算：没写死每 IP 额度，代码按 calls_per_consult()=11 现算
✓ 凭据核对：9 份文档的凭据记号全部一致
✓ 状态截图：21 张
**1 条不通过**，先修完再演。
1
```

**6 ✓ / 5 ! / 1 ×，退出码 1**（`--json` 给 `n_fail=1` `n_warn=5`）。
沙盒里 `cases.json` 确实不在（它是 `.gitignore` 的生成物），所以这一条红**是对的**
——一个在沙盒里全绿的演示自检脚本才是可疑的。

### 3.2 「不许有 skipped 状态」这条判据

初版给药理层、fixture 这类"文件可能不在"的检查留了第四种状态 `skipped`。
`test_no_check_reports_a_skipped_status` 把它钉死了：文件不在就是 warn 或 fail，
**带一句「这会让你讲不了 X」**。理由写在测试里——一个跳过的检查在终端里是一行
浅色的字，读的人会把它读成"这条没问题"，**静默跳过比没有检查更坏**。

同一条理由派生出 `test_every_failing_check_carries_a_fix`：逐条构造 fail 状态，
断言 `fix` 非空。演示前五分钟需要的不是"哪里坏了"，是"敲哪一行"。

### 3.3 七个环境残留变量：判据不是"设了没有"，是"你哪句话会变成假话"

| 变量 | 残留的后果 | 档位 |
|---|---|---|
| `RETRIEVER_MODE` | 讲「默认走全量上下文」，实际跑 top3——**不报错、结果合法、话是假的** | fail |
| `S3_BEST_OF_N` | 讲「采 3 次挑一张」，实际采 1 次 | warn |
| `FAST_MODE` | 四项降级静默生效（追问 0 轮 / ReAct 2 步 / 采样 1 次 / token 上限降档） | warn |
| `S3_REASONING_EFFORT` | 讲 `max`，实际按环境变量走 | warn |
| `LLM_BACKEND` | 讲 `deepseek-v4-pro`，实际打到别的后端 | warn |
| `REPLAY_*` | 讲「实时调用」，实际在放录音（反之亦然） | warn |
| `QUOTA_*` | 额度提示跟真实扣费对不上 | warn |
| 都没设 | ——（本轮沙盒实测：✓） | ok |

`RETRIEVER_MODE` 是唯一一个 fail：R21 之后它**直接决定演示时讲的那句主张**
（知识进缓存前缀，不走 top-3 检索），残留会让整场讲的和跑的是两个系统。

### 3.4 额度折算复用 `calls_per_consult()`

`test_the_quota_check_derives_calls_from_the_shared_helper` 断言脚本里没有字面 `11`、
且导入了 `core.usage.calls_per_consult`。这是 CLAUDE.md 第 31 条在"写死常量"上的落地
——R22 把默认 best-of-N 从 1 改到 3 时，`2 + 3 × 3 = 11` 这个数在四处出现过，
当时收成了一处；演示自检是**第五个**会用到它的地方，接在同一处上。

### 3.5 `MATERIALS.md` 的验证就是 `--check` 本身

- `test_the_materials_doc_is_in_the_credential_checker`：文件在 `DEFAULT_CHECK_PATHS` 里，
  且 `check()` 的 `mismatches == []`。
- `test_the_materials_doc_has_a_cannot_say_section`：「不能讲」那一节必须存在且非空。
  这条判据的存在本身是个主张：**一份对外材料如果只有能讲的，它就是一份宣传稿**。
- 写的时候当场被 `--check` 抓到三处：凭据记号和它修饰的数**不在同一行**
  （`check()` 按行核 `missing_in_line`）。拆成一行一个数才过——这正是这个核对器
  设计时想拦的东西，只是这回拦的是我自己。

### 3.6 第 9 段：估算怎么来的

| 子项 | 估算 | 依据 |
|---|---|---|
| `context_prefix --report` | **0** | 只数 token、不调模型 |
| `bench_consult --backend real --repeat 2` | **22** | 2 × `calls_per_consult()` = 2 × 11 |
| `RETRIEVER_MODE=full_context` 下 E3/E4 | **≈297** | 9 条主诉 × own/swapped/none 三种 × 11 |
| `subset_fonts` | **0** | 下载 + 子集化，不调模型 |
| 合计 | **320** | 剧本 `--dry-run` 实测打印 320 |

全剧本 `--dry-run` 合计从 3971 涨到 **4291** 次 ≈ **¥23.6**（均价 ¥0.0055/次，
来源是 README 里 `record_fixtures`「约 272 次 ¥1.5」那条——量级估算，不是账单）。

---

## 四、性能/规模数字对照表

| 项 | R24 | R25 | 对照基准 / 怎么读 |
|---|---|---|---|
| 全量测试条数 | 2885 | **2906**（+21） | 条数只增不减是硬约束。+21 全是不调 LLM 的脚本测试 |
| 全量测试墙钟 | 97.0 s | **97.8 s**（+0.8） | 加了 21 条只涨 0.8 s，因为它们只读文件、不起进程 |
| Playwright | 20 种 / 47.2 s | **20 种 / 47.8 s** | 本轮没动前端，这 20 种测的是**没有回归** |
| 冷进程 import | 0.611 s | **0.538 s** | 六轮在 0.43~0.61 之间来回摆——**落在噪声里，不是变快了** |
| `/health` p50 五位 / 三位 | 12.749 / 12.791 | **12.915 / 13.536** | 八次里第五次翻向；差 0.62 ms，仍在噪声内 |
| 上机剧本段数 | 9 段（0–8） | **10 段（0–9）** | 新段估 320 次调用 |
| 上机剧本合计调用 | 3971 | **4291** | +320 |
| 演示前自检项 | 0（散文四条命令） | **12 项（1 个退出码）** | 对照是「`DEMO.md` 那一节」——内容没变，形式从"要人读"变成"退出码" |

**这张表里有一行是不可比的，必须单说**：第 9 段的 320 次调用是
`RETRIEVER_MODE=full_context` 下估的，段 6/7/8 的历史数字是 top3 模式下测的。
同一个「一次问诊多少钱」的问法，两个模式的答案**差一个量级**（full_context
每次问诊要把该医家全部医案 + 药典 + 方剂喂进去，缓存未命中时按 $1.32/M 计）。
把它们并排放进一张"每段多少钱"的表里是这一轮最容易犯的错，所以剧本的段说明和
这里各写了一遍。

---

## 五、改了哪些测试断言

三条，全部是**契约变更**，不是放宽判据：

| 测试 | 原断言 | 现断言 | 为什么必须改期望值而不是放宽 |
|---|---|---|---|
| `test_run_onsite.py`（最后一段） | 「性能基准（段 8）必须是最后一段」 | 「第 9 段是最后一段，性能基准在它前面」 | 段 8 要量的是"跑完前面所有段之后的这套系统"，段 9 要的东西比它更多（真实 API + 联网）。判据钉的是**段的顺序有理由**，改的是理由本身 |
| `test_run_onsite_resume.py`（`SEGMENTS`） | 九元组 `("0",…,"8")` | 十元组 `("0",…,"9")` | 它钉的是"可续跑的段清单跟剧本里的段一一对应"。放宽成"包含关系"会让它再也发现不了误删一段 |
| `test_onsite_peak_hours.py`（`COSTLY_SEGMENTS`） | `["5","6","7","8"]` | `["5","6","7","8","9"]` | 段 9 估 320 次，是全剧本第二贵的段。不登记它等于漏了一个该提醒的段；改成"只要 5 在里面就算过"会让它再也发现不了漏登记 |

**第三条是被自己的流水线抓到的**，值得单记：R25 的第一次
`bench_sandbox --all --round R25` 跑出 **1 failed**，就是这条。它当时把
"1 failed / 2905 passed" 写进了 `sandbox.json` 和 `rounds/R25.json`
——**一次红的测量被当成了这一轮的快照**。处理方式是 `git checkout` 把
`sandbox.json` / `LATEST` 恢复到 R24 的一致状态、删掉那份 R25 快照、修完判据重跑，
而不是在红的快照上改文档去迁就它。教训进第七节。

---

## 六、无法完成项（⏳ 上机，不是 ❌）

按 §0.5 第 3 条，这些都不停流程；脚本、退出码、上机命令都已就位。

| # | 项 | 为什么沙盒做不了 | 上机命令 |
|---|---|---|---|
| 1 | `demo_preflight` 十二项全绿 | 沙盒没有 `cases.json`（生成物）、没有 fixture、没有离线字体 | `python -m offline.extract_cases && python -m scripts.record_fixtures && python -m scripts.subset_fonts --download && python -m scripts.subset_fonts && python -m scripts.demo_preflight` |
| 2 | 前缀缓存预热后的真实命中率 | 远端缓存状态这台机器查不到（那是 DeepSeek 的内部状态） | 段 9：`python -m scripts.bench_consult --backend real --repeat 2`，看第二次的 `prompt_cache_hit_tokens` |
| 3 | `full_context` 下的 E3/E4 | 要真实 API，≈297 次调用 | 段 9：`RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4` |
| 4 | 第 9 段 320 次的**实际**调用数与账单 | 估算不是账单 | `bash scripts/run_onsite.sh 9`，跑完对 `data/usage.jsonl` |
| 5 | 离线字体（断网演示的最后一块） | 出站代理挡 jsdelivr | `python -m scripts.subset_fonts --download && python -m scripts.subset_fonts` |

---

## 七、发现但未动 / 发现并当场修了

1. **当场修了：一次红的测量被写成了本轮快照。** 见第五节末。根因是
   `bench_sandbox --all` 无条件写快照，**不管 pytest 是不是全绿**。这一轮没有动它
   （改它属于流水线改造，不在本轮范围），但记下这个形状：
   *一个"记录测量结果"的脚本，在测量本身失败时应该拒绝落盘，还是照实落盘？*
   照实落盘看起来更诚实，可现在的用法里它同时是"这一轮的凭据来源"，
   于是一份红的测量会被后续文档当成基线去迁就。下一轮如果动 `bench_sandbox`，
   先答这个问题。
2. **发现但未动：`DEMO.md` 和 `demo_preflight` 有一处内容重叠。** 两者都列了
   "演示前要确认的东西"。没有合并，因为它们答的**不是同一个问题**
   （`DEMO.md`：为什么要查这些；脚本：这台机器过没过）——这正是 CLAUDE.md
   第 31 条那条例外的形状，所以按例外要求在 `MATERIALS.md` 和 SOURCES 第 68 条
   里写清了两个问题的区别。
3. **发现但未动：`demo_preflight` 的「前缀缓存预热」这一项永远是 warn。**
   它查不到远端缓存的死活，只能提醒人去跑一次预热问诊。诚实的做法是让它承认
   查不到（判据 `test_the_prefix_cache_check_admits_it_cannot_see_the_remote`
   钉的就是这句话在输出里），而不是伪装成一个 ok。
4. **发现但未动：`docs/screenshots/r16_browser_expanded.png` 每次跑 Playwright 都变。**
   同一份数据、同一套判据，字节不一样（渲染时序）。它进了这次提交的 diff，
   但不影响任何判据。真要治得给截图做归一化，属于另一轮的事。

---

## 八、13 条自查

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template` | ✅ 本轮没动任何 prompt |
| 2 | LLM 输出全部用 pydantic 承接 | ✅ 本轮没有 LLM 调用路径的改动 |
| 3 | `Field(min_length=1)` 没有放松 | ✅ **37** 处（口径 `grep -c "= Field(min_length=1"`），`core/schemas.py` 一个字没改 |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新数据文件（新增的是一个脚本、一份 md、一份测试） |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ `demo_preflight` 的每项检查都是函数内读文件；`collect_results` / `core.usage` 是函数内 import |
| 6 | S1 全局只跑一次 | ✅ 没动推理链 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 第四节每行都有 R24 对照；`MATERIALS.md` 七件事每件带对照基准，八行「不能讲」就是为这条而设 |
| 8 | 同一概念只有一处实现 | ✅ 额度折算复用 `calls_per_consult()`（有判据钉住"没有字面 11"）；凭据核对复用 `collect_results.check`；`DEMO.md` 的重叠走的是"不是同一个问题"那条例外，理由已写进代码与 SOURCES |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ 医家注册表那一项走 `enabled_physicians()`，不自己比字符串 |
| 10 | 安全否决在 S2 之前 | ✅ 没动 |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ 20 条新测试全部只读文件 / 造临时环境变量；`demo_preflight` 自己也不联网（缓存那项明写查不到） |
| 13 | 改了按注册表/图层循环渲染的东西 → 跑 Playwright | ✅ 本轮没动前端，仍跑了一次 20 种全过（测"没有回归"），墙钟 47.8 s |

---

## 九、下一轮（R26）前置条件

R26 是**可选的蒸馏**（从 v4-pro 蒸一版小模型，预算 ≤¥40）。前置条件：

- ⚠ **§0.5(c) 会在这一轮生效**：单次真实动作超过 ¥30 要停下来问。蒸馏的数据合成
  这一步按目前的估算会踩到这条线，所以 R26 的第一步是**先把钱算清楚再开跑**，
  估出来超过 ¥30 就停下来问一次，不先花钱。
- ✅ 训练侧代码已就绪（R5 的 `scripts/train_lora.py` + 三源合并 + 泄漏防护），
  蒸馏只是换数据源，不需要新训练代码。
- ⚠ **三个训练前提仍未过**（R5 报告第六节）：role 填充率闸门、药理层两个
  `.jsonl`、MES 盲评。R26 若要出"蒸馏后效果"的数，这三条一条不过就只能报
  过程、不能报结论——`MATERIALS.md` 的「不能讲」表里那一行（"本地模型/LoRA
  已经训好了"）在 R26 之后要按实际情况改，不能默认删掉。
- ✅ `demo_preflight` 已经能一条命令判断"这台机器能不能演"，R26 之后的任何配置
  改动（换后端、换模型）都应该先过它一遍再讲数。
