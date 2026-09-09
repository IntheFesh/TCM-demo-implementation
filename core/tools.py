"""G1 工具层：ReAct 智能体（G2）能调用的全部动作，以及追问候选生成（G3 用）。

设计约束（三条，后面加工具时照着来）：

1. **工具描述只写一处。** 每个工具的 description 只在这个文件的 TOOLS 注册表里
   定义。prompt 里的工具清单由 `tools_manifest()` 渲染出来，不在 yaml 里手抄一遍。
   手抄的那份迟早跟代码分叉，而模型看到的是手抄的那份——那时候"模型为什么不按
   预期调工具"就完全查不动了。

2. **工具永不抛异常给调用方。** 参数不合法、节点不存在、依赖的数据文件还没生成，
   一律返回带 `error` 或 `available: false` 的 dict。ReAct 循环里一个异常等于整条
   推理链断掉；返回错误信息则可以回灌给模型让它换个查法。

3. **不做 LLM 调用。** 工具层是确定性的：同样的输入永远同样的输出。tests/ 里能
   秒级全量跑完、不需要网络，靠的就是这一条。

数据依赖的现状（不要误读）：
  - `data/graph.json` 国标层（123 节点 / 377 边）是全的，query_graph、
    check_residual、question_candidates 都能真实工作。
  - `cases.json`（医案检索库）和 `data/case_triples.jsonl`（医案三元组，X3 产出）
    在这个 sandbox 里都没有，search_cases / query_case_graph 会如实返回
    available=false。**这不是 bug**，是这两份数据要在有真实 LLM 的机器上生成。
"""
from __future__ import annotations

import json
import threading
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from core.graph.store import NetworkXStore

ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = ROOT / "data" / "graph.json"
STANDARD_PATH = ROOT / "data" / "standard" / "syndromes.jsonl"
# X3 在 AutoDL 上从医案抽出来的三元组，一行一条：
# {"case_id", "physician", "s", "p", "o", "source_span"}
# source_span 是这条三元组在医案原文里的出处片段——没有它，工具查出来的东西
# 就跟凭空生成的没区别，防幻觉链条在工具这一层断掉。
CASE_TRIPLES_PATH = ROOT / "data" / "case_triples.jsonl"


# ---------- 惰性单例（模块顶层不加载任何文件） ----------

_graph_store: NetworkXStore | None = None
_standard_defs: list[dict] | None = None
_case_triples: list[dict] | None = None


def reset_tool_caches() -> None:
    """测试用：清掉所有惰性缓存。改了 monkeypatch 的路径之后必须调一次，
    否则读到的还是上一个用例加载的数据。"""
    global _graph_store, _standard_defs, _case_triples
    _graph_store = None
    _standard_defs = None
    _case_triples = None


# 三个惰性加载器共用一把锁：它们都是"读文件 → 赋给模块全局"，没有锁时两个
# 冷启动并发请求会各读一遍 data/graph.json（171KB 解析成 NetworkX 图要几十
# 毫秒）并各自发布一份——不会读到半成品（都是建好再赋值），只是重复劳动，
# 而且在途的两个请求会拿着两份不同的图对象。锁内再判一次 None 是双重检查。
_load_lock = threading.Lock()


def get_graph_store() -> NetworkXStore | None:
    """加载国标层图谱。文件不存在返回 None 而不是抛异常——见模块约束 2。"""
    global _graph_store
    if _graph_store is None:
        with _load_lock:
            if _graph_store is None:
                if not GRAPH_PATH.exists():
                    return None
                store = NetworkXStore()
                store.load(GRAPH_PATH)
                _graph_store = store
    return _graph_store


def _load_standard() -> list[dict]:
    global _standard_defs
    if _standard_defs is None:
        with _load_lock:
            if _standard_defs is None:
                if not STANDARD_PATH.exists():
                    _standard_defs = []
                else:
                    defs = []
                    for line in STANDARD_PATH.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line:
                            defs.append(json.loads(line))
                    _standard_defs = defs
    return _standard_defs


def _load_case_triples() -> list[dict] | None:
    """None = 文件还没生成（跟"文件存在但里面没有三元组"要区分开：
    前者是"这步还没跑"，后者是"跑了但没抽出东西"，对模型是两个不同的信号）。"""
    global _case_triples
    if _case_triples is not None:
        return _case_triples
    with _load_lock:
        if _case_triples is not None:
            return _case_triples
        if not CASE_TRIPLES_PATH.exists():
            return None
        rows = []
        for lineno, line in enumerate(
            CASE_TRIPLES_PATH.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # 坏行跳过不中断：三元组文件是机器生成的，一行坏了不该让整个
                # 工具不可用；行号留在 note 里给人排查。
                rows.append({"_bad_line": lineno})
                continue
            # 合法 JSON 但不是对象（数组/字符串）同样按坏行处理，否则后面 .get 直接炸
            rows.append(row if isinstance(row, dict) else {"_bad_line": lineno})
        _case_triples = rows
        return _case_triples


# ---------- 工具注册表 ----------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    fn: Callable[..., dict]


def tools_manifest() -> list[dict]:
    """给 prompt 用的工具清单。G2 的 ReAct prompt 直接渲染这个结果，
    所以工具描述、参数名、必填项全都跟代码同源，不会漂移。"""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema.model_json_schema(),
        }
        for spec in TOOLS.values()
    ]


