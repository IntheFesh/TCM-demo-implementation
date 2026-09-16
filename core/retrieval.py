"""医案检索层：给定患者症状，在某位医家的医案库里检索最相关的参考医案。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"

# 稠密检索用的编码模型。**只此一处写它的名字**：缓存的命中判据里带着模型名，
# 两处写就会出现"换了模型但缓存 key 没跟着换"——那是静默拿旧模型的向量去比新模型
# 编出来的查询，结果全错而且不报任何错。
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
# 语料向量的磁盘缓存目录。**进 .gitignore**：它是从版本控制里的输入（cases.json +
# 模型名）能完全重算出来的产物，而且指纹里带了输入的 sha，重算一定得到同一份。
_DEFAULT_EMBEDDING_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


def embedding_cache_dir() -> Path | None:
    """这次用哪个缓存目录；None = 这次不用缓存。

    两个环境变量：`EMBEDDING_CACHE=0` 整体关掉，`EMBEDDING_CACHE_DIR` 换个地方。
    **关得掉这件事本身是必需的**：`tests/conftest.py` 把它关掉，否则
    ① 测试会往仓库的 data/cache/ 里写东西；
    ② 更糟——上一条测试写的缓存会让下一条测试跳过编码，于是"编码过程中并发读取"
    这类**专门测编码时序**的用例永远等不到 encode 被调用。这个坑是加缓存当天就
    踩到的（tests/test_concurrency_init.py 立刻变红）。
    """
    if (os.environ.get("EMBEDDING_CACHE") or "").strip() in ("0", "false", "off"):
        return None
    override = os.environ.get("EMBEDDING_CACHE_DIR")
    return Path(override) if override else _DEFAULT_EMBEDDING_CACHE_DIR


def _sentence_transformers_version() -> str | None:
    """写进缓存元信息，方便日后排查"换了库版本向量就对不上了"这类事。
    取不到就是 None，不编。"""
    try:
        import sentence_transformers

        return getattr(sentence_transformers, "__version__", None)
    except ImportError:
        return None

# 检索相似度下限。实测正常匹配在 0.85-0.90，低于 0.70 基本是"库里没有相关案子"，
# 此时给空列表比塞三条不相关的更诚实。这是两位医家、839 条医案时校准的固定值，
# 现在只用于 eval/run_eval.py 的 E8（检索模式对比）——那里比较的是"同一批
# (query,physician) 在不同 RETRIEVER_MODE 下 top-1 是否够可信"，McNemar 检验
# 要求两个待比较分支套用同一条判据，阈值本身不能随分支变化；跟下面
# adaptive_min_score() 回答的不是同一个问题（那个是"这一次真实检索该筛掉
# 哪些结果"，是个逐请求的操作性阈值，不是评测用的固定判据）——两处都叫
# "阈值"但职责不同，故意没有合并成一个（CLAUDE.md「同一概念的匹配逻辑只能
# 有一处实现」的例外条款：不是同一个问题）。真实检索路径（_search_cases、
# ReAct 的 search_cases 工具）已经从这个固定值改成 adaptive_min_score()，
# 见 P0-7。
MIN_RETRIEVAL_SCORE = 0.70

# P0-7：三位医家加入张锡纯（87 条、大量方论体）后，MIN_RETRIEVAL_SCORE=0.70
# 这个写死的阈值对医案少/覆盖窄的医家经常把结果全部卡空（真实出现过"叶天士
# 未检索到相关医案"）。ADAPTIVE_MIN_SCORE_FLOOR 是新阈值的下限——不管这次
# 探测出来的 p25 多低，都不能低于这个值，否则退化成"什么都收"。
ADAPTIVE_MIN_SCORE_FLOOR = 0.60
ADAPTIVE_MIN_SCORE_PROBE_K = 10
ADAPTIVE_MIN_SCORE_PERCENTILE = 25

# P0-6：编码进检索向量/BM25 语料的原文摘录长度。跟 core.chain.
# CASE_EXCERPT_TRUNCATE_CHARS（300，喂给 S3 prompt 给模型读）是两个不同长度
# ——这里只是给向量化定位语义用，不需要那么多上下文，更短的窗口足够。
CASE_TO_TEXT_EXCERPT_CHARS = 200


class Retriever(ABC):
    @abstractmethod
    def search(
        self, query: str, physician: str, k: int = 3, min_score: float = 0.0
    ) -> list[tuple[CaseRecord, float]]:
        """返回 [(医案, 相似度), ...]，按相似度降序，只在给定医家的医案里排序。
        min_score 以下的结果不返回——宁可给空列表让 S3 知道"没有相关医案"，
        也不要塞三条不相关的案子进 prompt 逼模型模仿。"""
        raise NotImplementedError

    def case_count(self, physician: str) -> int | None:
        """这位医家在索引里有多少条医案；None = 这个实现不知道（测试里的假
        检索器）。给 search_cases 空返回时报"已查 N 条"用——那句话要让模型
        知道"确实查了、不是没查"，N 未知就老实说未知，不编一个数。"""
        return None


def _case_to_text(case: CaseRecord) -> str | None:
    """把结构化医案编码成一段紧凑文本用于向量化。返回 None 表示这条医案没有
    任何可编码的内容（无 symptoms 也无 raw_excerpt），调用方应该跳过、不建
    索引，不能编码成占位文字硬凑一条。

    P0-6 根因：约一半复诊记录的原文只是"加减了什么药"（如"加∶葶苈(一钱五分)
    二帖"），没有症状描述——旧实现对这类医案编码出"（无记录症状）。舌未记，
    脉未记"，所有这类医案的向量几乎完全相同，对任何主诉的相似度也一样，
    是纯噪声：检索命中它们时 own/swapped 两侧看到的都是同一批噪声，
    change_rate 当然还是低（这跟 P0-1~P0-4 修的"喂给模型看的内容"是两回事
    ——那边修的是 prompt 展示，这里修的是检索本身用什么信号排序，检索排序
    错了，展示层修得再好也没用：检索到的本来就是不该被检索到的医案）。

    有 symptoms 时：症状+舌+脉 之后拼上 raw_excerpt 前
    CASE_TO_TEXT_EXCERPT_CHARS 字，比原来多一路真实原文信号，不是替换掉
    结构化字段（结构化字段是人工整理过的，仍有信息量，两者互补）。
    symptoms 为空但有 raw_excerpt 时：只用 raw_excerpt，不再拼"（无记录
    症状）"这种占位文字——那正是让向量塌缩到同一点的元凶。

    复诊段要跟初诊区分开：复诊原文常只写"服药后如何"，症状极简
    （"肿胀未除""汗至眉上"），如果和初诊平等编码，检索时会大量命中
    这些碎片——实测吴鞠通的 top-3 曾全是第 6/11 诊，一条初诊都没有。
    把治疗反应拼进文本，让复诊段的向量落在"疗效描述"而不是"主诉"附近。
    这条框架只在有 symptoms 时套用——symptoms 为空的复诊段直接退化成
    "只用 raw_excerpt"分支，不再叠加"现症：（无记录症状）"这种空壳。
    """
    excerpt = (case.raw_excerpt or "")[:CASE_TO_TEXT_EXCERPT_CHARS]
    if not case.symptoms:
        return excerpt or None

    tongue = case.tongue or "未记"
    pulse = case.pulse or "未记"
    base = f"{'；'.join(case.symptoms)}。舌{tongue}，脉{pulse}"
    if case.visit_index and case.visit_index > 0:
        resp = case.response_to_prior or "（未记疗效）"
        base = f"复诊第{case.visit_index + 1}诊。前次治疗后：{resp}。现症：{base}"
    if excerpt:
        base = f"{base}。原文：{excerpt}"
    return base


def _percentile(sorted_values: list[float], pct: float) -> float:
    """线性插值百分位数（等价于 numpy.percentile 默认的 'linear' 方法）。
    这里的输入规模是个位数到十位数（top-10 探测），不为这么小的数据引入
    numpy 依赖——DenseRetriever._load 里的 numpy 用法是给几百条医案批量
    编码用的，跟这里"给十个数排个百分位"是两件事，不共用。"""
    if not sorted_values:
        raise ValueError("空列表没有百分位数")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def adaptive_min_score(
    retriever: Retriever, query: str, physician: str, **search_kwargs
) -> float:
    """P0-7：给这次 (query, physician) 检索算一个自适应的 min_score，取代写死
    的 MIN_RETRIEVAL_SCORE。全项目只在这里实现一次——core.chain._search_cases
    和 core.tools.search_cases 工具都调这一个函数，不各自算一遍（CLAUDE.md
    「同一概念的匹配逻辑只能有一处实现」）。

    取该医家这次查询的原始相似度 top-10（min_score=0.0 探测，不过滤），
    算这 top-10 的第 25 百分位，跟 ADAPTIVE_MIN_SCORE_FLOOR 取较大值——
    医案少/覆盖窄的医家，top-10 本身就够不上多高的相似度，p25 自然走低，
    阈值随之放宽；医案多、覆盖广的医家能挤出更高的 top-10，阈值保持接近
    原来的 0.70，不放松。

    P0-13 改动 2：min_score 参数现在只有 dense 模式的 _dense_ranking 会真的
    拿它去过滤——bm25/graph 模式的排名函数本来就不接受这个参数；hybrid
    模式的融合准入在 P0-13 之前也用它过滤，但那正是 P0-13 要修的根因
    （单路 dense 阈值否决 BM25 的发现），改完之后 hybrid 分支完全不再读
    这个参数（core/retrieval_hybrid.py::search() 的 hybrid 分支文档字符串）。
    也就是说，这次探测——完整跑一遍 retriever.search()——只对**显式请求
    dense 模式**的调用才有意义；default（不传 mode，解析成 hybrid，见
    HybridRetriever.search() 的 `mode or os.environ.get(..., "hybrid")`）
    和显式 bm25/graph/hybrid 都用不上探测出来的值，跑这次探测纯粹是浪费
    ——每次检索多一次完整的 search() 调用。

    所以只在 search_kwargs 里显式带着 mode="dense" 时才真的探测；其余情况
    （包括 mode 缺省）直接返回 0.0，不发起探测调用。这里只看
    search_kwargs.get("mode")，不去读 RETRIEVER_MODE 环境变量自己复算一遍
    "缺省到底解析成什么"——那份解析逻辑只在 HybridRetriever.search() 一处
    实现，这里重新猜一遍就是把同一个判断散到了第二处（CLAUDE.md「同一
    概念的匹配逻辑只能有一处实现」）。代价：如果调用方没有显式传 mode、
    但进程恰好设了 RETRIEVER_MODE=dense（README「检索模式不是环境变量」
    那条已经点名这是已知的风险操作），这次探测会被跳过、真正的 dense
    检索会退到 ADAPTIVE_MIN_SCORE_FLOOR 而不是探测出的阈值——比悄悄猜错
    一个阈值更安全的选择是不猜、给一个已知安全的下限，不是当作没有代价。

    只通过 Retriever.search() 这一个抽象接口方法探测，不要求具体实现额外
    暴露内部排名方法——测试用的 FakeRetriever、将来别的检索后端都不用为
    这个功能改 search() 之外的任何东西。**search_kwargs 原样转给探测调用，
    跟 core/chain.py::_search_cases「不传 mode 就不加这个关键字」的规则
    保持一致——这里只是转发，不新增判断。
    """
    if search_kwargs.get("mode") != "dense":
        return 0.0
    probe = retriever.search(
        query, physician, k=ADAPTIVE_MIN_SCORE_PROBE_K, min_score=0.0, **search_kwargs
    )
    if not probe:
        return ADAPTIVE_MIN_SCORE_FLOOR
    scores = sorted(score for _, score in probe)
    p25 = _percentile(scores, ADAPTIVE_MIN_SCORE_PERCENTILE)
    return max(ADAPTIVE_MIN_SCORE_FLOOR, p25)


# P0-12：top-1 和 top-k 的原始相似度差小于这个值时，认为这几个候选之间没有
# 真实区分度——实测 15 个 (主诉,医家) 样本里 8 个 top1-top3 分差 < 0.03，
# 0.78 和 0.80 的差别在 bge-small-zh 的噪声范围内，塞三条等于随机三选三，
# 还占掉 prompt 空间稀释信号。
LOW_DISCRIMINATION_MARGIN = 0.03

# P0-13 改动 3：这个判据的前提是"展示分就是排序用的那个分"——分差小意味着
# 排序本身分不出高下。dense/graph 模式满足这个前提：两者的展示分（真实
# 余弦相似度/真实 Jaccard 相似度）本身就是排序依据。hybrid 模式不满足：
# 展示分是 dense 相似度，排序依据是 RRF 融合分，P0-13 之后两者彻底脱钩
# （改动 1）——一条 BM25 精确命中、dense 分很低的医案可能排在很靠前的
# 融合名次，跟另一条同样是低 dense 分的医案比较展示分差值，比出来的不是
# "这次排序有没有区分度"，是两条医案凑巧撞上了接近的 dense 分，跟它们
# 真实的融合名次距离无关——套用这条判据会误砍掉 K3a 恰好要保留的那类
# 结果。bm25 模式的展示分是无界原始分（10-30 常态），跟按 dense 余弦
# 相似度校准的 0.03 这个量纲根本不是一个刻度，同样不成立（跟 adaptive_
# min_score/MIN_RETRIEVAL_SCORE 只对 dense/graph 这类 [0,1] 有界相似度
# 有意义是同一条已有的设计原则，见 core/retrieval_hybrid.py 模块文档
# 字符串——这里不是新发明一条规则，是把同一条规则应用到 P0-12 这个新场景）。
_LOW_DISCRIMINATION_VALID_MODES = {"dense", "graph"}


def low_discrimination_cutoff_enabled() -> bool:
    """默认开。全项目唯一的 LOW_DISCRIMINATION_CUTOFF 判定实现——跟
    USE_REACT（core/react.py::react_enabled）/FAST_MODE
    （core/followup.py::fast_mode_enabled）同一个约定：住在它主要治理的
    模块（检索层）里，显式参数优先、未指定才读环境变量。这条不确定是不是
    净收益（三条弱相关 vs 一条弱相关，谁更好没有先验答案），做成开关是为了
    让 E3 能跑两遍对比，不是先验认定它必然更好。"""
    return os.environ.get("LOW_DISCRIMINATION_CUTOFF", "1").lower() in ("1", "true", "yes")


def apply_low_discrimination_cutoff(
    hits: list[tuple[CaseRecord, float]], mode: str | None = None,
    enabled: bool | None = None,
) -> tuple[list[tuple[CaseRecord, float]], bool]:
    """P0-12：hits 已经按相似度降序排好（search() 的返回契约）。top-1 和
    最后一条的原始相似度差 < LOW_DISCRIMINATION_MARGIN 时只保留 top-1。
    返回 (处理后的 hits, 是否触发了这次截断)——第二个值原样交给调用方
    （core.chain.run_physician）带进结果字典，E3 报告要能看到这个标记
    出现的比例，不是只改行为不留痕迹。

    mode 是这次检索实际请求的模式（跟传给 search() 的 mode 关键字一致，
    缺省/None 表示会解析成 hybrid，见 HybridRetriever.search()）。P0-13
    改动 3：这个判据只对 _LOW_DISCRIMINATION_VALID_MODES 里的模式成立——
    hybrid（含缺省）和 bm25 下，展示分跟真正的排序依据脱钩或量纲不同，
    比较展示分的差值判断"有没有区分度"是在比较错误的维度，不能套用。
    这个范围检查独立于 enabled 开关：LOW_DISCRIMINATION_CUTOFF=1 只表示
    "在成立的场景下要不要用它"，不能反过来在不成立的场景下也用。

    enabled=None 时读 low_discrimination_cutoff_enabled()（显式参数优先，
    未指定才读环境变量——跟 core.chain._search_cases 的 retriever_mode/
    refs_mode 同一条规则，不在这个函数内部直接读环境变量，方便测试注入）。
    """
    if enabled is None:
        enabled = low_discrimination_cutoff_enabled()
    if not enabled or mode not in _LOW_DISCRIMINATION_VALID_MODES or len(hits) < 2:
        return hits, False
    if hits[0][1] - hits[-1][1] < LOW_DISCRIMINATION_MARGIN:
        return hits[:1], True
    return hits, False


def load_cases(cases_path: Path = CASES_PATH) -> tuple[list[CaseRecord], list[str], list[str]]:
    """读 cases.json → (可检索的医案, 各自的检索文本, 被跳过的 case_id)。

    R21 从 `DenseRetriever.__init__` 抽出来：`FullContextRetriever`（不需要
    embedding 模型）和 `core/context_prefix.py`（拼缓存前缀）都要读同一份医案，
    而"哪些医案算可用"这个判断只能有一处——三处各读一遍 cases.json 的话，
    full_context 下喂给模型的医案集合可能跟 top3 下检索的那一套不一样，
    两组数就不可比了（CLAUDE.md 第 31 条）。

    P0-6：既无 symptoms 也无 raw_excerpt 的医案编码不出任何有意义的文本
    （_case_to_text 返回 None），不能勉强塞进索引——那样它在向量空间里
    落点是未定义的（旧实现会落在"（无记录症状）"这个人工占位点，跟其他
    同样没内容的医案完全重合，变成检索噪声）。
    """
    if not cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {cases_path}。请先运行 `python -m offline.extract_cases` "
            "生成 cases.json，再使用检索功能。"
        )
    with cases_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    cases: list[CaseRecord] = []
    texts: list[str] = []
    skipped: list[str] = []
    for r in raw:
        case = CaseRecord.model_validate(r)
        text = _case_to_text(case)
        if text is None:
            skipped.append(case.case_id)
            continue
        cases.append(case)
        texts.append(text)
    if skipped:
        print(
            f"[retrieval] {len(skipped)} 条医案既无 symptoms "
            f"也无 raw_excerpt，编码不出任何文本，已从检索索引跳过（不影响 cases.json "
            f"本身，只影响能否被检索到）：{skipped[:10]}"
            + ("……" if len(skipped) > 10 else ""),
            file=sys.stderr,
        )
    return cases, texts, skipped


class DenseRetriever(Retriever):
    """用 sentence-transformers 的 bge-small-zh-v1.5 做稠密检索。惰性加载模型，
    禁止在模块顶层实例化（加载模型是重操作，不该在 import 时就发生）。"""

    def __init__(self, cases_path: Path = CASES_PATH):
        self._cases, self._case_texts, self.skipped_no_content_ids = load_cases(cases_path)

        self._model = None  # 惰性加载，避免 import 阶段就下载/加载模型
        self._embeddings = None  # 惰性编码，随 _model 一起初始化
        # 这次的向量是从磁盘缓存读的还是现编的。给 scripts/bench_startup.py 和
        # 测试用——"缓存有没有生效"必须能从外面看出来，不能只靠看耗时猜。
        self.embeddings_from_cache: bool | None = None
        # **每个实例一把锁，不是类属性。** 这把锁保护的是 self._model /
        # self._embeddings——per-instance 的状态，锁的作用域就该是 per-instance。
        # 原来它是类属性（进程级），后果是**一个实例的编码会挡住另一个实例的编码**：
        # api/main.py 的预热线程在单例上编码 941 条语料要几十秒，这段时间里任何
        # 别的 DenseRetriever 实例调 _ensure_encoded 都得干等——AutoDL 上
        # tests/test_concurrency_init.py 那两条就是这么挂的（预热线程起自
        # tests/test_api_stream.py 的 live server，收集序在前）。
        # 挡住"同一个实例被并发编码两次"靠的是这把锁；挡住"建出两个实例"靠的是
        # core.retrieval 模块级的 _retriever_lock，两件事分开。
        self._encode_lock = threading.Lock()

    def case_count(self, physician: str) -> int | None:
        return sum(1 for c in self._cases if c.physician == physician)

    def _ensure_encoded(self) -> None:
        # 冷启动时两个并发请求会各加载一份模型（几百 MB × 2）。
        # 双重检查：锁外先判一次避免每次请求都抢锁，锁内再判一次防竞态。
        #
        # 锁外快路径看的必须是 _embeddings 而不是 _model：_load() 里加载模型
        # 只要一两秒，随后给 839 条医案编码要十几秒——这段时间里 _model 已经
        # 非 None 而 _embeddings 还是 None。审计里实测过这个交错：线程 A 持锁
        # 在编码，线程 B 锁外看到 _model 就位直接放行，走到
        # `self._embeddings[i] @ query_vec` 时拿到的是 None，TypeError。
        # 所以 _load() 最后才发布 _embeddings，这里只认它。
        if self._embeddings is not None:
            return
        with self._encode_lock:
            if self._embeddings is not None:
                return
            self._load()

    def _cache_key(self) -> str:
        """这份语料 + 这个模型的指纹。三样东西全等才算命中：模型名、条数、
        **被编码的那些文本本身的 sha256**。

        指纹取的是 `self._case_texts` 而不是 `cases.json` 的整文件 sha：编码的输入
        就是这些文本，`cases.json` 里改一个跟检索无关的字段（比如补一条 raw）会让
        文件 sha 变、而编码结果一个字节都不会变——那样每次都白编码一遍，缓存等于没有。
        反过来，只要编码输入变了这个指纹必然变，不存在"该失效却没失效"。
        """
        digest = hashlib.sha256("\x00".join(self._case_texts).encode("utf-8")).hexdigest()
        return f"{EMBEDDING_MODEL.replace('/', '_')}_{digest[:12]}_{len(self._case_texts)}"

    def _read_cache(self, key: str):
        """读缓存，读不出来一律返回 None 并说明原因——**静默回退比慢更糟**：
        人以为缓存生效了，实际每次都在重新编码，而"启动还是 135 秒"这件事
        没有任何输出能解释。"""
        cache_dir = embedding_cache_dir()
        if cache_dir is None:
            return None
        path = cache_dir / f"embeddings_{key}.npy"
        if not path.exists():
            return None
        try:
            import numpy as np

            cached = np.load(path)
        except Exception as e:  # noqa: BLE001 - 缓存坏了就重编码，不能让它把服务拖垮
            print(f"[retrieval] embedding 缓存 {path.name} 读不出来（{e}），这次重新编码",
                  file=sys.stderr)
            return None
        if cached.shape[0] != len(self._case_texts):
            # 指纹里已经带了条数，走到这里说明文件被人换过。宁可重编码也不用它：
            # 行数对不上意味着 self._cases 和向量的下标错位，那是**静默给错结果**。
            print(f"[retrieval] embedding 缓存 {path.name} 的行数 {cached.shape[0]} 跟语料"
                  f"{len(self._case_texts)} 条对不上，这次重新编码", file=sys.stderr)
            return None
        return cached

    def _write_cache(self, key: str, embeddings) -> None:
        cache_dir = embedding_cache_dir()
        if cache_dir is None:
            return
        try:
            import numpy as np

            cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(cache_dir / f"embeddings_{key}.npy", embeddings)
            (cache_dir / f"embeddings_{key}.json").write_text(json.dumps({
                "model": EMBEDDING_MODEL,
                "n_cases": len(self._case_texts),
                "key": key,
                "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sentence_transformers": _sentence_transformers_version(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            # 只读挂载、磁盘满：写不了缓存不影响这次跑，下次照样重编码。
            print(f"[retrieval] embedding 缓存写不出去（{e}），不影响本次检索", file=sys.stderr)

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        # 先在局部变量里把两样东西都建好，再按 _model → _embeddings 的顺序发布。
        # _ensure_encoded 的锁外快路径只认 _embeddings，它最后一个写入，
        # 别的线程看到它非 None 时 _model 一定已经就位。
        #
        # **模型不管命不命中缓存都要加载**：缓存省掉的是"给全部语料编码"这一段
        # （941 条，实测占启动耗时的大头），而 search() 每次都要给**查询**编码，
        # 那一步没有模型不行。省的是 O(语料) 不是 O(1)。
        model = SentenceTransformer(EMBEDDING_MODEL)
        key = self._cache_key()
        embeddings = self._read_cache(key)
        if embeddings is None:
            # 复用 __init__ 里已经算好、过滤过的 self._case_texts，不重新调用
            # _case_to_text——两处算出不一致的文本会让 self._cases 和 self._embeddings
            # 的下标错位（P0-6 引入的过滤逻辑只在 __init__ 跑一次，这里必须认它）。
            embeddings = model.encode(
                self._case_texts, normalize_embeddings=True, convert_to_numpy=True
            )
            self._write_cache(key, embeddings)
            self.embeddings_from_cache = False
        else:
            self.embeddings_from_cache = True
        self._model = model
        self._embeddings = embeddings

    # 初诊在排序时的加成。复诊段症状简短、内容是疗效描述，
    # 作为"该医家如何辨证"的参考价值低于初诊，但不完全排除——
    # 长序列里的中段复诊有时正好记录了证型转变。
    INITIAL_VISIT_BOOST = 1.08

    # P0-11：herbs 为空的医案不剔除（剔除会损失约 21% 语料——实测张锡纯这类
    # 医案少的医家会因此只剩 74 条），排序时打折。它对"这位医家怎么开方"的
    # 参考价值低（S3 要学的是用药风格，没有方就没有风格可学），但症状描述
    # 仍有检索价值（可能带出同一病人有方的其他诊次）。跟 INITIAL_VISIT_BOOST
    # 同一个机制：只调整排序用的分，不改真实展示的相似度。
    NO_HERBS_PENALTY = 0.9

    def _rank_score(self, case: CaseRecord, raw_score: float) -> float:
        """把原始相似度转成排序用的加权分。DenseRetriever.search() 和
        HybridRetriever._dense_ranking() 共用这一份公式（CLAUDE.md「同一
        概念的匹配逻辑只能有一处实现」）——两处各写一份的话，日后再加一个
        调整项（这次是 NO_HERBS_PENALTY）必然会漏改一处。"""
        vi = case.visit_index or 0
        score = raw_score * (self.INITIAL_VISIT_BOOST if vi == 0 else 1.0)
        if not case.herbs:
            score *= self.NO_HERBS_PENALTY
        return score

    def search(
        self, query: str, physician: str, k: int = 3, min_score: float = 0.0
    ) -> list[tuple[CaseRecord, float]]:
        self._ensure_encoded()

        idxs = [i for i, c in enumerate(self._cases) if c.physician == physician]
        if not idxs:
            return []

        query_vec = self._model.encode(
            [query], normalize_embeddings=True, convert_to_numpy=True
        )[0]

        scored = []
        for i in idxs:
            raw_score = float(self._embeddings[i] @ query_vec)
            if raw_score < min_score:
                continue
            rank_score = self._rank_score(self._cases[i], raw_score)
            # 排序用加权分，返回给上层的仍是真实相似度，不要把加成混进展示值
            scored.append((i, rank_score, raw_score))

        scored.sort(key=lambda x: -x[1])
        return [(self._cases[i], raw) for i, _rank, raw in scored[:k]]


_retriever_singleton: Retriever | None = None
# 建单例要读整份 cases.json 并逐条 model_validate，几百毫秒；没有这把锁时两个
# 冷启动并发请求会各建一份 HybridRetriever，输的那份被在途请求引用着、之后又
# 各自加载一份几百 MB 的模型。**挡住这件事的只有这把锁**——DenseRetriever 的
# _encode_lock 是每实例一把（保护的是那个实例的 _model/_embeddings），
# 两个实例之间本来就不该互相排队。
_retriever_lock = threading.Lock()


#: `full_context` 模式下 refs 的分数。**1.0 不是 0.0 也不是 None**：
#: 0.0 会被读成"完全不相关"，而语义恰恰相反——这一条确实是这位医家的医案，
#: 只是没有算相似度这件事；None 会在前端变成 null。
FULL_CONTEXT_SCORE = 1.0


def full_context_hits(cases: list[CaseRecord], physician: str) -> list[tuple[CaseRecord, float]]:
    """该医家全部医案，按 case_id 排序、分数恒 FULL_CONTEXT_SCORE。

    **只此一处实现**：`FullContextRetriever.search` 和 `HybridRetriever.search`
    的 full_context 分支都调它。两处各写一遍排序的话，两条路给出的医案顺序
    可能不同，而顺序不同 = 缓存前缀 byte 不同 = 缓存永远不命中。
    """
    mine = sorted((c for c in cases if c.physician == physician), key=lambda c: c.case_id)
    return [(c, FULL_CONTEXT_SCORE) for c in mine]


class FullContextRetriever(Retriever):
    """R21：**不检索**——把该医家的全部医案原样交出去，交给缓存前缀。

    为什么它也是一个 Retriever 而不是绕过检索层：`run_physician` 只认识
    `search(query, physician, k, ...)` 这一个入口，E3/E4/E8 消融脚本也都从
    这个入口换 mode。做成一个 mode 之后，`full_context` 跟 `top3` 系是同一条
    代码路径上的两个取值，两组数才可比；绕过检索层的话它就成了另一条路径，
    "换了检索模式"这个对照里混进了"换了代码路径"这个额外变量。

    `score` 恒为 **1.0** 而不是 0.0 或 None：
      - 下游 `refs` 要把它当相似度展示，None 会在前端变成 `null`；
      - 0.0 会被读成"完全不相关"，而这里的语义恰恰相反——**这一条确实是
        这位医家的医案**，只是没有算相似度这件事。
    分数在这个模式下不参与排序也不参与筛选，顺序是 case_id 排序（确定性，
    缓存前缀的前提）。`k` 和 `min_score` 一律忽略，并在第一次被传非默认值时
    说一句——静默忽略会让调用方以为自己限了条数。
    """

    #: 兼容别名，真值在模块级 FULL_CONTEXT_SCORE（HybridRetriever 的
    #: full_context 分支也要用同一个数，写两处就会漂）。
    FIXED_SCORE = FULL_CONTEXT_SCORE

    def __init__(self, cases_path: Path = CASES_PATH):
        self._cases, _, self.skipped_no_content_ids = load_cases(cases_path)
        self._warned_about_k = False

    def case_count(self, physician: str) -> int | None:
        return sum(1 for c in self._cases if c.physician == physician)

    def search(
        self,
        query: str,
        physician: str,
        k: int = 3,
        min_score: float = 0.0,
        **kwargs,
    ) -> list[tuple[CaseRecord, float]]:
        if (k != 3 or min_score != 0.0) and not self._warned_about_k:
            self._warned_about_k = True
            print(f"[retrieval] full_context 模式忽略 k={k} / min_score={min_score}"
                  "：这个模式的全部意义就是不筛。", file=sys.stderr)
        return full_context_hits(self._cases, physician)


def get_retriever() -> Retriever:
    """惰性单例。返回 HybridRetriever（DenseRetriever 的超集，见
    core/retrieval_hybrid.py）——这样 RETRIEVER_MODE 环境变量能在每次
    search() 调用时动态生效，不需要按 mode 分别建单例（V1 的 E8 消融
    只改环境变量重跑，不重启进程）。放在这里而不是模块顶层 import，
    是为了避免 core.retrieval 反向依赖 core.retrieval_hybrid 造成循环
    import（retrieval_hybrid 依赖 retrieval，不能反过来在模块顶层互相依赖）。
    """
    global _retriever_singleton
    if _retriever_singleton is None:
        with _retriever_lock:
            if _retriever_singleton is None:
                from core.retrieval_hybrid import HybridRetriever

                _retriever_singleton = HybridRetriever()
    return _retriever_singleton


if __name__ == "__main__":
    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    first_query = queries_path.read_text(encoding="utf-8").splitlines()[0].strip()
    print(f"查询：{first_query}\n")

    retriever = get_retriever()
    for physician in ["ye_tianshi", "wu_jutong"]:
        print(f"=== {physician} top-3 ===")
        for case, score in retriever.search(first_query, physician, k=3):
            symptoms_summary = "；".join(case.symptoms[:4])
            print(f"  {case.case_id}  相似度={score:.3f}  症状摘要：{symptoms_summary}")
        print()
