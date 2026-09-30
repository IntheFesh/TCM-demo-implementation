# 设计说明

本文件记录数据处理、推理、评测与前端上的具体设计决策及其理由。跨模块的工程约定见
[`ARCHITECTURE.md`](ARCHITECTURE.md)，数据来源与版权见 [`../data/SOURCES.md`](../data/SOURCES.md)，
界面与产品设计见 [`DESIGN.md`](DESIGN.md)。

---

## §1 医案语料与切分

- **保留完整诊次序列。** 一位病人的初诊、二诊、三诊是同一个诊次序列，`CaseRecord` 用
  `case_group_id`（同一病人）、`visit_index`（第几诊）、`prev_case_id`（上一诊）串起来。复诊记录
  是观察证型演变的依据，切分时不截断。
- **两段式切分。** `offline/split_cases.py` 用正则按门类定位原文并切出粗段（零 LLM 调用，
  `--stats-only` 只做统计）；病人与诊次的边界交给 `offline/extract_cases.py` 的 LLM 步骤判断。
  三部书的复诊标记形态差别很大，正则只能提供线索（`follow_hint`），不能当作诊次数。
  `extract_cases.py` 把 LLM 切出的病人数、诊次数与正则估计交叉核对，相差 ≥2 的写入
  `extract_warnings.json` 供人工复核。
- **按书的体例选切分策略。** 《医学衷中参西录》的「医案」卷每个篇名恰好是一个病人，原文自带
  病因/证候/诊断/处方/效果等字段标记，所以用 `strategy="toc_case"`：按目录路径过滤门类、
  一个篇名一个粗段、不按长度再切。案首正则（`head_hints`）是照叶天士的案首体例写的，
  **只在案首体例相同的书之间可比**；换一部书先确认正则对它是否触发，不触发就改用该书自带的
  结构标记（张锡纯医案用「属性：」身份行）。
- **现代医案的切案规则写成可核对的判据。** 李可医案按"标题行（短、不含剂量）+ 医案判据
  （有剂量，且有病人标记或足够长）"切分，丢弃的段落计数报出；王云启医案先用 `probe()`
  数出病人行，切出的案数必须与病人行数一致。两份语料不随仓库分发，脚本保留。
- **参考医家的合并只追加。** `offline/merge_reference_cases.py` 把 `data/cases_<id>.json` 并入
  `cases.json`：按 `case_id` 去重、写盘前备份、幂等，`--remove` 可撤销。
- **西药单列。** 部分医案（张锡纯中西药并用）的处方里有西药。`core.schemas` 在构造 S3 输出时
  把混进 `herbs` 的西药拆到 `western_drugs`，分歧计算只比较中药集合；西药差异单独报告，
  双方都没有西药时为 `None`（这个维度不适用），不是 0。
- **训练/留出按 `case_group_id` 切分。** 同一病人的所有诊次落在同一侧（按 `case_group_id` 的
  哈希分桶），避免同一病人的初诊在训练集、复诊在留出集造成泄漏。

## §2 术语对齐与证候权重

- **医案术语与国标术语不对齐是数据的固有性质。** 清代医案的症状与证型表述（「脘痛」「木乘土」）
  与现代国标术语体系差异很大，按字面匹配时医案证型几乎对不上标准证候，λ1（证候层的医家偏好
  权重）因此接近 0。`offline/graph_stats.py` 区分并打印两种原因：图谱里没有医案节点
  （没有运行抽取），还是有医案但术语对不上。这个缺口靠术语映射层解决，不靠放宽匹配。
- **`graph_stats` 把医家权重回写进图谱。** `weight_by_physician` 由 `offline/graph_stats.py` 统计后
  写回 `data/graph.json`；漏跑这一步时后验计算会静默退化到无偏好。所以
  `python -m offline.build_graph --all` 把三步串起来：建图 → `graph_stats` 回写权重 →
  `build_element_index`，任何一步失败都以非 0 退出；单独运行某一步时打印警告。
