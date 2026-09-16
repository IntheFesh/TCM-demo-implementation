#!/usr/bin/env bash
# 上机剧本：把真机上要跑的全部串起来，**分段、可续跑、每段独立**。
#
#   bash scripts/run_onsite.sh --dry-run     # 只打清单和预估调用数/成本，什么都不跑
#   bash scripts/run_onsite.sh               # 从段 0 开始
#   bash scripts/run_onsite.sh --resume      # 从**第一个还没成功过**的段继续
#   bash scripts/run_onsite.sh --status      # 只看每段跑到哪了，不跑
#   bash scripts/run_onsite.sh --from 4      # 硬指定从段 4 继续
#   bash scripts/run_onsite.sh --only 3      # 只跑某一段
#   bash scripts/run_onsite.sh --yes         # 跳过两处人工卡点（无人值守时用，慎用）
#
# R19：`--resume` 跟 `--from` 的区别是「谁记得断在哪」。`--from` 要人自己记住
# ——而这套东西跑几个小时、中间会换终端、容器也可能被回收，"我记得是段 5 挂的"
# 这件事本身就是故障点。每段跑完把退出码写进 ONSITE_STATE（一行一段），
# `--resume` 读它，从第一个不是 0 的段接着跑。**已经成功的段不会重跑**——
# 段 5 是最贵的一段，重跑一次两千多次调用。
#
# 设计上的三条：
#   1. **一段失败不影响后面的段**。段与段之间只有"先后"没有"依赖崩塌"——
#      段 5 的药理层抽取挂了，段 6 的录制照样该跑。每段末尾打时间戳和退出码，
#      最后汇总，不在中途 `set -e` 掉整个脚本。
#   2. **段序按「依赖 + 成本」排，依赖优先**：零调用的先跑（免费的先把能发现的问题
#      发现掉）；段 5 药理层抽取（R8 实测后是最贵的一段）必须在段 6 录制、段 7 评测
#      之前——core/tools.py 读它落盘的 data/materia_medica.jsonl；全套评测（最贵）
#      排在录制之后；段 8 性能基准放最末，因为它量的必须是"跑完前面所有段之后的
#      这套系统"（开 ReAct 的那一次基准在段 5 之前跑出来的是"数据文件不存在"的耗时）。
#   3. **两处人工卡点**（段 3 role 填充率、段 5 抽取质量）**必须停下来等人确认**。
#      这两处一路冲到底的代价是：闸门没过就往下跑，后面几百次调用全部白花。
#
# 失败预案见 docs/onsite_troubleshooting.md。**第一条就是「某段静默很久是正常的」**
# ——上一轮因为误判卡死，杀了三个正常运行的进程，浪费一小时和几百次调用。

set -uo pipefail
cd "$(dirname "$0")/.."

# 每次调用的均价。**来源是本仓库唯一有过的一次成本记录**：README 里
# record_fixtures 那条「约 272 次，¥1.5」。⚠ 这是一个量级估算、不是账单，
# 换模型或改 prompt 长度都会变——它的用途是让人在开跑前决定「跑到哪一段」，
# 不是用来报销的。
YUAN_PER_CALL=0.0055

# 每段的退出码落在这里，一行 `段号<TAB>退出码<TAB>结束时间`。
# 放 out/ 下（gitignore）而不是仓库里：它是这台机器这一次跑的状态，不是项目内容。
# 路径可以用环境变量覆盖，测试就是靠这个把它指到 tmp_path 的。
ONSITE_STATE="${ONSITE_STATE:-out/onsite_state.tsv}"

# 记一段的结果。**同一段重跑要覆盖旧记录**，不是追加——追加的话 --resume 读到
# 的是第一次那条（失败的那条），已经修好重跑成功也还会再跑一遍。
record_segment() {
  local n="$1" rc="$2"
  mkdir -p "$(dirname "$ONSITE_STATE")"
  local tmp="${ONSITE_STATE}.tmp"
  if [ -f "$ONSITE_STATE" ]; then
    grep -v -P "^${n}\t" "$ONSITE_STATE" > "$tmp" 2>/dev/null || true
  else
    : > "$tmp"
  fi
  printf '%s\t%s\t%s\n' "$n" "$rc" "$(date -Is)" >> "$tmp"
  sort -n -o "$ONSITE_STATE" "$tmp"
  rm -f "$tmp"
}

