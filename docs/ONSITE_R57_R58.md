# R57 / R58 真机操作手册

给不参与开发、只需要在 AutoDL 机器上把这两轮真机数据跑出来的人看的。
每一步都是能直接粘贴执行的命令，没有 `<xxx>` 这种占位符（除非明确写了
"换成你自己的路径"）。跑不通、看不懂输出，回到本文件对应小节找答案；
本文件之外不需要再翻别的报告。

## 在开始之前：两个数字不要相信

这个仓库自己的记录里，R57/R58 单次真实问诊的墙钟耗时有两处互相矛盾的
数字（一处写"45~75 秒一档"，另一处写"264.6~276.3 秒"，差 3~4 倍），核对
后发现量出后一个数字的工具根本不存在于仓库里——**没有办法确认哪个准**。
`docs/reports/R51-R58_report.md`「R55」一节记了这处矛盾。

**结论：不要相信本文件之外任何地方写的时间/费用估算，包括 README 和历史
报告里的数字。** 本文件第一步永远是"先跑一小批，量出你自己机器上的真实
数字"，后面所有时间/费用估算都从这一步现场算，不引用任何历史数字。

---

## 零、两轮共用的前置检查

在你的 AutoDL 机器上，进到仓库目录，依次确认：

```bash
cd TCM-demo-implementation   # 换成你自己的仓库路径
git pull origin claude/tcm-demo-multi-round-hjp4q1
```

**1. 真实 API key 配好了**

```bash
cat .env | grep -E "^LLM_MODE|^LLM_API_KEY|^LLM_MODEL"
```

应该看到 `LLM_MODE=api`（或没写这一行，默认就是 `api`）、`LLM_API_KEY=`
后面跟着一串真实 key、`LLM_MODEL=deepseek-v4-pro`（如果是别的模型名，
注意 `deepseek-chat` 这个旧默认值已在 2026-09 下线，用它会导致每次调用
返回空响应）。没有 `.env` 文件就 `cp .env.example .env` 再编辑。

**2. `cases.json` 已经生成**（R57 的 A 组要检索医案，没有这份文件 A 组会
跑不出内容指标）

```bash
python3 -c "import json; d=json.load(open('data/cases.json')); print(len(d), '条医案')"
```

报错说明文件不在，按 `README.md`「快速开始」第 3 步生成（如果之前已经在
这台机器上跑过 demo，这一步通常已经做过，不用重做）。

**3. `data/standard/tcm_theory.jsonl` 在**（R51 医理规则层，R57 的核心组
C 全靠它）

```bash
wc -l data/standard/tcm_theory.jsonl   # 应该是 171
```

**4. Playwright 装好**（只有 R58 需要，R57 不需要）

```bash
python3 -c "import playwright" 2>/dev/null && echo OK || \
  (pip install playwright && playwright install chromium --with-deps)
```

四项都过了才往下走。

---

## 一、R57：四组消融，80 次问诊

### 1.1 第一步：先跑一小批，量出真实耗时（必须做，不要跳过）

```bash
export LLM_TIMEOUT_SECONDS=900
git clone https://github.com/zhuyan166/TCMEval.git   # 已经克隆过就跳过这行
python -m eval.ablation.r57 --backend real --limit 2 \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --out /tmp/r57_trial.json --md /tmp/r57_trial.md
```

`LLM_TIMEOUT_SECONDS=900` 这一步不是修复某个已知会触发的 bug——`api`
后端的代码默认值已经是读超时 600 秒/墙钟 900 秒，理论上够用；显式设成
900 只是让"够不够用"这件事不用去猜代码默认值对不对，多一道保险，不设
也大概率没事。

`--limit 2` 只跑 2 条主诉 × 4 组 = 8 次真实问诊，几分钟到二十分钟内应该
跑完。跑完看终端最后打出的表格，或者：

```bash
cat /tmp/r57_trial.md
```

表格最右一列"墙钟"就是每组的 `elapsed_s_mean`（单次问诊平均秒数）。
**记下这个数字**——不同组因为要不要检索医案、要不要跑第三相佐证，耗时
会不一样，一般 D 组（演绎+事后佐证）最慢。

### 1.2 第二步：用真实数字估算完整 80 次的时间和费用

设"最慢那组的 elapsed_s_mean"为 `T` 秒。完整跑法是 20 条主诉 × 4 组，
四组耗时不同，粗略按"四组平均墙钟 × 80"估算总耗时：

