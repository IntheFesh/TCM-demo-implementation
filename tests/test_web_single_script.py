"""钉住 web/index.html 里字面 <script> 标签只有一个。

12 个前端测试都用 `html.split("<script>")[-1].split("</script>")[0]` 抽那份
上线脚本。这个取法在只有一个字面 <script> 标签时是对的（第 7 行的 cytoscape
是 `<script src="...">`，不含字面 `<script>`），但再加一个内联脚本块就会**静默
抽错**——测试照样全绿，只是测的不是上线的那份代码。

这条测试把那个静默失败变成一条红的断言。要加第二个内联脚本块时，先把那 12 处
抽取逻辑改掉，再改这里，不要反过来。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_index_html_has_exactly_one_inline_script_block():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert html.count("<script>") == 1, (
        "web/index.html 里字面 <script> 标签不止一个。"
        "12 个前端测试用 split('<script>')[-1] 抽上线脚本，多一个标签会静默抽错。"
    )
    # 闭合标签有两个是正常的：第 7 行 cytoscape 那个 <script src=...></script>
    # 自带一个。它不含字面 <script>，所以不影响上面那条抽取。
    assert html.count("</script>") == 2


def test_inline_script_is_the_one_the_frontend_tests_extract():
    """抽出来的那段必须是真正的应用代码，不是别的什么。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    for marker in ("function computeLayout", "async function submitConsult", "function cardHtml"):
        assert marker in script, f"抽出来的脚本里没有 {marker}，说明抽错了块"
