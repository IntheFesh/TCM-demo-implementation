# 材料索引（R25）

**这份文件是给"要把这个项目讲给别人听"的场合用的**：竞赛材料、答辩、技术分享。
它不重复 README，只回答三个问题：**讲什么、每句话的凭据在哪、哪些还不能讲**。

## 一条铁律先摆在这里

**这份文件里出现的每个数，要么带凭据记号（`文件名:键=值`），要么标 ⏳。**
`python -m scripts.collect_results --check` 逐个核（这份文件在 `DEFAULT_CHECK_PATHS`
里，跟 README / RESULTS.md / DEMO.md 同一个核对器）。

理由是这个项目的另一条铁律：**任何数字旁边必须有它的对照基准**。
一个没有基准的数在这里等于没有——而"有基准"这件事只有在数被机器核过时才成立，
否则它就只是一句抄过来的话。

---

## 二、能讲的七件事，以及每件事的凭据

### 1. 引用可核、幻觉为零

每条结论都带它依据的医案 id，越界引用会被当场标出。

- 引用合法 **27** 条 —— `report_e3.json:hallucination.n=27`
- 幻觉 **0** 条（引了检索结果里不存在的 case_id）—— `report_e3.json:hallucination.n_hallucinated=0`
- **对照基准**：两组分母分开报（有医案可引 vs 没有），不合并成一个率。

### 2. 分歧度有噪声地板做对照

"两位医家不一样"这件事，要先排除"同一位医家跑两次也不一样"。

- ε_online mean **0.2611** —— `epsilon.json:epsilon_online.mean=0.2611`
- 逐条超出各自地板的 **9**/9 —— `report_e3.json:e3.paired_real_divergence=9`
- **对照基准**：ε 本身就是对照物；页面上那条带子左段是噪声、右段才是真实分歧。

### 3. 参考医案真的在起作用（消融）

- E3 换掉参考医案：change_rate **0.451**（闸门 ≥ 0.4）—— `report_e3.json:e3.change_rate=0.451`
- E4 完全不给参考医案：change_rate **0.497** —— `report_e4.json:e4.change_rate=0.497`
- **对照基准**：修复前分别是 0.335 / 0.351（📦 归档可核，见 RESULTS.md）。

### 4. 外部基准上的提升

- TCMEval-SDT Test：chain **23.173** —— `sdt/test_run_log.jsonl:sdt.chain_last=23.173`
- **对照基准**：同模型同输出头的 baseline **22.068** —— `sdt/test_run_log.jsonl:sdt.baseline=22.068`
- 两者之差 **+1.105**（23.173 − 22.068），这个差本身就是"注入证素分析"的效果
- ⚠ 同时要讲：关掉安全否决能到 **27.729** —— `sdt/test_run_log.jsonl:sdt.ignore_safety_veto=27.729`
- **安全是有分数代价的**（23.173 vs 27.729），这个代价我们选择付。

### 5. 安全否决在证素推断之前

危重主诉整页拦截，不产出任何方药。

- 截图：`docs/screenshots/r14_blocked.png`
- 判据不是"页面上看不见药名"，而是**响应体里搜不到**（Playwright 判据里那一条）。

### 6. 知识怎么进模型（R21–R22）

- 缓存命中的输入 token 比未命中便宜 **30 倍**（$0.044/M vs $1.32/M，官方价）
- 一次问诊 = `2 + 医家数 × 采样次数` = **11** 次调用（默认配置）
- ⏳ 真机命中率、真机每次问诊的钱：见 `eval/RESULTS.md` 的 full_context 系五行

### 7. 前端不是一个模板（R24）

- 20 种状态的真浏览器验收 —— `bench/sandbox.json:bench.playwright_states_passed=20`
- 四张 R24 截图：`r24_epigraph` / `r24_select_open` / `r24_advice_panel` / `r24_rings`
- **对照基准**：R14 起 6 → R15 10 → R16 13 → R17 15 → R18 16 → R24 20 → R25 仍 20（这一轮没动前端，测的是没有回归）。

---

## 三、工程规模（可核）

| 项 | 数 | 凭据 |
|---|---|---|
| 全量测试 | **2906** passed / **10** skipped / **0** failed | `bench/sandbox.json:bench.pytest_passed=2906` `bench/sandbox.json:bench.pytest_skipped=10` `bench/sandbox.json:bench.pytest_failed=0` |
| 前端状态验收 | **20** 种 | `bench/sandbox.json:bench.playwright_states_passed=20` |
| 知识图谱节点 | **1315** | `../data/graph.json:graph.n_nodes=1315` |
| 知识图谱边 | **3771** | `../data/graph.json:graph.n_edges=3771` |
| 证候 | **178** | `../data/graph.json:graph.n_syndromes=178` |
| 症状 | **1117** | `../data/graph.json:graph.n_symptoms=1117` |

---

## 四、**不能讲**的（这一节比上面三节更重要）

讲的时候主动说这些，比被问出来强。

| 不能讲 | 为什么 | 现在的状态 |
|---|---|---|
| "分歧度 0.53 说明两位医家分歧很大" | 0.53 单独出现没有意义，必须跟 ε 一起 | 页面上那条带子就是为此而做 |
| "换了 full_context 之后效果更好" | full_context 系的 E3/E4 **还没跑过** | ⏳ `eval/RESULTS.md` 那一节五行全 ⏳ |
| "best-of-N 提升了质量" | 没跟 ε 比过，分数差可能落在噪声里 | ⏳ 上机第 2 项（R22 报告第六节） |
| "本地模型/LoRA 已经训好了" | 训练三个前提（role 填充率、药理层、MES 盲评）都没过 | 代码就绪、没训 |
| "药理层有 1 万多条数据" | 那两个 `.jsonl` 要 AutoDL 上抽取才有 | ⏳ 上机段 5 |
| "断网也能完整演示" | 回放模式可以，**但字体还在 CDN 上** | ⏳ R24 第六节 1/2 |
| E2（师承内 vs 跨学派） | **两轮结论相反**，判据落在噪声里 | 只报出、绝不设闸门 |
| 任何来自 `deepseek-chat` 的数 | 那个模型 2026-09 已下线 | RESULTS.md ⚠ 第 4 条 |

---

## 五、演示前跑这一条

```bash
python -m scripts.demo_preflight          # 退出码 0 = 可以开始
python -m scripts.demo_preflight --strict # 连"建议项"也算失败
```

它查十二项：环境残留（`RETRIEVER_MODE` 这类**不报错但让结果跟你讲的话对不上**的
变量）、演示设置、语料、图谱、注册表、录制 fixture、离线字体、额度折算、
缓存预热、凭据核对、截图齐不齐。**每条 fail 都带一句怎么修。**

详细的演示流程（七个演示点、时间不够砍哪几点）在 `DEMO.md`，这里不重复。
