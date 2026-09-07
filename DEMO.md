# 演示脚本

给评审/演示用的一份可执行脚本：按顺序跑，每一步说清楚"在演示什么、判据是什么、
如果这台机器没有真实 LLM/网络该怎么办"。跟 `README.md`「快速开始」的区别是——
`README.md` 是给开发者的安装说明，这份是给演示者的时间线。

前置：已完成 `README.md`「快速开始」第 1-3 步（装依赖、配好 `.env`、生成
`cases.json`）。没有 `cases.json` 的环境仍能跑第 0、6、8 步（不需要真实医案），
其余步骤会如实报错并提示需要哪个文件——这是设计好的行为，不是 bug。

## 第 0 步：确认环境（30 秒）

```bash
pytest -q
```

判据：全绿。这条命令不碰网络、不需要 API key——如果这一步都不过，后面的
演示不用做了，先把这个修好。

## 第 1 步：核心流程——两位名医对同一条主诉的辨证对照（2 分钟）

打开 `http://localhost:8000`（先 `./run.sh`），输入 `tests/queries.txt` 第 1 条：

> 胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。

演示重点：
- **推理期间页面上有分步进度**（症状标准化 → 证素推断 → 追问 → 每位医家
  取证/开方），不是一句静态的"请耐心等待"。三位医家 + ReAct 全开时一次问诊
  能跑到几分钟，看不见进度的话演示现场很难熬。走的是 SSE 端点，
  想看原始事件流用 `curl -N`（README「HTTP 接口」一节有命令）
- 叶天士、吴鞠通各自的证型/治法/方药并排显示
- 每条结论下方能展开看引用了哪条真实医案（`case_id` + 相似度），点开能看到
  原文——这是防幻觉的核查入口，不是摆设
- 图上节点/边**划过就有速览**（症状的解释状态、证素的病位/病性、证型所属医家、
  边是主症还是次症）；药物节点只显示药名，剂量不挤进标签
- 分歧判定用的是用药 Jaccard 距离，不是证型名字符串比对（字符串比对在 10 条
  测试主诉上 9/9 全判分歧，没有区分度，见 `data/SOURCES.md`）
- 如果 `eval/epsilon.json` 已经生成过（跑过第 5 步），分歧度旁边会带着噪声
  地板 ε 做对照；没生成就诚实显示"未测"，不是编一个假的对照基准

## 第 2 步：安全否决——危重症状不出方（1 分钟）

输入第 10 条测试主诉：

> 胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。

判据：系统直接拒绝辨证，提示转诊，**不产出任何方药**（不是在结果后面加一句
"建议就医"）。这一步在证素推断之前拦截——`core/chain.py` 的 `consult()`
一进来就查 `check_safety()`，S2/S3 一次都不会被调用。

## 第 3 步：追问闭环（可选，2 分钟）

输入一条信息不足的主诉（比如只写"胃不舒服"），系统会问 1-3 轮封闭问题。
演示 `ScriptedPatient`（不调 LLM，机械按预设回答）：

```python
from core.chain import consult
from eval.patient_sim import ScriptedPatient
result = consult("胃脘胀痛，嗳气泛酸，纳差", ask_fn=ScriptedPatient(present=["两胁胀满"]))
```

演示重点：追问问出的答案先过 `check_safety`——如果回答里带危重信号，整轮
终止、不产出方药，这是防止追问变成安全否决层后门的硬约束（见
`data/SOURCES.md` 关于「有没有便血？」→「有」这条真实修复记录）。

## 第 4 步：检索三路融合（K3a/K3b，命令行演示，1 分钟）

```bash
python -m offline.build_jieba_dict
python -m offline.build_element_index   # 需要 cases.json + data/graph.json
python -c "
from core.retrieval import get_retriever
r = get_retriever()
for mode in ['dense', 'bm25', 'graph', 'hybrid']:
    try:
        hits = r.search('胃脘胀痛，嗳气泛酸', 'ye_tianshi', k=1, mode=mode,
                         query_elements=['脾','气滞'] if mode in ('graph','hybrid') else None)
        print(mode, hits[0][0].case_id if hits else '(空)')
    except Exception as e:
        print(mode, '不可用：', e)
"
```

判据：`bm25`/`graph` 两个模式不需要下载 embedding 模型，任何环境都能跑；
`dense`/`hybrid` 需要网络下载 `BAAI/bge-small-zh-v1.5`——没有网络的环境这里
会报错，如实说明，不是这两个模式的代码有问题（`data/SOURCES.md` 第 19、
21 条有这台开发环境的真实验证记录）。

