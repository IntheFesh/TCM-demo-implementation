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
from typing import Callable, NamedTuple

from pydantic import BaseModel, Field

from core.data_paths import pharmacology_read_path, pharmacology_read_path_or_canonical
from core.graph.store import NetworkXStore
from core.herbs import normalize_herb
from core.physicians import physician_choices_text, resolve_physician_id
from core.schemas import MateriaMedicaPredicate, ReferenceSource
# 模块级导入：`core.syndrome_norm` 不导入 core 里的任何东西，没有循环。
# R31 之前这一句写在 `_symptom_text_matches` 函数体里，而那个函数在一次
# `check_residual` 里被调用三千多次——函数内 import 每次都要查一遍 sys.modules。
from core.syndrome_norm import normalize as _syndrome_canonical

ROOT = Path(__file__).resolve().parent.parent
GRAPH_PATH = ROOT / "data" / "graph.json"
STANDARD_PATH = ROOT / "data" / "standard" / "syndromes.jsonl"
# X3 在 AutoDL 上从医案抽出来的三元组，一行一条：
# {"case_id", "physician", "s", "p", "o", "source_span"}
# source_span 是这条三元组在医案原文里的出处片段——没有它，工具查出来的东西
# 就跟凭空生成的没区别，防幻觉链条在工具这一层断掉。
CASE_TRIPLES_PATH = ROOT / "data" / "case_triples.jsonl"
# 总纲 2.2/2.4d：药理层三元组（offline/extract_materia_medica.py 产出，一行一条
# {s, p, o, source_span, source, book}，source 是 classic/modern）。
# R18-F 起它**进版本控制**（挪到 data/standard/ 下，见 core/data_paths.py 的
# 模块文档字符串）；旧位置 data/materia_medica.jsonl 仍然可读，因为 AutoDL 上
# 那台机器已经有一份。路径解析不在这里写死，走 core.data_paths 那一处。
MATERIA_MEDICA_PATH = pharmacology_read_path_or_canonical("materia_medica")


# ---------- 惰性单例（模块顶层不加载任何文件） ----------

_graph_store: NetworkXStore | None = None
_standard_defs: list[dict] | None = None
_case_triples: list[dict] | None = None
_materia_medica: list[dict] | None = None


def reset_tool_caches() -> None:
    """测试用：清掉所有惰性缓存。改了 monkeypatch 的路径之后必须调一次，
    否则读到的还是上一个用例加载的数据。"""
    global _graph_store, _standard_defs, _case_triples, _materia_medica
    _graph_store = None
    _standard_defs = None
    _case_triples = None
    _materia_medica = None


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


def _materia_medica_path() -> Path | None:
    """这一次要读哪个文件。

    两件事都要成立，所以不能只写一句：
      - `MATERIA_MEDICA_PATH` **可被覆盖**（测试 monkeypatch 它指到 tmp_path，
        部署也可能把它指到别处）。直接调 `pharmacology_read_path()` 会让那个
        覆盖失效——覆盖失效不报错，只是静默读了另一份文件，最难查的一类。
      - 文件可能在 import 之后才生成（run_onsite.sh 段 5 落盘、服务先起来）。
        只用 import 时算好的常量会永远读不到新文件。
    回退顺序本身仍然只有一处实现（core.data_paths），这里只加"覆盖优先"。
    """
    if MATERIA_MEDICA_PATH.exists():
        return MATERIA_MEDICA_PATH
    return pharmacology_read_path("materia_medica")


def _load_materia_medica() -> list[dict] | None:
    """None = 文件还没生成；坏行记成 {"_bad_line": n} 不中断——跟
    _load_case_triples 同一套处理，理由也一样（机器生成的文件一行坏了不该让
    整个工具不可用，行号留给人排查）。"""
    global _materia_medica
    if _materia_medica is not None:
        return _materia_medica
    with _load_lock:
        if _materia_medica is not None:
            return _materia_medica
        path = _materia_medica_path()
        if path is None:
            return None
        rows = []
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows.append({"_bad_line": lineno})
                continue
            rows.append(row if isinstance(row, dict) else {"_bad_line": lineno})
        _materia_medica = rows
        return _materia_medica