```
总耗时（秒）≈ (A组秒数 + B组秒数 + C组秒数 + D组秒数) / 4 × 80
```

比如四组分别是 60/55/70/90 秒，总耗时 ≈ (60+55+70+90)/4 × 80 ≈ 5500
秒 ≈ 1.5 小时。**这是从你自己机器、自己网络条件下现场量出来的数字，不是
猜的**——这正是不信任历史数字、自己重新量一遍的意义。

费用：`/tmp/r57_trial.md` 或 `/tmp/r57_trial.json` 里 `groups.*.llm_calls_mean`
是每次问诊平均调用几次模型。8 次问诊（--limit 2）的真实花费可以在你的
DeepSeek 账户后台账单里直接看到（几分钟内会更新），拿这个真实花费除以 8
再乘以 80，就是 80 次的费用估算——比套用任何写死在文档里的单价更准，
因为不同 prompt 长度、不同推理档位实际花费差异很大，账单是唯一的真话。

### 1.3 第三步：正式跑——推荐按组分开跑，不要一条命令跑完四组

一条命令跑完 ABCD 四组，中途断了（断网、断电、AutoDL 实例被抢占）**没有
断点续跑**——`eval.ablation.r57` 每次调用只把这次跑的组写进报告文件，
不会读、不会合并磁盘上已有的文件。80 次问诊按 1~2 小时算，断的概率不是
零，一条命令跑完损失的是全部已完成的进度。

**推荐做法：分四次跑，每组存到自己的文件**：

```bash
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --groups A --out eval/report_ablation_r57_A.json
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --groups B --out eval/report_ablation_r57_B.json
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --groups C --out eval/report_ablation_r57_C.json
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --groups D --out eval/report_ablation_r57_D.json
```

每条命令跑的是 20 次问诊（一组），耗时是总时间的 1/4。**哪一组断了就只
重跑哪一组**，前面跑完的组的文件原样留着，不用动。

如果你更想一次性跑完不分组（网络稳定、不怕断），也可以：

```bash
python -m eval.ablation.r57 --backend real \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT \
    --out eval/report_ablation_r57.json
```

这种情况下没有中间产物，断了就要整个重跑，直接跳到「1.5 判读」即可，
不需要下面的合并步骤。

### 1.4 第四步：合并四组的独立文件

```bash
python -m eval.ablation.merge_r57 \
    eval/report_ablation_r57_A.json eval/report_ablation_r57_B.json \
    eval/report_ablation_r57_C.json eval/report_ablation_r57_D.json \
    --out eval/report_ablation_r57.json
```

这一步不是简单拼 JSON 文本——它会把四份文件里每条主诉的原始结果重新
汇总一遍，算出完整的三条硬指标（尤其是 C 与 D 的一致率，这个数必须同
时看到 C 和 D 两组的原始数据才能算，单独一份文件里没有）。如果四份文件
不是同一批主诉跑出来的（比如中途换了 `--sdt-dir`），这一步会直接报错
拒绝合并，不会给你一份看起来正常、实际张冠李戴的假报告。

### 1.5 怎么看通过没通过——不用翻 JSON

```bash
cat eval/report_ablation_r57.md
```

（如果是分组合并出来的，是 `eval/report_ablation_r57.md`；如果一次跑
完没分组，同一个文件名。）这份 markdown 文件本身就是给人看的，最后
"总判定"那一行直接写着：

- `✅ 三条全过` —— R57 通过了，可以进 R58。
- `❌ 至少一条没过——回 R51 补规则` —— 看下面「二、R57 失败了怎么办」。

想确认退出码也行：合并命令 / 单组命令跑完，`echo $?` 是 0 表示（就
`n_fail` 而言）没有问诊失败，不直接等于三条硬指标都过——**判读以
markdown 文件里的"总判定"文字为准，不是退出码**。

---

## 二、R57 失败了怎么办

三条硬指标之一没过（"总判定"显示 ❌），按下面的判断走，**不要凭感觉
猜是哪里的问题，先跑诊断脚本拿证据**：

```bash
python -m scripts.diagnose_r57_group --group C \
    --sdt-dir TCMEval/evaluation/TCMEval-SDT
```

这条命令会用**同一批 20 条主诉**（跟正式跑的是同一个筛选算法，确定性
可复现，选出来的主诉应该跟正式跑一致）重新跑一次 C 组，但这次会把每
一步"依据不足"的原因和缺的规则类别都打印出来，输出长这样：

