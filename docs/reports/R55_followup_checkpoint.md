# R55 补充核实：MAX_ASK_ROUNDS 执行确认 + tcm_theory.jsonl 版本控制 + curated/classic 出处

用户在 R55 完成后提出三点核实，逐条回应如下。

## 1. `MAX_ASK_ROUNDS` 是不是真的没执行

**结论：R55 §5.1 第 4 条已执行，`core/followup.py:27` 的 `MAX_ASK_ROUNDS = 3`
保持不变是有意的，不是遗漏。**

`MAX_ASK_ROUNDS` 这个模块常量从来不是"产品默认追问轮数"——它是
`run_followup()` 自身签名的默认值，服务于两类调用方：

1. CLI / eval / 批跑脚本（不经过 API 层，也不知道"角色"这个概念）；
2. `student`/`researcher` 两个角色——spec 原话"学生可开（教学）"，意思是
   **不限制**，不是"给它另一个具体的数"。

真正的产品默认在 `core/followup.py::ROLE_MAX_ASK_ROUNDS = {"doctor": 0,
"patient": 1}` + `max_ask_rounds_for_role(role)`：

```
doctor    → 0（已完成四诊，再追问是把医师当患者审）
patient   → 1
student / researcher / 未识别角色 → 落回 MAX_ASK_ROUNDS（不限制）
```

这个整数由 `api/main.py` 在请求进来时算好（`max_ask_rounds_for_role(role)`），
经 `consult(max_ask_rounds=...)` 传给 `run_followup(max_rounds=...)`。`role`
本身**不**往 `core.chain.consult()` 里传——这是既有的架构边界（见
`api/main.py` 里 `_filter_response_by_role` 附近的注释："role 传得太深只会让
consult() 也去关心跟辨证无关的展示逻辑"），R55 延续了这条边界，只多传一个
已经解出来的整数。

而且 `core.product_mode.default_role()` 在产品模式（`PRODUCT_MODE=1`，
默认）下就是 `"doctor"`——请求不带 `role` 字段时，走的正是医师这一档，
不需要显式选择。

**新增的端到端证据**（回应"加测试：医师角色的事件流里 followup 轮数为 0"）：

`tests/test_api_stream.py::test_doctor_role_asks_zero_followup_rounds_in_the_real_event_stream`

- **不 mock `api_main.consult`**，让真实的 `core.chain.consult()`（含真实
  `run_followup` 调用）通过 `/api/consult/stream` 端点跑起来；
- 假后端的证素能匹配到候选问题（不是"候选池本来就是空的，问了 0 轮证明
  不了任何事"这种假阳性场景）；
- 断言 SSE 事件流里 `followup_done.rounds == 0`，以及最终 `done` 事件的
  `followup.rounds == 0`。

跑通：

```
$ python -m pytest tests/test_api_stream.py -k doctor_role_asks -q
1 passed
```

之前（R55 提交时）已有的两条单元测试（`tests/test_chain.py::
test_max_ask_rounds_zero_reaches_run_followup_and_asks_nothing`、
`tests/test_api.py::test_doctor_role_makes_the_api_pass_zero_ask_rounds`）
分别钉住"整数 0 传到 run_followup 之后真的不问"和"API 层把 role 正确翻译成
整数"两段，这条新测试把两段接成一条完整链路，直接对应用户要的"事件流里
followup 轮数为 0"。

## 2. `data/standard/tcm_theory.jsonl` 是否已 commit + push

**已确认：是。**

```
$ git log --oneline -- data/standard/tcm_theory.jsonl
dc80163 R51：医理规则层——藏象/病机/治则/配伍四类规则，给演绎推导一个地基

$ git log --oneline origin/claude/tcm-demo-multi-round-hjp4q1 -- data/standard/tcm_theory.jsonl
dc80163 R51：医理规则层——藏象/病机/治则/配伍四类规则，给演绎推导一个地基

$ git status --short data/standard/tcm_theory.jsonl
（空，无未提交改动）

$ git check-ignore -v data/standard/tcm_theory.jsonl
（无输出，未被 .gitignore 拦截）
```

R51 落地时就已提交并推送到远端分支，`git pull` 就有，不需要重新生成。

## 3. 171 条里 146 条 curated、25 条 classic——逐类说明依据

重新跑一遍统计（不依赖记忆，现读文件）：

```python
rows = [json.loads(l) for l in open('data/standard/tcm_theory.jsonl', encoding='utf-8')]
# total 171
```

按 `kind` × curated/classic 交叉统计：