def _unknown_physician_error(value: str) -> str:
    """physician 既不是 id 也不是已注册的中文名时给模型的报错——必须列出可用值，
    模型才能自我纠正；只回一句"未知医家"它下一步还是瞎猜。"""
    return f"physician={value!r} 不是已注册的医家。可用值：{physician_choices_text()}"


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
                "图中没有这个节点。国标层收的是教材/国标的标准症状名，患者原话往往"
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
    # 描述从 PHYSICIANS 动态生成，不写死医家清单——之前这里手写了
    # 「ye_tianshi / wu_jutong」，张锡纯注册进来之后没人记得改这一处。
    physician: str | None = Field(
        default=None,
        description=f"只看这位医家的医案，填医家 id（不是中文名），可用值：{physician_choices_text()}",
    )
    case_id: str | None = Field(default=None, description="只看这条医案")
    limit: int = Field(default=20, ge=1, le=200)


def query_case_graph(symptom: str | None = None, predicate: str | None = None,
                     physician: str | None = None, case_id: str | None = None,
                     limit: int = 20) -> dict:
    # 三种"空"必须分开（见模块顶部约束 2 和 SOURCES.md 第 31 条）：
    #   ① 参数错了 —— available:true + error，模型据此改参数重查
    #   ② 数据文件不存在 —— available:false + note
    #   ③ 数据在、确实没匹配 —— available:true + note（带"已查 N 条"）
    # 旧实现把 ① 和 ③ 混成同一个静默的 triples:[]，模型以为"这位医家没有
    # 相关医案"，实际是 physician 填了中文名匹配不上 id（AutoDL 实测 9 次全空）。
    if physician is not None:
        resolved = resolve_physician_id(physician)
        if resolved is None:
            return {"available": True, "triples": [], "error": _unknown_physician_error(physician)}
        physician = resolved
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

    # "已查 N 条"给模型看：③ 那种真的没匹配时，N 告诉它"确实查了、不是没查"，
    # 不会误以为数据缺失而放弃医案层。N 是过了 physician 过滤之后的条数。
    n_scanned = sum(
        1 for r in rows if physician is None or r.get("physician") == physician
    )
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
    if not matched:
        scope = f"该医家（{physician}）" if physician is not None else "全部医家"
        notes.append(f"{scope}的医案三元组里没有匹配项，已查 {n_scanned} 条三元组")
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
    physician: str = Field(
        min_length=1,
        description=f"在这位医家的医案库里检索，填医家 id（不是中文名），可用值：{physician_choices_text()}",
    )
    k: int = Field(default=3, ge=1, le=10)


def search_cases(query: str, physician: str, k: int = 3) -> dict:
    from core.retrieval import adaptive_min_score, get_retriever

    # 三种"空"分开报，见 query_case_graph 里同一段注释：① 参数错 → error，
    # ② 数据文件不存在 → available:false，③ 真没匹配 → note 带"已查 N 条"。
    resolved = resolve_physician_id(physician)
    if resolved is None:
        return {"available": True, "cases": [], "error": _unknown_physician_error(physician)}
    physician = resolved

    try:
        retriever = get_retriever()
    except FileNotFoundError as e:
        return {"available": False, "cases": [], "note": str(e)}

    # 跟 run_physician（core/chain.py::_search_cases）用同一个阈值算法（P0-7：
    # adaptive_min_score，不再是写死的 MIN_RETRIEVAL_SCORE）——这里不设的话，
    # 模型会拿到几条相似度很低的不相关医案并被鼓励去引用它们。
    min_score = adaptive_min_score(retriever, query, physician)
    hits = retriever.search(query, physician, k=k, min_score=min_score)
    if not hits:
        n = retriever.case_count(physician)
        scanned = f"已查 {n} 条医案" if n is not None else "医案库已查完（条数未知）"
        return {
            "available": True,
            "cases": [],
            "note": f"该医家（{physician}）医案中没有相似度达标的匹配项，{scanned}",
        }
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