- **证素索引由医案症状构建。** `data/element_index.json` 从 `cases.json` 的症状字段推导每条医案
  的证素集合（不依赖医案三元组），`graph` 检索模式用 Jaccard 相似度匹配，不需要 embedding。
- **修正表写明作用范围。** `data/standard/ocr_fixes.tsv` 的第四列是作用范围
  （`name` / `disease` / `symptom` / `all`）；加载时校验范围取值，并拒绝"右列包含另一条规则
  左列"的表（逐条 `str.replace` 会不幂等）。修正在切分之前施加，按左侧长度降序、带上下文
  整词匹配。
- **药名规范化只剥炮制字。** `normalize_herb` 去掉炮制前后缀与剂量，但不剥产地与「生」字：
  「生附子」与「附子」是不同的剂量上限条目。别名表 `HERB_ALIASES` 手工维护；批量合并来的别名
  在 `data/standard/herb_aliases_merged.tsv`，冲突时以代码里的手工表为准。
- **剂量上限的查找顺序。** `core/safety_output.py::dose_limit_entry()` 先按原名查，再按
  `normalize_herb` 的结果查；不能用十八反的粗粒度归类（`normalize_for_incompat`）去查剂量，
  那会把不同品种归到同一条上限。三处调用方共用这一个函数。
- **病位证素是最小集。** `core/elements.py` 的 `LOCATIONS` 只收录有可核验来源的病位，不为单条
  证候临时添加。

## §3 知识图谱

- **`indicates` 边用 `edge_key` 区分来源。** 同一对（症状，证素）之间可以有多条来自不同证候的
  `indicates` 边，`edge_key=f"indicates::{code}"` 让它们作为多重边共存；其余边类型保持
  "更新同一条边"的语义（例如 `weight_by_physician`）。判据是：同一对节点之间是否存在来自不同
  来源的同类边。医案层的边以 `case_id` 为源，不需要 edge_key。
- **症状→证素的边带 `via_syndrome`。** 按证候聚合症状时读边上的 `via_syndrome`（证候编码）；
  任何"按证候聚合"的函数都配一条断言：结果不能全为 0 或全部相同。
- **节点标签去剂量。** `to_graph` 的药名标签去掉剂量与括号注释（先去括号再去剂量），节点 id
  保留原文。
- **`data/graph.json` 是生成物。** 它可以由版本控制内的输入重新生成，所以不进版本控制；
  判断一个文件该不该入库的标准是"能否由仓库内的输入重新生成"。
- **提示词里不写死图谱规模。** `graph_miss_hint()` 在运行时数节点数，图谱不可用时不给数字。

## §4 检索

- **混合检索用 RRF 融合 BM25 与 dense。** BM25 分数在不同查询之间不可比，所以 BM25 一路不设
  `min_score`；`min_score` 只在纯 dense 模式下生效。融合之后不再对每一路另设阈值（那等于给
  单路否决权）。`apply_low_discrimination_cutoff` 只作用于分数有界的 dense / graph 模式。
- **BM25 保底名额。** RRF 只看名次，BM25 一路的强匹配可能被 dense 一路的中等匹配挤掉；
  `BM25_FLOOR_N = 2` 保证 BM25 的前 N 条进入融合结果。调整 `RRF_K` 或加权 RRF 都会改变所有
  查询的排序，保底名额只影响被挤掉的那几条。`scripts/verify_hybrid_fusion.py` 打印每一路的
  名次分解。
- **`full_context` 模式。** 默认检索模式把单个医家的全部医案放进 system prompt
  （`core/context_prefix.assemble()`），段落次序是：共享的知识速查表 → 指令 → 医案全量 →
  药材条目 → 本次问诊。**稳定段在前、变化段在后**，稳定前缀才能命中模型服务的前缀缓存；
  `tests/test_prefix_cache_order.py` 校验这个次序。前缀按 token 预算裁剪时医案永不裁剪，
  共享段按最大的那位医家统一裁剪；token 计数优先用 tiktoken，不可用时按保守上界估算，
  并报告所用的计数方式。
