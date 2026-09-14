#!/usr/bin/env bash
# 阶段二（药理层）的六个数据源下载。**下载完必须校验字节数**，对不上就退出。
#
# 用法：
#   bash scripts/fetch_pharmacology_sources.sh            # 下到 books/
#   bash scripts/fetch_pharmacology_sources.sh --dry-run  # 只打印会下什么、不下
#   bash scripts/fetch_pharmacology_sources.sh --dest /path/to/books
#   BOOKS_DIR=/root/autodl-tmp/books bash scripts/fetch_pharmacology_sources.sh
#
# 退出码：0 = 六个源全部就位且字节数对得上；1 = 有源下载失败或字节数不符；
#        2 = 环境不具备（没有 curl）。
#
# ============================================================
# 为什么要校验字节数：上一次的教训
# ============================================================
# 367（《临证指南医案》）那本曾经下到一个少了约 10% 的残缺文件，而**切粗段的
# 统计数字碰巧没变**（案数、门类分布都在合理范围），差一点带着残缺原文往下走。
# 从那以后的规矩是：下载完先比字节数，不对就退出，不看"统计像不像话"。
# 每个源的 expected_bytes 由第一次成功下载后 `wc -c` 得到，填进下面的表；
# 还没实测过的填 0 = **跳过校验但大声警告**（不是静默放过，见 verify_bytes）。
#
# ============================================================
# AutoDL 的代理规律
# ============================================================
# AutoDL 上 GitHub（含 raw.githubusercontent.com）要走学术加速代理，其余域名
# 走代理反而更慢或直接不通。这个脚本的六个源**全部来自 GitHub**，所以统一开
# 代理；留 --no-proxy 给"本机直连 GitHub 更快"的环境（比如本地开发机）。
#
# 代理开关用 AutoDL 官方给的那两个脚本；没有那两个脚本时（非 AutoDL 环境）
# 静默跳过、照常直连——不能因为"没有代理脚本"就拒绝下载。
#
# ============================================================
# 编码：古籍 GB18030，教材 UTF-8
# ============================================================
# 两类源的编码不同，抽取引擎只吃 UTF-8（extract_reference_triples.py 的
# --input 写死了 encoding="utf-8"）：**只有两本古籍要转码**，四本教材已经是
# UTF-8 的 markdown、原样保留。
# **不要"统一按 UTF-8 读一遍看会不会报错"来猜编码**：GB18030 的中文字节序列
# 有相当概率能被 UTF-8 解码成乱码而不抛异常，猜出来的结果是静默的乱码语料。
# 每个源的编码写死在表里，来源是实测（古籍仓库 xiaopangxia/TCM-Ancient-Books
# 全库 GB18030，见 data/SOURCES.md 第 1 节；教材仓库 PanckooAI/TCM_Datasets
# 是 UTF-8）。
set -uo pipefail

DEST="${BOOKS_DIR:-books}"
DRY_RUN=0
USE_PROXY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-proxy) USE_PROXY=0 ;;
    --dest) shift; DEST="$1" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# ---- 源表 ----