# 某段上次的退出码；没记录过回 `none`。
segment_state() {
  [ -f "$ONSITE_STATE" ] || { echo none; return; }
  local line
  line=$(grep -P "^$1\t" "$ONSITE_STATE" 2>/dev/null | tail -1) || true
  [ -n "$line" ] || { echo none; return; }
  echo "$line" | cut -f2
}

# --resume 的起点：第一个"上次不是退出码 0"的段（没记录也算）。
# 全都是 0 时回一个比最大段号还大的数——那时 --resume 什么都不跑，
# 并且下面会**说出来**，不是静默跑完 0 段。
first_unfinished_segment() {
  for row in "${SEGMENTS[@]}"; do
    IFS='|' read -r n _ _ _ _ <<< "$row"
    [ "$(segment_state "$n")" = "0" ] || { echo "$n"; return; }
  done
  echo 99
}

print_status() {
  echo "段  名称                     上次退出码  结束时间"
  echo "--------------------------------------------------------------------------"
  for row in "${SEGMENTS[@]}"; do
    IFS='|' read -r n name _ _ _ <<< "$row"
    local st line when
    st=$(segment_state "$n")
    when="—"
    if [ -f "$ONSITE_STATE" ]; then
      line=$(grep -P "^${n}\t" "$ONSITE_STATE" 2>/dev/null | tail -1) || true
      [ -n "$line" ] && when=$(echo "$line" | cut -f3)
    fi
    case "$st" in
      none) st="还没跑过" ;;
      0) st="0（成功）" ;;
      10) st="10（人工在卡点中止）" ;;
      *) st="$st（失败）" ;;
    esac
    printf "%-3s %-24s %-11s %s\n" "$n" "$name" "$st" "$when"
  done
  echo "--------------------------------------------------------------------------"
  echo "状态文件：$ONSITE_STATE"
  local nxt
  nxt=$(first_unfinished_segment)
  if [ "$nxt" = "99" ]; then
    echo "九段都是退出码 0。--resume 不会跑任何段。"
  else
    echo "--resume 会从段 $nxt 开始。"
  fi
}

# 段号|名称|预估调用数|人工卡点|一句话
# 「预估调用数」那一格可以写 `auto:<文件>`：跑的时候由 scripts/onsite_plan.py 从那份
# 文件现读（段 4 = eval/epsilon.json 三段 llm_calls 之和）。段 4 原来写死 215，ε 重跑
# 成 212 之后剧本、说明文字、测试断言三处同时过期——现读就不会再有这种过期。
SEGMENTS=(
  "0|环境自检|0|no|零成本：pytest / ruff / 环境变量残留 / 数据文件齐不齐"
  "1|零调用的验证|0|no|凭据核对 / 本地语料规范化 / 切块验证（要人看原文）/ SDT 失分分析"
  "2|本地模型|2|no|起 vLLM + verify_local_backend（要先装 vllm、下基座）"
  "3|R1 前提：role 填充率|60|YES|**不过就停**——填充率不够，分层 ε 三个数没有意义"
  "4|R1 验收：噪声地板 ε|auto:eval/epsilon.json|no|预估调用数**现读** eval/epsilon.json（三段 llm_calls 之和）——ε 一重跑这个数就变，不写死"
  "5|药理层抽取|2181|YES|六源预过滤后 2151 块（R8 实测，verify_pharmacology_chunks 合计行）+ 6×5 试抽；先 --limit-blocks 5 人工核质量，再全量 --crosscheck"
  "6|录制回放|278|no|record_fixtures（--dry-run 实测 278）+ verify_replay"
  "7|全套评测重跑|1200|no|最贵的一段：run_eval 四项 + SDT Test（会写台账）"
  "8|性能基准|38|no|bench_startup 冷/热各一次（0 调用）+ bench_consult 不开 ReAct ×3、开 ReAct ×1"
  "9|R21~R24 的上机项|320|no|前缀规模 0 + 缓存命中率 2 次问诊 ×11 = 22 + full_context 下 E3/E4（9 条主诉 × own/swapped/none 三种 × 11 次/问诊 ≈ 297）+ 字体子集化 0。**这个 11 是 R22 之后的 calls_per_consult()**（2 + 3 医家 × 采样 3 次）"
  "10|R26 蒸馏（可选）|350|YES|**不做也能交付**：蒸馏是研究证据、不进演示路径。预估 350 次 = 70 条 × calls_per_consult(3 位医家, best_of_n=1)=5。70 这个数是 ¥40 预算按高峰价现算出来的（offline/distill_from_v4 --estimate），**不是配方要的 8000 条**——那要 ¥3956，见 docs/reports/R26_report.md 第三节"
)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