- **知识块进入所有检索模式。** `build_focused_knowledge()` 按本次 S1/S2 的结果挑选本草与方剂
  条目，插在参考医案之前；`full_context` 下由 `assemble()` 组装。测试断言的是最终发给模型的
  字符串里确实有知识块，而不是生成函数的返回值。
- **响应体里的参考医案在序列化边界截断。** 返回给前端的 `refs` 默认最多 20 条
  （`REFS_IN_RESPONSE`），幻觉引用检查用完整列表；被引用的医案永远保留并排在前面。
- **并发安全。** `DenseRetriever` 先发布模型再发布向量，快速路径检查最后发布的字段；编码锁是
  实例级的，不同检索器实例互不阻塞。语料向量的磁盘缓存以"模型名 + 文本数量 + 文本 sha256"
  为指纹，缓存损坏时重新编码并打印警告。

## §5 推理链与安全

推理链的顺序约束（S1 只跑一次、安全否决在 S2 之前、追问回答先过 `check_safety`）见
`docs/ARCHITECTURE.md §3`。这里记录安全判定本身的设计。

- **否定识别按对象判断。** 「不」「无」等否定词只否定紧随其后的对象（正则具名组取对象前缀），
  「不少」「不止」这类数量表达不是否定；在同一句里逐个位置判断，而不是整句一刀切。
- **只有明确否认才放行。** 追问危重症状时，只有明确否认（「没有」「无」）才解除风险；含糊回答
  按风险处理。`mentions_danger`（这句话提到危险了吗）与 `check_safety`（是否需要拦截）回答的是
  两个问题，分开实现。
- **S1 与 S2 合并是一项安全改动。** 分两次调用时，安全闸门位于 S1 与 S2 之间
  （`check_safety([主诉] + s1.symptoms + s1.unmapped)`）。`S1S2_MERGED=1` 的路径保留给工程开关消融，
  打开时被拦截的请求丢弃证素、返回 `s2: null`；产品默认关闭。合并任何两个步骤之前，先确认
  它们之间有没有闸门。
- **按角色处理红旗症状。** 患者角色命中红旗时整页替换为就医提示；医生、学生、研究者角色保留
  完整推理并显示警示。判断集中在 `core/safety.py` 的 `role_sees_full_reasoning_on_red_flag`。
- **方剂建议层与安全层分开。** `core/safety_output.py` 回答"能不能开"（拦截级），
  `core/formula_check.py` 回答"几个候选里哪个更好"（建议级）。建议层五条规则中的三条直接调用
  安全层的函数（`check_incompatible` / `check_dose_limits` / `check_thermal_consistency`），不另写
  判据；`ADVICE_WEIGHTS` 只保证排序（配伍禁忌 > 超剂量 > 寒热 > 缺引经 > 重复），分数不跨问诊比较。
  患者角色的响应不包含建议字段。
- **剂量上限表人工维护。** `DOSE_LIMITS` 按《中国药典》常用量上限整理；药理层抽取的「用量」
  只与它比对（`--crosscheck`），不会改写它。
- **十八反十九畏只有一处实现。** `core/safety_output.check_incompatible`，含别名归类；训练数据
  导出、本地语料统计、医案标注都调用它。

## §6 ReAct 与追问

- **取证清单医案层优先。** ReAct 的取证清单先查医案层（医案三元组、同医家医案），再查标准层；
  清单偏向标准层时模型容易只做术语比对。`core/react.py` 另有两条止损提示（编码歧义、图谱未命中），
  它们是提示，不是禁止。
- **保留剩余步数提示。** 每步提示「还剩 N 步」会影响模型何时收尾；`terminated_by=max_steps`
  表示模型没有自己判断证据已经充分，下游不能把它当作"证据足够"。