# 每行：本地文件名 | URL | 编码 | source 标签 | expected_bytes | 用途
#
# source 标签直接对应 extract_*.py 的 --source 参数：古籍与现代必须分开抽、
# 分开存（古籍说"细辛，味辛温"，药典说"辛、温，归心肺肾经，1~3g"，术语和精度
# 都不同，混在一起会重演 λ1 那个"证型 0/116 对不上"的教训）。
#
# **URL 和 expected_bytes 都已由项目方在有网络的机器上实测校正**（第一版六条
# 全错：教材路径是 十四五教材/xxx.md 不是 books/xxx.txt、古籍编号是三位补零）。
# 详情与教训见 data/SOURCES.md 第 42 条。
#
# expected_bytes 是**上游原始文件**的字节数（古籍是 GB18030 原文，不是转成
# UTF-8 之后的大小）：这道检查要回答的是"下载完整了没有"，而 GB18030→UTF-8
# 中文会从 2 字节变 3 字节，记转换后的大小等于每次都要多记一个派生量。
# 所以校验在转码**之前**做。
#
# 上游仓库是活的（会修订错字、补内容），字节数变了不一定是下载出错。所以对不上
# 时的处置是"停下来让人看"，不是"自动接受新值"——自动接受等于把这道闸门关掉。
# 确认上游真的改了之后，手工更新表里的数字并在 data/SOURCES.md 记一笔。
#
# 每行第 6 列是**推荐的切块模式**（真实抽取时传给 --chunk-by）：教材是
# markdown 层级标题排版，必须用 heading；古籍是空行分段的纯文本，用 blank-line。
# 这一列不是装饰——切错了会打开一个防幻觉的洞，见
# offline/extract_reference_triples.split_blocks 的文档字符串。
SOURCES=(
  # 现代教材（十四五规划教材）。**是 .md 不是 .txt**，路径是 十四五教材/xxx.md
  # 不是 books/xxx.txt——第一版六条 URL 全猜错，由项目方在有网络的机器上实测
  # 校正（见 data/SOURCES.md 第 42 条）。已经是 UTF-8，不需要 iconv。
  # 同一批文件 offline/build_syndrome_textbook.py 已经在解析（中医内科学.md），
  # 排版是「# 第N节 病名 / # N.证型名」层级标题 + 逐行标注字段，所以切块用
  # heading 模式，见下面 chunk_by 那一列。
  "中药学.md|https://raw.githubusercontent.com/PanckooAI/TCM_Datasets/main/%E5%8D%81%E5%9B%9B%E4%BA%94%E6%95%99%E6%9D%90/%E4%B8%AD%E8%8D%AF%E5%AD%A6.md|utf-8|modern|1542065|heading|性味归经功效用量"
  "临床中药学.md|https://raw.githubusercontent.com/PanckooAI/TCM_Datasets/main/%E5%8D%81%E5%9B%9B%E4%BA%94%E6%95%99%E6%9D%90/%E4%B8%B4%E5%BA%8A%E4%B8%AD%E8%8D%AF%E5%AD%A6.md|utf-8|modern|951968|heading|临床用量、配伍"
  "中药炮制学.md|https://raw.githubusercontent.com/PanckooAI/TCM_Datasets/main/%E5%8D%81%E5%9B%9B%E4%BA%94%E6%95%99%E6%9D%90/%E4%B8%AD%E8%8D%AF%E7%82%AE%E5%88%B6%E5%AD%A6.md|utf-8|modern|1436809|heading|炮制方法与目的"
  "方剂学.md|https://raw.githubusercontent.com/PanckooAI/TCM_Datasets/main/%E5%8D%81%E5%9B%9B%E4%BA%94%E6%95%99%E6%9D%90/%E6%96%B9%E5%89%82%E5%AD%A6.md|utf-8|modern|1098496|heading|方剂组成、君臣佐使、加减法"
  # 古籍（GB18030，下载后转 UTF-8）。编号是**三位补零**（仓库里 704 个文件
  # 全是 NNN-书名.txt，从 000 开始）；第一版猜的 1- 和 9- 都是 404。
  # 本地文件名保留编号前缀，跟 README 3.1 里 367/361/584 那三本一个写法。
  "000-神农本草经.txt|https://raw.githubusercontent.com/xiaopangxia/TCM-Ancient-Books/master/000-%E7%A5%9E%E5%86%9C%E6%9C%AC%E8%8D%89%E7%BB%8F.txt|gb18030|classic|180115|blank-line|古籍本草"
  "018-本草备要.txt|https://raw.githubusercontent.com/xiaopangxia/TCM-Ancient-Books/master/018-%E6%9C%AC%E8%8D%89%E5%A4%87%E8%A6%81.txt|gb18030|classic|293521|blank-line|古籍本草"
)

command -v curl >/dev/null 2>&1 || { echo "没有 curl，装了再跑。" >&2; exit 2; }
# 古籍要从 GB18030 转 UTF-8，没有 iconv 就只能下到一个抽取引擎读不了的文件。
# 提前退出（2 = 环境不具备），不要下完六个源才发现转不了码。
command -v iconv >/dev/null 2>&1 || {
  echo "没有 iconv——两本古籍是 GB18030，需要它转成 UTF-8（抽取引擎只读 UTF-8）。" >&2
  echo "装了再跑；或者先只下四本教材（把 SOURCES 表里 gb18030 那两行注释掉）。" >&2
  exit 2
}

proxy_on() {
  [ "$USE_PROXY" = "1" ] || return 0
  # AutoDL 学术加速：官方脚本路径固定。不是 AutoDL 就没有这个文件，静默跳过。
  if [ -f /etc/network_turbo ]; then
    # shellcheck disable=SC1091
    source /etc/network_turbo && echo "已开启学术加速代理（/etc/network_turbo）"
  else
    echo "没有 /etc/network_turbo（不是 AutoDL 环境），直连 GitHub"
  fi
}

proxy_off() {
  [ "$USE_PROXY" = "1" ] || return 0
  # 六个源全在 GitHub，下完就关——AutoDL 上代理对非 GitHub 域名有害，
  # 留着它开会影响后面装包/跑评测时访问其它站点。
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 2>/dev/null || true
  echo "已关闭代理（六个源全在 GitHub，其余域名直连更快）"
}