def run_tool(name: str, args: dict) -> dict:
    """统一入口：校验参数 -> 调用 -> 返回 dict。任何失败都变成 {"error": ...}，
    因为 ReAct 循环需要把错误回灌给模型而不是崩掉（模块约束 2）。"""
    spec = TOOLS.get(name)
    if spec is None:
        return {
            "error": f"没有名为 {name} 的工具",
            "available_tools": sorted(TOOLS.keys()),
        }
    if not isinstance(args, dict):
        return {"error": f"{name} 的参数必须是一个 JSON 对象，收到 {type(args).__name__}"}
    try:
        parsed = spec.input_schema.model_validate(args)
    except Exception as e:  # noqa: BLE001 - 校验错误要变成给模型看的文本，不是异常
        return {
            "error": f"{name} 的参数不合法：{e}",
            "expected_parameters": spec.input_schema.model_json_schema(),
        }
    try:
        return spec.fn(**parsed.model_dump())
    except Exception as e:  # noqa: BLE001 - 工具内部的意外（模型加载、文件坏行）不能炸掉整条问诊
        return {"error": f"{name} 执行失败：{type(e).__name__}: {e}"}


# ---------- 1. query_graph ----------


class QueryGraphInput(BaseModel):
    node: str = Field(min_length=1, description="节点名或节点 id，如「口苦」「肝胃不和证」「element::脾」")
    edge_type: str | None = Field(
        default=None, description="只看这一类边：indicates / composes / is_a；不填则全部"
    )
    physician: str | None = Field(
        default=None, description="按这位医家取 indicates 边权重（ye_tianshi / wu_jutong）；不填取各医家均值"
    )
    limit: int = Field(default=20, ge=1, le=200)


def _resolve_node(store: NetworkXStore, node: str) -> str | None:
    """名字 -> 节点 id。证候节点的 id 是 syndrome::<code>、名字是「肝胃不和证」，
    两者对不上，所以名字查不到时还要按 name 属性再扫一遍。"""
    if store.get_node(node) is not None:
        return node
    for prefix in ("symptom::", "element::", "syndrome::"):
        candidate = f"{prefix}{node}"
        if store.get_node(candidate) is not None:
            return candidate
    for node_type in ("syndrome", "symptom", "element"):
        for nid in store.find_nodes(node_type, name=node):
            return nid
    # 证候名允许唯一的部分匹配（「胃阴虚」→「胃阴虚证」），跟 lookup_standard 的
    # 规则一致。两个工具对同一个词给出相反答案，是 CLAUDE.md 里列的第三次撞墙。
    partial = [
        nid for nid in store.find_nodes("syndrome")
        if node and node in ((store.get_node(nid) or {}).get("name") or "")
    ]
    if len(partial) == 1:
        return partial[0]
    return None


def _edge_weight(data: dict, physician: str | None) -> float | None:
    wbp = data.get("weight_by_physician")
    if not wbp:
        return None
    if physician is not None:
        return wbp.get(physician)
    return sum(wbp.values()) / len(wbp)


def _edge_view(other_id: str, data: dict, store: NetworkXStore, physician: str | None,
               direction: str) -> dict:
    other = store.get_node(other_id) or {}
    return {
        "direction": direction,
        "edge_type": data.get("edge_type"),
        "node": other_id,
        "name": other.get("name", other_id),
        "node_type": other.get("node_type"),
        "source": data.get("source"),
        "via_syndrome": data.get("via_syndrome"),
        "is_cardinal": data.get("is_cardinal"),
        "weight": _edge_weight(data, physician),
    }


def query_graph(node: str, edge_type: str | None = None,
                physician: str | None = None, limit: int = 20) -> dict:
    store = get_graph_store()
    if store is None:
        return {"found": False, "error": f"图谱文件不存在：{GRAPH_PATH}，先跑 offline/build_graph.py"}
    node_id = _resolve_node(store, node)
    if node_id is None:
        # 只回一句"没有这个节点"会把模型逼进死胡同：实测它查「口苦」查不到就
        # 放弃了，而图里明明有「口干或口苦」。用 check_residual 那套片段匹配器
        # 给出近似节点，让它下一步能直接改用正确的名字重查。
        near = [
            (store.get_node(i) or {}).get("name")
            for i in _match_graph_symptoms(store, node)
        ]
        return {
            "found": False,
            "query": node,
            "neighbors": [],
            "near_matches": [n for n in near if n][:10],
            "note": (
                "图中没有这个节点。国标层只收了脾胃门 93 个标准症状，患者原话往往"
                "不在其中——如果 near_matches 非空，改用其中一个名字重查。"
            ),
        }
    attrs = store.get_node(node_id) or {}
    # 出边和入边都要给：图里的边都是单向的（symptom->element->syndrome），
    # 只给出边的话「这个证候由哪些证素构成」永远查不到。
    edges = [
        _edge_view(dst, data, store, physician, "out")
        for dst, data in store.neighbors(node_id, edge_type=edge_type)
    ] + [
        _edge_view(src, data, store, physician, "in")
        for src, data in store.in_neighbors(node_id, edge_type=edge_type)
    ]
    edges.sort(key=lambda e: (-(e["weight"] or 0.0), e["name"]))
    return {
        "found": True,
        "node": node_id,
        "name": attrs.get("name", node_id),
        "node_type": attrs.get("node_type"),
        "attributes": {k: v for k, v in attrs.items() if k not in ("name", "node_type")},
        "total_neighbors": len(edges),
        "neighbors": edges[:limit],
    }