- **ReAct 的调用成本。** 每位医家最多 `MAX_STEPS` 次工具调用加一次 S3。
- **追问按信息增益选题。** `question_candidates` 在当前证候后验下计算每个候选症状的信息增益；
  已问过的症状不再出现，症状匹配复用 `check_residual` 的匹配器。追问历史的每一条记录
  `{question, answer, asserted, denied}`。比较候选时比较分子而不是商，差距在浮点误差以内的
  候选视为并列。
- **不合并近义节点。** 图谱里的近义症状节点不自动合并，保留可追溯性；近义关系由同义表
  （`core.syndrome_norm.SYNONYMS`）在匹配时处理。

## §7 符号验证器

- **两种级别、两个数据源。** `core/formula_verifier.py` 的规则分为 veto（不可下发：十八反十九畏、
  超剂量、引用原文张冠李戴）与 revise（回灌给模型重开：归经覆盖、寒热方向、功效与治法、君臣佐使、
  治则与禁忌等）。规则分别从本草/方剂本体与医理规则层取证，两个数据源各自独立可用；医理规则库
  不是穷尽的，所以医理相关的规则只到 revise 级。
- **「无法验证」不等于「通过」。** 数据源缺少所需内容时记为
  `Unverifiable(rule, herbs, missing_predicate, reason)`。只有没有否决、没有修订、且没有无法
  验证项时才是 `passed`，否则是 `partially_verified`；`checked_rules` 不包含无法验证的规则。
- **比率写明分母。** `herbs_grounded_ratio` 的分母是本方的药味数，不是本体的药材总数；本体对
  语料的覆盖率按药名种类与按出现次数两种口径分别报告。
- **依据原文的归属要唯一。** 判断一段依据原文"属于哪味药"时，要求它在本体中能唯一归属
  （`_find_span_owner`，重叠长度下限 `_MIN_ATTRIBUTABLE_OVERLAP`）；格式化短语（归经、用量行）
  在多味药下逐字相同，不能据此判定张冠李戴。`scripts/diagnose_veto_attribution.py` 用本体自己的
  原文测量误判率。
- **覆盖不足时补规则，不放宽阈值。** 医理规则库覆盖不到的病位由补充默认规则解决
  （`core/theory.py::principles_for`）。
- **用 schema 约束模型行为。** 要改变模型的输出，就改变"通过校验需要什么"：推导模式的
  `S3Derived` 没有 `physician_influences` / `cited_case_ids` 字段，而要求每一步给出
  `rule_refs` 或标记 `insufficient`。

## §8 分歧量化与噪声地板

- **噪声地板 ε。** 同一主诉、同一配置、同一医家重复采样，两两之间用药集合的 Jaccard 距离；
  按「主诉 × 医家」逐条给出。分歧指标与它自己那条主诉、那位医家的地板配对比较，不用全局均值
  一刀切（全局均值会在地板低的主诉上漏判、在地板高的主诉上误判，两个方向分开报告）。
  ε 的逐条摊平逻辑只在 `core/chain.py` 一处实现，评测与凭据核对都调用它。
- **三层分歧。** `core_jaccard`（君臣药）、`adjunct_jaccard`（佐使药）与整体 `herb_jaccard` 并列；
  某位医家缺少君臣佐使标注时该层为 `None`（没有数据），不是 0（完全一致）。
  `scripts/verify_role_fill.py` 检查 role 填充率。
- **两两配对与学派分组。** 多位医家之间按两两配对计算距离，按注册表的 `school` 分为师承内 /
  跨学派 / 未知三组；学派未知的医家不参与学派比较。E2（跨学派分歧 > 师承内分歧）在两份报告中
  方向相反，所以只报出、不设闸门。