class SymptomMatchKeys(NamedTuple):
    """一个标准症状名的匹配材料。**只取决于 `name`**，所以按名字算一次就够。

    批量匹配（`_match_graph_symptoms_many`）拿它省掉的是这个乘法：图里一千多个
    标准症状名 × 患者症状条数，每一对都要切一次并列片段、查一次 SYNONYMS。
    按名字算一次之后是"名字数"次，不是"名字数 × 患者症状数"次。
    """

    fragments: list[str]
    concepts: frozenset[str]


def _match_keys(name: str) -> SymptomMatchKeys:
    return SymptomMatchKeys(_symptom_fragments(name), frozenset(_syndrome_canonical(name)))


def _symptom_text_matches(name: str, patient_symptom: str,
                          keys: SymptomMatchKeys | None = None) -> bool:
    """标准症状名（或三元组里的症状文本）跟患者原话对不对得上：双向包含，
    对不上再按并列片段试一次，字面都够不到再查一遍 core.syndrome_norm 的
    SYNONYMS 表。**全模块唯一的症状文本匹配器**——_match_graph_symptoms 和
    query_case_graph 都走这里。

    AutoDL 实测：query_case_graph 查患者现代说法「胃脘胀痛」查不到医案三
    元组，而三元组存的是医案原文的古文简写「脘痛」（417 条）——两个词面上
    没有公共子串，字面双向包含/并列拆分都够不到，是真实的术语映射问题，
    跟 λ1 恒 0、element_index 覆盖率 47% 同一根：医案原文用的是古代医家自己
    的措辞，患者/S1 normalize 用的是现代标准词，字面匹配天然覆盖不全。

    这里补的是 CLAUDE.md「同一概念的匹配逻辑只能有一处实现」要求的那种
    复用——不新开一套症状同义词表，直接查 core.syndrome_norm.SYNONYMS
    （已经在用于医案门类归一化）：两个文本只要有一个方向能查到 canonical
    概念、且两边 canonical 后相同，就算匹配上。**这不是术语映射问题的
    根本解法**：SYNONYMS 现在只覆盖"胃痛"这一类已经登记过的门类词，像
    「嗳气」这种没有对应门类条目的具体症状词依然查不到——要系统性解决，
    需要阶段二/三给每个症状词建标准化映射表，这里只是在现有表能覆盖到的
    范围内，让 query_case_graph 和 _match_graph_symptoms 不必各自绕开
    SYNONYMS 另写一套。
    """
    if not name or not patient_symptom:
        return False
    if name in patient_symptom or patient_symptom in name:
        return True
    k = keys if keys is not None else _match_keys(name)
    if any(f in patient_symptom for f in k.fragments):
        return True
    # `k.concepts` 为空时**不必**再算患者那一侧——省掉的是那一整遍 SYNONYMS 扫描，
    # 而绝大多数标准症状名在 SYNONYMS 里没有条目。
    return bool(k.concepts) and bool(k.concepts & _syndrome_canonical(patient_symptom))


def _match_graph_symptoms_many(
    store: NetworkXStore, patient_symptoms: list[str]
) -> dict[str, list[str]]:
    """一次匹配多条患者原话 -> {患者原话: [图里的标准症状节点 id]}。

    **为什么要有批量版**：图里一千多个标准症状名，逐条患者症状各扫一遍的话，
    每个名字的并列片段和 SYNONYMS 概念会被重算"患者症状条数"次。
    `check_residual` 一次传三五条症状，那就是三五倍的重复计算。
    这里把"按名字算一次"提到外层循环——**不是加缓存**，是把循环嵌套的顺序换过来。

    同名的患者症状只算一次（用 dict 收结果），返回的 id 顺序跟
    `store.find_nodes("symptom")` 的遍历顺序一致，跟单条版逐字节相同。
    """
    out: dict[str, list[str]] = {s: [] for s in patient_symptoms}
    if not out:
        return out
    wanted = list(out)
    for sym_id in store.find_nodes("symptom"):
        name = (store.get_node(sym_id) or {}).get("name", "")
        if not name:
            continue
        keys = _match_keys(name)
        for s in wanted:
            if _symptom_text_matches(name, s, keys):
                out[s].append(sym_id)
    return out


