# R37~R39 验收报告

**范围** `efa926a`（R36 结束）→ `57b5bb2`（R39 结束），三轮共 **58 个文件、+4903 行**。

验收不是"再跑一遍测试"——那三轮自己已经跑过了。这一份做的是**独立复核**：
每一轮报告里的关键数，用**不看报告的方式**重新量一遍，看两边对不对得上；
以及逐条核 CLAUDE.md 那几条铁律现在还成不成立。

---

## 一、全局闸门（现跑现报）

| 项 | 结果 | 命令 |
|---|---|---|
| 全量测试 | **3607** passed / **7** skipped / **0** failed | `python -m pytest -q` |
| ruff | 干净 | `ruff check .` |
| Playwright 真浏览器 | **29** 种全过 | `python -m scripts.screenshot_states` |
| 凭据核对 | **24** 份文档，退出码 **0** | `python -m scripts.collect_results --check` |
| 演示自检 | 0 fail / 4 提醒（缺 key、缺 fixture、缓存预热、FAST_MODE） | `python -m scripts.demo_preflight` |
| 上机剧本 | 12 段解析正常，总账 **4791** 次 ≈ ¥75.6（高峰价） | `bash scripts/run_onsite.sh --dry-run` |

四条提醒**不是缺陷**：它们要么要真实 API key（fixture、缓存预热），要么是
演示当天才设的开关（FAST_MODE）。每条都带着怎么修。

---

## 二、独立复核：报告里的数，重新量一遍

| 报告里的说法 | 复核方式 | 复核结果 |
|---|---|---|
| R38：四组调用数 3/5/5/2 | 换一条主诉、重跑 `eval.ablation --backend fake --limit 1` | **3/5/5/2** ✓ |
| R38：本体覆盖语料 0.3979 / 0.7043 | 直接调 `ontology_coverage_of_corpus()` | **0.3979**（230/578）/ **0.7043**（3380/4799）✓ |
| R38：legacy 组验证器"不适用"不是 0 | 看复跑输出的那一格 | 打印「不适用」✓ |
| R38：假后端不出内容指标 | 同上 | 三列全 ⏳ ✓ |
| R39：docx 补充后原文未变 | 合成件走一遍 fingerprint → annotate → verify | `ok: True`，新增 1 处 ✓ |
| R39：改坏一个字要报红 | 在补充后的文件里给标题**加一个空格** | `ok: False`，报出「一、项目概述」6 字 → 7 字 ✓ |
| R37：节点释义零 LLM | `grep -c 'get_llm\|generate(' core/node_explain.py` | **0** ✓ |
| R37/38/39 各自的新增测试条数 | `pytest --collect-only` 逐个数 | 27 / 19 / 12 / 20 / 13 ✓ |

**加一个空格就报红**这条是这次验收里最值得记的一次复核：它证明那条校验不是
"看起来在查"，而是真的逐字节比。

---

## 三、CLAUDE.md 的铁律，逐条现核

| 铁律 | 怎么核的 | 结果 |
|---|---|---|
| prompt 一律 `string.Template`，禁止 `str.format()` | `grep '\.format(' core/llm.py` | 0 处 ✓ |
| `Field(min_length=1)` 不许放松 | `grep -c "= Field(min_length=1" core/schemas.py` | **72**（R36~R39 四轮一处未动）✓ |
| 新增 `.jsonl` 只放 `data/standard/` | `git ls-files '*.jsonl'` 按目录分组 | `data/standard/` 6 个 + `eval/sdt/` 1 个（R2 的过拟合台账，早于这条约定且有 `.gitignore` 例外）✓ |
| 安全否决在 S2 之前 | 跑 `test_consult_rejects_before_s2_on_danger_symptoms` + `test_s1s2_merged.py` | 24 条全过 ✓ |
| 追问的回答必须先过 `check_safety` | `core/chain.py` 那一段 + `core/tools.py` 的 `is_safety_relevant` | 在 ✓ |
| 图层结构变更必须跑 Playwright | R37 改了单链图，29 种状态跑过（含 `single_chain_graph`） | ✓ |
| 评测代码放 `eval/` 不放 `tests/` | R38 新增的 `eval/ablation.py`、`eval/mtcmb/` 都在 `eval/`；`tests/` 里那 32 条零 LLM、零网络 | ✓ |
| 任何数字都必须带对照 | RESULTS.md 21 行 ⏳ 逐行看：每行要么带命令，要么显式指向另一行的命令 | ✓（其中 5 行的命令写在散文里，机器 grep 认不出，人工读过） |