- **ε 与模型和思考设置绑定。** 换模型或换 S3 的思考设置都是换实验条件，要重新估计 ε；
  关闭思考的 ε 单独存为 `eval/epsilon_s3_disabled.json`，报告里带 `comparability_warning`。

## §9 评测与凭据

- **四类指标。** `eval/run_eval.py` 报告分歧对照 ε、幻觉引用率（按有无参考医案分组）、安全否决率
  及其代价、检索模式比较（McNemar 精确二项检验与连续性校正卡方，`eval/mcnemar.py` 自实现，
  不依赖 scipy）。检索模式比较只在分数有界的模式之间用阈值判据，BM25 用"是否有结果"这一较弱判据，
  并在结果里标明所用判据。
- **调用失败与安全否决分开计数。** 单条调用失败不中断整批评测：失败的样本记
  `skip_reason="call_failed"` 并跳过，失败率超过 `FAILURE_RATE_WARNING_THRESHOLD`（20%）时警告；
  失败分类按异常类型（`core/batch.py::classify_llm_failure`），不解析错误字符串。
- **文档中的数字不手抄。** 每个数字旁写凭据记号 `文件名:键=值`，`scripts/collect_results.py --check`
  逐个去文件里取真值核对，并要求数字出现在同一行正文里；报告的 `.md` 与 `.json` 必须是同一次渲染。
- **TCMEval-SDT。** 评分使用官方脚本；`chain`（注入证素分析）与 `baseline`（同模型、同输出头、
  不注入）必须成对报告，差值才是这条推理链的贡献。安全否决会让部分记录交空答案，这是系统的真实
  行为；`--ignore-safety-veto` 只用于量化安全层的代价，结果单独标注。每次在 Test 上运行都追加到
  `eval/sdt/test_run_log.jsonl`（两段式：运行结束写 `event=run`，官方计分后写 `event=scored`，两者
  按提交文件路径关联）；在 Test 上反复调 prompt 会过拟合，脚本在 Test 运行前打印已运行次数。
  `--error-analysis` 对已有提交重新聚合，零 LLM 调用。
- **MES 盲评。** 导出时隐藏医家与引用的医案 id，字母顺序与抽样使用各自独立的随机种子；回收后
  把胜负映射回医家。
- **消融表的空格要说明原因。** 不适用、假后端量不出内容指标、真实运行失败三种情况分开标注
  （`content_metrics_valid`）；调用数在假后端下照常报告。

## §10 LLM 调用层

- **两层超时。** httpx 的读超时是"每次读 socket"，不是整次调用的总时长；所以每次调用另有一层
  墙钟兜底（`_complete_within_deadline`），超时抛 `LLMCallTimeout`。取值按后端区分
  （云端 API、本地 vLLM 服务、进程内推理），`LLM_TIMEOUT_SECONDS` 可覆盖。常量旁写明取值所依据
  的调用形态：S3 的提示词很长、开启思考、非流式调用只有一次 socket 读。
- **`max_tokens` 按思考设置取值。** 推理模型的 `max_tokens` 同时覆盖不可见的推理 token，所以
  开启思考时默认上限更高，`reasoning_effort=max` 时再提高一档；`LLM_MAX_TOKENS` 可覆盖。
- **截断判定。** 模型输出短于 `max(TRUNCATION_MIN_LENGTH, schema 的最小合理长度)` 且无法解析时
  判为截断（`LLMTruncatedError`），错误信息写明当时的上限与思考设置。
- **并发。** 一次问诊内多位医家并发推理，结果按注册表顺序返回（不按完成先后）；进度事件交错到达，
  按医家 id 路由。请求级的后端覆盖（BYOK、降级回放）通过 `ContextVar` 传递，工作线程用
  `copy_context().run` 继承调用方上下文，包括 `_complete_within_deadline` 的内部线程。进程内同时
  在途的调用数由 `LLM_MAX_INFLIGHT` 信号量限制；限流与超时按 1/2/4 秒加抖动退避，按类别记入
  `manifest.retries`。