DRY_RUN=0; FROM=0; ONLY=""; ASSUME_YES=0; RESUME=0; STATUS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --resume) RESUME=1 ;;
    --status) STATUS=1 ;;
    --from) FROM="${2:?--from 要一个段号}"; shift ;;
    --only) ONLY="${2:?--only 要一个段号}"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# --resume 和 --from 一起传是自相矛盾的（一个说"读状态文件"，一个说"我指定"）。
# 直接拒绝而不是挑一个赢——挑一个赢的话，传错的人不会知道自己被忽略了。
if [ "$RESUME" = "1" ] && [ "$FROM" != "0" ]; then
  echo "--resume 和 --from 不能一起传：前者从状态文件算起点，后者是你指定起点。" >&2
  exit 2
fi
if [ "$RESUME" = "1" ] && [ -n "$ONLY" ]; then
  echo "--resume 和 --only 不能一起传。" >&2
  exit 2
fi

# 段表里「预估调用数」那一格 → 一个整数。`auto:<文件>` 交给 scripts/onsite_plan.py
# 现读（bash 和 tests/test_run_onsite.py 调的是同一个 resolve_calls，不各数一遍）。
resolve_calls() {
  case "$1" in
    auto:*) python3 -m scripts.onsite_plan --calls "$1" ;;
    *) echo "$1" ;;
  esac
}

# R21：峰谷分时。**判定只有一处实现**（core/usage.py::is_peak / peak_note），
# bash 这边不自己算时区——算两遍就会有一处把夏令时/UTC 偏移弄错，
# 而弄错的表现是"按五折估的预算，实际按原价扣"。
peak_note() { python3 -c "from core.usage import peak_note; print(peak_note())"; }
is_peak() { python3 -c "import sys; from core.usage import is_peak; sys.exit(0 if is_peak() else 1)"; }

# 这几段是花钱的大头（段 5 药理层抽取、段 6 录制、段 7 全套评测、段 8 性能基准）。
# 高峰时段启动它们只提醒、**不阻止**：有时就是得现在跑。
COSTLY_SEGMENTS=" 5 6 7 8 9 10 "

warn_if_peak() {
  local n="$1"
  case "$COSTLY_SEGMENTS" in
    *" $n "*) ;;
    *) return 0 ;;
  esac
  if is_peak; then
    printf '\033[33m>>> [段 %s] 现在是**高峰时段**，这一段比较贵。谷段（北京 12–14、18–09）五折。\033[0m\n' "$n"
    printf '\033[33m>>> 不拦你——要等就 Ctrl-C，之后 `--resume` 从这一段接着跑。\033[0m\n'
  fi
}