```
## 1. 「依据不足」按缺的规则类别计数

| 规则类别 | insufficient 次数 | 举例（what） |
|---|---:|---|
| 病机传变（pathomechanism） | 7 | 脾虚夹湿证的传变链条查不到 |

最薄的一类：病机传变——回 R51 补这一类规则……
```

看这份输出决定下一步：

- **表格非空，某一类规则出现次数明显多**：说明 `data/standard/tcm_theory.jsonl`
  这一类规则（藏象关系/病机传变/治则推导/配伍理论）覆盖面不够，需要回
  R51 补规则——参照 `offline/extract_tcm_theory.py` 里同类规则已有的
  生成方式加条目，不是这份诊断脚本的职责，是需要找开发方处理的事。
- **表格是空的（零 insufficient）**：说明模型每一步都声称引用了规则，
  但完整率或验证器通过率还是不够——问题更可能在符号验证器的判据本身
  （`core/formula_verifier.py`），或者规则质量有问题（引了规则但引得不
  对），这两种都需要把 `python -m scripts.diagnose_r57_group` 的完整
  输出**发给开发方**，附上 `eval/report_ablation_r57.md`，不是自己能
  当场判断的事。
- **看到"⚠ ... 引用的 id 在 core/theory.py 里查不到"**：这条比规则库
  覆盖不够更严重，说明防幻觉校验被绕过了，立刻把这条警告和
  `eval/report_ablation_r57.json` 一起发给开发方，不要继续往下跑。

**C 与 D 一致率不达标**（不是 rule_refs 完整率或验证器通过率）：这条
诊断脚本帮不上忙——C 与 D 的差异是要不要跑第三相事后佐证，理论上不该
影响结论。看 `eval/report_ablation_r57.json` 里 `c_vs_d_consistency.mismatches`
字段，把不一致的 `record_id` 和 `eval/report_ablation_r57.md` 一起发给
开发方，这属于代码逻辑问题，不是规则库覆盖面问题。

**任何一条硬指标不达标，都不要把医案检索加回 C 组的推导过程"救"
一下**——那是回退到 R51 之前的做法，不是修复，任务书原话就是这么写的。

---

## 三、R58：真机验收

### 3.1 前置：R57 必须先过

```bash
python3 -c "import json; d=json.load(open('eval/report_ablation_r57.json')); print(d['all_gates_passed'])"
```

打印 `True` 才能继续。打印 `False` 或文件不存在，回上面「二、R57 失败了
怎么办」，不要跳过直接跑 R58——R58 脚本自己也会检查这件事，没过 R57 时
仍然会把 9 次问诊跑完，但报告里 `overall_ok` 恒为 `False`，跑了也白跑。

### 3.2 跑

```bash
python -m scripts.acceptance_r58
```

不需要手动起服务——脚本自己会拉起一个临时的 uvicorn 子进程（自动选空闲
端口），跑完自动关掉。9 次问诊（3 条主诉 × 3 个角色）+ 5 张真实截图，
参照「一、1.2」的方法自己估算耗时（比 R57 的 80 次问诊少得多，通常是
分钟到十几分钟量级，具体看 1.1 步量出来的单次耗时乘以 9 再加上截图的
固定开销）。

### 3.3 怎么看通过没通过

终端最后一行直接打印：

```
总判定：✅ 通过
```

或

```
总判定：❌ 未通过（看上面各项明细）
```

退出码同步（0/1）。完整结果在 `eval/report_acceptance_r58.json`，但
**日常判读不需要打开它**——终端那一行已经是最终结论。只有"未通过"时才
需要看细节，方法见下面「四、R58 失败了怎么办」。

### 3.4 五张截图在哪、怎么看

```bash
ls docs/screenshots/r58/
```

五张图对应问诊的五个阶段（首屏/进行中/问诊完成/图谱浏览器/节点释义），
文件名带序号，直接用图片查看器打开检查页面显示是否正常——这一步是给
人眼看的，没有自动判据。

### 3.5 禁词残留怎么看

```bash
python3 -c "
import json
d = json.load(open('eval/report_acceptance_r58.json'))
hits = d.get('banned_word_hits') or []
if not hits:
    print('没有禁词残留')
for h in hits:
    print(h['name'], '→', h['banned_words_found'], '截图:', h['screenshot'])
"
```