- **思考设置集中在一张表。** `STEP_THINKING` 规定每一步是否开启思考（S3 开启，其余关闭），
  `S3_THINKING` 可覆盖 S3；开启思考时不传 `temperature`。实际生效的设置记入 manifest。
- **本地 vLLM 后端。** `LLM_MODE=local` 走 OpenAI 兼容的 vLLM 服务（`VLLMBackend` 继承
  `OpenAICompatBackend`），`local_inproc` 在进程内加载 `vllm.LLM`；vllm 延迟导入。LoRA 目录缺失时
  直接报错，不静默退回基座模型。`scripts/verify_local_backend.py` 检查三件事：输出通过校验、只有
  一次后端调用（guided decoding 参数生效）、每位医家都有 adapter 目录；
  `--show-prompt-budget` 打印各步骤的提示词长度，用于确定 `--max-model-len`。

## §11 录制与回放

- **键是 (schema 名, sha256(system))。** 不按"第几次调用"索引；`user` 消息必须为空。未命中时
  抛 `LLMError` 并列出可能的原因（环境变量与录制时不同），不退回实时 API。
- **跨场景录制不覆盖。** 同一条主诉的 S1 在开启与关闭 ReAct 两个场景中键相同。
  `RecordingBackend.begin_scenario()` 冻结之前场景的键（复用、不再调用），同一场景内允许覆盖
  （校验重试以最后一次为准）。录制结束后用全部 fixture 自检一遍回放。
- **角色不需要分别录制。** 角色只影响响应字段的裁剪，`scripts/verify_replay.py` 用三种角色分别回放
  验证这一点。
- **回放自报。** `replay_mode` 是响应与 `/health` 的顶层字段（`manifest` 只下发给研究者角色，
  挂在那里会对其他角色不可见），措辞由服务端给出，页面显示「回放模式」说明。

## §12 药理层

- **抽取引擎。** `offline/extract_reference_triples.py`：分块 → LLM 抽取 → `source_span` 逐字核验
  （不在原文中的三元组丢弃并计数）→ 按块增量落盘；截断与调用失败分开记录，`--only-blocks` 可重跑
  指定块。古籍与现代教材分开记录（`source` + `book`）。
- **源文件先核对字节数再转码。** 期望字节数是上游原始文件的大小；每个源的编码固定
  （GB18030 的文件可能被静默地按 UTF-8 解出乱码）。
- **按标题切块。** 教材按空行切块时，药名标题会单独成块（短于 `MIN_BLOCK_CHARS` 被丢弃），
  用量段落里没有药名，模型只能猜主语，而 `source_span` 只核验宾语。`heading` 模式让一味药的正名
  与它的性味、归经、功效、用量落在同一块。预过滤按结构跳过过短（`MIN_ENTRY_CHARS`）、表格、
  索引、过长与无结构标记的块。`scripts/verify_pharmacology_chunks.py` 打印每个源的前几块与被跳过
  的块，供人工检查。
- **产物进版本控制。** 抽取花费真实调用且结果不可逐字复现，所以 `data/standard/` 下的本体文件
  随仓库提交；路径解析集中在 `core/data_paths.py`。
- **本地语料声明表。** `offline/local_corpora.py` 为每份本地语料声明两个独立判断：
  `out_of_scope`（是否在脾胃门定位内，影响训练导出）与 `pharmacology_source`（能否作为药理层
  抽取的输入）。抽取引擎在发出第一次调用之前拒绝非参考文献输入。
- **名医用药规律。** 由 `cases.json` 计数得出，每条回指 `case_id`。剂量规律按方中已知的药名反向
  锚定剂量（药名集合已知时不让正则猜实体）；每位医家的规律条数有上限，裁剪记录在知识块的
  `trimmed_sections` 里。
- **开源数据只填空槽。** 合并开源数据集时只补本体中缺失的谓词，不覆盖已有条目，每条带
  `dataset` 字段。