print_plan() {
  local total=0
  echo "段  名称                     预估调用  人工卡点  说明"
  echo "--------------------------------------------------------------------------"
  for row in "${SEGMENTS[@]}"; do
    IFS='|' read -r n name calls gate note <<< "$row"
    calls=$(resolve_calls "$calls")
    total=$((total + calls))
    printf "%-3s %-24s %8s  %-8s  %s\n" "$n" "$name" "$calls" "$gate" "$note"
  done
  echo "--------------------------------------------------------------------------"
  local yuan
  yuan=$(python3 -c "print(f'{$total * $YUAN_PER_CALL:.1f}')")
  echo "全部跑完预估 ${total} 次调用 ≈ ¥${yuan}（均价 ¥${YUAN_PER_CALL}/次，"
  echo "来源是 README 里 record_fixtures 那条「约 272 次 ¥1.5」——量级估算，不是账单）"
  echo
  echo "⚠ **这个总数是下限，不是上限。** 那个均价是 top3 时代量出来的：一次调用只带"
  echo "  三条医案。R21 之后 full_context 每次调用要带 ~18 万 token 的知识前缀，"
  echo "  R22 之后 S3 在 effort=max 下思考 token 按输出计费——段 9/10 的每次调用"
  echo "  贵一个量级。段 10 自己的估算器（offline/distill_from_v4 --estimate）报的是"
  echo "  ¥40 买 70 条，也就是 ¥0.11/次，是均价的 20 倍。**按段问它自己的估算器，"
  echo "  不要拿这个均价去算那两段。**"
  echo
  echo "段 0/1 零调用，先跑它们：免费的问题先发现掉。段 5 最贵，但段 6/7 要用它落盘的"
  echo "药理层数据（core/tools.py 读 data/materia_medica.jsonl），所以它在两者之前。"
  echo "段 7 是（top3 系里）最贵的一段；段 8 性能基准要量的是跑完前面所有段之后的这套系统。"
  echo "段 9（R21~R24 的上机项）要的东西前面几段都得先有——前缀规模要 cases.json、"
  echo "命中率要真实 API、full_context 下的 E3/E4 要评测框架跑通、字体子集化要联网取原始字体。"
  echo "段 10（R26 蒸馏）排最末，而且**整段可以不做**：它是研究证据，不进演示路径。"
}