# ---------- 2. query_case_graph ----------


class QueryCaseGraphInput(BaseModel):
    symptom: str | None = Field(default=None, description="按症状/实体文本匹配三元组的主语或宾语")
    predicate: str | None = Field(
        default=None,
        description="只看这一类关系，如 提示 / 治以 / 用药（X3 受控词表六选一："
                    "提示/属于/治以/用方/含/用药，见 core.schemas.CaseTriplePredicate）",
    )
    physician: str | None = Field(default=None, description="只看这位医家的医案（ye_tianshi / wu_jutong）")
    case_id: str | None = Field(default=None, description="只看这条医案")
    limit: int = Field(default=20, ge=1, le=200)


def query_case_graph(symptom: str | None = None, predicate: str | None = None,
                     physician: str | None = None, case_id: str | None = None,
                     limit: int = 20) -> dict:
    rows = _load_case_triples()
    if rows is None:
        return {
            "available": False,
            "triples": [],
            "note": (
                f"医案三元组尚未生成（{CASE_TRIPLES_PATH} 不存在）。"
                "这份数据由 X3 在有真实 LLM 的机器上从医案抽取产出，不是代码缺陷。"
            ),
        }
    bad_lines = [r["_bad_line"] for r in rows if "_bad_line" in r]
    rows = [r for r in rows if "_bad_line" not in r]
    # 没有 source_span 的三元组不返回：模块顶部说了，没有原文出处的结论跟凭空
    # 生成的没区别。丢掉的条数报在 note 里，让人知道抽取那边有问题，不是静默吞。
    n_no_span = sum(1 for r in rows if not (r.get("source_span") or "").strip())
    rows = [r for r in rows if (r.get("source_span") or "").strip()]

    matched = []
    for r in rows:
        s, o = r.get("s") or "", r.get("o") or ""
        if not s and not o:
            continue  # 主宾都空的行匹配任何 symptom 查询，是脏数据
        if physician is not None and r.get("physician") != physician:
            continue
        if case_id is not None and r.get("case_id") != case_id:
            continue
        if predicate is not None and predicate not in (r.get("p") or ""):
            continue
        if symptom is not None:
            # 「症状文本是否对得上」只有 _symptom_text_matches 一处实现：之前这里是
            # 裸的双向子串，而同模块的 _match_graph_symptoms 还会拆「胃脘胀满或疼痛」
            # 这种并列名——同一个词，两个工具给出不同答案（CLAUDE.md 第三次撞墙）。
            if not (_symptom_text_matches(s, symptom) or _symptom_text_matches(o, symptom)):
                continue
        matched.append(r)

    out = {
        "available": True,
        "total_matched": len(matched),
        # source_span 必须原样带出来：这是"这条结论出自医案哪一句"的唯一凭据。
        "triples": [
            {
                "case_id": r.get("case_id"),
                "physician": r.get("physician"),
                "s": r.get("s"),
                "p": r.get("p"),
                "o": r.get("o"),
                "source_span": r.get("source_span"),
            }
            for r in matched[:limit]
        ],
    }
    notes = []
    if bad_lines:
        notes.append(f"三元组文件有 {len(bad_lines)} 行无法解析，已跳过（行号：{bad_lines[:10]}）")
    if n_no_span:
        notes.append(f"另有 {n_no_span} 条缺 source_span（原文出处）的三元组被剔除")
    if notes:
        out["note"] = "；".join(notes)
    return out


# ---------- 3. search_cases ----------


class SearchCasesInput(BaseModel):
    query: str = Field(min_length=1, description="检索用的症状描述文本")
    physician: str = Field(min_length=1, description="在这位医家的医案库里检索")
    k: int = Field(default=3, ge=1, le=10)


