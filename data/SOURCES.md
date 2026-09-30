# 数据来源与版权

本文件说明仓库里每一类数据从哪里来、怎样获取、版权状态如何、哪些不随仓库分发。
数据处理与建模上的设计决策见 [`docs/DESIGN_NOTES.md`](../docs/DESIGN_NOTES.md)。

## 1. 医案

| 医家 | 出处 | 作者 | 年代 | 仓库中的形式 | 版权状态 |
|---|---|---|---|---|---|
| 叶天士 | 《临证指南医案》 | 叶桂（天士） | 清 | `data/ye_tianshi/*.json` 粗段；`cases.json` 结构化诊次 | 公有领域 |
| 吴鞠通 | 《吴鞠通医案》 | 吴瑭（鞠通） | 清 | `data/wu_jutong/*.json` 粗段；`cases.json` 结构化诊次 | 公有领域 |
| 张锡纯 | 《医学衷中参西录》 | 张锡纯 | 近代（作者卒于 1933 年） | `data/zhang_xichun/*.json` 粗段；`cases.json` 结构化诊次 | 公有领域 |
| 李可 | 李可肿瘤医案汇编 | — | 现代 | **不随仓库分发** | 版权受限 |
| 王云启 | 《王云启治癌验案录》 | — | 现代 | **不随仓库分发** | 版权受限 |

`cases.json` 只包含前三位医家（公有领域）的诊次，每条带 `copyright_status: public_domain`。
三部古籍的作者均已去世超过五十年，超出著作权保护期；上游仓库是机械数字化转录，转录行为
不产生新的独创性表达。原文可以自由使用，项目文档中注明来源。

### 获取方式

