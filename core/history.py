"""R46 §7.5 第 14 条：本医师的问诊历史、收藏与统计。

**不引数据库**（CLAUDE.md：demo 阶段不要加数据库）。一份 JSONL 追加写，
跟审计链同一个目录、同一种形态；医院上线时换实现的是这一个模块，调用方不改。

## 三件事，三种用途，不要合并

- **历史**：本医师问过什么，可按日期/证型/方剂检索，可回看完整推导轨迹。
  它回答"我上周那个胃脘痛的病人当时是怎么辨的"。
- **收藏与模板**：医师把常用方存下来、加自己的批注。
  它回答"我惯用的那个加减是什么"。
- **统计**：近 N 次的证型分布、常用方、验证器提示分布。
  **这是给医师自己反思用的，不是考核指标**——所以不排名、不跟别人比、
  不算"正确率"。一个会被拿去考核的统计，医师会开始为它而开方。
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_PATH = DATA_DIR / "consult_history.jsonl"
FAVORITES_PATH = DATA_DIR / "favorites.jsonl"

_lock = threading.Lock()

#: 统计看最近多少次。**默认 50**：太少看不出习惯，太多把半年前的习惯算进来。
DEFAULT_RECENT = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, row: dict) -> dict:
    row = {**row, "at": row.get("at") or _now()}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # 半行（写到一半掉电）：跳过这一行，**不让整份历史读不出来**
                    continue
    return out


def record_consult(*, doctor_id: str, record_id: str, complaint: str,
                   syndrome: str = "", disease: str = "", formula: str = "",
                   advice_kinds: list[str] | None = None, at: str = "",
                   patient_ref: str = "", method: str = "",
                   herb_items: list[dict] | None = None,
                   doses_count: int | None = None, usage: str = "",
                   dosage_form: str = "",
                   path: Path | None = None) -> dict:
    """记一次问诊。

    ## R62 §6.2 改了"不记方药全文"这条

    这个函数原来只记摘要与编号，理由是"完整轨迹在审计链里，按 record_id 回查"。
    R62 §6.2 要求左栏「既往记录」点 `[载入此方]` 能把上次那张方载回处方表，
    这条决定就不成立了：

      - **审计链是按 `record_id` 的哈希链**，它回答"这一份有没有被改过"，
        不是"这位患者上次开的什么"——它没有按患者检索的索引，也不该有
        （那会让审计记录变成一个可按人查询的库）。
      - **病历文书（`save_emr`）只在医师点了「生成记录」时才有**。复诊载方
        不能依赖医师上次有没有点那个按钮。

    所以这里记整张方。**仍然不记推理过程**——那部分在审计链里，两处各存一份
    才是原来那条注释真正要防的事。

    `patient_ref` 是 §5.2 那个"患者称呼或编号，随便填"的字段：它**只用于本地
    分组**，不进推导，也不是患者身份标识。
    """
    return _append(path or HISTORY_PATH, {
        "doctor_id": doctor_id or "",
        "record_id": record_id or "",
        "patient_ref": (patient_ref or "").strip(),
        "complaint": (complaint or "")[:120],
        "syndrome": syndrome or "",
        "disease": disease or "",
        "method": method or "",
        "formula": formula or "",
        # 整张方。形状跟 `core/schemas.py::HerbItem` 一致——载回处方表时
        # 逐字段还原，不需要在两边各做一次转换。
        "herb_items": list(herb_items or []),
        "doses_count": doses_count,
        "usage": usage or "",
        "dosage_form": dosage_form or "",
        "advice_kinds": list(advice_kinds or []),
        # `at` 留成可传：补录历史与测试要指定时间。不传就是现在。
        "at": at,
    })


def delete_consult(record_id: str, *, path: Path | None = None) -> bool:
    """删一条记录（§6.2「可导出可删除」）。

    **写一行墓碑，不改原来那一行。** 这份文件是追加写的——改成可改写的格式
    要么整份重写（写到一半掉电就全没了），要么定长记录（中文记录没法定长）。
    墓碑还有一个好处：删除本身也留了痕，"这条记录什么时候被谁删的"查得出来。
    """
    if not (record_id or "").strip():
        return False
    _append(path or HISTORY_PATH, {"record_id": record_id, "deleted": True})
    return True


def list_consults(*, doctor_id: str = "", syndrome: str = "", formula: str = "",
                  since: str = "", patient_ref: str = "", limit: int = 50,
                  path: Path | None = None) -> list[dict]:
    """按医师/证型/方剂/日期/患者备注过滤，最近的在前。**墓碑行不出现在结果里**。

    `patient_ref` 是精确相等而不是子串包含：其余几个过滤条件是"找找看"，
    这一个是"就是这位"——按子串匹配会让备注「张三」把「张三丰」也带出来，
    而复诊载方载错人是这个功能最不能出的错。
    """
    rows = _read(path or HISTORY_PATH)
    deleted = {r.get("record_id") for r in rows if r.get("deleted")}
    out = []
    for r in rows:
        if r.get("deleted") or r.get("record_id") in deleted:
            continue
        if doctor_id and r.get("doctor_id") != doctor_id:
            continue
        if patient_ref and (r.get("patient_ref") or "") != patient_ref:
            continue
        if syndrome and syndrome not in (r.get("syndrome") or ""):
            continue
        if formula and formula not in (r.get("formula") or ""):
            continue
        if since and (r.get("at") or "") < since:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out[:limit]


def consults_by_patient(*, doctor_id: str = "", limit_per: int = 20,
                        path: Path | None = None) -> list[dict]:
    """既往记录按患者备注分组（§6.2 左栏那一块）。

    没填备注的归到一个 `patient_ref=""` 的组里——**不丢掉**：门诊现场最常见
    的就是没来得及填备注，而那几条记录照样要看得见。
    """
    groups: dict[str, list[dict]] = {}
    for r in list_consults(doctor_id=doctor_id, limit=10_000, path=path):
        groups.setdefault(r.get("patient_ref") or "", []).append(r)
    out = []
    for ref, rows in groups.items():
        out.append({
            "patient_ref": ref,
            "n": len(rows),
            # `visit_index` 是"第几诊"：最早的一条是第 1 诊。rows 是按时间
            # 倒序的，所以下标 i 对应的诊次是 n-i。
            "items": [{**r, "visit_index": len(rows) - i}
                      for i, r in enumerate(rows[:limit_per])],
        })
    out.sort(key=lambda g: (g["items"][0].get("at") or "") if g["items"] else "", reverse=True)
    return out


def next_visit_index(patient_ref: str, *, doctor_id: str = "",
                     path: Path | None = None) -> int:
    """这位患者下一次是第几诊。没有备注（空串）时恒返回 1——
    没填备注就没法说"这是同一个人的第二次"，硬按空串归组会把当天所有
    没填备注的患者算成同一个人的连续复诊。"""
    if not (patient_ref or "").strip():
        return 1
    return len(list_consults(doctor_id=doctor_id, patient_ref=patient_ref,
                             limit=10_000, path=path)) + 1


def add_favorite(*, doctor_id: str, name: str, herbs: list[str],
                 note: str = "", syndrome: str = "",
                 herb_items: list[dict] | None = None,
                 doses_count: int | None = None, usage: str = "",
                 path: Path | None = None) -> dict:
    """存一张个人常用方（R62 §6.5「个人模板」）。

    ## 为什么长在收藏上，不是新开一份模板存储

    R46 建这一层时叫「收藏与模板」，形状是 doctor_id / name / herbs / note；
    §6.5 要的是同一件事**带上剂量炮制煎法与适用证型**——医师存下来的"我惯用
    的那个加减"，本来就该带剂量，不带剂量的"模板"填进处方表还得再填一遍。
    这是同一个概念长厚了，不是第二个概念（CLAUDE.md 第 31 条）。

    `herbs`（只有药名的列表）**保留**：R46 起就有的调用方按它读，删掉等于
    为了加字段把既有契约改了。两者由这里一处同时写出，不会分叉。

    §6.5 明确写了不做科室方/院内验方——那是医院内部管理，不在这个产品的范围
    （§1.1），所以这里没有"作用域"这一维。
    """
    items = list(herb_items or [])
    return _append(path or FAVORITES_PATH, {
        "doctor_id": doctor_id or "",
        "name": name or "",
        "syndrome": syndrome or "",
        # 只有药名的那一份从 herb_items 现算（传了的话），保证两者永远一致。
        "herbs": list(herbs or []) or [str(i.get("name") or "") for i in items],
        "herb_items": items,
        "doses_count": doses_count,
        "usage": usage or "",
        "note": note or "",
    })


def delete_favorite(name: str, *, doctor_id: str = "", path: Path | None = None) -> bool:
    """删一张模板。跟 `delete_consult` 同一套墓碑语义（见那个函数）。
    按 `(doctor_id, name)` 定位——模板没有独立编号，医师是按名字找它的。"""
    if not (name or "").strip():
        return False
    _append(path or FAVORITES_PATH,
            {"doctor_id": doctor_id or "", "name": name, "deleted": True})
    return True


def list_favorites(*, doctor_id: str = "", path: Path | None = None) -> list[dict]:
    """这位医师的模板，最近存的在前。**同名只留最新的一份**——
    医师改了模板会再存一次同名的，列出两份会让"调用模板"下拉里出现两个
    一模一样的名字，而点哪一个的结果不同。"""
    rows = _read(path or FAVORITES_PATH)
    if doctor_id:
        rows = [r for r in rows if r.get("doctor_id") == doctor_id]
    rows.sort(key=lambda r: r.get("at") or "")
    latest: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("doctor_id") or "", r.get("name") or "")
        if r.get("deleted"):
            latest.pop(key, None)
        else:
            latest[key] = r
    out = list(latest.values())
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out


def stats(*, doctor_id: str = "", recent: int = DEFAULT_RECENT,
          path: Path | None = None) -> dict:
    """近 N 次的证型分布、常用方、验证器提示分布。

    **每个数旁边都带 `n`（这批一共几次）**——「肝胃不和证 3 次」在 5 次里
    和在 50 次里是完全不同的两件事（CLAUDE.md：任何数字都必须带对照）。
    """
    rows = list_consults(doctor_id=doctor_id, limit=recent, path=path)
    n = len(rows)
    syn = Counter(r.get("syndrome") for r in rows if r.get("syndrome"))
    fml = Counter(r.get("formula") for r in rows if r.get("formula"))
    adv = Counter(k for r in rows for k in (r.get("advice_kinds") or []))
    return {
        "n": n,
        "window": recent,
        "syndromes": [{"name": k, "count": v, "of": n} for k, v in syn.most_common(10)],
        "formulas": [{"name": k, "count": v, "of": n} for k, v in fml.most_common(10)],
        "advice_kinds": [{"name": k, "count": v, "of": n} for k, v in adv.most_common(10)],
        "note": ("这是给自己看的用药习惯回顾，不是考核指标——它不排名、不跟别人比、"
                 "也不算正确率。" if n else "这位医师还没有问诊记录。"),
    }


# ---------- 病历文书的存取（R46 §7.5 第 15 条的 GET 接口要用） ----------
#
# 跟问诊历史同一种形态：一份 JSONL 追加写，按 `record_id` 取最后一条
# ——**最后一条是最新的那一版**（医师改过之后重新存，改前那版留在前面的行里，
# 这样"这份病历改过几次、每次改了什么"可以直接从文件里看出来）。

EMR_PATH = DATA_DIR / "emr_drafts.jsonl"


def save_emr(record_id: str, draft: dict, *, doctor_id: str = "",
             path: Path | None = None) -> dict:
    return _append(path or EMR_PATH, {
        "record_id": record_id or "",
        "doctor_id": doctor_id or "",
        "draft": draft,
    })


def get_emr(record_id: str, path: Path | None = None) -> dict | None:
    """按编号取**最新的那一版**。没有就是 None——调用方据此回 404，
    而不是回一份空文书（空文书会被当成"这次问诊什么都没生成"）。"""
    rows = [r for r in _read(path or EMR_PATH) if r.get("record_id") == record_id]
    return rows[-1] if rows else None


def emr_versions(record_id: str, path: Path | None = None) -> list[dict]:
    """这份病历的全部版本，旧的在前。质控调阅要看"改过什么"。"""
    return [r for r in _read(path or EMR_PATH) if r.get("record_id") == record_id]