def search_cases(query: str, physician: str, k: int = 3) -> dict:
    from core.retrieval import MIN_RETRIEVAL_SCORE, get_retriever

    try:
        retriever = get_retriever()
    except FileNotFoundError as e:
        return {"available": False, "cases": [], "note": str(e)}

    # 跟 run_physician 用同一个相似度下限：这里不设的话，模型会拿到几条相似度 0.3
    # 的不相关医案并被鼓励去引用它们。
    hits = retriever.search(query, physician, k=k, min_score=MIN_RETRIEVAL_SCORE)
    return {
        "available": True,
        "cases": [
            {
                "case_id": c.case_id,
                "score": round(score, 3),
                "visit_index": c.visit_index or 0,
                "symptoms": c.symptoms or [],
                "tongue": c.tongue,
                "pulse": c.pulse,
                "syndrome": c.syndrome,
                "treatment_principle": c.treatment_principle,
                "formula": c.formula,
                "herbs": c.herbs or [],
            }
            for c, score in hits
        ],
    }


# ---------- 4. lookup_standard ----------


class LookupStandardInput(BaseModel):
    query: str = Field(min_length=1, description="证候名或证候编码，如「脾胃湿热证」「SP-03」")


def lookup_standard(query: str) -> dict:
    defs = _load_standard()
    if not defs:
        return {"found": False, "error": f"证候定义文件不存在或为空：{STANDARD_PATH}"}
    q = query.strip()
    for d in defs:
        if d.get("code") == q or d.get("name") == q:
            return {"found": True, "definition": d}
    # 「SP-01 肝胃不和证」这种"编码+空格+名称"的写法要认。实测真实模型拿到
    # candidates 之后正是这么回传的——第一版把 candidates 拼成 "CODE NAME" 字符串，
    # 模型照抄回来却查不到，白烧一步。candidates 现在改成结构化对象（见下），
    # 这里再兜一层，两头都堵上。
    for d in defs:
        code, name = d.get("code") or "", d.get("name") or ""
        if q in (f"{code} {name}", f"{code}{name}", f"{name}（{code}）"):
            return {"found": True, "definition": d, "note": "按「编码+名称」的合写形式匹配"}
    partial = [d for d in defs if q in (d.get("name") or "")]
    if len(partial) == 1:
        return {"found": True, "definition": partial[0], "note": "按名称部分匹配到唯一一条"}
    return {
        "found": False,
        "query": query,
        # 查不到时给出候选：模型下一步能直接改用正确的名字重查，比只回一句
        # "未找到"有用得多。给结构化对象而不是拼好的字符串——拼成
        # "SP-01 肝胃不和证" 会诱导模型把整串当 query 传回来。
        "candidates": [
            {"code": d.get("code"), "name": d.get("name")} for d in (partial or defs)
        ],
        "hint": "用 candidates 里的 code 或 name 之一重查，不要把两个拼在一起。",
    }


# ---------- 5. check_residual ----------


class CheckResidualInput(BaseModel):
    symptoms: list[str] = Field(min_length=1, description="患者当前全部症状")
    elements: list[str] = Field(default_factory=list, description="目前已推断出的证素名列表")


# 拆出来的片段里，这些词单独出现时不指向任何具体症状：「疼痛」会让所有含
# 「疼痛」的患者主诉命中三条胃脘疼痛节点。片段必须带部位或性质才算数。
_GENERIC_FRAGMENTS = frozenset({"疼痛", "胀痛", "隐痛", "不适", "加重", "减轻", "或", "甚则"})


def _symptom_fragments(name: str) -> list[str]:
    """把「胃脘胀满或疼痛」这类含并列项的标准症状名拆成可单独匹配的片段。
    不拆的话患者说「胃脘胀满」就匹配不上整条标准症状名。"""
    parts = [name]
    for sep in ("，", "、", "或", "；"):
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [p for p in parts if len(p) >= 2 and p not in _GENERIC_FRAGMENTS]


def _symptom_text_matches(name: str, patient_symptom: str) -> bool:
    """标准症状名（或三元组里的症状文本）跟患者原话对不对得上：双向包含，
    对不上再按并列片段试一次。**全模块唯一的症状文本匹配器**——
    _match_graph_symptoms 和 query_case_graph 都走这里。"""
    if not name or not patient_symptom:
        return False
    if name in patient_symptom or patient_symptom in name:
        return True
    return any(f in patient_symptom for f in _symptom_fragments(name))


def _match_graph_symptoms(store: NetworkXStore, patient_symptom: str) -> list[str]:
    """患者原话 -> 图里的标准症状节点 id。刻意只做字面（片段级双向包含）匹配，
    不引入向量相似度：这一层必须离线可跑、确定性可测。匹配不上的症状会被单独
    报成 off_graph，而不是默默算进"未解释"——把"图里没有这个词"和"这个症状
    确实没被证素解释"混为一谈，会把覆盖率算成一个假数。"""
    hits = []
    for sym_id in store.find_nodes("symptom"):
        name = (store.get_node(sym_id) or {}).get("name", "")
        if _symptom_text_matches(name, patient_symptom):
            hits.append(sym_id)
    return hits


