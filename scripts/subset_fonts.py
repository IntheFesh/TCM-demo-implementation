"""R24：把两套 Noto 字体子集化成**只含本页真正用到的字**，落进 web/vendor/fonts/。

## 为什么要这一步

现在 `web/app.css` 的 `@font-face` 指向 CDN（jsdelivr）。这有两个问题，
而第二个是这个项目的立身之本：

1. 中文全字重 woff2 每个约 3~9 MB，四个面（Serif 400/600、Sans 400/500）
   加起来是几十 MB 的首屏代价；
2. **断网就没有字体**。这个项目的整个演示形态（`LLM_MODE=replay`，零网络、
   零成本、每次一致）在没有字体的情况下会退化成系统默认字——而"宋体说中医的话、
   黑体说系统的话"是它的视觉主张，退化掉这一层，演示看起来就是另一个东西。

子集化之后每个面通常只有几十 KB（本仓库实际用到的汉字是**几百个**量级，
不是几万个），可以直接进版本控制，`@font-face` 改成相对路径，断网照常。

## 字表从哪里来（这是这个脚本唯一需要想清楚的事）

**从仓库里真实出现的文本收集，不手写字表**：

  · `web/index.html` / `web/app.css` / `web/app.js` / `web/ui/*.js` / `web/graph.js`
      —— 界面上的固定文案、按钮、标签、提示语
  · `data/standard/*.jsonl` 的证候名与症状词 —— 图谱浏览器和证素层要显示它们
  · `core/physicians.py` 的医家名与书名 —— 顶栏和列头
  · 三条示例主诉（`api/main.py` 的 EXAMPLE_COMPLAINTS）

**医案原文（cases.json / data/*/*.json）不进字表**：它们是十几万字的古籍文本，
把那些字全收进来等于没子集化。这一条是有代价的、必须写清楚的取舍——
**医案原文在页面上会掉到系统字体**。折中的做法是：医案原文那一块（`.ref-excerpt`
和证据面板）本来就用 `--font-classic` 的系统兜底那几档（Songti/SimSun），
它们在装了中文系统字体的机器上表现正常；真正需要子集字体的是界面文案。
如果以后要让医案原文也用上子集字体，正确做法是**把语料也纳进字表**（这个脚本
加一个 `--include-cases`），而不是偷偷把它们渲染成别的字体还不说。

## 这台机器上跑不了的部分

子集化本身要 `fonttools`（`pip install "fonttools[woff]" brotli`），
而且要先有原始 woff2 —— 沙盒的出站网络挡掉了 jsdelivr。所以：

    python -m scripts.subset_fonts --check-deps   # 退出码 0 = 依赖齐了
    python -m scripts.subset_fonts --charset-out /tmp/charset.txt   # 只出字表，零依赖
    python -m scripts.subset_fonts --download     # 上机：取原始字体（要网络）
    python -m scripts.subset_fonts                # 上机：真子集化

`--charset-out` 在沙盒里能跑完（纯文本处理），所以"字表对不对"这件事是可测的；
"子集化出来的字体对不对"只能上机验。
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "web" / "vendor" / "fonts"
RAW_DIR = OUT_DIR / "_raw"

# 四个字面 = CSS 里那四个 @font-face。**跟 app.css 一一对应**：少一个的后果是
# 某个字重在离线时掉回系统字体，而那种差异要盯着看才发现。
FACES = (
    ("noto-serif-sc", 400, "chinese-simplified-400-normal"),
    ("noto-serif-sc", 600, "chinese-simplified-600-normal"),
    ("noto-sans-sc", 400, "chinese-simplified-400-normal"),
    ("noto-sans-sc", 500, "chinese-simplified-500-normal"),
)
CDN_TEMPLATE = "https://cdn.jsdelivr.net/fontsource/fonts/{family}@5.3.0/{slug}.woff2"

# 字表的来源。**只收界面文案与短词表，不收医案原文**（理由见模块文档）。
TEXT_SOURCES = (
    "web/index.html",
    "web/app.css",
    "web/app.js",
    "web/graph.js",
    "web/ui/select.js",
    "core/physicians.py",
    "core/elements.py",
    "core/diseases.py",
    "api/main.py",
)
JSONL_SOURCES = ("data/standard/syndromes.jsonl",)

# 标点与数字：ASCII 全收（32~126），中文标点单独列——列出来而不是靠"凡是标点都收"，
# 因为 unicodedata 的标点类别里有几千个字符，全收会把子集化的意义抹掉。
CJK_PUNCT = "　、。！？；：（）【】《》「」“”‘’·—…－～％"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def collect_charset(root: Path = ROOT, include_cases: bool = False) -> set[str]:
    """本页真正会显示的字符集合。

    `include_cases=True` 时把 `cases.json` 的原文也收进来——**默认不收**，
    那是十几万字的古籍文本，收了等于没子集化（模块文档里写了这个取舍的代价）。
    """
    chars: set[str] = set(chr(c) for c in range(32, 127))
    chars |= set(CJK_PUNCT)
    for rel in TEXT_SOURCES:
        chars |= set(_read(root / rel))
    for rel in JSONL_SOURCES:
        chars |= set(_read(root / rel))
    if include_cases:
        chars |= set(_read(root / "cases.json"))
    # 控制字符不进字表：它们不是字形，只会让 --unicodes 参数变长
    return {c for c in chars if unicodedata.category(c)[0] != "C"}


def charset_arg(chars: set[str]) -> str:
    """fonttools `--unicodes=` 要的形状：逗号分隔的 U+XXXX。排序让它可复现。"""
    return ",".join(f"U+{ord(c):04X}" for c in sorted(chars))


def face_filename(family: str, weight: int) -> str:
    return f"{family}-{weight}-subset.woff2"


def missing_deps() -> list[str]:
    """缺哪些依赖。分开列是为了让提示能直接抄成一条 pip 命令。"""
    missing = []
    try:
        import fontTools  # noqa: F401
    except ImportError:
        missing.append("fonttools[woff]")
    try:
        import brotli  # noqa: F401
    except ImportError:
        missing.append("brotli")
    return missing


def download_raw(root: Path = ROOT) -> list[Path]:
    """把四个原始 woff2 取到 `_raw/`。沙盒的出站代理挡 jsdelivr，
    所以这一步注定只能上机跑——**失败时说清是哪一个 URL、什么错**，
    不是笼统地说"下载失败"。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for family, weight, slug in FACES:
        url = CDN_TEMPLATE.format(family=family, slug=slug)
        dest = RAW_DIR / f"{family}-{weight}.woff2"
        if dest.exists() and dest.stat().st_size > 0:
            print(f"已存在，跳过　{dest.name}（{dest.stat().st_size} 字节）")
            out.append(dest)
            continue
        print(f"取　{url}")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                dest.write_bytes(resp.read())
        except Exception as exc:  # noqa: BLE001
            print(f"× 取不到 {url}：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(f"→ {dest}（{dest.stat().st_size} 字节）")
        out.append(dest)
    return out


def subset_one(src: Path, dest: Path, chars: set[str]) -> bool:
    """调 fonttools 的 subset 入口。**不 shell 出去**：同一个解释器里调，
    缺依赖时报的是 ImportError 而不是"命令没找到"。"""
    from fontTools import subset

    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(src),
        f"--unicodes={charset_arg(chars)}",
        "--flavor=woff2",
        f"--output-file={dest}",
        # 布局特性只留中文排版真正会用到的那几个；全留会把体积撑回去
        "--layout-features=kern,liga,vert,vrt2",
        "--no-hinting",
        "--desubroutinize",
    ]
    subset.main(args)
    return dest.exists()


