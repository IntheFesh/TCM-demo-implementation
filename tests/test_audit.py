"""core/audit.py 的离线测试：哈希链 JSONL 的追加、校验、并发写入。不需要
网络、不需要 API key，都是本地文件 IO，秒级跑完。"""
import json
import threading

import pytest

import core.audit as audit


@pytest.fixture(autouse=True)
def _isolated_audit_path(tmp_path, monkeypatch):
    """每条测试都用一个全新的临时文件，不碰真实的 data/audit.jsonl——
    真实审计日志一旦被测试写脏，seq/hash 链就跟生产数据混在一起了。"""
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")


def _record(**overrides) -> dict:
    base = dict(
        doctor_id="dr_ye", patient_ref="patient-001",
        model_suggestion={"name": "瓜蒌薤白半夏汤"}, final={"name": "瓜蒌薤白半夏汤加减"},
        diffs=["附子 10g→15g"], safety_at_export={"incompatible": []}, override_reason=None,
    )
    base.update(overrides)
    return base


# ---------- append_audit ----------


def test_append_audit_first_record_uses_genesis_prev_hash():
    record = audit.append_audit(_record())
    assert record.seq == 1
    assert record.prev_hash == audit.GENESIS_HASH
    assert len(record.hash) == 64  # sha256 hexdigest


def test_append_audit_chains_prev_hash_to_previous_record():
    r1 = audit.append_audit(_record())
    r2 = audit.append_audit(_record(doctor_id="dr_wu"))
    assert r2.seq == 2
    assert r2.prev_hash == r1.hash
    assert r2.hash != r1.hash


def test_append_audit_writes_one_json_line_per_call():
    audit.append_audit(_record())
    audit.append_audit(_record())
    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # 每一行本身必须是合法 JSON，不是拼接坏了


def test_append_audit_preserves_override_reason():
    record = audit.append_audit(_record(override_reason="患者对轻剂无效，加大附子剂量"))
    assert record.override_reason == "患者对轻剂无效，加大附子剂量"


# ---------- verify_audit_chain：正常情况 ----------


def test_verify_audit_chain_true_for_nonexistent_file():
    assert audit.verify_audit_chain() == (True, [])


def test_verify_audit_chain_true_for_ten_clean_records():
    """M8 闸门：verify_audit_chain 对一条 10 record 的链返回 True。"""
    for i in range(10):
        audit.append_audit(_record(doctor_id=f"dr_{i}"))
    ok, problems = audit.verify_audit_chain()
    assert ok is True
    assert problems == []


# ---------- verify_audit_chain：篡改检测 ----------


def test_verify_audit_chain_false_and_points_at_tampered_seq():
    """M8 闸门：手工篡改第 5 条后返回 False 且指出 seq=5。"""
    for i in range(10):
        audit.append_audit(_record(doctor_id=f"dr_{i}"))

    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    tampered = json.loads(lines[4])  # 第 5 条，下标 4
    assert tampered["seq"] == 5
    tampered["doctor_id"] = "被篡改的医生id"  # 只改内容，不重算 hash——最常见的篡改形态
    lines[4] = json.dumps(tampered, ensure_ascii=False)
    audit.AUDIT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = audit.verify_audit_chain()
    assert ok is False
    assert any("seq=5" in p and "hash" in p for p in problems), problems


def test_verify_audit_chain_reports_only_the_tampered_record_not_cascading():
    """篡改第 5 条不该把第 6-10 条也一起判成有问题——比较基准用"文件里
    实际写的 hash"而不是重算出来的，问题只在真正出问题的那一条上报一次。"""
    for i in range(10):
        audit.append_audit(_record(doctor_id=f"dr_{i}"))
    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    tampered = json.loads(lines[4])
    tampered["final"] = {"name": "被篡改的方名"}
    lines[4] = json.dumps(tampered, ensure_ascii=False)
    audit.AUDIT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = audit.verify_audit_chain()
    assert ok is False
    flagged_seqs = {int(p.split("seq=")[1].split("：")[0]) for p in problems if "seq=" in p}
    assert flagged_seqs == {5}


def test_verify_audit_chain_detects_deleted_record_via_seq_gap():
    for i in range(5):
        audit.append_audit(_record(doctor_id=f"dr_{i}"))
    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    del lines[2]  # 删掉第 3 条（seq=3）
    audit.AUDIT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, problems = audit.verify_audit_chain()
    assert ok is False
    # 第 4 条现在紧跟在第 2 条后面：seq 不连续 + prev_hash 对不上（它期望
    # 接在第 3 条后面，但第 3 条已经不存在了），两个维度都应该报出来。
    assert any("seq=4" in p and "序号不连续" in p for p in problems), problems
    assert any("seq=4" in p and "prev_hash" in p for p in problems), problems


def test_verify_audit_chain_detects_hash_field_itself_tampered():
    """反过来的篡改：内容没改，"hash" 字段本身被换成了别的值（比如企图把
    这条记录的哈希对齐成篡改后应该有的样子，但算错了/换了个不相关的哈希）。"""
    audit.append_audit(_record())
    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    data = json.loads(lines[0])
    data["hash"] = "f" * 64
    audit.AUDIT_PATH.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    ok, problems = audit.verify_audit_chain()
    assert ok is False
    assert any("seq=1" in p and "hash" in p for p in problems)


def test_verify_audit_chain_detects_malformed_json_line():
    audit.append_audit(_record())
    with audit.AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write("这不是合法 JSON\n")
    ok, problems = audit.verify_audit_chain()
    assert ok is False
    assert any("不是合法 JSON" in p for p in problems)


# ---------- 并发追加 ----------


def test_concurrent_appends_keep_the_chain_intact():
    """两个医生"同时"导出：不加锁的话两边可能读到同一个 prev_hash，链会
    分叉。用 threading.Barrier 逼真实并发重叠（不加屏障的话两个线程很可能
    一前一后串行执行，就算真有竞态也测不出来——跟 test_retriever_mode.py
    里用 Barrier 逼并发重叠是同一个做法）。"""
    n = 8
    barrier = threading.Barrier(n)
    results: list[audit.AuditRecord] = []
    results_lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # n 个线程都到齐了才同时冲进 append_audit
        record = audit.append_audit(_record(doctor_id=f"dr_{i}"))
        with results_lock:
            results.append(record)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(results) == n
    # seq 必须是 1..n 各出现恰好一次——如果锁没生效，两个线程可能拿到同一个
    # seq，或者干脆漏掉一个数。
    assert sorted(r.seq for r in results) == list(range(1, n + 1))

    ok, problems = audit.verify_audit_chain()
    assert ok is True, problems


def test_concurrent_appends_interleaved_with_sequential_reads_stay_consistent():
    """并发追加之后，链条不仅要"完整"，seq=k 那条记录的 prev_hash 也必须
    真的等于 seq=k-1 那条记录的 hash——不是只查"没有 problems"，是逐条
    重新核实链接关系，双重确认 verify_audit_chain 自己的判断没有假阴性。"""
    n = 6
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        audit.append_audit(_record(doctor_id=f"dr_{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    lines = [json.loads(line) for line in audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")]
    lines.sort(key=lambda d: d["seq"])
    assert [d["seq"] for d in lines] == list(range(1, n + 1))
    assert lines[0]["prev_hash"] == audit.GENESIS_HASH
    for prev, cur in zip(lines, lines[1:]):
        assert cur["prev_hash"] == prev["hash"]
