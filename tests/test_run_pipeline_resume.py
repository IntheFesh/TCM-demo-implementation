"""`scripts/run_pipeline.sh` 的续跑。

`--from` 要人自己记住断在哪，而整条流水线要跑几个小时，中间会换终端、容器也可能
被回收——"我记得是步骤 5 挂的"本身就是故障点。`--resume` 读状态文件算起点。

这些测试真的跑那个 bash 脚本（`--status` / `--resume --dry-run` 都是零调用的路径），
不是读它的文本猜行为。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_pipeline.sh"
# 脚本里有哪几步的期望值。下面那条测试拿它跟脚本里现数出来的比，两者不一致就失败：
# 可续跑的步骤清单必须跟脚本里的步骤一一对应，不能放宽成包含关系，
# 否则发现不了误删一步。
STEPS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11")


def _run(args, state: Path | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    if state is not None:
        env["PIPELINE_STATE"] = str(state)
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _state(tmp_path: Path, rcs: dict[str, int]) -> Path:
    p = tmp_path / "state.tsv"
    p.write_text("".join(f"{n}\t{rc}\t2026-09-15T01:00:00+00:00\n"
                         for n, rc in sorted(rcs.items())), encoding="utf-8")
    return p


def test_the_script_steps_match_the_expected_list():
    """步骤号从脚本里现数，跟上面那个期望元组比——不是在两处各写一个步骤数。"""
    src = SCRIPT.read_text(encoding="utf-8")
    block = src[src.index("STEPS=("):src.index("\n)", src.index("STEPS=("))]
    nums = re.findall(r'^\s*"(\w+)\|', block, re.M)
    assert nums == list(STEPS), nums


def test_status_on_a_fresh_machine_says_nothing_ran_yet(tmp_path):
    r = _run(["--status"], state=tmp_path / "nope.tsv")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("未运行") == len(STEPS)
    assert "--resume 会从步骤 0 开始" in r.stdout


def test_status_reports_each_steps_last_exit_code(tmp_path):
    st = _state(tmp_path, {"0": 0, "1": 0, "2": 1})
    r = _run(["--status"], state=st)
    assert r.returncode == 0, r.stderr
    assert "0（成功）" in r.stdout
    assert "1（失败）" in r.stdout
    assert "--resume 会从步骤 2 开始" in r.stdout


def test_status_distinguishes_a_manual_abort_from_a_failure(tmp_path):
    """退出码 10 是人在确认点上按了 N，不是脚本挂了。两件事要分开显示——
    混成"失败"会让人去排查一个不存在的故障。"""
    st = _state(tmp_path, {"0": 0, "3": 10})
    r = _run(["--status"], state=st)
    assert "10（人工中止）" in r.stdout


def test_resume_starts_at_the_first_step_that_never_succeeded(tmp_path):
    """已经成功的步骤不重跑。`--resume --dry-run` 要能回答"会从哪一步开始"，
    所以起点算在 --dry-run 退出之前。"""
    st = _state(tmp_path, {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 1})
    r = _run(["--resume", "--dry-run"], state=st)
    assert r.returncode == 0, r.stderr
    assert "--resume：从步骤 5 开始" in r.stdout
    assert "它之前的步骤上次都是退出码 0，不重跑" in r.stdout
    assert "--dry-run：什么都没跑" in r.stdout


def test_resume_skips_over_a_gap_in_the_state_file(tmp_path):
    """中间一步没有记录也算没完成：状态里缺步骤 3，--resume 落在步骤 3。"""
    st = _state(tmp_path, {"0": 0, "1": 0, "2": 0, "4": 0, "5": 0})
    r = _run(["--resume", "--dry-run"], state=st)
    assert r.returncode == 0, r.stderr
    assert "--resume：从步骤 3 开始" in r.stdout


def test_resume_counts_a_never_run_step_as_unfinished(tmp_path):
    """没记录 ≠ 成功。只看"有没有失败记录"会把从没跑过的步骤直接跳过。"""
    st = _state(tmp_path, {"0": 0, "1": 0})
    r = _run(["--status"], state=st)
    assert "--resume 会从步骤 2 开始" in r.stdout


def test_resume_says_so_when_there_is_nothing_left(tmp_path):
    """每一步都成功过时 --resume 不跑任何步骤，并且要说出来——静默退出会被当成
    "又跑了一遍，都过了"。"""
    st = _state(tmp_path, {n: 0 for n in STEPS})
    r = _run(["--resume"], state=st)
    assert r.returncode == 0, r.stderr
    assert "没有需要续跑的步骤" in r.stdout
    assert "--only" in r.stdout


def test_resume_conflicts_with_from_and_only(tmp_path):
    """一个说"读状态文件"，一个说"我指定"。拒绝而不是挑一个生效——挑一个生效的话，
    传错的人不会知道自己被忽略了。"""
    for args in (["--resume", "--from", "4"], ["--resume", "--only", "3"]):
        r = _run(args, state=tmp_path / "s.tsv")
        assert r.returncode == 2, f"{args} 应该被拒绝：{r.stdout}"
        assert "不能一起传" in r.stderr


def test_record_overwrites_instead_of_appending(tmp_path):
    """同一步重跑要覆盖旧记录。追加的话 --resume 读到的是第一次那条（失败的），
    修好重跑成功也还会再跑一遍。"""
    st = tmp_path / "state.tsv"
    script = (
        f'PIPELINE_STATE="{st}"\n'
        f'STEPS=("2|2|n/a|本地模型|0|no|x")\n'
        + _extract_fn("record_step") + _extract_fn("step_state")
        + 'record_step 2 1\nrecord_step 2 0\n'
        'echo "lines=$(wc -l < "$PIPELINE_STATE")"\n'
        'echo "state=$(step_state 2)"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "lines=1" in r.stdout, r.stdout
    assert "state=0" in r.stdout, r.stdout


def _extract_fn(name: str) -> str:
    """从脚本里抠一个 bash 函数出来单独跑。不是重写一份——重写的话测的是副本，
    脚本改了测试照样绿。"""
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


def test_state_file_lands_in_a_gitignored_place():
    """它是这台机器这一次运行的状态，不是项目内容。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'PIPELINE_STATE="${PIPELINE_STATE:-out/pipeline_state.tsv}"' in src
    r = subprocess.run(["git", "check-ignore", "-q", "out/pipeline_state.tsv"],
                       cwd=ROOT, capture_output=True)
    assert r.returncode == 0, "状态文件会被提交进仓库"


def test_every_step_records_its_exit_code_in_run_step():
    """落盘在 run_step 里而不是在最后汇总时：汇总时才写的话，容器被回收、终端被
    关掉就一个字都没留下——而"跑了三小时之后断了"恰恰是续跑要应对的场景。"""
    body = _extract_fn("run_step")
    assert "record_step" in body
    assert body.index("record_step") < body.index("return 0")


def test_failure_summary_tells_you_the_resume_command():
    src = SCRIPT.read_text(encoding="utf-8")
    tail = src[src.index('echo "全部步骤退出码 0。"'):]
    assert "--resume" in tail
    assert "已经成功的步骤不重跑" in tail


def test_no_echo_line_contains_a_backtick():
    """反引号在双引号里是命令替换：写在 echo 里想当引号用，结果是执行一条命令
    （比如把脚本自己递归跑一遍）。注释里的反引号无所谓，echo 行里的不行。"""
    bad = [ln.strip() for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
           if ln.strip().startswith("echo ") and "`" in ln]
    assert bad == [], f"这些 echo 行的反引号会被当成命令替换：{bad}"