def _match_graph_symptoms(store: NetworkXStore, patient_symptom: str) -> list[str]:
    """患者原话 -> 图里的标准症状节点 id。刻意只做字面（片段级双向包含）匹配，
    不引入向量相似度：这一层必须离线可跑、确定性可测。匹配不上的症状会被单独
    报成 off_graph，而不是默默算进"未解释"——把"图里没有这个词"和"这个症状
    确实没被证素解释"混为一谈，会把覆盖率算成一个假数。

    **是批量版的一层投影**，不是第二份实现（CLAUDE.md 第 31 条）：
    单条和多条走的是同一个循环，不会出现"两个工具对同一个词给出相反答案"。
    """
    return _match_graph_symptoms_many(store, [patient_symptom])[patient_symptom]


def check_residual(symptoms: list[str], elements: list[str] | None = None) -> dict:
    store = get_graph_store()
    if store is None:
        return {"error": f"图谱文件不存在：{GRAPH_PATH}"}
    elements = elements or []
    element_ids = {f"element::{e}" for e in elements}

    explained, unexplained, off_graph = [], [], []
    matched_by_symptom = _match_graph_symptoms_many(store, symptoms)
    for s in symptoms:
        matched = matched_by_symptom[s]
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


# ---------- 5b. query_materia_medica（总纲 2.4d，药理层） ----------


class QueryMateriaMedicaInput(BaseModel):
    herb: str = Field(min_length=1, description="药名（如「黄芪」「炙甘草」），炮制前缀/剂量/括号注会被归一后再匹配")
    predicate: MateriaMedicaPredicate | None = Field(
        default=None, description="只看这一类：性味 / 归经 / 功效 / 用量 / 禁忌 / 炮制；不填全部"
    )
    source: ReferenceSource | None = Field(
        default=None, description="classic=古籍本草，modern=现代教材/药典；不填两者都返回、各自标明来源"
    )
    limit: int = Field(default=20, ge=1, le=200)


