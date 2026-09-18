"""R62 §12 第 1 项：一次问诊结果里**全部可点术语的释义**，随结果一次性下发。

## 为什么是一次性下发，不是点一次查一次

§11 那张表给"点术语到释义出现"的预算是 **100 毫秒**。一次 HTTP 往返在院内
网络上就吃掉大半，而 `/api/node_explain` 每次还要现查本体。所以这一层在
问诊结束时把这次结果里会被点到的全部术语查一遍，跟结果一起发下去，前端点
的时候只是本地取一个键——那才是 100 毫秒能做到的事。

代价是一次问诊多查几十到一百多条。这一层**零 LLM、全部走已经装在内存里的
本体与证候表**，实测见 `build_explanations` 的注释。

## 这个模块不生成任何释义

释义只有一处实现：`core/node_explain.py::explain_node`（八节固定顺序、
取不到就 `available=False`）。这个模块只回答另一个问题——**这一次结果里有
哪些术语要释义**。两个问题分开是因为它们变化的原因不同：新增一种可点的东西
（R62 给鉴别证型和加减建议里的药加了可点）改的是这里；释义里多一节
（R56 给证型加了「相似证型与鉴别点」）改的是那边。

## 术语 id 用的就是问诊图那一套前缀

`api/main.py::LAYER_PREFIX` 那九个前缀 + `rule::`。**同一个 id 在图上和在
释义里是同一个东西**，前端因此可以点图上的节点、也可以点正文里的词，走同一
条取数路径。id 另起一套的话，那两处就成了两份映射表。
"""
from __future__ import annotations

from core.node_explain import explain_node

#: 一次结果最多下发多少条释义。
#:
#: **120 是按最坏情况估的，不是拍的**：一张方 16 味药（§6.6 里最大的那一档
#: 是"16 以上"）+ 加减建议 4 条各 1 味 = 20 味药；九层链上的证型/病名/病机/
#: 治则/治法/病位 6 条；辨证要点 5 条、鉴别 4 条；医理规则每步一条、五步链
#: 加鉴别约 20 条；症状 20 条。合计 75 上下，120 留了七成余量。
#: 超了就截断**并说出来**——静默截断会让某个词点了没反应，而那看起来像 bug
#: 不像"太多了"。
MAX_TERMS = 120


def _add(out: list[dict], seen: set[str], node_id: str, kind: str,
         name: str, where: str) -> None:
    """去重保序。同一个词在多处出现（治法里的「疏肝」和药的功效里的「疏肝」）
    只留第一处——释义是同一份，`where` 记的是"最早在哪儿见到它"，前端按 id
    取数，不按 where 取。"""
    name = (name or "").strip()
    if not name or node_id in seen:
        return
    seen.add(node_id)
    out.append({"id": node_id, "kind": kind, "name": name, "where": where})