# 校验字节数。expected=0 时不是"通过"，是"没有基准"——大声警告并把实测值打出来
# 让人填回表里。
verify_bytes() {
  local path="$1" expected="$2" name="$3"
  local actual
  actual=$(wc -c < "$path" | tr -d ' ')
  if [ "$expected" = "0" ]; then
    echo "  ⚠ $name：字节数 $actual（**表里没有基准值，本次跳过校验**）"
    echo "    → 把 $actual 填进 scripts/fetch_pharmacology_sources.sh 的 SOURCES 表，"
    echo "      下次跑才有这道闸门。367 那本少 10% 而统计碰巧没变的事就是这么发现的。"
    return 0
  fi
  if [ "$actual" != "$expected" ]; then
    echo "  ✗ $name：字节数 $actual ≠ 期望 $expected（差 $((actual - expected))）" >&2
    echo "    下载可能不完整。**不要跳过这个检查往下走**：367 那本少了约 10% 时," >&2
    echo "    切粗段的统计数字碰巧没变，差点带着残缺原文抽了几百次。" >&2
    echo "    如果确认是上游修订了内容，手工更新表里的数字并在 data/SOURCES.md 记一笔。" >&2
    return 1
  fi
  echo "  ✓ $name：字节数 $actual，与期望一致"
  return 0
}

mkdir -p "$DEST"
echo "目标目录：$DEST"
echo "源数量：${#SOURCES[@]}（4 本现代教材 UTF-8 + 2 本古籍 GB18030→转 UTF-8）"
echo

if [ "$DRY_RUN" = "1" ]; then
  printf '%-22s %-9s %-8s %-10s %-11s %s\n' 文件 编码 source 期望字节 切块模式 用途
  for row in "${SOURCES[@]}"; do
    IFS='|' read -r name url enc src bytes chunk purpose <<< "$row"
    printf '%-22s %-9s %-8s %-10s %-11s %s\n' "$name" "$enc" "$src" \
      "$([ "$bytes" = "0" ] && echo 未实测 || echo "$bytes")" "$chunk" "$purpose"
    echo "  $url"
  done
  echo
  echo "--dry-run：不下载。去掉这个参数真下。"
  echo
  echo "URL 和期望字节数已由项目方在有网络的机器上实测校正（见 data/SOURCES.md 第 42 条）。"
  echo "期望字节数是**上游原文**的大小：古籍落盘后（转成 UTF-8）会变大，那是转码不是缺失。"
  echo "切块模式那一列真实抽取时要传给 --chunk-by：教材必须 heading，"
  echo "按空行切会把一味药切成五六块、其中"用量"那块里根本没有药名。"
  exit 0
fi

proxy_on
trap proxy_off EXIT

failed=()
for row in "${SOURCES[@]}"; do
  IFS='|' read -r name url enc src bytes chunk purpose <<< "$row"
  out="$DEST/$name"
  echo "[$src] $name（$purpose）"
  tmp="$out.download"
  # -f：HTTP 错误码不要写进文件（否则会得到一个装着 404 页面的"语料"）
  # -L：跟随重定向；--retry：网络抖动重试
  if ! curl -fL --retry 3 --retry-delay 2 -o "$tmp" "$url"; then
    echo "  ✗ 下载失败：$url" >&2
    rm -f "$tmp"
    failed+=("$name（下载失败）")
    continue
  fi
  # **先校验字节数，再转码**：expected_bytes 记的是上游原始文件的大小
  # （古籍是 GB18030 原文），这道检查要回答的是"下载完整了没有"。转码之后
  # 中文从 2 字节变 3 字节，拿转换后的大小去比那个数会每次都不符。
  if ! verify_bytes "$tmp" "$bytes" "$name"; then
    failed+=("$name（字节数不符）")
    rm -f "$tmp"
    continue
  fi
  if [ "$enc" = "gb18030" ]; then
    if ! iconv -f GB18030 -t UTF-8 "$tmp" -o "$tmp.utf8" 2>/dev/null; then
      echo "  ✗ GB18030 转 UTF-8 失败——原文编码可能不是 GB18030，人工确认后再改表" >&2
      rm -f "$tmp" "$tmp.utf8"
      failed+=("$name（转码失败）")
      continue
    fi
    mv "$tmp.utf8" "$tmp"
    echo "  已从 GB18030 转成 UTF-8（抽取引擎只读 UTF-8）；"\
         "落盘后的字节数会大于上面那个数，那是转码带来的，不是下载缺失"
  fi
  mv "$tmp" "$out"
done

echo
if [ ${#failed[@]} -gt 0 ]; then
  echo "【失败】${#failed[@]}/${#SOURCES[@]} 个源有问题：" >&2
  for f in "${failed[@]}"; do echo "  - $f" >&2; done
  echo "**不要带着残缺文件往下抽**（一次真实抽取几百次调用）。先修下载。" >&2
  exit 1
fi
echo "六个源全部就位，字节数检查通过。下一步（零成本）："
echo "  python -m scripts.verify_pharmacology_chunks --books-dir $DEST"
echo "确认切块切的是「一味药 / 一张方」再真跑抽取——切错了会白花几百次调用。"
