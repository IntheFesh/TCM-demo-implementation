"""本地语料的**声明表**：哪几份、规范名、版权、定位、以及**能不能进药理层抽取**。

这些语料是用户自行获取的本地文件：李可、王云启的医案出自现代出版物，版权受限；
《脾胃论》原著属公有领域，但排印本的电子文件同样不随仓库分发（见 data/SOURCES.md
「版权受限的语料（需自行获取）」）。仓库里只保留据这张表生成的
`data/local_corpora/MANIFEST.json`，使用者可以据此核对自己拿到的是不是同一份文件。

这张表放在 offline/ 这一层：抽取引擎（`offline/extract_reference_triples.py`）要在
开跑前拦住"拿医案去抽本草三元组"这种输入，而 offline/ 不能反过来 import scripts/，
所以表要放在最底层。规范化脚本（`scripts/normalize_local_corpora.py`）据这张表写出
MANIFEST.json，它 import 这里。设计说明见 docs/DESIGN_NOTES.md §12。

## 两个独立的判断，不合并

- `out_of_scope`：这份语料**不在本项目脾胃门定位内**。回答的是"训练集要不要它"，
  过滤发生在 `offline/export_sft.py`（`CaseRecord.out_of_scope`）。
- `pharmacology_source`：这份语料**是不是本草/方剂类参考文献**。回答的是"药理层
  抽取能不能拿它当输入"。

两个判断的答案在两份现代医案语料上碰巧一致（都是"不要"），但**理由完全不同**，
合并成一个字段以后改一边会看不出会不会连带影响另一边（docs/ARCHITECTURE.md §4 的
例外：两处回答的不是同一个问题，就要写清区别）。《脾胃论》就是分开的例子：它
**在定位内**（`out_of_scope=False`，脾胃门原典），但同样不能进药理层抽取——
按空行切出来方名和组成不在同一块（切块验证打得出来），而 `s`（方名）不过
`source_span` 核验，喂进去等于让模型猜方名。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR_NAME = "local_corpora"
MANIFEST_NAME = "MANIFEST.json"

# 这本古籍属于 books/：data/ 根目录里出现的同名文件是重复副本，处理规则见
# scripts/normalize_local_corpora.py 的规则 2。
DUPLICATE_OF_BOOKS = "584-医学衷中参西录.txt"


@dataclass(frozen=True)
class CorpusSpec:
    """一份本地语料的声明。original_prefix/suffix 用来在 data/ 根目录里认出原文件
    （原名里有空格和没闭合的括号，不按全名匹配）。"""
    original_prefix: str
    suffix: str
    target: str
    kind: str                 # case_docx = 现代医案 docx；classic_text = 古籍排印本电子文本
    encoding: str
    origin: str
    copyright_status: str     # 跟 CaseRecord.copyright_status 同一套值
    out_of_scope: bool        # 不在脾胃门定位内（整本书的声明，用药规律挖掘据此跳过）
    scope_reason: str
    pharmacology_source: bool  # 是不是本草/方剂参考文献 → 药理层抽取能不能拿它当输入
    pharmacology_reason: str


LOCAL_CORPORA: tuple[CorpusSpec, ...] = (
    CorpusSpec(
        original_prefix="王云启", suffix=".docx", target="王云启医案.docx",
        kind="case_docx", encoding="docx",
        origin="《王云启治癌验案录》，现代出版的肿瘤科医案集；版权受限，不随仓库分发，需自行获取",
        copyright_status="copyrighted", out_of_scope=True,
        scope_reason="肿瘤科医案，不在本项目脾胃门定位内；含脾胃门门类词的段落比例见 scope_stats。"
                     "接入时标 out_of_scope",
        pharmacology_source=False,
        pharmacology_reason="药理层抽的是「性味/归经/功效/用量/禁忌/炮制」，医案里没有这些字段。"
                            "转出来的 txt 开头大量是目录页和序言，拿它运行抽取只会浪费调用",
    ),
    CorpusSpec(
        original_prefix="李可医案", suffix=".docx", target="李可医案.docx",
        kind="case_docx", encoding="docx",
        origin="李可肿瘤医案汇编（脑瘤/鼻硬结症/宫颈癌等），现代出版物；版权受限，"
               "不随仓库分发，需自行获取",
        copyright_status="copyrighted", out_of_scope=True,
        scope_reason="整份是肿瘤医案，不在脾胃门定位内；且含十八反配伍（海藻反甘草），"
                     "抽成医案后 has_incompatible_pair 会命中——两个标记各管各的",
        pharmacology_source=False,
        pharmacology_reason="医案没有性味/归经/功效/用量这些字段（同王云启那条），"
                            "开头几块还是目录页",
    ),
    CorpusSpec(
        original_prefix="脾胃论", suffix=".txt", target="脾胃论.txt",
        kind="classic_text", encoding="utf-8",
        origin="《脾胃论》金·李东垣，现代排印本的电子文本；原著公有领域，排印本不随仓库分发，"
               "需自行获取。文件头的版权页/CIP 等非正文内容由切块预过滤按结构跳过",
        copyright_status="public_domain", out_of_scope=False,
        scope_reason="脾胃门理论原典，在定位内",
        pharmacology_source=False,
        pharmacology_reason="它是理论专著 + 方论，按空行切出来"
                            "方名和组成不在同一块（切块验证打得出来），而 s（方名）不过 source_span "
                            "核验——喂进去等于让模型猜方名，正是 heading 模式要堵的那个洞。"
                            "要接入它，需要先按「方名 + 组成」配对切块",
    ),
)


def spec_for_target(name: str) -> CorpusSpec | None:
    """按规范名（`脾胃论.txt`）查声明。"""
    for spec in LOCAL_CORPORA:
        if spec.target == name:
            return spec
    return None


def spec_for_path(path: Path | str) -> CorpusSpec | None:
    """按路径查声明：认规范名本身，也认 docx 派生出来的同名 .txt
    （`李可医案.txt` ← `李可医案.docx`）——真正会被拿去喂抽取的是那份 txt。
    只看文件名不看目录：从别处拷一份改名叫 `李可医案.txt` 同样该被拦住。"""
    name = Path(path).name
    spec = spec_for_target(name)
    if spec is not None:
        return spec
    for candidate in LOCAL_CORPORA:
        if Path(candidate.target).with_suffix(".txt").name == name:
            return candidate
    return None


def non_reference_reason(path: Path | str) -> str | None:
    """这个输入不能进药理层抽取的理由；None = 没有异议（不在声明表里的文件一律
    放行——用户自己下的本草/方书不该被这张表挡住）。"""
    spec = spec_for_path(path)
    if spec is None or spec.pharmacology_source:
        return None
    scope = "定位外（out_of_scope）" if spec.out_of_scope else "在定位内"
    return f"{spec.target}：{scope}，但不是本草/方剂参考文献——{spec.pharmacology_reason}"


def non_pharmacology_corpora() -> tuple[CorpusSpec, ...]:
    return tuple(s for s in LOCAL_CORPORA if not s.pharmacology_source)