| kind（规则类别） | curated | classic | 小计 |
|---|---:|---:|---:|
| `organ_relation`（藏象关系，A 类） | 43 | 0 | 43 |
| `pathomechanism`（病机传变，B 类） | 50 | 0 | 50 |
| `treatment_principle`（治则推导，C 类） | 53 | 0 | 53 |
| `compatibility`（配伍理论，D 类） | 0 | 25 | 25 |
| **合计** | **146** | **25** | **171** |

四类规则里，**A/B/C 三类整体是 curated，D 类整体是 classic**——不是逐条
混杂，判据是"这一类背后有没有一份项目里真的持有、可以逐字核对的原文"。

### curated 的依据（146 条：A/B/C 三类）

出自 `offline/extract_tcm_theory.py` 模块文档字符串（该文件是这 146 条的
唯一生成来源，人工编写的规则条目本身也在这个文件里，`ORGAN_RELATIONS`/
`PATHOMECHANISMS`/`TREATMENT_PRINCIPLES` 三个列表）：

- **A 类（藏象关系，43 条）**：按中医基础理论"藏象学说"的通用表述人工
  整理——五行生克（相生/相乘/相侮）、脏腑表里、脏腑相关等，属于学科公认
  的基础理论常识。`source` 字段如实写明"本项目未持有该教材电子版"：
  授权 clone `PanckooAI/TCM_Datasets`（含《中医基础理论》电子版）未完成，
  没有一份可以逐字核对页码的教材原文，所以标 `curated` 不标 `classic`，
  **不冒充某一版教材的逐字引用**。
- **B 类（病机传变，50 条）**：同样按"病机学说"通用表述整理，同一条
  理由（教材电子版缺失）。
- **C 类（治则推导，53 条）**：按历代治则理论通用表述整理，包含
  "虚则补之""实则泻之"这类经典治则口诀（脚本注释称这部分为"C 类的 12
  条经典治则"），以及针对具体病位/病性把治则具体化的操作性条目（其余
  41 条）。口诀本身是学科公认的标准提法，但项目里没有《素问》或《中医
  基础理论》的电子版可供子串校验，同样标 `curated`。

三类的 `source` 字段原文（各自在 jsonl 里精确出现的次数）：

```
43 条 A 类：source = "人工整理（据中医基础理论藏象学说通用表述，本项目未持有该教材电子版）"
50 条 B 类：source = "人工整理（据中医基础理论病机学说通用表述，本项目未持有该教材电子版）"
53 条 C 类：source = "人工整理（据历代治则理论通用表述，本项目未持有可校验页码的教材版本）"
```

**这不是降级，是诚实标注**：内容本身是学科公认的基础理论（任何一本
《中医基础理论》教材都会写同样的内容），只是这个项目里没有一份可核对
页码的电子版本。脚本文档字符串里写明了后续路径：将来拿到教材全文，
这三类可以整批换成 `classic` 并补 `span`/`source`，`core/theory.py` 的
查询接口不用改。

### classic 的依据（25 条：D 类，配伍理论）

`books/中药学.md`（18 条）、`books/方剂学.md`（7 条）两本教材总论**确实
在本项目里**（`books/` 目录，README 3.1 节说明的下载来源）——七情
（相须/相使/相畏/相杀/相恶/相反/单行）、君臣佐使、组方原则、药味加减、
药对经验，全部来自这两本书总论段落的表述。

`offline/extract_tcm_theory.py::_verify_span_in_book()` 在写文件**之前**
逐条断言 `span` 是对应书文件内容的真实子串——抽不到就让脚本崩溃退出，
不静默放行一条编造的出处，跟 CLAUDE.md 防幻觉约束的精神一致（这里用的是
子串校验而不是 `Field(min_length=1)`，因为约束的对象是"这段引文真的存在
于书里"，不是"字段非空"，两者是不同层面的校验，都不能放松）。

### R57 消融若 C 组不达标，第一个要查什么

`compatibility`（D 类，25 条 classic）在四类规则里样本量最小、且是
`verify_formula` 十一条规则里**唯一**同时被 `ONTOLOGY_RULES`
（`role_structure`）与 `THEORY_RULES`（`role_structure_by_rule`）两条
规则引用的类别——如果 C 组（关掉医案佐证、只靠医理层）的配伍相关判据
（`role_structure_by_rule`）通过率偏低，大概率是这 25 条 D 类规则本身
的覆盖面不够（只覆盖组方通则，不是分证型/分脏腑的细则），不是 A/B/C
三类 curated 规则的问题——后者数量大（146 条）、覆盖藏象/病机/治则三个
维度，是"演绎推导能不能凭医理走通"的主要依据。反过来，如果 C 组在
`principle_matches_syndrome`/`pathomechanism_consistent`（依赖 curated
的 A/B/C 类）上通过率低，才该回头查这 146 条 curated 规则的颗粒度或
覆盖面是否不够——两类问题的诊断路径不同，不能混在一起查。