def collect_terms(s3_structured: dict | None, s3: dict | None) -> list[dict]:
    """这一次结果里全部可点的术语。**顺序跟界面上从上到下的阅读顺序一致**
    ——截断时留下的是先读到的那些。

    §7.1 列的可点项一条不少：证型、病名、辨证要点、鉴别证型、病机、治则、
    治法、方名、每一味药名（含加减建议里的）、每一条医理规则、病位脏腑、症状。
    """
    out: list[dict] = []
    seen: set[str] = set()
    d = s3_structured or {}
    flat = s3 or {}

    syn = (d.get("syndrome") or {})
    _add(out, seen, f"syn::{syn.get('name') or flat.get('syndrome') or ''}", "syndrome",
         syn.get("name") or flat.get("syndrome") or "", "syndrome")
    disease = syn.get("disease") or flat.get("disease") or ""
    # 病名走证型那一套查询（证候表里带 disease 列）——**不另起一个 disease::
    # 前缀**：那会长出一个 `explain_node` 认不出的 kind，点了整块隐藏。
    _add(out, seen, f"syn::{disease}", "syndrome", disease, "disease")

    for o in (d.get("organs") or []):
        _add(out, seen, f"organ::{o.get('organ')}", "element", o.get("organ") or "", "organ")
        _add(out, seen, f"mech::{o.get('pathogenesis')}", "pathogenesis",
             o.get("pathogenesis") or "", "pathogenesis")
        for sym in (o.get("supporting_symptoms") or []):
            _add(out, seen, f"sym::{sym}", "symptom", sym, "symptom")

    method = (d.get("method") or {})
    principle = method.get("principle") or flat.get("treatment_principle") or ""
    _add(out, seen, f"method::{principle}", "method", principle, "method")
    for t in (method.get("targets") or []):
        _add(out, seen, f"principle::{t}", "principle", t, "principle")

    fml = ((d.get("formula") or {}).get("candidate") or {})
    fname = fml.get("name") or ""
    _add(out, seen, f"formula::{fname}", "formula", fname, "formula")
    for it in (fml.get("herb_items") or []):
        _add(out, seen, f"herb::{it.get('name')}", "herb", it.get("name") or "", "herb")

    for kp in (d.get("key_points") or []):
        _add(out, seen, f"sym::{kp.get('point')}", "symptom", kp.get("point") or "", "key_point")
    for df in (d.get("differential") or []):
        _add(out, seen, f"syn::{df.get('syndrome')}", "syndrome",
             df.get("syndrome") or "", "differential")
    for mod in (d.get("modifications") or []):
        nm = ((mod.get("item") or {}).get("name")) or ""
        _add(out, seen, f"herb::{nm}", "herb", nm, "modification")

    # 医理规则：五步链上的 + 鉴别与加减里的。**全部收**——⑨「推导依据」那一栏
    # 每一条都可点，漏一条就有一行点了没反应。
    buckets = [*(d.get("organs") or []), syn, method, d.get("formula") or {},
               *(d.get("herb_choices") or []), *(d.get("differential") or []),
               *(d.get("modifications") or [])]
    for b in buckets:
        for ref in ((b or {}).get("rule_refs") or []):
            rid = ref.get("rule_id") or ""
            _add(out, seen, f"rule::{rid}", "rule", rid, "rule")
    return out


def build_explanations(s3_structured: dict | None, s3: dict | None, *,
                       ontology=None, limit: int = MAX_TERMS) -> dict:
    """术语表 + 每条的释义。**零 LLM**。

    实测：一张 6 味药的方、五步链齐全、鉴别 2 条，共 30 条术语，本体已预热的
    情况下整批 40 毫秒上下（本轮开发沙盒）。本体冷启动那一次另算——它由
    `api/warmup.py` 在服务起来时预热，不落在第一个患者头上。

    `ontology` 可注入：**默认 None 一路传给 `explain_node`**，由它按既有规矩
    取单例。在这里先 `get_ontology()` 一次再传下去看似能省几次单例查找，
    实际会让测试没法用"本体不可用"的桩——而"本体不在时这一层不崩"正是要测的。
    """
    terms = collect_terms(s3_structured, s3)
    truncated = len(terms) > limit
    kept = terms[:limit]
    by_id: dict[str, dict] = {}
    for t in kept:
        # 取不到释义的条目**照样收进来**：前端点了要能显示"这一条查不到释义"，
        # 而不是点了没反应——后者跟"这个词不可点"在界面上长得一模一样。
        by_id[t["id"]] = explain_node(t["id"], name=t["name"], ontology=ontology)
    n_available = sum(1 for v in by_id.values() if v.get("available"))
    note = ""
    if truncated:
        note = (f"本次可点术语 {len(terms)} 条，超过一次下发的上限 {limit} 条，"
                f"后 {len(terms) - limit} 条没有随结果下发（点开时会现查）。")
    return {
        "terms": kept,
        "by_id": by_id,
        "n": len(kept),
        "n_total": len(terms),
        "n_available": n_available,
        "truncated": truncated,
        "note": note,
    }
