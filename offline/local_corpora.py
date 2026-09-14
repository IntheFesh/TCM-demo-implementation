"""本地语料的**声明表**：哪几份、规范名、版权、定位、以及**能不能进药理层抽取**。

这张表原来住在 scripts/normalize_local_corpora.py 里。R8 收尾时挪到 offline/：
抽取引擎（`offline/extract_reference_triples.py`）要在开跑前拦住"拿医案去抽本草
三元组"这种输入，而 offline/ 不能反过来 import scripts/，所以表要放在最底层。
规范化脚本仍然是这张表的唯一写入方，它 import 这里。

## 两个独立的判断，不合并

- `out_of_scope`：这份语料**不在本项目脾胃门定位内**。回答的是"训练集要不要它"，
  过滤发生在 `offline/export_sft.py`（`CaseRecord.out_of_scope`）。
- `pharmacology_source`：这份语料**是不是本草/方剂类参考文献**。回答的是"药理层
  抽取能不能拿它当输入"。

两个判断的答案在这三份语料上碰巧一致（都是"不要"），但**理由完全不同**，合并成
一个字段以后改一边会看不出会不会连带影响另一边（CLAUDE.md「同一概念的匹配逻辑
只能有一处实现」那条的例外形状：两处回答的不是同一个问题，就要写清区别）。
具体地：《脾胃论》**在定位内**（`out_of_scope=False`，脾胃门原典），但它同样不能
进药理层抽取——按空行切出来方名和组成不在同一块（切块验证打得出来），而
`s`（方名）不过 `source_span` 核验，喂进去等于让模型猜方名。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR_NAME = "local_corpora"
MANIFEST_NAME = "MANIFEST.json"

# books/ 里已经有的那本：data/ 根目录的同名文件是重复上传，见
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
    out_of_scope: bool        # 不在脾胃门定位内 → 训练导出默认排除
    scope_reason: str
    pharmacology_source: bool  # 是不是本草/方剂参考文献 → 药理层抽取能不能拿它当输入
    pharmacology_reason: str


LOCAL_CORPORA: tuple[CorpusSpec, ...] = (
    CorpusSpec(
        original_prefix="2_王云启", suffix=".docx", target="王云启医案.docx",
        kind="case_docx", encoding="docx",
        origin="《王云启治癌验案录》，现代出版的肿瘤科医案集，用户上传（R8 实测 1599 个非空段落）",
        copyright_status="copyrighted", out_of_scope=True,
        scope_reason="肿瘤科医案，不在本项目脾胃门定位内；含脾胃门门类词的段落比例见 scope_stats，"
                     "选项 ②：接进来但标 out_of_scope，训练导出默认排除",
        pharmacology_source=False,
        pharmacology_reason="药理层抽的是「性味/归经/功效/用量/禁忌/炮制」，医案里没有这些字段。"
                            "转出来的 txt 前几百块是目录页和序言"
                            "（实测切 1434 块、只靠长度能滤掉 497 块），拿它跑抽取是纯浪费",
    ),
    CorpusSpec(
        original_prefix="李可医案", suffix=".docx", target="李可医案.docx",
        kind="case_docx", encoding="docx",
        origin="李可肿瘤医案汇编（脑瘤/鼻硬结症/宫颈癌等），用户上传（R8 实测 556 个非空段落，"
               "75 段海藻甘草同用）",
        copyright_status="copyrighted", out_of_scope=True,
        scope_reason="整份是肿瘤医案，不在脾胃门定位内；且含十八反配伍（海藻反甘草），"
                     "抽成医案后 has_incompatible_pair 会命中、训练导出默认排除——两道过滤各管各的",
        pharmacology_source=False,
        pharmacology_reason="医案没有性味/归经/功效/用量这些字段（同王云启那条）。实测切 546 块，"
                            "前几块是目录页「1.1 脑瘤头痛…1.2 鼻硬结症…」",
    ),
    CorpusSpec(
        original_prefix="脾胃论", suffix=".txt", target="脾胃论.txt",
        kind="classic_text", encoding="utf-8",
        origin="《脾胃论》金·李东垣，人民卫生出版社 2005 排印本的电子文本（z-library），"
               "原著公有领域；文件头约 30 行是版权页/CIP/推广广告，切块预过滤按结构跳过",
        copyright_status="public_domain", out_of_scope=False,
        scope_reason="脾胃门理论原典，在定位内",
        pharmacology_source=False,
        pharmacology_reason="它是理论专著 + 方论，按空行切出来"
                            "方名和组成不在同一块（切块验证打得出来），而 s（方名）不过 source_span "
                            "核验——喂进去等于让模型猜方名，正是 heading 模式要堵的那个洞。"
                            "要接它得先按「方名 + 组成」配对切块，那是另一轮的事",
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