gate() {   # $1 = 段号, $2 = 这一步要人确认什么
  if [ "$ASSUME_YES" = "1" ]; then
    echo ">>> [段 $1] 人工卡点被 --yes 跳过：$2"
    return 0
  fi
  echo
  echo ">>> [段 $1] **人工卡点**：$2"
  echo ">>> 看完上面的输出，确认没问题再继续。输入 y 继续，其它任意键中止这一段。"
  read -r -p ">>> 继续？[y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

seg_0() {
  # **全量测试分两段跑。** 无卡模式 2GB 上一次跑完实测 34% 被 OOM 杀掉（退出码 137，
  # 只留一个 `Killed`，看不出是哪条测试）。根因是一批用例会把 400MB 的
  # sentence-transformers 模型真加载进来。现在默认全部走假编码器，只有标了
  # `@pytest.mark.real_embedding` 的才真加载——把那几条单独放一段跑，中间 sleep 20
  # 让上一段的进程真正退干净、内存被回收。
  python -m pytest tests/ -q -m "not real_embedding" || return 1
  sleep 20
  python -m pytest tests/ -q -m real_embedding || return 1
  ruff check . || return 1
  python -m scripts.collect_results --check || return 1
  echo "--- 环境变量（期望：没有上次调试留下的残留）---"
  # LLM_TIMEOUT_SECONDS/LLM_TIMEOUT/LLM_MAX_TOKENS 也要看：R8 段 6 卡死 46 分钟之后
  # 第一件要排除的就是"超时被谁设成了一个大数"，而原来这条 grep 看不见它们。
  env | grep -E "RETRIEVER_MODE|USE_REACT|EVAL_MODE|FAST_MODE|LLM_MODE|LORA_DIR|LLM_TIMEOUT|LLM_MAX_TOKENS" || echo "（干净）"
  echo "--- 数据文件 ---"
  for f in cases.json data/graph.json data/element_index.json data/case_triples.jsonl; do
    [ -e "$f" ] && echo "  ✓ $f" || echo "  ✗ $f 缺失（后面依赖它的段会失败）"
  done
  echo "--- LLM_MODEL 服务端还认不认（**不是 LLM 调用**，查 /models 清单，零成本）---"
  # R10 的教训：deepseek-chat 已下线，拿它发请求得到的是 **HTTP 200 + 空响应体**，
  # 不是 404。症状是"每次调用空串 → 校验失败 → 重试三次 → LLMError"，一整段的钱
  # 白花，而错误信息里看不出根因在模型名上。这里在花第一分钱之前就把它挡掉。
  python3 - <<'PY' || return 1
import json, os, urllib.request
base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
want = os.environ.get("LLM_MODEL", "deepseek-v4-pro")
req = urllib.request.Request(
    f"{base}/models",
    headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"})
try:
    names = [m["id"] for m in json.load(urllib.request.urlopen(req, timeout=15))["data"]]
except Exception as e:  # noqa: BLE001
    # 查不到不算失败：没网/没 key/本地后端都会走到这里，而这一段是"零成本自检"，
    # 不该因为查不到一个清单就把后面几段拦住。
    print(f"  ⚠ 查不到模型清单（{e}）——跳过这一项。本地后端或没配 key 时正常")
    raise SystemExit(0)
if want in names:
    print(f"  ✓ LLM_MODEL={want} 在服务端清单里（清单：{names}）")
    raise SystemExit(0)
print(f"  ✗ LLM_MODEL={want} **不在**服务端清单里：{names}")
print("    后面每一次调用都会返回 HTTP 200 + 空响应体（deepseek-chat 下线时就是这个"
      "表现），整段的钱白花。改 .env 的 LLM_MODEL 再跑。")
raise SystemExit(1)
PY
}

seg_1() {
  python -m scripts.collect_results || return 1
  echo
  echo "--- 本地语料规范化（幂等：第二遍就是没事做）---"
  python -m scripts.normalize_local_corpora || return 1
  echo
  echo "--- 切块验证：**下面会打出每个源预过滤后的前 3 块、被跳过的前 5 块原文，要人看** ---"
  echo "--- 最后一行「合计 … 真实抽取预估调用数 N」要跟本脚本段 5 的预估对得上 ---"
  python -m scripts.verify_pharmacology_chunks || return 1
  echo
  echo "--- 本地语料的切块验证：**只看段落粒度，这三份都不进药理层抽取** ---"
  echo "--- （理由见 offline/local_corpora.py 的声明表；引擎直接 --input 指到它们会被拒绝）---"
  local_ok=0
  if [ -f data/local_corpora/脾胃论.txt ]; then
    python -m scripts.verify_pharmacology_chunks --file data/local_corpora/脾胃论.txt --source classic || local_ok=1
  fi
  for f in data/local_corpora/李可医案.txt data/local_corpora/王云启医案.txt; do
    [ -f "$f" ] && { python -m scripts.verify_pharmacology_chunks --file "$f" || local_ok=1; }
  done
  [ "$local_ok" = "0" ] || return 1
  echo
  echo "--- SDT 失分分析（零调用，对已有提交文件重新聚合）---"
  echo "--- **第一次跑必然跳过**：它的输入 out/sdt_chain_v2.txt 是段 7 的产物，"
  echo "--- 段 7 跑过一次之后这一项才有东西可分析。看到「跳过」不是配置错了。---"
  if [ -n "${SDT:-}" ] && [ -f out/sdt_chain_v2.txt ]; then
    python -m eval.sdt.run --sdt-dir "$SDT" --split Test --error-analysis out/sdt_chain_v2.txt
  else
    echo "跳过：需要 \$SDT 和 out/sdt_chain_v2.txt（还没跑过 SDT 就没有这个文件）"
  fi
}

seg_2() {
  bash scripts/start_vllm.sh &
  echo "vLLM 在后台起（日志见该脚本里指定的文件）。等它 ready 再验——"
  echo "**这里静默很久是正常的**：加载权重要几分钟，看日志文件的 mtime，不要 kill。"
  python -m scripts.verify_local_backend
}

seg_3() {
  python -m scripts.verify_role_fill
}

seg_4() {
  # 默认设置（S3_THINKING=enabled）那一套，落 eval/epsilon.json。
  python -m offline.estimate_epsilon --n-repeats 3 || return 1
  echo "--- 判据：ε_core < ε_online < ε_adjunct（不成立就如实报，不调参去凑）---"
  python -m scripts.collect_results | sed -n '/ε 分层/,/^$/p'
  # R19：**关掉思考再跑一套**，落 eval/epsilon_s3_disabled.json。
  # 两套数不可比（README「S3_THINKING」那一行），所以是并列的两个文件、两组凭据键，
  # 不是同一个文件覆盖一次。跑这一套的意义是回答「思考模式值不值那几十倍的时间」
  # ——只有一套数的时候这个问题连提都提不出来。
  # `|| true`：这一套挂了不该让段 4 整段算失败，默认那一套已经落盘了。
  echo "--- 再跑一套：S3_THINKING=disabled（并列对照，不覆盖上面那套）---"
  S3_THINKING=disabled python -m offline.estimate_epsilon --n-repeats 3 || \
    echo "（disabled 那一套没跑成。默认那一套已落盘，段 4 不因此算失败。）"
}

seg_5() {
  echo "--- 先每个源抽 5 块看质量（**这一步的输出要人逐条看**）---"
  # R8 之前这里写的是 `offline.extract_materia_medica --limit 5`：引擎的
  # --input/--source/--book 都是必填，那条命令在 argparse 就退出，段 5 一步都跑不了。
  # 现在走批量入口，六个源的参数从 offline/pharmacology_sources.EXPECTED_SOURCES 取。
  python -m scripts.run_pharmacology_extraction --dry-run || return 1
  python -m scripts.run_pharmacology_extraction --limit-blocks 5
}

seg_5b() {
  python -m scripts.run_pharmacology_extraction --crosscheck
}

seg_6() {
  python -m scripts.record_fixtures || return 1
  python -m scripts.verify_replay
}

seg_7() {
  python -m eval.run_eval --queries-path tests/queries.txt --e3 --e4 --e8 --e9 || return 1
  echo "--- 上面重跑的数要回写 eval/RESULTS.md 的凭据记号，然后 ---"
  python -m scripts.collect_results --check
  echo
  echo "--- SDT Test：**只在最终定型后跑**，会写台账 eval/sdt/test_run_log.jsonl ---"
  if [ -n "${SDT:-}" ]; then
    python -m eval.sdt.run --sdt-dir "$SDT" --split Test --solver chain --out out/sdt_chain_v3.txt
  else
    echo "跳过：需要 \$SDT"
  fi
}

seg_8() {
  # **放最后不是因为贵，是因为它量的必须是"跑完前面所有段之后的这套系统"**：
  # core/tools.py 读段 5 落盘的 data/materia_medica.jsonl，开 ReAct 的那一次基准在
  # 段 5 之前跑出来的是"工具返回数据文件不存在"的耗时，跟真实形态不是一回事。
  echo "--- 启动耗时：冷（第一次进程） ---"
  python -m scripts.bench_startup || return 1
  echo "--- 启动耗时：热（第二次进程，R12 起应命中 embedding 磁盘缓存） ---"
  python -m scripts.bench_startup || return 1
  echo "--- 一次问诊，不开 ReAct ×3（预算 ≤ 90s，见 docs/DESIGN.md §9） ---"
  python -m scripts.bench_consult --backend real --repeat 3 --no-react || return 1
  echo "--- 一次问诊，开 ReAct ×1（预算 ≤ 240s） ---"
  python -m scripts.bench_consult --backend real --repeat 1 --react
}

seg_9() {
  # R21~R24 攒下来的上机项，合成一段。段号排在最后（它要的东西前面几段都得先有：
  # 前缀规模要 cases.json、命中率要真实 API、E3/E4 要评测框架跑通），
  # **但顺序上建议提前单独跑**：
  #
  #     bash scripts/run_onsite.sh --only 9
  #
  # 理由是②③两步决定的是**默认配置成不成立**（full_context 能不能当默认），
  # 而它们只要 cases.json + 真实 key，不依赖段 5~8 的产物。闸门没过要退回 hybrid
  # ——那件事越早知道越好：等段 5/7 那几千次调用花完再发现，钱是按错的默认配置花的。
  #
  # ⚠ **这一段跑出来的数跟段 7 的历史数不可比**：R21 把检索默认换成
  # full_context、R22 把 S3 改成采 3 次 + effort=max。三个旋钮都换了实验条件，
  # 所以下面 E3/E4 的结果是**新起的一行**（eval/RESULTS.md 的 full_context 系），
  # 不是覆盖上面那张 top3 系的表。
  echo "--- ① 前缀真实规模（0 调用）。判据：三位医家各自 ≤ 500K，且没有段被裁 ---"
  python -m core.context_prefix --report || return 1

  echo "--- ② 前缀缓存命中率。**判据：第二次 ≥ 0.9**（第一次必然接近 0，那是对的） ---"
  python -m scripts.bench_consult --backend real --repeat 2 || return 1

  echo "--- ③ full_context 下的 E3/E4。判据：两个 change_rate 各自 ≥ 0.4 ---"
  echo "--- **跟 top3 系那两个数（0.451 / 0.497）并列报，不相减**：换掉的量不同 ---"
  RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4 || return 1
  # **闸门没过要当场停，并且给出退路。** 这是 R21 那个决定的兜底：把默认检索
  # 模式从 hybrid 换成 full_context 是拿 E3/E4 闸门担保的，闸门不过就说明
  # "全量语料让模型更认得出这是谁的医案"这个前提在真机上不成立——那时该退回
  # hybrid，而不是带着一个没过闸门的默认配置去演示。
  # 闸门阈值不在这里写死：问 eval/run_eval.py 的 GATE_OUTPUT_CHANGE_RATE
  # （全项目一处），bash 这边只负责读报告和给退路。
  python - <<'GATE_PY' || return 1
import json, sys
from pathlib import Path
from eval.run_eval import GATE_OUTPUT_CHANGE_RATE as GATE

bad = []
for name, key in (("report_e3.json", "e3"), ("report_e4.json", "e4")):
    path = Path("eval") / name
    if not path.exists():
        bad.append(f"{name} 不在（这一段没跑完）")
        continue
    rate = (json.loads(path.read_text(encoding="utf-8")).get(key) or {}).get("change_rate")
    if rate is None:
        bad.append(f"{key}: change_rate 是 null（可用样本两侧检索全为空，闸门无法判定）")
    elif rate < GATE:
        bad.append(f"{key}: change_rate {rate:.3f} < {GATE}")
if bad:
    print("闸门未通过：" + "；".join(bad), file=sys.stderr)
    print("**默认检索模式退回 hybrid：export RETRIEVER_MODE=hybrid**", file=sys.stderr)
    print("（退回之后 top3 系那套数才是可引的；full_context 系的行留 ⏳ 并写明闸门没过）",
          file=sys.stderr)
    sys.exit(1)
print(f"E3/E4 两个 change_rate 都 ≥ {GATE}，full_context 可以继续当默认。")
GATE_PY

  echo "--- ④ 字体子集化（0 调用，要网络取原始字体）。判据：每个面 < 原始的 10% ---"
  python -m scripts.subset_fonts --check-deps || {
    echo "（缺 fonttools：pip install \"fonttools[woff]\" brotli，然后重跑这一段）"
    return 1
  }
  python -m scripts.subset_fonts --download || return 1
  python -m scripts.subset_fonts || return 1
  echo "--- 把上面打印的四段 @font-face 贴进 web/app.css，再跑一次演示自检 ---"
  python -m scripts.demo_preflight || true
}

seg_10() {
  # R26：从 v4-pro 蒸六步链样本。**这一段可以整段不做**——蒸馏是研究证据，
  # 不进演示、不阻塞交付（总纲 R26 原话）。
  #
  # ⚠ 它是全剧本唯一一段「预算决定规模」而不是「规模决定预算」的段：
  # 配方要 8000 条，¥40 只买得起 70 条（高峰价）。脚本默认按预算现算条数，
  # 所以**不传 --limit 就不会超预算**；真要 8000 条得先决定花 ¥3956。
  echo "--- ① 先估钱（0 调用）。判据：打印出的条数 × 预算跟你打算花的钱一致 ---"
  python -m offline.distill_from_v4 --estimate || return 1

  echo "--- ② 真跑。超过 ¥30 要 --yes-spend（§0.5 第 2 条第三款），这是人工卡点那一步 ---"
  python -m offline.distill_from_v4 --yes-spend || return 1

  echo "--- ③ 训练计划（0 调用、不加载模型）。判据：train 条数不是 0 ---"
  python -m scripts.train_lora --data distill_v4 --base qwen3.5-9b --dry-run || return 1

  echo "--- ④ 真训要 GPU（32GB 单卡跑 9B 的 LoRA）。判据：heldout loss 不比 train 高出 20% ---"
  python -m scripts.train_lora --data distill_v4 --base qwen3.5-9b --check-deps || {
    echo "（缺训练依赖：pip install -r requirements-train.txt，然后重跑这一段）"
    return 1
  }
  echo "--- ⑤ 蒸馏模型只跑一次 SDT Test 作为旁证，**跟 v4-pro 并列报、不覆盖** ---"
  echo "    命令：LLM_MODE=local_inproc LORA_DIR=lora_out/qwen3.5-9b python -m eval.sdt.run --split Test"
  echo "    判据：台账 eval/sdt/test_run_log.jsonl 多一行，且 RESULTS.md 那一行标明是蒸馏模型"
}

run_segment() {
  local n="$1" name="$2" gate_flag="$3"
  echo
  echo "=========================================================================="
  echo "[段 $n] $name    开始 $(date -Is)"
  echo "=========================================================================="
  warn_if_peak "$n"
  local rc=0
  case "$n" in
    0) seg_0 || rc=$? ;;
    1) seg_1 || rc=$? ;;
    2) seg_2 || rc=$? ;;
    3) seg_3 || rc=$?
       if [ "$rc" = "0" ]; then
         gate 3 "role 填充率过 90% 闸门了吗？**不过就停**——分层 ε 的三个数建立在它上面" \
           || { echo "[段 3] 人工中止"; rc=10; }
       fi ;;
    4) seg_4 || rc=$? ;;
    5) seg_5 || rc=$?
       if [ "$rc" = "0" ]; then
         if gate 5 "上面 5 条抽出来的三元组，s（药名）对得上原文吗？o 和 source_span 呢？"; then
           seg_5b || rc=$?
         else
           echo "[段 5] 人工中止，没有跑全量"; rc=10
         fi
       fi ;;
    6) seg_6 || rc=$? ;;
    7) seg_7 || rc=$? ;;
    8) seg_8 || rc=$? ;;
    9) seg_9 || rc=$? ;;
  esac
  echo "--------------------------------------------------------------------------"
  echo "[段 $n] $name    结束 $(date -Is)    退出码 $rc"
  RESULTS+=("$n|$name|$rc")
  # 落盘**在这里**而不是在汇总时统一写：汇总写的话，容器被回收/终端被关掉就
  # 一个字都没留下——而"跑了三小时之后断了"恰恰是这件事要解决的场景。
  record_segment "$n" "$rc"
  return 0   # **一段失败不影响后面的段**，见文件头第 1 条
}

