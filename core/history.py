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
                   path: Path | None = None) -> dict:
    """记一次问诊。**只记摘要与编号，不记方药全文**——完整轨迹在审计链里，
    按 `record_id` 回查；两处各存一份会让"改了哪一份才算数"没有答案。"""
    return _append(path or HISTORY_PATH, {
        "doctor_id": doctor_id or "",
        "record_id": record_id or "",
        "complaint": (complaint or "")[:120],
        "syndrome": syndrome or "",
        "disease": disease or "",
        "formula": formula or "",
        "advice_kinds": list(advice_kinds or []),
        # `at` 留成可传：补录历史与测试要指定时间。不传就是现在。
        "at": at,
    })


def list_consults(*, doctor_id: str = "", syndrome: str = "", formula: str = "",
                  since: str = "", limit: int = 50,
                  path: Path | None = None) -> list[dict]:
    """按医师/证型/方剂/日期过滤，最近的在前。"""
    rows = _read(path or HISTORY_PATH)
    out = []
    for r in rows:
        if doctor_id and r.get("doctor_id") != doctor_id:
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


def add_favorite(*, doctor_id: str, name: str, herbs: list[str],
                 note: str = "", path: Path | None = None) -> dict:
    """收藏一张常用方，带医师自己的批注。"""
    return _append(path or FAVORITES_PATH, {
        "doctor_id": doctor_id or "",
        "name": name or "",
        "herbs": list(herbs or []),
        "note": note or "",
    })


def list_favorites(*, doctor_id: str = "", path: Path | None = None) -> list[dict]:
    rows = _read(path or FAVORITES_PATH)
    if doctor_id:
        rows = [r for r in rows if r.get("doctor_id") == doctor_id]
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows


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
