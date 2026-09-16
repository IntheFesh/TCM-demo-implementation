"""R19：`scripts/run_onsite.sh` 的续跑。

`--from` 要人自己记住断在哪，而这套东西跑几个小时、中间会换终端、容器也可能
被回收——"我记得是段 5 挂的"本身就是故障点。`--resume` 读状态文件算起点。

这些测试**真的跑那个 bash 脚本**（`--status` / `--resume` 都是零调用的路径），
不是读它的文本猜行为。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_onsite.sh"
# R25 加了段 9（R21~R24 的上机项）。**这个元组是"脚本里有哪几段"的期望值**，
# 不是段数的第二处定义——下面那条测试拿它跟脚本里现数出来的比，两者不一致就红。
# 加段时改这一处是有意的：它是一道"你知道自己加了一段"的确认。
# **有意的契约变更（R26）**：十元组 → 十一元组（加了段 10「R26 蒸馏」）。
# 这个元组钉的是"可续跑的段清单跟剧本里的段一一对应"，所以段一多就必须改，
# 不能放宽成包含关系——后者再也发现不了误删一段。
SEGMENTS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10")


def _run(args, state: Path | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    if state is not None:
        env["ONSITE_STATE"] = str(state)
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _state(tmp_path: Path, rcs: dict[str, int]) -> Path:
    p = tmp_path / "state.tsv"
    p.write_text("".join(f"{n}\t{rc}\t2026-09-15T01:00:00+00:00\n"
                         for n, rc in sorted(rcs.items())), encoding="utf-8")
    return p


def test_the_script_segments_match_the_expected_list():
    """**R25 起十段**（原来九段，名字也跟着改）。段号从脚本里现数，
    跟上面那个期望元组比——不是在两处各写一个段数。"""
    src = SCRIPT.read_text(encoding="utf-8")
    block = src[src.index("SEGMENTS=("):src.index("\n)", src.index("SEGMENTS=("))]
    nums = re.findall(r'^\s*"(\w+)\|', block, re.M)
    assert nums == list(SEGMENTS), nums


def test_status_on_a_fresh_machine_says_nothing_ran_yet(tmp_path):
    r = _run(["--status"], state=tmp_path / "nope.tsv")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("还没跑过") == len(SEGMENTS)
    assert "--resume 会从段 0 开始" in r.stdout


def test_status_reports_each_segments_last_exit_code(tmp_path):
    st = _state(tmp_path, {"0": 0, "1": 0, "2": 1})
    r = _run(["--status"], state=st)
    assert r.returncode == 0, r.stderr
    assert "0（成功）" in r.stdout
    assert "1（失败）" in r.stdout
    assert "--resume 会从段 2 开始" in r.stdout


def test_status_distinguishes_a_manual_abort_from_a_failure(tmp_path):
    """退出码 10 是人在卡点上按了 N，不是脚本挂了。两件事要分开显示——
    混成"失败"会让人去查 docs/onsite_troubleshooting.md 找一个不存在的故障。"""
    st = _state(tmp_path, {"0": 0, "3": 10})
    r = _run(["--status"], state=st)
    assert "10（人工在卡点中止）" in r.stdout


def test_resume_starts_at_the_first_segment_that_never_succeeded(tmp_path):
    """**已经成功的段不重跑**：段 5 重跑一次两千多次调用。

    `--resume --dry-run` 要能回答"它会从哪一段开始"——那正是开跑前最想知道的
    一件事，所以起点算在 --dry-run 退出**之前**。
    """
    st = _state(tmp_path, {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 1})
    r = _run(["--resume", "--dry-run"], state=st)
    assert r.returncode == 0, r.stderr
    assert "--resume：从段 5 开始" in r.stdout
    assert "段 0..4 上次都是退出码 0，不重跑" in r.stdout
    assert "--dry-run：什么都没跑" in r.stdout


def test_resume_counts_a_never_run_segment_as_unfinished(tmp_path):
    """没记录 ≠ 成功。只看"有没有失败记录"会把从没跑过的段直接跳过。"""
    st = _state(tmp_path, {"0": 0, "1": 0})
    r = _run(["--status"], state=st)
    assert "--resume 会从段 2 开始" in r.stdout


def test_resume_says_so_when_there_is_nothing_left(tmp_path):
    """每段都成功过时 --resume 不跑任何段。**要说出来**——静默退出会被当成
    "又跑了一遍，都过了"。"""
    st = _state(tmp_path, {n: 0 for n in SEGMENTS})
    r = _run(["--resume"], state=st)
    assert r.returncode == 0, r.stderr
    assert "没有需要续跑的段" in r.stdout
    assert "--only" in r.stdout


def test_resume_conflicts_with_from_and_only(tmp_path):
    """一个说"读状态文件"，一个说"我指定"。拒绝而不是挑一个赢——挑一个赢的话，
    传错的人不会知道自己被忽略了。"""
    for args in (["--resume", "--from", "4"], ["--resume", "--only", "3"]):
        r = _run(args, state=tmp_path / "s.tsv")
        assert r.returncode == 2, f"{args} 应该被拒绝：{r.stdout}"
        assert "不能一起传" in r.stderr


def test_record_overwrites_instead_of_appending(tmp_path):
    """同一段重跑要覆盖旧记录。追加的话 --resume 读到的是第一次那条（失败的），
    修好重跑成功也还会再跑一遍。"""
    st = tmp_path / "state.tsv"
    script = (
        f'ONSITE_STATE="{st}"\n'
        f'SEGMENTS=("2|本地模型|0|no|x")\n'
        + _extract_fn("record_segment") + _extract_fn("segment_state")
        + 'record_segment 2 1\nrecord_segment 2 0\n'
        'echo "lines=$(wc -l < "$ONSITE_STATE")"\n'
        'echo "state=$(segment_state 2)"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "lines=1" in r.stdout, r.stdout
    assert "state=0" in r.stdout, r.stdout


def _extract_fn(name: str) -> str:
    """从脚本里抠一个 bash 函数出来单独跑。**不是重写一份**——重写的话测的是
    副本，脚本改了测试照样绿。"""
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


def test_state_file_lands_in_a_gitignored_place():
    """它是这台机器这一次跑的状态，不是项目内容。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'ONSITE_STATE="${ONSITE_STATE:-out/onsite_state.tsv}"' in src
    r = subprocess.run(["git", "check-ignore", "-q", "out/onsite_state.tsv"],
                       cwd=ROOT, capture_output=True)
    assert r.returncode == 0, "状态文件会被提交进仓库"


def test_every_segment_records_its_exit_code_in_run_segment():
    """落盘在 run_segment 里而不是在最后汇总时：汇总写的话，容器被回收/终端被
    关掉就一个字都没留下——而"跑了三小时之后断了"恰恰是这件事要解决的场景。"""
    body = _extract_fn("run_segment")
    assert "record_segment" in body
    assert body.index("record_segment") < body.index("return 0")


def test_failure_summary_tells_you_the_resume_command():
    src = SCRIPT.read_text(encoding="utf-8")
    tail = src[src.index('echo "全部段退出码 0。"'):]
    assert "--resume" in tail
    assert "已经成功的段不重跑" in tail


def test_no_echo_line_contains_a_backtick():
    """反引号在双引号里是**命令替换**。R19 第一版在失败汇总那句里写了
    一对反引号想当引号用（想让「--status」看起来像代码），结果是脚本把自己
    递归跑一遍。注释里的反引号无所谓，`echo` 行里的不行。
    """
    bad = [ln.strip() for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
           if ln.strip().startswith("echo ") and "`" in ln]
    assert bad == [], f"这些 echo 行的反引号会被当成命令替换：{bad}"