if [ "$STATUS" = "1" ]; then
  print_status
  exit 0
fi

echo "$(peak_note)"
echo
print_plan

# --resume 的起点**在 --dry-run 之前算**：`--resume --dry-run` 要能回答
# "它会从哪一段开始"——那正是开跑前最想知道的一件事。算在后面的话这两个参数
# 一起传时只打清单、不说起点。
if [ "$RESUME" = "1" ]; then
  FROM=$(first_unfinished_segment)
  echo
  print_status
  if [ "$FROM" = "99" ]; then
    # **说出来**：九段都成功过时 --resume 不跑任何段，静默退出会被当成"跑完了"。
    echo
    echo "--resume：没有需要续跑的段。要重跑某一段用 --only <段号>。"
    exit 0
  fi
  echo
  echo "--resume：从段 $FROM 开始（段 0..$((FROM - 1)) 上次都是退出码 0，不重跑）。"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "--dry-run：什么都没跑。去掉这个参数开始。"
  exit 0
fi

RESULTS=()
for row in "${SEGMENTS[@]}"; do
  IFS='|' read -r n name calls gate_flag note <<< "$row"
  if [ -n "$ONLY" ]; then
    [ "$n" = "$ONLY" ] || continue
  elif [ "$n" -lt "$FROM" ]; then
    continue
  fi
  run_segment "$n" "$name" "$gate_flag"
done

echo
echo "=========================================================================="
echo "汇总（退出码 0 = 这一段自己跑完了；10 = 人工在卡点中止）"
echo "=========================================================================="
failed=0
for r in "${RESULTS[@]}"; do
  IFS='|' read -r n name rc <<< "$r"
  printf "  段 %-2s %-24s 退出码 %s\n" "$n" "$name" "$rc"
  [ "$rc" = "0" ] || failed=1
done
if [ "$failed" = "0" ]; then
  echo "全部段退出码 0。"
else
  echo "有段没跑成。对照 docs/onsite_troubleshooting.md 修掉，然后："
  echo "    bash scripts/run_onsite.sh --resume"
  echo "它从第一个没成功的段接着跑，**已经成功的段不重跑**（段 5 重跑一次两千多次调用）。"
  echo "状态记在 $ONSITE_STATE，用 --status 能单独看。"
fi
exit "$failed"