def check_residual(symptoms: list[str], elements: list[str] | None = None) -> dict:
    store = get_graph_store()
    if store is None:
        return {"error": f"图谱文件不存在：{GRAPH_PATH}"}
    elements = elements or []
    element_ids = {f"element::{e}" for e in elements}

    explained, unexplained, off_graph = [], [], []
    for s in symptoms:
        matched = _match_graph_symptoms(store, s)
        if not matched:
            off_graph.append(s)
            continue
        targets = {
            dst for sym_id in matched
            for dst, _ in store.neighbors(sym_id, edge_type="indicates")
        }
        (explained if targets & element_ids else unexplained).append(s)

    judged = len(explained) + len(unexplained)
    return {
        "explained": explained,
        "unexplained": unexplained,
        # 图里没有对应标准症状、无法判断的，单列
        "off_graph": off_graph,
        "coverage": round(len(explained) / judged, 3) if judged else None,
        "coverage_denominator": judged,
        "note": (
            f"共 {len(symptoms)} 条症状，其中 {len(off_graph)} 条在国标层图里没有对应节点、"
            "不计入覆盖率分母。"
        ),
    }


# ---------- 6. ask_user ----------


class AskUserInput(BaseModel):
    question: str = Field(min_length=1, description="要问患者的一个具体问题")
    reason: str = Field(min_length=1, description="为什么现在需要问这个——哪两个证候分不开")


def ask_user(question: str, reason: str) -> dict:
    """终止工具：ReAct 循环看到 terminate=True 就停下，把问题交给患者。
    它不"执行"任何检索——存在的意义是让"我需要更多信息"成为一个可被观测、
    可被计数的显式动作，而不是模型在自由文本里含糊地提一句。"""
    return {"terminate": True, "question": question, "reason": reason}


TOOLS: dict[str, ToolSpec] = {
    "query_graph": ToolSpec(
        name="query_graph",
        description=(
            "查国标层知识图谱。给一个症状/证素/证候的名字，返回它相连的节点："
            "症状 --indicates--> 证素（带该医家权重、是否主症、出自哪条证候）、"
            "证素 --composes--> 证候。出边入边都返回，所以「这个证候由哪些证素构成」"
            "「这个证素被哪些症状提示」都能查。用于确认某个判断在标准里有没有依据。"
        ),
        input_schema=QueryGraphInput,
        fn=query_graph,
    ),
    "query_case_graph": ToolSpec(
        name="query_case_graph",
        description=(
            "查医案三元组图（从两位医家的真实医案里抽出的 主语-关系-宾语）。"
            "可按症状、关系、医家、医案 id 过滤。每条结果都带 source_span——"
            "医案原文出处，引用结论时必须带上它。用于回答「这位医家遇到这个症状"
            "实际是怎么处理的」，跟 query_graph 的「标准怎么规定」互补。"
        ),
        input_schema=QueryCaseGraphInput,
        fn=query_case_graph,
    ),
    "search_cases": ToolSpec(
        name="search_cases",
        description=(
            "在指定医家的医案库里做相似医案检索，返回 top-k 条完整医案"
            "（症状、舌脉、证型、治法、方药）。用于找可直接引用的先例；"
            "cited_case_ids 只能引用这里返回过的 case_id。"
        ),
        input_schema=SearchCasesInput,
        fn=search_cases,
    ),
    "lookup_standard": ToolSpec(
        name="lookup_standard",
        description=(
            "按证候名或编码查标准证候定义，返回定义原文、病位/病性证素、"
            "主症、次症、舌脉、来源档次。用于核对「我想给的这个证型，标准里"
            "要求的主症患者到底有没有」。查不到会返回全部候选名供改写重查。"
        ),
        input_schema=LookupStandardInput,
        fn=lookup_standard,
    ),
    "check_residual": ToolSpec(
        name="check_residual",
        description=(
            "给一组患者症状和一组已推断证素，算出哪些症状已被这些证素解释、"
            "哪些还没有、哪些在国标层图里查无此症因而无法判断。用于决定"
            "「现在的证素够不够，还要不要继续追问或考虑兼夹证」。"
        ),
        input_schema=CheckResidualInput,
        fn=check_residual,
    ),
    "ask_user": ToolSpec(
        name="ask_user",
        description=(
            "向患者提出一个具体问题并结束本轮推理。只在现有信息真的分不开两个"
            "候选证候时用，且问题必须是可以直接回答的封闭问题"
            "（「有没有口苦？」可以，「你还有什么症状？」不行）。"
        ),
        input_schema=AskUserInput,
        fn=ask_user,
    ),
}