---

## 四、验收中发现并修掉的一处

**演示自检报的医家数会让人误读。** 它原来只说「启用 3 位 / 注册表共 5 位」——
那是 legacy 三列集注的口径，而**产品默认是 structured（五位全参与综合分析）**。
演示前看到"3 位"、界面上写着"五家综合"，当场解释不清。

改法：三个数一起报，且本次模式那个数**从注册表现算**：

```
✓ 医家注册表：本次模式 structured 参与 5 位（叶天士、吴鞠通、张锡纯、李可、王云启）；
  三列集注启用 3 位 / 注册表共 5 位
```

这是同一个形状的老问题（SOURCES 第 82 条：一个注册表字段回答两个问题）的
**显示层残留**——`enabled` 与 `in_synthesis` 早就分开了，自检脚本还在只读前者。

---

## 五、需要用户决定的一件事

**CLAUDE.md 里 `Field(min_length=1)` 的基线数字停在 31，实际是 72。**

那一节的历史是连贯的（13 → 19 → 17 → 31），断点在 31 之后：M 系列与 R33/R34
新增的 schema（`S3Structured` 五步链、符号验证器的 `Violation`/`Unverifiable`、
药理层四个 schema 等）把它推到了 72，而**每一轮的报告都如实报了 72**
（R36/R37/R38/R39 四份报告的自查里都有这个数），只是 CLAUDE.md 那一行没跟着改。

两件事要分清：
- **约束没有被放松**——72 > 31，且这四轮"一处未动"；
- **基线记录过期了**——下一个人按 CLAUDE.md 的 31 去核，会以为多出来 41 处是违规。

`CLAUDE.md` 是您的文件，所以这一轮**没有动它**。建议把那一行改成
「**当前基线：72 处（截至 `57b5bb2`）**」，并保留 13→19→17→31→72 这条演变链——
它本身就是"约束只增不减"的证据。

---

## 六、这三轮之后，还欠什么（全部是 ⏳，不是缺陷）

| 欠的 | 为什么欠 | 补的办法 |
|---|---|---|
| 四组消融的三指标真机数 | 沙盒没有 API key | `scripts/run_onsite.sh` 段 11（150 次调用） |
| MTCMB TCM-PR 的分 | 数据不在这台机器上，**字段映射也没实测过** | 上机先 `--probe`（0 调用），再两组各跑一次 |
| 跟 BianCang / ShizhenGPT 的对比 | 要 GPU 起模型 | 当后端起起来跑同一份 `eval.mtcmb.run --solver baseline` |
| 申报书的实际补充 | **原件不在仓库里** | `annotate_docx fingerprint` 是第一步，零风险只读 |
| 一次问诊墙钟 / 首 token / 超时失败数 | 要真实 LLM | `bench_consult --backend real`（R36 的 P11~P13） |

**⏳ 的数量不是问题，⏳ 有没有路径才是。** 这五项每一项都有一条能跑的命令，
跑完那条命令数就有了——而不是跑完之后有人去改一段描述。

---

## 七、验收结论

三轮的交付**全部落地且可核**：

- R37 的九段单链在真浏览器里**看得见**（不是"类名对"），并在过程中修掉了四个
  "判据全绿、人看不见"的洞；
- R38 交付的是量具而不是数，而"没有数"这件事本身被做成了可核的
  （`content_metrics_valid`、三种空格子、`--probe`）；
- R39 把"申报书黑字一个都没动"从承诺变成了一条**会报红**的命令。

这一轮验收自己也遵守同一条规矩：上面每个数都是现跑出来的，
没有一个是从报告里抄过来的。
