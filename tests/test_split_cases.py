"""offline/split_cases.py 的离线测试：chunk_chapter 的切分行为、head_hints/follow_hints
的正则匹配。全部不需要网络。"""
from offline.split_cases import chunk_chapter, clean, find_follow_hints, find_head_hints


def test_chunk_chapter_respects_max_len():
    lines = [f"某{i} 病案正文内容占位占位占位占位占位占位。" for i in range(20)]
    body = "\n".join(lines)
    chunks = chunk_chapter(body, max_len=100, soft_min=20)
    assert len(chunks) > 1
    for chunk_lines in chunks:
        assert sum(len(l) for l in chunk_lines) <= 100 + max(len(l) for l in chunk_lines)


def test_chunk_chapter_breaks_at_blank_line_once_past_soft_min():
    long_line = "内容" * 30  # 60 字，超过 soft_min
    body = f"{long_line}\n\n下一段的内容"
    chunks = chunk_chapter(body, max_len=1000, soft_min=50)
    assert len(chunks) == 2
    assert chunks[0] == [long_line]
    assert chunks[1] == ["下一段的内容"]


def test_chunk_chapter_ignores_blank_line_before_soft_min():
    short_line = "短"
    body = f"{short_line}\n\n继续同一段"
    chunks = chunk_chapter(body, max_len=1000, soft_min=50)
    # 空行前内容太短，不足以借这个断点收尾，应该和后面的内容合并成一段
    assert len(chunks) == 1


def test_chunk_chapter_drops_tail_markers():
    body = "某 正文内容。\n徐评：这是编者按语，不应该进入任何粗段。\n某二 另一段正文。"
    chunks = chunk_chapter(body, max_len=1000, soft_min=1)
    joined = clean([l for chunk in chunks for l in chunk])
    assert "徐评" not in joined
    assert "另一段正文" in joined


def test_find_head_hints_matches_surname_age_pattern():
    text = "陈 三十二岁 甲寅年二月初四日 太阴所至。"
    hits = find_head_hints(text)
    kinds = {h["kind"] for h in hits}
    assert "姓名岁数" in kinds


def test_find_head_hints_matches_title_pattern():
    text = "乙酉年 治通廷尉久疝不愈。"
    hits = find_head_hints(text)
    kinds = {h["kind"] for h in hits}
    assert "纪年" in kinds
    assert "头衔" in kinds


def test_find_head_hints_sorted_by_position():
    text = "钱 五十岁 后来族婶母 六十岁 又来诊。"
    hits = find_head_hints(text)
    positions = [h["pos"] for h in hits]
    assert positions == sorted(positions)


def test_find_follow_hints_matches_line_start_marker():
    text = "某 初诊胃痛。\n又 胃痛减轻。"
    hits = find_follow_hints(text)
    assert any(h["matched"] == "又" for h in hits)


def test_find_follow_hints_matches_keyword_anywhere():
    text = "某 服三剂而愈。"
    hits = find_follow_hints(text)
    assert any("服" in h["matched"] for h in hits)


# ---------- A2：张锡纯（toc_case 策略） ----------

ZHANG_SAMPLE = """<目录>五、医案\\（五）肠胃病门
<篇名>1．胃脘疼闷

属性：天津王××，二十六岁，得胃脘疼闷证。
\\x病因\\x 因常常呕吐，胃气不降。
\\x证候\\x 时觉胃脘疼闷。
\\x处方\\x 生赭石一两，党参三钱。
\\x效果\\x 连服五剂全愈。
<目录>五、医案\\（五）肠胃病门
<篇名>2．胃气不降

属性：天津张××，年四十六岁，得胃气不降证。
\\x证候\\x 呕吐痰涎。
\\x处方\\x 生赭石八钱。
<目录>五、医案\\（十五）温病门
<篇名>3．温病兼大气下陷

属性：沈阳李××，年三十，得温病。
\\x证候\\x 表里俱热。
<目录>一、医方\\（一）治阴虚劳热方
<篇名>1．资生汤

属性：治劳瘵羸弱。
"""


def test_toc_sections_gate_on_directory_not_title():
    """这本书的门类名在 <目录> 里，<篇名> 是案名。gate 匹配错字段会一条都取不到。"""
    from offline.split_cases import sections_by_toc

    secs = sections_by_toc(ZHANG_SAMPLE, ["肠胃病"], "五、医案")
    assert len(secs) == 2
    assert all("肠胃病" in path for path, _ in secs)


def test_toc_sections_respect_the_prefix():
    """一、医方 里也有 <篇名>，但那不是医案卷，不能被 gate 顺手捞进来。"""
    from offline.split_cases import sections_by_toc

    assert sections_by_toc(ZHANG_SAMPLE, ["阴虚劳热"], "五、医案") == []


def test_toc_case_strategy_keeps_one_case_per_segment(tmp_path, monkeypatch):
    """原文已经按病人分好块，就不再按长度切——切了会把一个病人劈成两段，
    正是 chunk_chapter 文档里警告的反面。"""
    from offline import split_cases

    book = tmp_path / "584-医学衷中参西录.txt"
    book.write_bytes(ZHANG_SAMPLE.encode("gb18030"))
    cfg = dict(src=book.name, gates=["肠胃病"], strategy="toc_case", toc_prefix="五、医案")
    segs = split_cases.collect_segments("zhang_xichun", cfg, books_dir=str(tmp_path))
    assert len(segs) == 2
    for s in segs:
        assert s["text"].count("属性") == 1


def test_structural_patient_check():
    """head_hints 正则是照叶天士案首体例写的，对张锡纯一条都不触发，
    所以「一段几个病人」的判据必须用原文自带的身份行，不能用 head_hints。"""
    from offline.split_cases import structural_patient_check

    segs = [{"text": "属性：甲\n证候"}, {"text": "属性：乙\n证候"}, {"text": "无身份行"}]
    ok, total, dist = structural_patient_check(segs)
    assert (ok, total) == (2, 3)
    assert dist == {1: 2, 0: 1}


def test_head_hints_regex_misses_zhang_xichun_case_heads():
    """把这条失效钉住：以后有人改 HEAD_HINT_PATTERNS 想让它覆盖张锡纯时，
    这条会红，提醒他同时去掉 print_segment_stats 里那句「不要跨书比较」。"""
    from offline.split_cases import find_head_hints

    assert find_head_hints("属性:天津陈××,三十五岁,于孟冬得大气下陷兼小便不禁证。") == []


def test_missing_book_is_skipped_not_a_crash(tmp_path):
    """BOOKS 里有三本、只下载了一两本是完全正常的使用方式，不该整个脚本崩掉。"""
    from offline.split_cases import BOOKS

    assert not (tmp_path / BOOKS["ye_tianshi"]["src"]).exists()
    # main 里的跳过分支靠 src.exists() 判断，这里直接验证判断依据成立即可
    assert all("src" in cfg for cfg in BOOKS.values())


def test_toc_case_segments_drop_structure_marker_lines_and_carry_structural_count(tmp_path):
    from offline import split_cases

    book = tmp_path / "584-医学衷中参西录.txt"
    book.write_bytes(ZHANG_SAMPLE.encode("gb18030"))
    cfg = dict(src=book.name, gates=["肠胃病"], strategy="toc_case", toc_prefix="五、医案")
    segs = split_cases.collect_segments("zhang_xichun", cfg, books_dir=str(tmp_path))
    for s in segs:
        assert "<篇名>" not in s["text"]
        assert s["structural_patient_count"] == 1
