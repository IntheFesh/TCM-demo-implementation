"""方剂学三元组入口。抽取逻辑全在 offline/extract_reference_triples.py
（跟 extract_materia_medica.py 共用一份引擎），这里只把 kind 固定成 formulary。

君臣佐使的教材记载从这里来：S3 输出里 HerbItem.role 由模型标注，有了教材的
标准答案才有对照（对照属于评测，不在抽取脚本里做）。

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
