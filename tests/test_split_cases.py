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
