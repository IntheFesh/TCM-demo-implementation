"""录制 / 回放后端：把一次真实的推理**录下来**，之后零调用、零延迟、断网可用地
**放出来**。

**为什么需要它**：作品提交、网站上线之后，一次问诊约 20 次调用（三位医家 +
ReAct），访问者点几下就能烧光余额；`.env` 里的 key 跟着部署走容易泄露；API
抖动或断网时演示直接挂。本地模型（`VLLMBackend`）是长期方案，但它要 GPU 常驻，
竞赛演示现场未必有。录制回放是这个场景下唯一同时满足"零成本 + 断网可用 +
每次结果完全一致"的方案。

## 索引：`(schema.__name__, sha256(送进模型的 system 内容))`

同样的输入必然回放同样的输出——这跟 manifest 的可复现性诉求是同一件事。

**刻意不用"第 N 次调用"当索引**：链路顺序一变（加一步工具调用、ReAct 多走一步、
两位医家的并发顺序换了）整份 fixture 就全体错位，而且错位之后每一条都对不上，
调试时看不出是哪一条的问题。按内容索引则是"哪条没录到就报哪条"。

哈希的是 `messages[0]["content"]`，也就是 `generate()` 拼好 schema_hint 之后
真正送进模型的那段 system 文本，不是调用方传进来的原始 system。理由：schema 改了
就该算不同的输入（输出形状本来就不一样），用拼好的文本当键这件事自动成立。

**user 消息没有进索引，因为全项目 11 个 `generate()` 调用点的 `user` 都是空串**
（实测 grep 过）。空的东西进哈希只是噪音。但"以后有人加了非空 user"这件事必须
爆出来而不是静默回放错的结果——`_require_empty_user` 就是这道闸门。

## 未命中：抛 `LLMError`，绝不静默退化

不返回空、不退回真实 API。静默退化会让"这是录制的结果"这个声称变成假的，而且
录漏了的情况必须暴露——演示前跑一次 `scripts/verify_replay.py` 就能发现，
而不是演示到一半突然开始发真实请求（那还会悄悄烧钱）。

错误消息里给足定位信息：schema 名、prompt 前 100 字、sha 前 12 位、fixture 目录、
已录了几条、**以及录制时的环境变量跟现在的差异**——实测最常见的未命中原因不是
"忘了录"，而是 `USE_REACT` / `RETRIEVER_MODE` 这类环境变量跟录制时不一样，
导致链路走了另一条路、prompt 跟着不同。差异直接打出来比让人自己想快得多。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from pydantic import BaseModel

from core.llm import LLMBackend, LLMError, get_backend

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# 这些环境变量会改变链路或 prompt 内容，录制时记下来、回放时比对。
# **不是"所有环境变量"**：LLM_API_KEY 之类既不影响 prompt 又是敏感信息，
# 录进 fixture 等于把 key 写进将要提交的文件里（这一轮的动机之一正是不想让 key
# 跟着部署走）。这张表只收"会让同一条主诉走出不同 prompt"的那几个。
REPLAY_RELEVANT_ENV = (
    "USE_REACT",             # 开 ReAct 走 s3_react 模板，prompt 完全不同
    "FAST_MODE",             # 追问 0 轮 + ReAct 2 步 + 关残差，少掉整段调用
    "EVAL_MODE",             # 安全否决不中止，被拦的主诉会继续往下跑
    "RETRIEVER_MODE",        # 检索模式变了 -> 参考医案变了 -> S3 的 prompt 变了
    "LOW_DISCRIMINATION_CUTOFF",  # 低区分度截断，同样改参考医案
    "LLM_MAX_TOKENS",        # 不改 prompt，但改输出是否被截断
)


def fixture_key(schema_name: str, system: str) -> str:
    """fixture 的索引：`{schema}_{sha12}`，同时就是文件名（去掉 .json）。

    **全项目唯一一处算这个键的地方**——录制和回放必须用同一个函数，各写一遍
    的话"录下来的能不能放出来"就取决于两段代码有没有漂移（CLAUDE.md「同一概念
    只能有一处实现」）。
    """
    return f"{schema_name}_{hashlib.sha256(system.encode('utf-8')).hexdigest()[:12]}"


def _require_empty_user(messages: list[dict]) -> None:
    """索引不含 user 消息，所以 user 非空时必须报错而不是照旧查表。

    今天全项目 11 个调用点的 user 都是空串；哪天有人加了非空 user，两条只有
    user 不同的调用会撞到同一个键——回放会拿出**另一条**调用的输出，而且看起来
    完全正常。这种错比未命中难查得多，所以宁可在这里炸掉。
    """
    user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    if user:
        raise LLMError(
            "录制/回放的索引只覆盖 system 消息，但这次调用带了非空 user 内容"
            f"（前 80 字：{user[:80]!r}）。两条只有 user 不同的调用会撞到同一个"
            "fixture 键，回放会拿出另一条的输出而且看起来正常。要支持这种调用，"
            "先把 user 一起纳入 core/llm_replay.fixture_key 的哈希（录制的 fixture "
            "也要跟着重录），不要绕过这条检查。"
        )


def _system_of(messages: list[dict]) -> str:
    """取送进模型的 system 文本。generate() 固定把它放在 messages[0]，
    重试时只往后面追加，所以第 0 条始终是这次调用的 system。"""
    return messages[0].get("content", "") if messages else ""


class FixtureMeta(BaseModel):
    """一份 fixture 的元信息。**用 pydantic 承接不裸用 dict**（CLAUDE.md），
    因为它是要写进文件、之后被另一个进程读回来的跨边界数据——少一个字段、
    类型错一个，回放时才发现就太晚了。

    这些字段全部是"这段输出是怎么来的"的凭据：manifest 的 replayed_from 和前端
    那行"演示模式"小字都从这里取。**不许有默认值兜底的字段**（除了确实可能
    取不到的 git_commit / cases_sha256）：默认值会让一份缺元信息的 fixture
    看起来是完整的。
    """

    schema_name: str
    key: str
    model: str
    backend: str
    prompt_version: str
    recorded_at: str
    cases_sha256: str | None = None
    git_commit: str | None = None
    # 录制时这几个环境变量的值（未设置 = None）。回放未命中时拿它跟当前环境
    # 比对，直接告诉人"录制时 USE_REACT=1，现在是 0"。
    env: dict[str, str | None] = {}
    # prompt 前 100 字，只为了让人打开 fixture 目录时能认出这是哪一步的调用。
    # **不是索引**：索引是 key 里的 sha12，改这一段不影响命中。
    system_preview: str = ""


class Fixture(BaseModel):
    """一条 fixture：索引 + 原始输出 + 元信息。

    存的是**模型返回的原始文本**（`output`），不是解析好的对象：回放时让它照原样
    再走一遍 `generate()` 的 strip_code_fence + pydantic 校验，这样"录下来的输出
    到底合不合法"这件事在回放时跟实时调用是同一条判断。存解析后的对象会绕过
    这段校验，录到一份非法输出时反而看不出来。
    """

    meta: FixtureMeta
    system: str
    output: str


def git_commit() -> str | None:
    """当前 HEAD 短 hash；拿不到返回 None，不编一个值。跟
    eval/sdt/runlog.py 里那个是同一件事，但不共用：那个是 SDT 台账的一部分
    （跟着 eval/ 走），这个是 fixture 元信息的一部分（跟着 core/ 走），
    eval/ 不该被 core/ 依赖。两处都是 3 行 subprocess，没有判断逻辑可以漂移。"""
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10,
                             cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def current_env() -> dict[str, str | None]:
    """REPLAY_RELEVANT_ENV 这几个变量当前的值。未设置记 None 而不是 ""：
    "没设置"和"设成空串"在 os.environ.get 之后就分不开了，而前者是默认行为、
    后者是有人显式设成了空——回放未命中时这个区别要看得见。"""
    return {name: os.environ.get(name) for name in REPLAY_RELEVANT_ENV}


# 录制附带的基线文件（场景名 -> 这次跑出来的 canonical 文本），
# `scripts/verify_replay.py` 拿它验"逐字节一致"。
#
# **刻意是 .json 不是 .jsonl**：`.gitignore` 里 `*.jsonl` 是整体忽略，只给
# `data/standard/` 和 `eval/sdt/test_run_log.jsonl` 开了例外——这个坑已经咬过
# 四次，不再去添第五条例外，换个扩展名从根上避开。而且这个文件本来就是整体
# 读写的（不是追加日志），JSON 对象比 JSONL 更贴它的用法。
BASELINE_FILENAME = "_baseline.json"


def canonical_outcome(outcome: dict) -> str:
    """把一次 `consult()` 的结论压成一段可逐字节比对的文本。

    **录制和验证必须用同一个函数**：这是"什么算同一个输出"的定义，各写一份的话
    "逐字节一致"这句话就取决于两段代码有没有漂移（CLAUDE.md「同一概念只能有
    一处实现」）。放在 core/ 而不是某个脚本里，就是为了让两个脚本都只能 import
    它、不能自己再写一个。

    只取**模型产出**的部分（S1/S2 + 每位医家的 S3 全字段 + 分歧），刻意**不含**
    manifest：`elapsed_ms` 每次都不同，算进去这个比对就永远不一致、这个验证也
    就废了。`sort_keys=True` 让 dict 迭代顺序不参与比较——要验的是"模型给出的
    结论一样"，不是"字典顺序一样"。
    """
    payload = {
        "s1": outcome["s1"].model_dump() if outcome.get("s1") else None,
        "s2": outcome["s2"].model_dump() if outcome.get("s2") else None,
        "rejected": outcome.get("rejected"),
        "reject_reason": outcome.get("reject_reason"),
        "insufficient": outcome.get("insufficient"),
        "results": [
            {"physician": r["physician"], "s3": r["s3"].model_dump()}
            for r in outcome.get("results") or []
        ],
        "divergence": outcome.get("divergence"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def fixtures_dir() -> Path:
    """fixture 目录，REPLAY_FIXTURES_DIR 可覆盖，默认仓库根的 fixtures/。"""
    raw = os.environ.get("REPLAY_FIXTURES_DIR")
    return Path(raw) if raw else DEFAULT_FIXTURES_DIR


class RecordingBackend(LLMBackend):
    """包在真实后端外面：正常调用，同时把 (schema, system, 原始输出) 录进
    fixture 目录。

    做成**包装**而不是在 OpenAICompatBackend 里加开关：录制是临时动作
    （录一次用很久），不该让在线服务那条路上多一个"要不要录"的分支——那个分支
    忘了关就会在生产里往磁盘写文件。包装还让它对后端无差别：录 DeepSeek 的、
    录本地 vLLM 的、录 claude_cli 的，代码完全一样。

    `_complete` 之外的所有方法都转发给内层后端：manifest 里记的必须是**真实**
    跑出这些输出的那个模型，不是 "recording"。
    """

    def __init__(self, inner: LLMBackend | None = None, out_dir: Path | None = None):
        self.inner = inner or get_backend()
        self.out_dir = Path(out_dir) if out_dir else fixtures_dir()
        self.n_written = 0
        self.keys_written: list[str] = []

    def model_name(self) -> str:
        return self.inner.model_name()

    def backend_id(self) -> str:
        return self.inner.backend_id()

    def comparability_warning(self) -> str | None:
        return self.inner.comparability_warning()

    def lora_for(self, physician: str | None) -> str | None:
        return self.inner.lora_for(physician)

    def lora_dir(self) -> str | None:
        return self.inner.lora_dir()

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs) -> str:
        raw = self.inner._complete(messages, temperature, max_tokens=max_tokens,
                                   schema=schema, physician=physician, **kwargs)
        if schema is None:
            # generate() 一定会传 schema；没有 schema 的调用（如果将来有）
            # 索引不成立，如实跳过不录，不编一个键。
            return raw
        _require_empty_user(messages)
        self.write(schema.__name__, _system_of(messages), raw)
        return raw

    def write(self, schema_name: str, system: str, output: str) -> Path:
        """写一条 fixture。一条一个文件，文件名就是索引——**刻意不用一个大
        JSON**：一个大文件在 git 里每录一次就整体改写，diff 看不出新录了哪几条；
        分文件时新增/覆盖哪一条一目了然。

        同一个键重复写是**覆盖**，这是对的：`generate()` 校验失败会带着回灌消息
        重试，messages[0] 不变所以键不变，最后一次写进去的是最终通过校验的那份
        输出。先写进去的那份非法输出被覆盖掉，正是想要的。
        """
        from core.chain import cases_sha256

        key = fixture_key(schema_name, system)
        fixture = Fixture(
            meta=FixtureMeta(
                schema_name=schema_name, key=key,
                model=self.inner.model_name(), backend=self.inner.backend_id(),
                prompt_version="v1",
                recorded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                cases_sha256=cases_sha256(), git_commit=git_commit(),
                env=current_env(), system_preview=system[:100],
            ),
            system=system, output=output,
        )
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{key}.json"
        path.write_text(fixture.model_dump_json(indent=2), encoding="utf-8")
        if key not in self.keys_written:
            self.keys_written.append(key)
        self.n_written += 1
        return path


class ReplayBackend(LLMBackend):
    """从预先录好的 fixture 回放，**不发任何网络请求**。

        LLM_MODE=replay
        REPLAY_FIXTURES_DIR=fixtures        # 可选，默认仓库根的 fixtures/

    fixture 惰性加载（第一次 `_complete` 时才扫目录）：模块 import 时读文件违反
    "加载大文件的对象一律惰性初始化"，而且 api/main.py 起服务时就会去读一个
    可能还不存在的目录。
    """

    def __init__(self, fixtures_path: Path | None = None):
        self._dir = Path(fixtures_path) if fixtures_path else fixtures_dir()
        self._fixtures: dict[str, Fixture] | None = None
        self._bad_files: list[str] = []
        self.n_hits = 0

    # ---------- 加载 ----------

    @property
    def fixtures(self) -> dict[str, Fixture]:
        if self._fixtures is None:
            self._fixtures = self._load()
        return self._fixtures

    def _load(self) -> dict[str, Fixture]:
        out: dict[str, Fixture] = {}
        if not self._dir.exists():
            return out
        for path in sorted(self._dir.glob("*.json")):
            if path.name.startswith("_"):
                # 下划线开头的是录制附带的元文件（BASELINE_FILENAME），不是
                # fixture。不跳过的话它每次都会进 _bad_files，让每条未命中消息
                # 都带一行假的"加载失败"噪音。
                continue
            try:
                fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001 - 坏文件要记下来、在未命中消息里报出
                # 不静默跳过：一份因为元信息缺字段而加载失败的 fixture，症状是
                # "明明录过却未命中"，不说清楚就会去重录一遍（白花钱）。
                self._bad_files.append(f"{path.name}: {type(e).__name__}: {e}")
                continue
            # 键以文件里的 meta.key 为准、不以文件名为准，但两者不一致要报出来：
            # 文件名是给人看的，meta.key 是给程序用的，不一致说明有人手改过文件名
            if fixture.meta.key != path.stem:
                self._bad_files.append(
                    f"{path.name}: 文件名与 meta.key({fixture.meta.key}) 不一致，"
                    "按 meta.key 装载（文件名给人看，meta.key 给程序用）")
            out[fixture.meta.key] = fixture
        return out

    # ---------- manifest 用的三件事 ----------

    def model_name(self) -> str:
        """**不伪装成录制时那个模型**：如实说这是回放，录制时用的是哪个模型写在
        括号里。manifest 的 model 字段被误读成"这次是这个模型现场跑的"就是伪造
        实验条件——跟基类 model_name() 那段注释同一条纪律。"""
        recorded = self._recorded_model()
        return f"replay({recorded})" if recorded else "replay(未知，fixtures 为空)"

    def backend_id(self) -> str:
        return "replay"

    def comparability_warning(self) -> str | None:
        info = self.replay_info()
        if not info:
            return ("后端：replay，但 fixtures 目录里没有可用的录制内容——"
                    "这一次不会产出任何结果，不是「分数不可比」而是「跑不起来」。")
        return (f"本次结果来自 {info['recorded_at']} 录制的推理"
                f"（模型 {info['model']}），非实时调用。")

    def replay_info(self) -> dict | None:
        """录制来源。多份 fixture 理论上可以来自不同批次的录制，所以 recorded_at
        取**最早**那一份、并报出批次数：报最新的会让"这份 demo 有多旧"看起来比
        实际新，而 n_batches > 1 本身是个提醒（混了几次录制，模型可能不一样）。"""
        fixtures = self.fixtures
        if not fixtures:
            return None
        metas = [f.meta for f in fixtures.values()]
        recorded_ats = sorted(m.recorded_at for m in metas)
        models = sorted({m.model for m in metas})
        commits = sorted({m.git_commit for m in metas if m.git_commit})
        return {
            "recorded_at": recorded_ats[0],
            "recorded_at_latest": recorded_ats[-1],
            # 混了多个模型时全列出来，不挑一个当代表
            "model": models[0] if len(models) == 1 else "、".join(models),
            "git_commit": commits[0] if len(commits) == 1 else "、".join(commits) or None,
            "n_fixtures": len(fixtures),
            "n_batches": len(set(recorded_ats)),
            "fixtures_dir": str(self._dir),
        }

    def _recorded_model(self) -> str | None:
        info = self.replay_info()
        return info["model"] if info else None

    # ---------- 回放 ----------

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs) -> str:
        _require_empty_user(messages)
        system = _system_of(messages)
        schema_name = schema.__name__ if schema is not None else "<no-schema>"
        key = fixture_key(schema_name, system)
        fixture = self.fixtures.get(key)
        if fixture is None:
            raise LLMError(self._miss_message(key, schema_name, system))
        self.n_hits += 1
        return fixture.output

    def _miss_message(self, key: str, schema_name: str, system: str) -> str:
        """未命中的错误消息。**定位信息要够到不用再跑一遍就能判断怎么办。**"""
        lines = [
            f"回放未命中：fixtures 里没有 key={key}。",
            f"  schema：{schema_name}",
            f"  system 前 100 字：{system[:100]!r}",
            f"  system 的 sha256 前 12 位：{key.rsplit('_', 1)[-1]}",
            f"  fixtures 目录：{self._dir}（已装载 {len(self.fixtures)} 条）",
        ]
        env_diff = self._env_diff()
        if env_diff:
            lines.append("  **当前环境里有录制时从未出现过的取值，这是最常见的未命中"
                         "原因**（环境变量改了 -> 链路走了另一条路 -> prompt 跟着变）：")
            lines.extend(f"    {name}：录制过的取值 {was!r}，现在是 {now!r}"
                         for name, was, now in env_diff)
        if self._bad_files:
            lines.append("  有 fixture 文件加载失败（也会表现成未命中）：")
            lines.extend(f"    {b}" for b in self._bad_files)
        if not self.fixtures:
            lines.append("  fixtures 目录是空的或不存在——先跑 "
                         "`python -m scripts.record_fixtures` 录一次。")
        lines.append("  **不会退回真实 API，也不会返回空结果**：静默退化会让"
                     "「这是录制的结果」这个声称变成假的，而且录漏了必须暴露出来。"
                     "补录这一条：把触发它的那条主诉加进 scripts/record_fixtures.py "
                     "的清单，用录制时同样的环境变量重跑。")
        return "\n".join(lines)

    def _env_diff(self) -> list[tuple[str, list[str | None], str | None]]:
        """录制时的环境 vs 现在。只报"现在这个值在录制时**从来没出现过**"的变量。

        **不能拿任意一份 fixture 的 env 当基准**：录制清单刻意把开 ReAct 和不开
        ReAct 各录一遍（两者 prompt 不同、fixture 不共用），所以同一个目录里
        `USE_REACT` 天然既有 "1" 又有 "0"，拿其中一份当基准会对另一半误报
        "环境变了"，把人引到错的方向。正确的判据是集合归属：当前值落在录制过的
        取值集合里就不算差异。
        """
        fixtures = self.fixtures
        if not fixtures:
            return []
        now = current_env()
        diffs: list[tuple[str, list[str | None], str | None]] = []
        for name in REPLAY_RELEVANT_ENV:
            recorded_values = {f.meta.env.get(name) for f in fixtures.values()}
            if now.get(name) not in recorded_values:
                diffs.append((name, sorted(recorded_values, key=lambda v: (v is not None, v)),
                              now.get(name)))
        return diffs
