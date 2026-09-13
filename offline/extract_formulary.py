"""总纲 2.3（M13）：方剂学三元组入口。抽取逻辑全在
offline/extract_reference_triples.py（跟 extract_materia_medica.py 共用一份
引擎），这里只把 kind 固定成 formulary。

君臣佐使从这里来——M1 的 HerbItem.role 现在靠模型标，准确率未知，有了
教材的标准答案才有对照（对照本身是后续评测的事，不在抽取脚本里做）。

用法：
    python -m offline.extract_formulary --input books/方剂学.txt --source modern --book 方剂学
    python -m offline.extract_formulary --input books/方剂学.txt --source modern --book 方剂学 --dry-run
"""
from __future__ import annotations

from offline import extract_reference_triples as engine


def main(argv: list[str] | None = None) -> None:
    engine.run(argv, kind_name="formulary")


if __name__ == "__main__":
    main()