def css_face_block(family_css: str, weight: int, filename: str) -> str:
    """子集化之后 `@font-face` 该写成什么。打印出来让人贴进 app.css——
    **不自动改 CSS**：那个文件是设计令牌的唯一定义处，脚本偷偷改它
    会让"颜色/字体在哪定义"这件事多出一个不可见的作者。"""
    return (f'@font-face {{\n'
            f'  font-family: "{family_css}";\n'
            f'  font-style: normal; font-weight: {weight}; font-display: swap;\n'
            f'  src: url("vendor/fonts/{filename}") format("woff2");\n'
            f'}}')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-deps", action="store_true", help="只查依赖，退出码 0 = 齐了")
    ap.add_argument("--charset-out", type=Path, help="只导出字表（零依赖，沙盒可跑）")
    ap.add_argument("--download", action="store_true", help="取原始 woff2（要网络）")
    ap.add_argument("--include-cases", action="store_true",
                    help="把 cases.json 的医案原文也纳进字表（体积会大很多，见模块文档）")
    args = ap.parse_args(argv)

    chars = collect_charset(include_cases=args.include_cases)
    print(f"字表：{len(chars)} 个字符"
          f"（医案原文{'已' if args.include_cases else '未'}纳入）")

    if args.charset_out:
        args.charset_out.write_text("".join(sorted(chars)), encoding="utf-8")
        print(f"→ {args.charset_out}")
        return 0

    if args.check_deps:
        missing = missing_deps()
        if missing:
            print("缺依赖：pip install " + " ".join(f'"{m}"' for m in missing), file=sys.stderr)
            return 1
        print("依赖齐了")
        return 0

    if args.download:
        got = download_raw()
        return 0 if len(got) == len(FACES) else 1

    missing = missing_deps()
    if missing:
        print("缺依赖：pip install " + " ".join(f'"{m}"' for m in missing), file=sys.stderr)
        print("（沙盒里跑不了，这是上机项：先 --download 取原始字体，再跑这条）",
              file=sys.stderr)
        return 1

    ok = 0
    for family, weight, _slug in FACES:
        src = RAW_DIR / f"{family}-{weight}.woff2"
        if not src.exists():
            print(f"× 缺原始字体 {src}——先跑 --download", file=sys.stderr)
            continue
        dest = OUT_DIR / face_filename(family, weight)
        if subset_one(src, dest, chars):
            ratio = dest.stat().st_size / max(1, src.stat().st_size)
            print(f"→ {dest.name}　{src.stat().st_size} → {dest.stat().st_size} 字节"
                  f"（{ratio:.1%}）")
            ok += 1

    if ok != len(FACES):
        print(f"只成功 {ok}/{len(FACES)} 个字面，不改 CSS", file=sys.stderr)
        return 1

    print("\n把 web/app.css 顶部那四个 @font-face 换成下面这四段（脚本不自动改）：\n")
    for family, weight, _slug in FACES:
        css_family = "Noto Serif SC" if "serif" in family else "Noto Sans SC"
        print(css_face_block(css_family, weight, face_filename(family, weight)))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
