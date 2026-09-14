"""R2-3：Test 集跑次台账。**让"跑过几次"这件事可追溯，不是靠人记。**

SDT Test 到今天已经跑过 3 次（chain 21.702 修复前 / chain 22.833 / baseline
22.068），外加 1 次只重跑被安全否决那几条的局部跑。再反复在 Test 上调 prompt
就是在测试集上过拟合——那会让"外部可比的分数"这个本项目最硬的证据失效。

规矩（写进 eval/sdt/README.md）：prompt 改动先在 **Train**（200 条，金标准
就在 JSON 里）上验证方向，Validation（50 条）做中间验证，**Test 只在最终定型
后跑一次**。代码这一层能做的是：跑 Test 时把跑过几次摆在人眼前，并且把每次跑
都记下来。

两种事件，都是追加、不改写（JSONL 的意义就在这里）：
  run     —— 跑了一次 Test。这个是数"暴露了几次"用的。
  scored  —— 对某份 Test 提交算了一次分。零 LLM 调用、不构成新的暴露，
             单独记是为了让分数跟那次跑对得上（run 事件发生时分还没算出来，
             官方计分要另外跑一趟）。

**文件名不叫 test_*.py 是故意的**：`eval/sdt/test_run_log.py` 会被 pytest
当成测试模块收集。日志文件本身叫 `test_run_log.jsonl`（用户指定的路径），
它是数据不是模块，不会被收集。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "test_run_log.jsonl"
# 提醒里显示相对路径：绝对路径在一条要人读的警告里只是噪音。从 LOG_PATH 现算，
# 不另写一个字符串常量——同一个路径写两遍，改一处忘另一处。
LOG_PATH_DISPLAY = LOG_PATH.relative_to(Path(__file__).resolve().parents[2]).as_posix()
# prompt 模板的版本目录（prompts/v1/）。prompt 改了而这个版本号没动的话，
# 台账上两条记录会看起来是同一个 prompt 跑出来的——所以同时记 git commit，
# 两个一起才定得住"这一次跑的到底是哪份 prompt"。
PROMPT_VERSION = "v1"


def git_commit() -> str | None:
    """当前 HEAD 的短 hash。拿不到（不是 git 仓库 / 没装 git）返回 None，
    不编一个值——台账上一个假 commit 比没有 commit 更糟。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10,
                             cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_log(path: Path | None = None) -> list[dict]:
    """读台账。文件不存在 = 空台账（第一次跑）。坏行跳过并不静默：
    坏行意味着有人手改过这个文件，要让它显出来。"""
    path = Path(path or LOG_PATH)
    if not path.exists():
        return []
    entries = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"event": "unparseable", "line_no": i, "raw": line[:200]})
    return entries


def count_test_runs(entries: list[dict]) -> dict:
    """数 Test 暴露次数。**完整跑和局部重跑分开报**：`--only-ids` 那种只重跑
    被安全否决的几条，也确实看过 Test 的数据、也是一次暴露，但它跟"完整 50 条
    又跑了一遍"不是一回事。合成一个数字会让读的人分不清，所以两个都给。"""
    runs = [e for e in entries if e.get("event") == "run" and e.get("split") == "Test"]
    partial = [e for e in runs if e.get("partial")]
    return {
        "total": len(runs),
        "full": len(runs) - len(partial),
        "partial": len(partial),
        "entries": runs,
    }


def append(entry: dict, path: Path | None = None, dedupe: bool = False) -> dict:
    """追加一条。dedupe=True 时，若已存在一条"除时间戳外完全相同"的记录就不写
    （scored 事件用：同一份提交文件可以反复算分，台账不该被相同的行灌满；
    run 事件永不去重——两次跑就是两次暴露，哪怕参数一模一样）。"""
    path = Path(path or LOG_PATH)
    entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    if dedupe:
        probe = {k: v for k, v in entry.items() if k != "timestamp"}
        for existing in read_log(path):
            if {k: v for k, v in existing.items() if k != "timestamp"} == probe:
                return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def log_run(split: str, solver: str, out_path: Path, n_records: int, *,
            partial: bool, ignore_safety_veto: bool, model: str | None,
            backend: str | None, path: Path | None = None) -> dict | None:
    """跑完一次记一条。**只记 Test**：这个台账是给过拟合护栏用的，Train 和
    Validation 本来就该反复跑，记进来只会把真正要看的那几行埋掉。"""
    if split != "Test":
        return None
    return append({
        "event": "run",
        "split": split,
        "solver": solver,
        "submission": str(out_path),
        "n_records": n_records,
        # 局部重跑（--only-ids / --limit）不是完整的 50 条，单独标出来
        "partial": partial,
        "ignore_safety_veto": ignore_safety_veto,
        "model": model,
        "backend": backend,
        "prompt_version": PROMPT_VERSION,
        "git_commit": git_commit(),
        # 分数在这一刻还不知道（官方计分要另外跑一趟），由 scored 事件补
        "score": None,
    }, path=path)


def log_scored(split: str, submission: Path, score: float | None,
               score_kind: str = "official_automated_score",
               path: Path | None = None) -> dict | None:
    """算了一次分记一条（零 LLM 调用，不构成新的暴露）。同一份提交重复算分
    只留一条。

    score_kind 必须跟着 score 一起记：官方 automated_score 和我们逐条加权求和
    是两个口径（数值接近但凭据不同，见 eval/sdt/score.py 的文档），只记一个
    数字的话以后比较几次跑的人不知道自己在比什么。
    """
    if split != "Test":
        return None
    return append({
        "event": "scored",
        "split": split,
        "submission": str(submission),
        "score": score,
        "score_kind": score_kind,
        "prompt_version": PROMPT_VERSION,
        "git_commit": git_commit(),
    }, path=path, dedupe=True)


def test_split_warning(entries: list[dict]) -> str:
    """跑 Test 时打在 stdout 上的提醒。措辞刻意直白：这条提醒的作用是让人在
    按下回车之前停一秒，含糊的措辞起不到这个作用。"""
    counts = count_test_runs(entries)
    breakdown = (f"：完整跑 {counts['full']} 次 + --only-ids/--limit 局部重跑 "
                 f"{counts['partial']} 次" if counts["partial"] else "")
    scores = [e.get("score") for e in counts["entries"] if e.get("score") is not None]
    scored = [e for e in entries
              if e.get("event") == "scored" and e.get("score") is not None]
    known = sorted({*scores, *(e["score"] for e in scored)})
    return (
        "⚠ Test 集应只在最终定型后跑一次。本项目已跑过 "
        f"{counts['total']} 次{breakdown}（台账 {LOG_PATH_DISPLAY}）。"
        + (f"已记录的分数：{known}。" if known else "")
        + "反复调优会过拟合，让这个分数失去外部可比性。"
        "prompt 改动请先在 Train（200 条，金标准在 JSON 里）上验证方向，"
        "Validation 做中间验证（注意它的满分上限是 48.9998/50，官方金标准带 BOM）。"
    )
