#!/usr/bin/env bash
# 上机剧本：把真机上要跑的全部串起来，**分段、可续跑、每段独立**。
#
#   bash scripts/run_onsite.sh --dry-run     # 只打清单和预估调用数/成本，什么都不跑
#   bash scripts/run_onsite.sh               # 从段 0 开始
#   bash scripts/run_onsite.sh --from 4      # 断了之后从段 4 继续
#   bash scripts/run_onsite.sh --only 3      # 只跑某一段
#   bash scripts/run_onsite.sh --yes         # 跳过两处人工卡点（无人值守时用，慎用）
#
# 设计上的三条：
#   1. **一段失败不影响后面的段**。段与段之间只有"先后"没有"依赖崩塌"——
#      段 5 的药理层抽取挂了，段 6 的录制照样该跑。每段末尾打时间戳和退出码，
#      最后汇总，不在中途 `set -e` 掉整个脚本。
#   2. **段序按「依赖 + 成本」排，依赖优先**：零调用的先跑（免费的先把能发现的问题
#      发现掉）；段 5 药理层抽取（R8 实测后是最贵的一段）必须在段 6 录制、段 7 评测
#      之前——core/tools.py 读它落盘的 data/materia_medica.jsonl；没有段依赖的
#      全套评测放最后。
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

# 段号|名称|预估调用数|人工卡点|一句话
SEGMENTS=(
  "0|环境自检|0|no|零成本：pytest / ruff / 环境变量残留 / 数据文件齐不齐"
  "1|零调用的验证|0|no|凭据核对 / 本地语料规范化 / 切块验证（要人看原文）/ SDT 失分分析"
  "2|本地模型|2|no|起 vLLM + verify_local_backend（要先装 vllm、下基座）"
  "3|R1 前提：role 填充率|60|YES|**不过就停**——填充率不够，分层 ε 三个数没有意义"
  "4|R1 验收：噪声地板 ε|215|no|实测 215 次调用 / 1347s（eval/epsilon.json 的 llm_calls）"
  "5|药理层抽取|2182|YES|六源预过滤后 2152 块（R8 实测，verify_pharmacology_chunks 合计行）+ 6×5 试抽；先 --limit-blocks 5 人工核质量，再全量 --crosscheck"
  "6|录制回放|278|no|record_fixtures（--dry-run 实测 278）+ verify_replay"
  "7|全套评测重跑|1200|no|最贵，放最后：run_eval 四项 + SDT Test（会写台账）"
)

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

DRY_RUN=0; FROM=0; ONLY=""; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --from) FROM="${2:?--from 要一个段号}"; shift ;;
    --only) ONLY="${2:?--only 要一个段号}"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
  shift
done

print_plan() {
  local total=0
  echo "段  名称                     预估调用  人工卡点  说明"
  echo "--------------------------------------------------------------------------"
  for row in "${SEGMENTS[@]}"; do
    IFS='|' read -r n name calls gate note <<< "$row"
    total=$((total + calls))
    printf "%-3s %-24s %8s  %-8s  %s\n" "$n" "$name" "$calls" "$gate" "$note"
  done
  echo "--------------------------------------------------------------------------"
  local yuan
  yuan=$(python3 -c "print(f'{$total * $YUAN_PER_CALL:.1f}')")
  echo "全部跑完预估 ${total} 次调用 ≈ ¥${yuan}（均价 ¥${YUAN_PER_CALL}/次，"
  echo "来源是 README 里 record_fixtures 那条「约 272 次 ¥1.5」——量级估算，不是账单）"
  echo
  echo "段 0/1 零调用，先跑它们：免费的问题先发现掉。段 5 最贵，但段 6/7 要用它落盘的"
  echo "药理层数据（core/tools.py 读 data/materia_medica.jsonl），所以它在两者之前；段 7 放最后。"
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
  python -m pytest tests/ -q || return 1
  ruff check . || return 1
  python -m scripts.collect_results --check || return 1
  echo "--- 环境变量（期望：没有上次调试留下的残留）---"
  env | grep -E "RETRIEVER_MODE|USE_REACT|EVAL_MODE|FAST_MODE|LLM_MODE|LORA_DIR" || echo "（干净）"
  echo "--- 数据文件 ---"
  for f in cases.json data/graph.json data/element_index.json data/case_triples.jsonl; do
    [ -e "$f" ] && echo "  ✓ $f" || echo "  ✗ $f 缺失（后面依赖它的段会失败）"
  done
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
  echo "--- 本地语料的切块验证（脾胃论按古籍判据；两份医案 txt 没有结构判据，只看粒度）---"
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
  python -m offline.estimate_epsilon --n-repeats 3 || return 1
  echo "--- 判据：ε_core < ε_online < ε_adjunct（不成立就如实报，不调参去凑）---"
  python -m scripts.collect_results | sed -n '/ε 分层/,/^$/p'
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

run_segment() {
  local n="$1" name="$2" gate_flag="$3"
  echo
  echo "=========================================================================="
  echo "[段 $n] $name    开始 $(date -Is)"
  echo "=========================================================================="
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
  esac
  echo "--------------------------------------------------------------------------"
  echo "[段 $n] $name    结束 $(date -Is)    退出码 $rc"
  RESULTS+=("$n|$name|$rc")
  return 0   # **一段失败不影响后面的段**，见文件头第 1 条
}

print_plan
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
[ "$failed" = "0" ] && echo "全部段退出码 0。" || \
  echo "有段没跑成——看上面是哪一段，对照 docs/onsite_troubleshooting.md，然后 --from <段号> 续跑。"
exit "$failed"