# ---------- 追问候选：国标层 indicates 权重上的信息增益 ----------
#
# 目标：在 S2 已经推出一批证素、但还分不清几个候选证候的时候，选下一个该问的
# 症状。判据是信息增益（bit）——问完这一句，对"到底是哪个证候"的不确定性能降
# 多少。这天然把"你还有什么症状"这类问题排除在外：它不对应图上任何一个症状
# 节点，压根进不了候选池；也天然压低了"所有候选证候都有/都没有"的症状，因为
# 它们的答案不改变后验。
#
# 概率模型（三个常数，都写明它们在起什么作用，不要当调参旋钮随手改）：
#
#   P(证候 | 已知证素)：命中一个证素，该证候的相对可能性乘以
#   ELEMENT_MATCH_ODDS。等价于对每个已知证素做一次"命中概率 0.9 / 未命中 0.1"
#   的独立判断。**不惩罚"该证候要求、但当前还没观察到"的证素**——追问阶段的
#   前提就是信息不全，那些没观察到的证素正是要问出来的东西，拿它们扣分会把
#   信息最多的候选问题提前踢掉。
#
#   P(症状=有 | 证候)：取该症状在这条证候下的 indicates 边权重。当前 λ1≡0，
#   权重就等于标准先验层（主症 1.0 / 次症 0.5），正好是"主症/次症"的字面读法。
#   **等医案数据让 λ1 变成非 0 之后这个复用要重新审**：那时权重会掺进
#   P(证素|症状) 的成分，跟 P(症状|证候) 不是同一个量。
#   标准没给这条症状的证候，取 P_UNLISTED（标准没列不等于不会出现）。
#
#   钳位 P_MAX：主症权重 1.0 会让"没有"这个回答的似然变成 0，一次否认就把该
#   证候的后验永久打成 0。国标主症也不是 100% 出现，所以钳到 0.95。

ELEMENT_MATCH_ODDS = 9.0
P_UNLISTED = 0.05
P_MAX = 0.95
# 低于这个增益就认为"问了也白问"，退到十问歌
MIN_INFORMATION_GAIN = 1e-6

# 十问歌后备。触发条件写在 question_candidates 的文档字符串里，一共 5 条。
# 顺序就是十问歌本身的顺序，不排序、不打分——后备的意义正是"图算不出来时
# 有一个确定的、不依赖数据的兜底顺序"。
SHIWEN_QUESTIONS: list[tuple[str, str]] = [
    ("寒热", "平时怕冷还是怕热？最近有没有发热、畏寒？"),
    ("汗", "出汗情况怎么样？有没有白天动一动就出汗、或者睡着后出汗？"),
    ("头身", "头和周身有没有不适？比如头晕、头痛、身体困重酸痛？"),
    ("二便", "大小便怎么样？次数、稀干、颜色跟平时比有没有变化？"),
    ("饮食", "胃口怎么样？吃得下多少，口味有没有变化？"),
    ("胸腹", "胸口和肚子有没有胀满、疼痛？什么时候明显？"),
    ("耳目", "耳朵眼睛有没有不适？比如耳鸣、听力下降、眼睛干涩模糊？"),
    ("口渴", "口渴吗？想喝水吗，想喝热的还是凉的？"),
    ("旧病", "以前得过什么病？现在还在吃什么药？"),
    ("因由", "这次是怎么起的病？有没有明显的诱因，比如受凉、生气、饮食不当？"),
]

# 带这些字样的症状名是"什么情况下加重/缓解"，用「是否…？」问才通顺，
# 用「有没有…？」会读成病句。
_CONDITION_MARKERS = ("加重", "减轻", "痛减", "而发", "尤甚", "即泻", "缓解")


def phrase_question(symptom: str) -> str:
    """标准症状名 -> 能直接念给患者听的封闭问题。"""
    if any(m in symptom for m in _CONDITION_MARKERS):
        return f"是否{symptom}？"
    if "，" in symptom or "、" in symptom:
        return f"有没有这样的表现：{symptom}？"
    return f"有没有{symptom}？"


def is_safety_relevant(symptom: str) -> bool:
    """这个症状一旦为"有"，会不会触发 S2 之前的安全否决？

    追问循环把答案收回来之后必须先跑 check_safety 再更新后验——否则会出现
    "患者答了有黑便，系统继续辨证开方"这种事，把安全否决在 S2 之前的约束
    从后门绕过去了。判据复用 core.safety 的关键词/正则表，不另建一张。
    """
    from core.safety import check_safety

    return check_safety([symptom]) is not None


def _entropy(probs) -> float:
    return -sum(p * math.log2(p) for p in probs if p > 0)


def _syndrome_index(store: NetworkXStore) -> dict[str, dict]:
    """证候 code -> {name, elements}。类目词排除：国标明确写了类目词不适用于
    临床诊断，把它放进假设空间会让信息增益去区分一个不能作为结论的东西。"""
    out: dict[str, dict] = {}
    for syn_id in store.find_nodes("syndrome"):
        attrs = store.get_node(syn_id) or {}
        if attrs.get("is_category"):
            continue
        elements = set()
        for src, _data in store.in_neighbors(syn_id, edge_type="composes"):
            name = (store.get_node(src) or {}).get("name")
            if name:
                elements.add(name)
        out[attrs.get("code") or syn_id] = {
            "id": syn_id,
            "name": attrs.get("name") or syn_id,
            "elements": elements,
        }
    return out