## §13 训练数据与 LoRA

- **链路格式。** `offline/export_sft.py --format chain` 输出分步推理链；样本来自医案、TCMEval-SDT
  训练集、药理层与教材证候。每一步有两个出处字段：`source`（结论出处）与 `rationale_source`
  （依据原文出处）；依据只取原文片段，取不到时为 `None`，不另编一段文字填进去。
- **出处 id 不进训练目标。** 模型背下医案 id，推理时就会编造 id；id 由检索层放进提示词。
- **泄漏检查。** 报告两个数：训练集与留出集的 `case_group_id` 交集（结构上应为 0），以及逐字相同
  的输入条数（只报告，不自动去重）。
- **过滤。** 训练导出只使用公有领域的医案（`filter_public_domain`）。含十八反十九畏配伍的
  医案默认导出，并在推理链末尾附一句固定的配伍提示（`INCOMPATIBLE_TRAINING_NOTE`），
  使模型学到的是"名家这样用过、且这是反药配伍"，与输出侧的 `check_incompatible` 不冲突；
  `--exclude-incompatible` 可排除它们，`--exclude-scope` 按门类排除。
- **LoRA 输出目录。** `scripts/train_lora.py` 写到 `<out-dir>/<基座>/<physician_id>/`，与
  `core/llm.py::_resolve_lora_path` 的查找方式一致；基座只由 `--base-model` 指定。heldout loss 与训练
  前的基线并列报告，train/heldout 差距超过 20% 时提示去看下游指标（这条线是约定，不是实测值）。
- **蒸馏按预算定规模。** `offline/distill_from_v4.py --estimate` 按 `core/usage.py` 的价目表与预算
  上限计算可蒸馏的条数，不传 `--limit` 就不会超预算。

## §14 服务与部署

- **先监听，后预热。** 服务启动后立即开始监听，预热在后台进行；预热期间 `/health` 返回 503 与
  进度（响应体完整），`/health/live` 始终返回 200。
- **并发上限。** `MAX_CONCURRENT_CONSULTS` 限制同时进行的问诊数，满了立即返回 503 与
  `Retry-After`；`scripts/loadtest.py` 可在目标机器上测量吞吐拐点，只统计成功的请求。
- **SSE 队列有界。** 流式增量事件（`s3_delta`）可丢弃并计数（最终内容在完成事件里），其他事件
  阻塞等待，超时则关闭流。
- **审计日志同步追加。** 响应 200 意味着审计记录已写入；日志以哈希链串联，
  `core.audit.verify_audit_chain()` 可校验整条链（见 `docs/DEPLOYMENT.md`）。
- **传输优化有选择。** `/api/graph` 等 JSON 响应启用 gzip，SSE 与已压缩的二进制（woff2/png）不压缩；
  `web/vendor/` 长期缓存，自有前端文件每次校验。
- **代理与额度。** 默认不读 `X-Forwarded-For`（`TRUSTED_PROXY_HOPS=0`）；共享额度按 LLM 调用数计量，
  请求前预扣。峰谷计价只记录高峰调用数，不改变额度。
- **产品模式默认开启。** `PRODUCT_MODE=1` 时首页是产品界面，研究界面需要显式设置 `PRODUCT_MODE=0`。

## §15 前端

- **零构建。** `web/index.html`（结构）+ `app.css` + `app.js` + `graph.js`，按固定顺序加载；
  `index.html` 中没有内联脚本和样式。测试通过 `tests/web_harness.py` 按同样顺序在 node 里加载脚本。
- **设计 token 与身份色单一来源。** 颜色、圆角、阴影、字体都定义为 `:root` 上的 token，
  `tests/test_css_tokens.py` 禁止硬编码颜色；医家身份色定义在 `core/physicians.py`，经 `/health`
  下发并注入为 `--phys-<id>` CSS 变量。颜色只承担语义（身份或报警）。