有输出说明哪张截图对应的页面文字里出现了禁词表（唯一出处
`tests/test_no_demo_artifacts.py::BANNED`）里的词——打开对应截图确认
是在哪个界面元素里说出来的（常见位置：模型的自由文本回答、思考过程），
这是内容问题，需要开发方去查模型输出或 prompt，不是这份验收脚本能自动
修的。

### 3.6 对照 R56 十条清单的销号表在哪

`docs/reports/R51-R58_report.md`「R56 产品面与交互收口」一节「二、十条
清单逐条销号」——那张表是**代码层面的静态核实**（截图里的原始 JSON、
λ1 说明这类文案有没有被拿掉）。R58 验的是另一件事：**这次真实问诊、真实
渲染出来的页面，模型说的话本身有没有漏出禁词**（3.5 那步），两者不是
同一层，R58 的报告不会重复 R56 的十条清单，R58 通过不代表 R56 清单
所有条目都自动核实过——如果怀疑 R56 有回归，去看那张表逐条重新肉眼确认。

---

## 四、R58 失败了怎么办

先看 `overall_ok` 为什么是 `False`——四个原因互斥，分别处理：

```bash
python3 -c "
import json
d = json.load(open('eval/report_acceptance_r58.json'))
print('R57 闸门：', d['r57_gate'])
print('响应形状问题：', d['shape_problems'])
print('医案摘录溯源问题：', d['case_excerpt_ungrounded'])
print('禁词残留：', [(h['name'], h['banned_words_found']) for h in d['banned_word_hits']])
"
```

- **`r57_gate.passed` 是 `False`**：回「一、二」重新跑通 R57，R58 没有
  单独的修复路径。
- **`shape_problems` 非空**：某个角色（patient/doctor/researcher）看到
  的响应字段跟 `api/main.py::_filter_response_by_role` 应有的形状对不
  上——把这段输出发给开发方，这是代码逻辑问题。
- **`case_excerpt_ungrounded` 非空**：发给模型的 prompt 里引用了某条
  医案，但摘录内容在 `cases.json` 里核对不上——**这是防幻觉链路上最
  严重的一类问题**，立刻把完整输出发给开发方，不要继续演示或部署。
- **`banned_word_hits` 非空**：见「3.5」。

四类问题互不排斥，可能同时出现多个，`overall_ok` 是它们的逻辑与——
全部清零才是 `True`。

---

## 五、两条命令的先后与依赖

**必须先 R57 后 R58，不能反过来、不能并行**：

- R58 脚本自己会读 `eval/report_ablation_r57.json` 的 `all_gates_passed`，
  没有这份文件或者是 `False` 都不放行（`overall_ok` 恒 `False`）——这
  是代码里写死的前置检查，不是操作习惯上的建议。
- 两者验的不是同一件事：R57 验的是"这套系统不靠模仿医案也能推出正确
  结论"（内容正确性），R58 验的是"这套系统端到端跑起来好不好用、有没有
  露怯"（可用性与合规性）。R57 没过说明系统本身推理机制有缺陷，此时
  R58 测出来的可用性数字没有意义——地基没打好，装修得再好也没用。

**能不能并行跑（比如两个终端同时开）**：不能，但不是因为会互相报错——
R57、R58 都直接在 Python 进程内调用 `core.chain.consult()`，不经过
`api/main.py` 那层 HTTP 服务，所以 `MAX_CONCURRENT_CONSULTS` 这个应用层
并发上限根本不适用于它们，两边同时跑不会互相拦截。不能并行是因为：
（1）**硬依赖**——R58 要读 R57 跑完写出的 `eval/report_ablation_r57.json`，
R57 没跑完这份文件就不存在，R58 无从判断该不该放行；（2）两边都在打真实
DeepSeek API，同时发请求会抢你账户自己的速率限制，也会让两边「一、1.2」
步现场量出来的墙钟耗时都失真（网络排队跟真实计算时间混在一起，量出来的
数字既不能拿来估算 R57 剩余时间，也不能拿来估算 R58 的时间）。**先跑完
R57、拿到 `all_gates_passed: true`，再跑 R58**，不要图快同时开两个。

**要不要共用同一个已经起好的服务**：不需要考虑这个问题——R57 全程不经
过 HTTP 服务，是直接在 Python 进程内调用 `core.chain.consult()`；R58
的 9 次问诊同样是进程内调用，只有最后拍 5 张截图那一步才会临时拉起一个
uvicorn 子进程，而且是脚本自己管理生命周期（起、等健康检查过、截图、
关掉），不需要你手动起服务，也不存在"跟已有服务共用端口"这类问题。