def _symptom_index(store: NetworkXStore, physician: str | None) -> dict[str, dict[str, float]]:
    """症状名 -> {证候 code: 权重}。同一症状对同一证候可能有多条 indicates 边
    （该证候有几个证素就有几条），取最大值——它们本来就同源于"这条证候的主症
    还是次症"，不是几份独立证据。"""
    out: dict[str, dict[str, float]] = {}
    for sym_id in store.find_nodes("symptom"):
        name = (store.get_node(sym_id) or {}).get("name")
        if not name:
            continue
        per: dict[str, float] = {}
        for _dst, data in store.neighbors(sym_id, edge_type="indicates"):
            code = data.get("via_syndrome")
            w = _edge_weight(data, physician)
            if code is None or w is None:
                continue
            per[code] = max(per.get(code, 0.0), w)
        if per:
            out[name] = per
    return out


def _p_symptom_given_syndrome(weights_by_code: dict[str, float], code: str) -> float:
    """P(症状=有 | 证候)，钳位后的。常数含义见上面那段注释。"""
    return min(max(weights_by_code.get(code, P_UNLISTED), P_UNLISTED), P_MAX)


def syndrome_posterior(
    current_elements: list[str],
    store: NetworkXStore | None = None,
    asserted_symptoms: list[str] | None = None,
    denied_symptoms: list[str] | None = None,
    physician: str | None = None,
) -> dict[str, float]:
    """P(证候 | 已知证素, 追问答案)。证素为空时退化为均匀先验——那是合理的初始
    状态（还没问出任何东西），不是错误。

    asserted/denied 是 G3 追问收回来的答案。**否认必须进后验**，不能只用来从
    候选池里去重：患者说「口不渴」是一条真证据，它把「口干或口苦」列为主症的
    那几个证候压下去，跟他说「口苦」把它们抬上来是同一件事的两面。只去重不
    更新，等于把一半的追问收益扔掉。

    用的似然跟信息增益那套完全一致（同一组常数、同样的钳位），所以「问这个问题
    预期能得到多少 bit」和「答完之后后验变成什么」在数学上是自洽的——两处各写
    一套的话，IG 排出来的最优问题答完可能并不最优。
    """
    store = store or get_graph_store()
    if store is None:
        return {}
    index = _syndrome_index(store)
    if not index:
        return {}
    current = set(current_elements or [])
    # 权重按同一位医家取——question_candidates 的似然也按这位医家取，两处不一致
    # 的话「问这个问题预期得到多少 bit」和「答完后验变成什么」就不再自洽。
    # 当前 λ1≡0 时各医家权重相同，这个参数没有可观测差异；等医案数据接入后会有。
    symptom_weights = _symptom_index(store, physician)

    scores = {}
    for code, info in index.items():
        s = float(ELEMENT_MATCH_ODDS ** len(current & info["elements"]))
        for sym in asserted_symptoms or []:
            s *= _p_symptom_given_syndrome(symptom_weights.get(sym, {}), code)
        for sym in denied_symptoms or []:
            s *= 1.0 - _p_symptom_given_syndrome(symptom_weights.get(sym, {}), code)
        scores[code] = s

    total = sum(scores.values())
    if total <= 0:
        # 所有证候的似然都被压到 0（答案互相矛盾时可能出现）。退回均匀分布而不是
        # 抛异常：追问循环还要继续，NaN 会让它整个哑掉。
        return {code: 1.0 / len(index) for code in index}
    return {code: s / total for code, s in scores.items()}


def _shiwen_fallback(k: int, asked: set[str], reason: str) -> list[dict]:
    out = []
    for topic, question in SHIWEN_QUESTIONS:
        if topic in asked or question in asked:
            continue
        out.append({
            "question": question,
            "symptom": None,
            "topic": topic,
            "information_gain": None,
            "p_yes": None,
            "source": "shiwen_fallback",
            "fallback_reason": reason,
            "if_yes_top": None,
            "if_no_top": None,
            "prior_entropy": None,
            # 键集必须跟 graph_ig 那一支完全一致：消费方按同一份契约读。十问歌问的
            # 是话题不是具体症状，答案里的危重内容由 check_safety 兜，这里恒 False。
            "safety_relevant": False,
        })
        if len(out) >= k:
            break
    return out