网页上也能直接切：问诊页「检索模式」下拉框，或者请求体里带 `retriever_mode`。
**这是逐请求的，不是服务端全局设置**——两个人同时用同一个服务、各选一种模式
互不影响。可以现场演示两条失败路径的区别：

```bash
# 模式名写错 → 立刻 400，一次 LLM 调用都不花
curl -s -X POST http://127.0.0.1:8000/api/consult -H "Content-Type: application/json" \
  -d '{"complaint": "纳差乏力", "retriever_mode": "没有这个模式"}'

# 模式认识但这台机器缺数据（graph 缺 element_index.json）→ 200 + 一句人话，
# 不是 500，也不会偷偷换个模式跑完假装成功
curl -s -X POST http://127.0.0.1:8000/api/consult -H "Content-Type: application/json" \
  -d '{"complaint": "纳差乏力", "retriever_mode": "graph"}'
```

第二条的重点是**没有静默降级**：降级的话演示对象会以为自己看到的是证素路的
结果，E8 消融那组数字也就失去意义了。

## 第 4.5 步：图谱浏览器页签（1 分钟）

切到页面顶部的「图谱浏览器」页签。初始只铺 17 个证型节点，点开才逐步展开
连着的证素/症状；能按名字搜索，能按医家切换 λ1 权重（边的透明度按 λ1 编码）。

演示重点是**顶部那段 λ1 说明**：它按当前这张图的实际内容动态生成，两种成因
说的是不同的话——图里根本没挂医案是一种，挂了医案但医案证型跟国标术语对不上
是另一种。后者是这个项目一个真实的负面发现（839 条医案仅 91 条标注证型、
其中 0 条匹配国标证候名），**不藏**。如果医案层是空的，"国标层/医案层"
切换按钮直接不显示，而不是显示一个点了没反应的。

## 第 5 步：噪声地板 ε（命令行，3-5 分钟，消耗真实 LLM 调用）

```bash
python -m offline.estimate_epsilon --dry-run          # 先看预估调用数
python -m offline.estimate_epsilon --n-repeats 3       # 真的跑，写 eval/epsilon.json
```

判据：`eval/epsilon.json` 生成后，第 1 步前端的分歧度会自动带上这个 ε 做
对照——这是「任何数字都必须带对照」这条项目硬约束在 UI 上的落地。

## 第 6 步：证素轨迹（附属功能，命令行，1 分钟）

```bash
python -m offline.quota                                # 先看样本量够不够门槛
curl http://localhost:8000/api/trajectories/ye_tianshi | python -m json.tool
```

判据：只有带复诊序列（同一 `case_group_id` 下 ≥2 诊）的病人会出现在结果里；
`data/element_index.json` 没生成时端点返回 503（不是 500，前端能区分"数据
没准备好"和"系统故障"）。**这一步只展示轨迹，不预测下一诊会怎么转移**
——样本量不够，拟合转移模型只会制造一个看着像结论的噪声数字，故意不做。

## 第 7 步：评测汇总 + 盲评（命令行，5+ 分钟，消耗真实 LLM 调用）

```bash
python -m eval.run_eval --queries-path tests/queries.txt
cat eval/report.md
```

判据：报告里每个数字旁边都带着对照基准（ε、有无参考医案分组、否决/正常
两组调用数、McNemar p 值）——`build_report()`/`render_markdown()` 的设计
就是不允许产出一个没有对照的数字。

盲评（人工评分，跳过实际填写只演示流程）：

```bash
python -m eval.mes.export --limit 3
cat eval/mes/items.json   # 展示：只有 A/B，看不出哪个是叶天士哪个是吴鞠通
```

## 第 8 步：外部评测 TCMEval-SDT（如果有数据集，命令行，视规模而定）

```bash
export SDT=<TCMEval-SDT 数据集路径>
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver chain --out out/sdt_chain.txt
```

见 [`eval/sdt/README.md`](eval/sdt/README.md)——这是外部数据集、外部计分
标准，跟第 7 步的 V1（本项目自定义指标）是两套独立的评测，不要混着报数。

## 讲清楚"这个 demo 做不到什么"（演示末尾，1 分钟）

演示到这里之后，主动说明这些边界（见 `README.md`「不能做」一节），
不要等评审自己发现：

- 样本量各 30 案，不足以支撑统计结论，仅够验证系统管线
- 安全拦截关键词表未经医学生审核，可能漏报/误报
- λ1（医家层图谱权重）恒为 0——古籍证型跟国标术语体系基本不相交，
  是数据的客观性质，不是代码缺陷
- 这台环境的具体网络限制（连不上 huggingface hub）导致 dense/hybrid
  检索的真实数字这台环境产不出来，代码路径已验证、真实数字待 AutoDL