def query_materia_medica(herb: str, predicate: str | None = None,
                         source: str | None = None, limit: int = 20) -> dict:
    """三种"空"分开报，跟 search_cases / query_case_graph 同一套（SOURCES.md
    第 31 条）：① 参数错 → error；② 数据文件不存在 → available:false；
    ③ 数据在、确实没匹配 → note 带"已查 N 条"。药名的归一只有
    core.herbs.normalize_herb 一处实现——两边（查询词、数据里的 s）都过它，
    不在这里另写一套前缀剥离。"""
    rows = _load_materia_medica()
    if rows is None:
        return {
            "available": False,
            "triples": [],
            "note": (
                f"药理层数据尚未生成（{MATERIA_MEDICA_PATH} 不存在）。这份数据由 "
                "offline/extract_materia_medica.py 在有真实 LLM 的机器上从本草/教材"
                "原文抽取产出，不是代码缺陷。"
            ),
        }
    target = normalize_herb(herb)
    if not target:
        return {"available": True, "triples": [],
                "error": f"herb={herb!r} 归一后为空，不是可查的药名"}
    bad_lines = [r["_bad_line"] for r in rows if "_bad_line" in r]
    rows = [r for r in rows if "_bad_line" not in r]
    n_scanned = sum(1 for r in rows if source is None or r.get("source") == source)
    matched = [
        r for r in rows
        if (source is None or r.get("source") == source)
        and (predicate is None or r.get("p") == predicate)
        and normalize_herb(r.get("s") or "") == target
    ]
    out = {
        "available": True,
        "herb": target,
        "total_matched": len(matched),
        # source_span 和 source（古籍/现代）必须原样带出来：前者是出处凭据，
        # 后者让模型知道"这是古籍怎么说"还是"现代药典怎么说"，两者不混。
        "triples": [
            {"s": r.get("s"), "p": r.get("p"), "o": r.get("o"),
             "source_span": r.get("source_span"), "source": r.get("source"), "book": r.get("book")}
            for r in matched[:limit]
        ],
    }
    notes = []
    if not matched:
        scope = f"{source} 来源" if source else "全部来源"
        kind = f"「{predicate}」" if predicate else ""
        notes.append(f"药理层（{scope}）里没有「{target}」的{kind}记录，已查 {n_scanned} 条三元组")
    if bad_lines:
        notes.append(f"药理层文件有 {len(bad_lines)} 行无法解析，已跳过（行号：{bad_lines[:10]}）")
    if notes:
        out["note"] = "；".join(notes)
    return out


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
            "每条结果的 score 是稠密向量（语义）相似度，不是最终排序依据——"
            "默认检索模式会融合关键词精确匹配，一条医案可能因为关键词精确"
            "命中而排在前面，score 却不高（比如只有 0.5-0.6）；score 低不代表"
            "这条医案跟本例不相关，不要仅凭这个数字就判断某条结果无关。"
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
    "query_materia_medica": ToolSpec(
        name="query_materia_medica",
        description=(
            "查药理层：一味药的性味、归经、功效、用量、禁忌、炮制（从本草古籍与"
            "现代教材/药典原文抽出的三元组，每条带 source_span 出处和 classic/modern"
            " 来源标签，古籍记载与现代记载分开返回、不混）。用于回答「为什么这味药"
            "能治这个证」「这味药的用量上限/禁忌是什么」；药名会先归一（炮制前缀、"
            "剂量、括号注去掉）再匹配。"
        ),
        input_schema=QueryMateriaMedicaInput,
        fn=query_materia_medica,
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
    """证候 code -> {name, elements, disease}。类目词排除：国标明确写了类目词
    不适用于临床诊断，把它放进假设空间会让信息增益去区分一个不能作为结论的
    东西。disease 是 R2 教材扩表时补的字段（手工的 17 条没有，是 None）——
    syndrome_posterior 的 disease_hint 用它收窄候选池。"""
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
            "disease": attrs.get("disease"),
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


def _scope_by_disease(index: dict[str, dict], disease_hint: str | None) -> dict[str, dict]:
    """按病名收窄候选池——R2 教材扩表把候选证候从 17 条撑到 200+ 条后，
    单条追问答案在全量候选上归一化，movement 会小到跟浮点噪声（float64
    相对精度约 2e-16）同量级，两个候选谁大谁小可能已经是舍入误差在决定，
    不是真的没有区分度（见追问链路那轮修复报告的根因验证）。先按病名收窄
    到同一病名下的证候（通常几条到十几条），再在这个小得多的空间里归一化，
    是"先辨病再辨证"在数学上的落地——不是为了让数字好看设的技巧。

    未标 disease 的条目（手工的原始 17 条）**总是保留**，不管有没有给
    disease_hint、给的是哪个病名：它们是这个项目里核验最严格的一批条目
    （K1 那轮 7 个独立来源交叉确认），不能因为没打病名标签就被病名收窄
    误伤掉；只排除"标了病名、但标的是另一个病名"的条目。

    收窄后 < 3 条就退回未收窄的 index——病名判断本身可能错（match_disease
    是规则打分，不是精确诊断），候选池小于 3 条时"病名判错了"的代价
    （把真正的证候排除在候选外）比"候选池大导致精度低"更严重，宁可退回
    全量也不要在一个几乎没有选择余地的假候选池里瞎猜。"""
    if not disease_hint:
        return index
    scoped = {
        code: info for code, info in index.items()
        if info["disease"] is None or info["disease"] == disease_hint
    }
    if len(scoped) < 3:
        return index
    return scoped


def syndrome_posterior(
    current_elements: list[str],
    store: NetworkXStore | None = None,
    asserted_symptoms: list[str] | None = None,
    denied_symptoms: list[str] | None = None,
    physician: str | None = None,
    disease_hint: str | None = None,
    index: dict[str, dict] | None = None,
    symptom_weights: dict[str, dict[str, float]] | None = None,
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

    disease_hint 见 _scope_by_disease 的文档字符串——收窄逻辑单独抽出一个
    函数，因为 question_candidates 算 IG 时也要用同一份候选池，不能两处
    各写一套收窄规则。

    `index` / `symptom_weights` 是**已经算好的两张索引**，不传就自己算。
    加这两个参数是为了去掉一次重复扫图：`question_candidates` 先调这个函数拿
    后验、再自己建同样的两张索引，于是 `_symptom_index`（扫全部症状节点的
    indicates 边，R31 实测 1087 个）和 `_syndrome_index` 在一次追问里各跑两遍。
    传参而不是给它们加缓存——**这两张索引的内容完全由 (store, physician) 决定，
    调用方手里就有，没有必要为此引进一层缓存**（CLAUDE.md：demo 阶段不加缓存）。
    """
    store = store or get_graph_store()
    if store is None:
        return {}
    if index is None:
        index = _syndrome_index(store)
    if not index:
        return {}
    index = _scope_by_disease(index, disease_hint)
    current = set(current_elements or [])
    # 权重按同一位医家取——question_candidates 的似然也按这位医家取，两处不一致
    # 的话「问这个问题预期得到多少 bit」和「答完后验变成什么」就不再自洽。
    # 当前 λ1≡0 时各医家权重相同，这个参数没有可观测差异；等医案数据接入后会有。
    if symptom_weights is None:
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
    disease_hint: str | None = None,
) -> list[dict]:
    """按信息增益给出接下来最该问的 k 个问题，算不出来时退到十问歌固定顺序。

    disease_hint 原样传给 syndrome_posterior（见 core.tools._scope_by_disease）
    ——这里和 syndrome_posterior 必须用同一份候选池，不能这边收窄了那边没收窄：
    那样"问这个问题预期得到多少 bit"（在收窄后的候选池上算）和"答完后验变成
    什么"（如果没收窄，在全量候选池上算）就不是同一个假设空间下的数字，IG
    排出来的最优问题就不再对应真实会发生的后验更新。

    **退到十问歌的触发条件（穷举，5 条）：**
      1. 图谱不可用——data/graph.json 不存在或加载失败。
      2. 图里没有可用的假设空间——非类目证候节点为 0，或一条 indicates 边都没有。
      3. 候选症状池为空——图里的标准症状全部已被患者陈述过或已经问过。
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

    **安全相关症状（吐血/便血/黑便/意识改变……）保证进入返回列表，不参与 IG
    排名竞争、也不吃 MIN_INFORMATION_GAIN 这道门槛。** 判据是 is_safety_relevant
    （复用 core/safety.py 的表）。理由和取舍见下面挑选阶段的代码注释。
    """
    store = store or get_graph_store()
    asked_set = set(asked or [])

    if store is None:
        return _shiwen_fallback(k, asked_set, "图谱不可用（data/graph.json 不存在）")

    # **两张索引在这里各建一次，然后传给 syndrome_posterior。**
    # 原来的写法是先调 syndrome_posterior（它自己建一遍），回来再建一遍——
    # `_symptom_index` 要扫全部症状节点的 indicates 边（R31 实测 1087 个），
    # 原来一次追问白扫两遍。
    index = _syndrome_index(store)
    symptom_weights = _symptom_index(store, physician)
    posterior = syndrome_posterior(
        current_elements, store,
        asserted_symptoms=asserted_symptoms, denied_symptoms=denied_symptoms,
        physician=physician, disease_hint=disease_hint,
        index=index, symptom_weights=symptom_weights,
    )
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
        for ids in _match_graph_symptoms_many(store, all_known).values()
        for sym_id in ids
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

    # 后验的键和值各拉成一条列表，内层循环就不必再按 code 建三个字典。
    # 顺序跟 `posterior` 的迭代顺序一致，所以每一步的浮点运算次序**逐位不变**
    # ——这是纯粹去掉字典哈希开销，不是换算法（换算法会让 `ig` 在
    # MIN_INFORMATION_GAIN 门槛上的边界候选跳变）。
    # ---------- 前缀和：内层从 O(证候数) 降到 O(这个症状实际指向的证候数) ----------
    #
    # **为什么可以这样算。** 一个症状对某个证候的似然 g_c 只有两种来源：
    # 它在这个证候的 indicates 边上（`weights` 里有这个 code），或者没有——没有的
    # 一律取钳位后的 `P_UNLISTED`，**同一个常数 q**。实测：图里一千多个症状平均只
    # 指向 3 个证候（3282 条 indicates 边 / 1087 个症状节点），所以 174 个证候里
    # 171 个走的是同一个 q。
    #
    # 记 S = 这个症状列出了的证候，U = 其余。对 U 里每个 c：
    #   post_yes_c = p_c·q/p_yes = p_c·ry        （ry = q/p_yes，与 c 无关）
    #   −Σ_U v·log2 v = −ry·Σ_U p_c·log2 p_c − ry·log2(ry)·Σ_U p_c
    # 而 Σ_U p_c·log2 p_c = A − A_S、Σ_U p_c = B − B_S，A/B 是全集的量、**只算一次**。
    # 所以 U 那一整块退化成两次乘加，不必逐个遍历。post_no 同理（rn = (1−q)/p_no）。
    #
    # **数学上等价，浮点上不逐位相同。** 实测（1087 个候选 × 6 组证素）：
    # `round(ig, 4)`、`p_yes`、两个分叉结论、以及**整个排名**全部一致；
    # 未取整的 ig 最大绝对差 3.3e-14、相对差 2.2e-11。
    # 唯一的理论风险是 `ig <= MIN_INFORMATION_GAIN`（1e-6）这道门槛上的边界候选
    # ——扰动比门槛小 8 个数量级，实测 6 组证素下候选集合一条不差。
    # 判据在 tests/test_r29_review_fixes.py：跟教科书写法比排名和取整后的值。
    codes = list(posterior)
    priors = [posterior[c] for c in codes]
    pos = {c: i for i, c in enumerate(codes)}
    log2 = math.log2          # 局部绑定：这几行在内层循环里被调用几万次
    # 未列出的证候一律取钳位后的 P_UNLISTED。值跟 `min(max(P_UNLISTED, P_UNLISTED), P_MAX)`
    # 逐位相同，不是近似。
    q = min(max(P_UNLISTED, P_UNLISTED), P_MAX)
    total_p = sum(priors)
    total_plogp = sum(p * log2(p) for p in priors if p > 0)
    # U 里 prior 最大的那个下标。`post_yes` 和 `post_no` 在 U 上都是 prior 的单调
    # 增函数，所以两边的 argmax 是同一个；平局取下标小的，跟原来
    # 「按迭代顺序扫、严格大于才换」的解法一致。
    by_prior_desc = sorted(range(len(codes)), key=lambda i: (-priors[i], i))
    scored = []
    for name in pool:
        weights = symptom_weights[name]
        listed = sorted((pos[c], min(max(g, P_UNLISTED), P_MAX))
                        for c, g in weights.items() if c in pos)
        b_s = 0.0
        a_s = 0.0
        yes_from_s = 0.0
        for i, _g in listed:
            p = priors[i]
            b_s += p
            if p > 0:
                a_s += p * log2(p)
        for i, g in listed:
            yes_from_s += priors[i] * g
        p_yes = yes_from_s + q * (total_p - b_s)
        p_no = 1.0 - p_yes
        if p_yes <= 0 or p_no <= 0:
            continue
        # S 那一段逐个算（平均 3 项），顺带记下 S 内的 argmax
        #
        # **argmax 比的是分子 `p·g`，不是商 `p·g/p_yes`。** 两者的数学序完全一样
        # （p_yes > 0 是公共除数），但分子**不含 p_yes**，于是"谁赢"不再取决于
        # p_yes 是怎么加出来的。这一点是实测逼出来的：主诉证素「肝」+ 肯定「口苦」+
        # 否认「口渴」时，「大便秘结」这一条的三个候选证候
        # （热重于湿证 0.0099636 / 气郁化火证 同值 / 肝阳上亢证 0.0099638）
        # 分子差在 1 个 ulp 上，比商的话前缀和算出的 p_yes 跟全量求和差 1e-17,
        # 就足够让"谁赢"翻过来——**一个 1 ulp 决定的展示字段**。
        # 比分子把这份敏感性去掉了：它只剩原本就存在的"三个候选确实几乎相等"。
        ent_yes = ent_no = 0.0
        iy = in_ = -1
        best_y = best_n = -1.0
        for i, g in listed:
            p = priors[i]
            vy = p * g / p_yes
            vn = p * (1 - g) / p_no
            if vy > 0:
                ent_yes -= vy * log2(vy)
            if vn > 0:
                ent_no -= vn * log2(vn)
            num_y = p * g
            num_n = p * (1 - g)
            if num_y > best_y:
                best_y, iy = num_y, i
            if num_n > best_n:
                best_n, in_ = num_n, i
        # U 那一整块：两次乘加
        ry = q / p_yes
        rn = (1.0 - q) / p_no
        rest_plogp = total_plogp - a_s
        rest_p = total_p - b_s
        ent_yes += -ry * rest_plogp - ry * log2(ry) * rest_p
        ent_no += -rn * rest_plogp - rn * log2(rn) * rest_p
        ig = prior_entropy - p_yes * ent_yes - p_no * ent_no
        # 这道 MIN_INFORMATION_GAIN 门槛对安全相关症状也照样生效，不单独放宽——
        # 见下面挑选阶段那段注释。
        if ig <= MIN_INFORMATION_GAIN:
            continue
        # U 里的最大值参与 argmax。listed 覆盖全部证候时没有 U（实测不会发生：
        # 一个症状指向 174 个证候才可能），那时 next() 给 None。
        listed_idx = {i for i, _ in listed}
        u_best = next((i for i in by_prior_desc if i not in listed_idx), None)
        if u_best is not None:
            p = priors[u_best]
            num_y, num_n = p * q, p * (1.0 - q)
            # 严格大于 + U 的下标可能比 S 的小，所以平局时要取下标小的那个
            # （原来的写法是按下标顺序扫全集、严格大于才换，等价于"并列取下标最小"）
            if num_y > best_y or (num_y == best_y and u_best < iy):
                best_y, iy = num_y, u_best
            if num_n > best_n or (num_n == best_n and u_best < in_):
                best_n, in_ = num_n, u_best
        top_yes = codes[iy]
        top_no = codes[in_]
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

    # 安全相关症状不参与 IG 排名竞争。R2 教材扩表把症状候选池从 93 撑到一千多个
    # （R31 实测 1087，这个数每重建一次图谱都会变，所以这里不写死）之后，吐血/便血/黑便这类危重症状即使跟当前证候确实相关（清得过上面那道
    # MIN_INFORMATION_GAIN 门槛），排名也很容易被成百上千个普通症状挤到 k 名
    # 开外——生产链路（core/followup.py::run_followup）固定 k=1、只取
    # candidates[0]，挤不进 top-k 就等于问不到，safety_relevant=True 那条
    # "答了要不要先过 check_safety" 的判断压根拿不到数据，G3 追问是安全否决层
    # 后门这条约束（CLAUDE.md 改造期约定）就从这里被绕过去了。
    #
    # 做法：先按 IG 正常排序、正常截到 k 条；如果这 k 条里一条安全相关的都没有，
    # 就把（已经清过 MIN_INFORMATION_GAIN 门槛的）安全候选里 IG 最高的那个换到
    # 最前面，顶替掉原本排名最后的一条，长度仍是 k。选择"固定占最前面的一个
    # 位置"而不是"占 k 个位置之外的额外名额"：后一种做法在 k=1 的生产调用下
    # 形同虚设——多出来的名额永远不会被读到，等于没修。
    #
    # 但只在**这轮追问会话里还一条安全相关症状都没问过**时才这样强行插队——
    # `asked` 传进来的是本次会话已经问过的症状。原始 17 条手工条目里有的证候
    # （比如胃热壅盛证）本身就正当地带吐血/便血/黑便好几个安全相关主症，不加
    # 这道"问过一条就不再抢跑"的限制，会把这些症状一个接一个地排到队首，
    # 追问 3 轮的预算会被同一个安全门类的不同措辞耗光，真正该问的鉴别诊断
    # 问题一个都问不上（实测：主诉两胁胀满时命中过这个情况）。只保证"至少有
    # 机会问到一条"，不保证"问到所有能问的"——后者对这个 demo 的追问轮次预算
    # 不现实，前者已经能把 G3 的安全后门链路接通。
    already_asked_safety = any(is_safety_relevant(a) for a in asked_set)
    top_k = scored[:k]
    if not already_asked_safety and not any(c["safety_relevant"] for c in top_k):
        safety_candidates = [c for c in scored if c["safety_relevant"]]
        if safety_candidates:
            promoted = safety_candidates[0]
            top_k = [promoted] + top_k[: max(0, k - 1)]
    return top_k