- **本地 vendor，离线可用。** cytoscape 与 dagre 使用仓库内的副本并按需加载，不依赖 CDN 脚本；
  中文字体使用子集化的 woff2（`scripts/subset_fonts.py`），`font-display: swap` 并带系统字体后备。
- **用药对照带。** 分歧对照带上的集合运算在后端完成，用两两距离均值与逐条噪声地板比较，
  界面同时显示灰色（噪声以内）与黑色（超出噪声）两段。
- **患者模式是独立形态。** 显示病名、科室与红旗症状（不折叠）和免责声明，不包含任何药名与方剂；
  响应在服务端裁剪（`to_graph(role="patient")` 在构造时跳过，不是事后过滤）。
- **图谱布局。** 问诊图谱按层固定列（x）、由 dagre 决定层内次序（y）；复合节点作为一个整体参与
  布局。图谱浏览器的展开有上限（`GB_EXPAND_CAP`），标签宽度用 token 控制。布局常量定义在
  `graph.js`，依赖方向只有 `app.js → graph.js`。
- **自绘下拉。** 自绘下拉包装原生 `<select>`（原生控件保持可访问，改值后派发 `change` 事件），
  键盘行为由纯函数 `selectKeyAction` 定义。
- **虚拟滚动。** 参考医案列表超过阈值才启用窗口化渲染，行高常量与 CSS 中的行高由测试保持一致。
- **产品面文案。** 产品界面不出现研究术语与开发词汇（`tests/test_ui_banned_terms.py`，扫描前剥掉
  注释），监管措辞使用「方剂核查」而非「方剂建议」；界面上不直接显示内部 id，查不到名称时回退
  到注册表里的中文名。

## §16 生成数据与可复现性

- **生成物带清单。** `data/standard/syndromes_manifest.json` 记录落盘 jsonl、OCR 修正表与解析器
  代码的指纹（解析器指纹由 `offline/code_fingerprint.py` 计算，忽略注释与文档字符串）。测试离线
  检查前两者与解析器指纹；有教材 markdown 时 `scripts/verify_generated_data.py` 重新抽取并逐字节
  比较，拿不到教材时以退出码 3 表示"没有核对"。
- **测试不修改版本控制内的文件。** 需要"坏掉的输入"时在 `tmp_path` 里复制一份；核对脚本提供
  `--manifest` / `--jsonl` 参数指向副本。
- **数字不手抄。** 规模类数字（条目数、调用数估算）在运行时从文件读取或由代码计算，文档中的评测
  数字走凭据核对（§9）。
- **映射表写明作用范围。** 修正表、别名表、同义表、白名单都写明作用于哪些字段，并有测试校验。

## §17 测试纪律

- **不断言绝对耗时。** 并发相关的测试断言比例与单侧界限（例如墙钟 / 串行和 ≈ 1/N），并配一条
  串行对照；只有真实浏览器与真实后端才能回答的时间问题放在 `scripts/` 的基准脚本里。
- **可选依赖用 `pytest.importorskip`。** sentence-transformers、vllm、python-docx、playwright
  不是运行测试的前提；`tests/test_conftest_embedding_marker.py` 扫描测试源码校验这一点。
- **断言真正发给模型的字符串。** 提示词相关的测试检查最终的 system 文本，而不是中间函数的返回值。
- **输出形状的 fixture 由生产代码构造。** 例如用 `VerificationResult(...).to_dict()` 生成，而不是手写
  字典；手写只用于输入。
- **自扫描测试剥掉自己的说明。** 扫描源码的测试先去掉注释与文档字符串（Python 用 `ast`，JS 用
  字符级扫描），避免说明文字命中禁词。
- **改默认值之前列出依赖方。** 默认检索模式、采样次数等默认值会影响成本估算、fixture 的键、评测
  基线与 SDT 台账；改动时逐一检查这些依赖方。