def question_candidates(
    current_elements: list[str],
    store: NetworkXStore | None = None,
    k: int = 3,
    known_symptoms: list[str] | None = None,
    asked: list[str] | None = None,
    physician: str | None = None,
    asserted_symptoms: list[str] | None = None,
    denied_symptoms: list[str] | None = None,
) -> list[dict]:
    """按信息增益给出接下来最该问的 k 个问题，算不出来时退到十问歌固定顺序。

    **退到十问歌的触发条件（穷举，5 条）：**
      1. 图谱不可用——data/graph.json 不存在或加载失败。
      2. 图里没有可用的假设空间——非类目证候节点为 0，或一条 indicates 边都没有。
      3. 候选症状池为空——图里 93 个标准症状全部已被患者陈述过或已经问过。
      4. 先验熵为 0——后验已经塌缩到单一证候，此时任何问题的信息增益都是 0，
         再按 IG 排序等于随机挑一个。
      5. 全部候选的信息增益 ≤ MIN_INFORMATION_GAIN——图区分不了当前这几个候选
         证候（比如它们的症状集合完全重合），问哪个都不改变后验。

    条件 4、5 数学上是同一件事的两种表现，分开列是因为它们对使用者的含义不同：
    4 是"已经问清楚了，不必再问"，5 是"图上没有能区分它们的症状"。返回结果的
    fallback_reason 会如实区分这两种。

    `known_symptoms` 传患者已经陈述过的症状（会从候选池里去掉），`asked` 传已经
    问过的（症状名或十问歌 topic 都认）。不传的话会反复问同一个问题。

    `asserted_symptoms` / `denied_symptoms` 是追问已经问出来的肯定/否定回答：
    两者都进后验（见 syndrome_posterior），也都从候选池里去掉——问过的问题不该
    再问第二遍，无论答案是有还是没有。
    """
    store = store or get_graph_store()
    asked_set = set(asked or [])

    if store is None:
        return _shiwen_fallback(k, asked_set, "图谱不可用（data/graph.json 不存在）")

    posterior = syndrome_posterior(
        current_elements, store,
        asserted_symptoms=asserted_symptoms, denied_symptoms=denied_symptoms,
        physician=physician,
    )
    symptom_weights = _symptom_index(store, physician)
    if not posterior or not symptom_weights:
        return _shiwen_fallback(k, asked_set, "图里没有可用的证候假设空间或 indicates 边")

    # 排除已知症状用的是 check_residual 那套片段匹配器，不另写一套字面规则——
    # 同一个模块里两套"这条症状算不算已经知道了"的判断迟早会分叉。
    # 它只做字面匹配，患者换个说法（「大便溏薄」vs 标准里的「大便稀溏」）就漏，
    # 这个缺口的根因是缺一层术语映射，见 data/SOURCES.md 第 7 节第 10 条。
    # 答过的（不管答有还是答没有）等同于已知，不再问第二遍
    all_known = list(known_symptoms or []) + list(asserted_symptoms or []) \
        + list(denied_symptoms or [])
    known_ids = {
        sym_id
        for ks in all_known
        for sym_id in _match_graph_symptoms(store, ks)
    }
    known_names = {(store.get_node(i) or {}).get("name") for i in known_ids}
    pool = [
        name for name in symptom_weights
        if name not in asked_set and name not in known_names
    ]
    if not pool:
        return _shiwen_fallback(k, asked_set, "图里的标准症状已全部问过或已由患者陈述")

    prior_entropy = _entropy(posterior.values())
    if prior_entropy <= 0:
        return _shiwen_fallback(
            k, asked_set, "后验已塌缩到单一证候，任何问题的信息增益都是 0"
        )

    index = _syndrome_index(store)
    scored = []
    for name in pool:
        weights = symptom_weights[name]
        p_yes_given = {
            code: min(max(weights.get(code, P_UNLISTED), P_UNLISTED), P_MAX)
            for code in posterior
        }
        p_yes = sum(posterior[c] * p_yes_given[c] for c in posterior)
        p_no = 1.0 - p_yes
        if p_yes <= 0 or p_no <= 0:
            continue
        post_yes = {c: posterior[c] * p_yes_given[c] / p_yes for c in posterior}
        post_no = {c: posterior[c] * (1 - p_yes_given[c]) / p_no for c in posterior}
        ig = prior_entropy - p_yes * _entropy(post_yes.values()) - p_no * _entropy(post_no.values())
        if ig <= MIN_INFORMATION_GAIN:
            continue
        top_yes = max(post_yes, key=post_yes.get)
        top_no = max(post_no, key=post_no.get)
        scored.append({
            "question": phrase_question(name),
            "symptom": name,
            "topic": None,
            "information_gain": round(ig, 4),
            "p_yes": round(p_yes, 3),
            "source": "graph_ig",
            "fallback_reason": None,
            # 回答"有"和"没有"之后各自最可能的证候。两者不同 = 这个问题真的在
            # 分叉；相同 = 它只是在加强/削弱同一个结论。
            "if_yes_top": index[top_yes]["name"],
            "if_no_top": index[top_no]["name"],
            "prior_entropy": round(prior_entropy, 4),
            # 回答"有"会命中安全否决层的问题（吐血、便血、黑便……）。信息增益上
            # 它们是合法候选，但答案绝不能被当成普通症状喂回证候后验——必须先过
            # check_safety。判据直接复用 core/safety.py 的表，不在这里另抄一份。
            "safety_relevant": is_safety_relevant(name),
        })

    if not scored:
        return _shiwen_fallback(
            k, asked_set, "所有候选症状的信息增益都约等于 0，图上区分不了当前候选证候"
        )

    # 同分时按症状名排序，保证同样输入给出同样顺序（可测、可复现）
    scored.sort(key=lambda d: (-d["information_gain"], d["symptom"]))
    return scored[:k]