古籍原文来自 GitHub 仓库 [`xiaopangxia/TCM-Ancient-Books`](https://github.com/xiaopangxia/TCM-Ancient-Books)
（中医药古籍文本，文件名为三位补零编号 `NNN-书名.txt`，编码 GB18030）：

| 文件 | 用途 |
|---|---|
| `367-临证指南医案.txt` | 叶天士医案 |
| `361-吴鞠通医案.txt` | 吴鞠通医案 |
| `584-医学衷中参西录.txt` | 张锡纯医案（按原书「医案」卷的目录逐案切分） |

下载后放在 `books/`（不进版本控制），然后：

```bash
python -m offline.split_cases --books-dir books --stats-only   # 只做正则统计，零 LLM 调用
python -m offline.split_cases --books-dir books                # 切出粗段
python -m offline.extract_cases                                # 粗段 → cases.json（需要 LLM）
```

仓库已经带有切好的粗段与 `cases.json`，复现上面的步骤不是运行系统的前提。

### 版权受限的语料（需自行获取）

李可、王云启两位医家的医案出自现代出版物，**原文、转写文本与由其切分出的诊次都不随仓库
分发**。仓库只保留：

- 声明清单 [`data/local_corpora/MANIFEST.json`](local_corpora/MANIFEST.json)：每份语料的
  规范文件名、字节数、sha256、编码、来源说明、版权状态与定位判断，可用来核对自行获取的文件
  是否为同一份；
- 处理脚本：`scripts/normalize_local_corpora.py`（规范文件名、docx 转文本、写清单）、
  `offline/extract_cases_li_ke.py`、`offline/extract_cases_wang_yunqi.py`（切案，输出
  `data/cases_<id>.json`）、`offline/merge_reference_cases.py`（合并进 `cases.json`，可用
  `--remove` 撤销）。

两份语料都是肿瘤科医案，不在本项目的脾胃门定位内（`out_of_scope: true`）；训练数据导出只使用
公有领域的医案，不包含它们。在界面上它们只作为「参考医家」展示。

`data/local_corpora/` 中还声明了《脾胃论》（金·李东垣）的排印本电子文本：原著属公有领域，
但排印本的电子文件不随仓库分发。由它规则抽取的 278 条方论三元组
（`data/standard/rationale_pwl.jsonl`，每条带原文片段）随仓库分发。

## 2. 标准证候

`data/standard/syndromes.jsonl` 共 337 条：

- 17 条人工整理的国标/共识证候，按来源可信度分档（`source` 字段：`official_consensus`、
  `journal`、`group_standard`、`secondary_verified`、`manual`；各档的定义见 `core/schemas.py`）；
- 320 条由《中医内科学》教材（十四五规划教材，取自
  [`PanckooAI/TCM_Datasets`](https://github.com/PanckooAI/TCM_Datasets) 的 `十四五教材/中医内科学.md`）
  按规则解析得到（`source: textbook`），生成命令与指纹记在
  `data/standard/syndromes_manifest.json`，可用 `python -m scripts.verify_generated_data` 核对。

## 3. 药理层：本草与方剂本体

`data/standard/materia_medica.jsonl`（本草三元组）与 `data/standard/formulary.jsonl`
（方剂三元组）由 LLM 从下列文献中逐块抽取，每条三元组带 `source_span`（原文片段，抽取时
逐字核验）、`source`（`classic` 古籍 / `modern` 现代教材）与 `book`：

| 文献 | 获取位置 | 编码 |
|---|---|---|
| 《中药学》《临床中药学》《中药炮制学》《方剂学》（十四五规划教材） | `PanckooAI/TCM_Datasets` 的 `十四五教材/*.md` | UTF-8 |
| 《神农本草经》《本草备要》 | `xiaopangxia/TCM-Ancient-Books` 的 `000-神农本草经.txt`、`018-本草备要.txt` | GB18030 |

下载与校验（URL 与期望字节数写在脚本里）：`bash scripts/fetch_pharmacology_sources.sh`；
抽取：`python -m scripts.run_pharmacology_extraction`（先 `--dry-run` 查看预估调用数）。

另外合并了两个开源数据集中本体里缺失的条目（每条带 `dataset` 字段，可逐条追溯）：

| 数据集 | 授权 | 合并内容 |
|---|---|---|
| [`jangviktor-web/nihaixia-app`](https://github.com/jangviktor-web/nihaixia-app) | Apache-2.0 | 本草 341 条、方剂 1883 条 |
| [`chenzhanyi/zhongyao-xuexi-baodian`](https://github.com/chenzhanyi/zhongyao-xuexi-baodian) | 仓库未声明许可证；其中 `jianbie_extra.js` 自述底层数据来自 Apache-2.0 项目 | 本草 39 条、药名别名 709 条（`data/standard/herb_aliases_merged.tsv`） |

**授权提示**：现代教材受著作权保护，教材原文不随仓库分发；但三元组中的 `source_span` 是教材
原文的短摘录。未声明许可证的数据集在商用前需要另行确认授权。

## 4. 其他参考表

| 文件 | 内容与来源 |
|---|---|
| `data/standard/tcm_theory.jsonl` | 171 条医理规则（治则、病机、藏象、配伍），部分出自《中药学》《方剂学》总论，其余为人工整理的通用理论表述；每条的 `source` 字段写明出处 |
| `data/standard/guidelines.jsonl` | 425 条「证型 → 推荐方剂/治法」对照，出自《方剂学》教材与上面的开源方剂数据 |
| `data/standard/prescribing_patterns.jsonl` | 名医用药规律，由 `cases.json` 统计得出，每条回指支持它的 `case_id` |
| `data/standard/diseases.jsonl` | 15 个病名的导诊信息（主症、常见证型、红旗症状、科室） |
| `data/standard/ocr_fixes.tsv` 等 `*.tsv` | 人工维护的修正表、别名表与同义表，表头写明作用范围 |
| `core/safety_output.py` 的 `DOSE_LIMITS` | 按《中国药典》常用量上限人工整理的剂量表；抽取结果只与它比对，不会改写它 |

## 5. 评测数据

- `tests/queries.txt`：10 条为本项目**合成**的测试主诉，不是真实病例。覆盖肝胃不和、脾胃
  虚寒、湿热中阻、胃阴不足、痰饮内停、食滞、瘀血阻络、脾虚泄泻、寒热错杂；第 10 条含消化道
  出血征象，用于验证安全否决。
- **TCMEval-SDT**：[`zhuyan166/TCMEval`](https://github.com/zhuyan166/TCMEval)（CC BY 4.0）
  的 `evaluation/TCMEval-SDT/`，Train / Validation / Test = 200 / 50 / 50 条，不进版本控制，
  运行时用 `--sdt-dir` 指向。评分使用官方 `evaluate.py`，不重新实现。数据本身的几个特点：
  - Validation / Test 的 JSON 中答案字段为空，金标准在 `Results/{split}_data_result.txt`；
  - `Results/Validation_data_result.txt` 开头带 UTF-8 BOM，官方脚本读取后第一条病案恒得 0 分，
    Validation 的满分上限因此是 48.9998 / 50；Test 没有这个问题，主报告使用 Test；
  - `automated_score` 返回的是总分（满分 50），不是均值；
  - 官方 `evaluate.py` 在顶层 import pandas（`requirements.txt` 中已注明）。

## 6. 数据层面的已知局限

1. **样本量小且不均衡。** `cases.json` 共 941 个诊次：叶天士 495、吴鞠通 359、张锡纯 87。
   这个规模足以验证推理管线，不足以支撑关于医家风格的统计结论。项目设定的样本量目标是每位医家
   至少 60 案、其中至少 50 案带复诊序列（医家层权重需要的门槛）；`python -m offline.quota`
   报告当前数据与目标的差距。
2. **时代是混杂因素。** 三位医家分处清代与近代，观测到的用药差异中含有时代成分，不能直接等同
   于个人风格差异。
3. **门类不对齐。** 三部书的分类体系不同，跨医家比较应先按证素归一，不直接比较门类分布。
4. **机械转录可能有讹字**（例如「伊芳氏」）。抽取时遇到明显不通的原文，记入失败清单而不是
   强行解析。
5. **诊次切分依赖 LLM。** 粗段切分阶段的 `follow_hint` 只是正则扫到的疑似复诊标记，不是确认过
   的诊次数；`offline/extract_cases.py` 把 LLM 切出的诊次数与正则估计交叉核对，相差 ≥2 的记入
   `extract_warnings.json` 供人工复核。
6. **术语不对齐。** 清代医案的症状与证型表述和现代国标术语体系差异很大，按字面匹配时医案证型与
   标准证候几乎对不上，详见 [`docs/DESIGN_NOTES.md`](../docs/DESIGN_NOTES.md) §2。
7. **只覆盖脾胃门。** 医案选取、证候表与安全规则都围绕脾胃门设计。
8. **病位证素是最小集。** `core/elements.py` 的 `LOCATIONS` 只收录有可核验来源支撑的病位；
   例如痰气交阻证（SP-12）的病位记为「胃」，没有单独设「食道」。
