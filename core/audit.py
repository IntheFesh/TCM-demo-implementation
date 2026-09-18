"""M8：处方导出审计日志。哈希链 JSONL，不用 SQLite。

为什么不用 SQLite（任务描述原文的理由，抄一遍以便看代码的人不用回去翻）：
审计日志的核心属性是防篡改。SQLite 默认给不了——任何人打开 db 文件改一行，
改完看不出来。哈希链每条记录带前一条的哈希，改中间任何一条，后面全部对不上，
一条命令（verify_audit_chain）就能校验出来。而且零依赖、纯文本可读，
AutoDL 上就是普通文件写入，现在就能跑。

CLAUDE.md「加载模型/大文件的对象一律惰性初始化」这里不适用——这个文件不是
一次性加载进内存的资源，是一路追加写的日志，append_audit/verify_audit_chain
每次调用都各自开关文件，没有可以惰性初始化的单例状态。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

# 放在 data/ 顶层而不是 data/standard/：这不是人工整理的静态参考表（那类才
# 走 data/standard/ 的 .gitignore 例外），是运行时生成的审计流水——跟
# data/graph.json、data/case_triples.jsonl 同一类"生成物"，.gitignore 的
# `*.jsonl` 整体忽略规则已经会把它挡在版本控制外，不需要新开例外
# （CLAUDE.md 那条 jsonl 坑专指人工整理表被误挡的情况，这里刚好反过来：
# 这份文件本来就不该进版本库，现有规则已经是对的）。
AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "audit.jsonl"

# 第一条记录的 prev_hash——用全 0 而不是空字符串/None，这样 verify_audit_chain
# 里"上一条的 hash"这个变量从头到尾都是同一个类型（64 位十六进制字符串），
# 不用在第一条特判成另一种形状。
GENESIS_HASH = "0" * 64


class AuditRecord(BaseModel):
    """一条处方导出审计记录。字段形状跟任务描述原文的示例一致。"""

    seq: int
    timestamp: str  # ISO8601 UTC
    doctor_id: str
    patient_ref: str | None
    model_suggestion: dict  # FormulaCandidate.model_dump()
    final: dict
    diffs: list[str]
    safety_at_export: dict
    override_reason: str | None
    prev_hash: str
    hash: str


def _record_hash(payload: dict) -> str:
    """payload 除 hash 外全部字段的 sha256。`sort_keys=True` 保证同样内容
    不管字典构造顺序如何都算出同一个哈希——哈希链的可复现校验依赖这一点，
    如果哈希会因为字段写入顺序不同而变化，verify_audit_chain 重新算一遍
    时可能对不上，那就不是"内容变了"检测出的问题，是这个函数自己不稳定。
    """
    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != "hash"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_audit(record_data: dict) -> AuditRecord:
    """追加一条审计记录。`record_data` 需要带 doctor_id / patient_ref /
    model_suggestion / final / diffs / safety_at_export / override_reason
    七个字段——seq / timestamp / prev_hash / hash 四个由这个函数负责算，
    调用方不该自己编（编错就是一条假记录，审计的意义就没了）。

    并发写入：整个"读最后一行取 prev_hash、算本条 hash、追加写入"必须在
    同一把独占锁下完成，不能只锁"写"那一步——两个医生同时导出，如果只锁
    写入，两边都可能先各自读到同一个 prev_hash（这时都还没写），各自算出
    的 hash 在自己看来都合法，但两条记录会争同一个 seq、同一个 prev_hash，
    链在这里分叉，之后 verify_audit_chain 只能看见"某处断了"，看不出这是
    并发竞态还是真的被篡改。用 fcntl.flock(fd, LOCK_EX) 把"读+算+写"整段
    包进临界区，第二个线程/进程会阻塞在拿锁那一步，直到第一个写完释放锁，
    保证任何时刻只有一个人能看到"当前最后一条是什么"这件事。

    Linux 专用（fcntl 模块），跟任务描述原文一致（"fcntl.flock，Linux 上
    够用"）——这个项目部署目标是 AutoDL（Linux），不需要跨平台。
    """
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # "a+"：不存在就创建；写入永远追加到文件末尾（POSIX O_APPEND 语义，
    # 不受当前读游标位置影响），不用像 "r+" 那样自己再 seek 到文件尾。
    with AUDIT_PATH.open("a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            lines = [line for line in f.readlines() if line.strip()]
            if lines:
                last = json.loads(lines[-1])
                seq = last["seq"] + 1
                prev_hash = last["hash"]
            else:
                seq = 1
                prev_hash = GENESIS_HASH

            payload = {
                "seq": seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "doctor_id": record_data["doctor_id"],
                "patient_ref": record_data.get("patient_ref"),
                "model_suggestion": record_data["model_suggestion"],
                "final": record_data["final"],
                "diffs": record_data["diffs"],
                "safety_at_export": record_data["safety_at_export"],
                "override_reason": record_data.get("override_reason"),
                "prev_hash": prev_hash,
            }
            payload["hash"] = _record_hash(payload)
            record = AuditRecord.model_validate(payload)

            f.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")
            f.flush()
            return record
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def verify_audit_chain(path: Path | None = None) -> tuple[bool, list[str]]:
    """从头校验整条链。返回 (是否完整, 问题列表)——两者独立返回，不是从
    "列表非空"反推 False，调用方不用自己再判一次。

    `path` 默认 `None`、函数体内部才取 `AUDIT_PATH`，不写成
    `path: Path = AUDIT_PATH` 这种在函数定义时就绑死默认值的写法——那样
    测试里 `monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / ...)`
    换掉模块属性之后，已经定义好的函数签名里那个默认参数值还是模块导入
    那一刻绑定的旧对象，测试换了个寂寞。默认值在调用时才查 `AUDIT_PATH`，
    才能跟着 monkeypatch 走。

    文件不存在或是空文件都视为"完整"（还没有任何记录，谈不上断没断），
    不是异常状态。

    每条记录分三个维度分别检查、分别报（不是笼统一句"第 N 条有问题"）：
      - hash 对不上：内容被改了但没跟着重算 hash（或反过来，hash 字段本身
        被单独改动）——这条记录自己内部不自洽。
      - seq 不连续：丢了一条，或者被插了一条不该在的。
      - prev_hash 跟上一条实际记的 hash 不一致：链被剪断重接（即便这条
        自己的 hash 算对了，也说明它声称的"前一条"跟文件里真实的前一条
        对不上）。
    往下走时用"这条记录文件里实际写的 hash"（不是重新算出来的）作为下一条
    prev_hash 的比较基准——这样一条记录的问题只会在它自己身上报一次，
    不会因为基准选错了而连累到它之后所有记录都被误判成"prev_hash 断裂"。
    """
    if path is None:
        path = AUDIT_PATH
    if not path.exists():
        return True, []
    problems: list[str] = []
    prev_hash = GENESIS_HASH
    expected_seq = 1
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                # 不吞：一行坏 JSON 说明文件本身损坏，这比"某条记录的字段
                # 对不上"更严重，必须报出来，不能当成"这一行不存在"跳过。
                problems.append(f"第 {line_no} 行不是合法 JSON：{e}")
                continue

            seq = data.get("seq")
            recorded_hash = data.get("hash")
            recomputed_hash = _record_hash(data)
            if recomputed_hash != recorded_hash:
                problems.append(f"seq={seq}：hash 对不上（内容被篡改，或 hash 字段本身被改动）")
            if seq != expected_seq:
                problems.append(f"seq={seq}：序号不连续（期望 {expected_seq}）")
            if data.get("prev_hash") != prev_hash:
                problems.append(f"seq={seq}：prev_hash 跟上一条记录的实际 hash 对不上（链被剪断重接）")

            prev_hash = recorded_hash
            expected_seq = (seq + 1) if isinstance(seq, int) else expected_seq + 1

    return (len(problems) == 0), problems


# ---------- R40：异步写入（给"不在关键路径上"的审计点用） ----------
#
# ## 为什么这里有两条路，而不是把所有审计都改成异步
#
# 实测（R40 profiler，本沙盒）：`append_audit()` 一次 **0.3 ms**（读尾行取
# prev_hash + flock + 追加写）。异步化能省的就是这 0.3 ms。
#
# 而处方导出那个端点**必须同步**：医生点了"导出"之后拿到 200，意味着这张方
# 已经进了审计链。改成异步的话，进程在那 0.3 ms 内被 kill（重启、OOM、
# 编排器滚动更新）就会出现"药房拿到了方、审计链里没有这条记录"——
# 三甲的审计要求下这是个合规缺陷，而换来的是 0.3 ms。
# **不为性能牺牲正确性**（R40 §12 第 4 条）：这一项的正确做法是量出来、
# 指出代价不对等、把同步保留，而不是把它异步掉再在报告里写"已优化"。
#
# 那为什么还要有异步路：R45 要把哈希链扩展到**完整推理轨迹**（每次问诊都写，
# 不只是导出）。那条路上的记录不是"发给药房的凭据"，丢一条的后果是
# "少一条可回放的轨迹"，跟合规凭据不是一个量级；而它在问诊的关键路径上，
# 每次问诊都同步写一次磁盘会累加。所以机制先建好并验清楚语义边界。

_async_pool = None
_async_lock = threading.Lock()


def _pool():
    """单线程的写入池。**必须是单线程**：哈希链要求"读尾行取 prev_hash、算 hash、
    追加写"整段串行。两个写线程会各自读到同一个 prev_hash，链在那里分叉——
    `flock` 挡得住跨进程，挡不住同一进程里两个线程之间的逻辑竞态（它们各自
    拿锁、各自看到的"最后一条"可能相同，取决于谁先拿到）。
    单线程池把并发压成队列，这是这条路唯一安全的形状。"""
    global _async_pool
    if _async_pool is None:
        with _async_lock:
            if _async_pool is None:
                from concurrent.futures import ThreadPoolExecutor

                _async_pool = ThreadPoolExecutor(max_workers=1,
                                                 thread_name_prefix="audit")
    return _async_pool


def append_audit_async(record_data: dict):
    """排队写一条审计记录，立刻返回 `Future`。

    **调用方必须自己决定要不要等**：不等就是接受"这条记录可能没落盘"。
    合规凭据类的审计**不许**走这条路（见上面那段）。
    """
    return _pool().submit(append_audit, record_data)


def flush_audit(timeout: float = 5.0) -> bool:
    """等队列里的异步写全部落盘。返回是否在 `timeout` 内等完。

    进程退出前、以及测试里断言审计内容之前必须调它——否则断言的是一个
    还没写完的文件，而那种测试会随机红，比没有测试更糟。
    """
    pool = _async_pool
    if pool is None:
        return True
    # 往单线程池里再排一个空活：它跑完就意味着前面排的全跑完了（FIFO 单线程）。
    fut = pool.submit(lambda: None)
    try:
        fut.result(timeout=timeout)
    except Exception:  # noqa: BLE001 - 等不到就如实回 False，不抛
        return False
    return True
