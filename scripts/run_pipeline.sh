#!/usr/bin/env bash
# 完整流水线：把需要真实 API、GPU 或外部数据的各项作业按固定顺序串起来。
# 每一步独立运行、可续跑，退出码写进状态文件。
#
#   bash scripts/run_pipeline.sh --dry-run     # 只打印步骤清单与预估调用数/成本，不执行
#   bash scripts/run_pipeline.sh               # 从步骤 0 开始按顺序跑
#   bash scripts/run_pipeline.sh --resume      # 从第一个没有成功过的步骤继续
#   bash scripts/run_pipeline.sh --status      # 只看每一步的上次结果，不执行
#   bash scripts/run_pipeline.sh --from 4      # 从步骤 4 开始，跑到最后
#   bash scripts/run_pipeline.sh --only 3      # 只跑步骤 3
#   bash scripts/run_pipeline.sh --yes         # 跳过人工确认（无人值守时用，慎用）
#
# 设计上的三条：
#   1. 一步失败不影响后面的步骤。步骤之间只有先后，没有连锁中止：药理层抽取失败时，
#      录制回放照样该跑。每一步开始和结束都打时间戳，结束时打退出码，最后汇总；
#      脚本不用 `set -e`。
#   2. 顺序按「依赖优先，其次成本」排：零调用的步骤先跑，免费能发现的问题先发现；
#      full_context 检验排在所有花钱的步骤之前，它决定后面按哪个默认检索模式、
#      哪套单价跑；药理层抽取在录制回放和评测之前（core/tools.py 读它落盘的数据）；
#      性能基准量的是前面各步完成之后的系统。
#   3. 三个人工确认点（步骤 4 role 填充率、步骤 6 抽取质量、步骤 11 蒸馏花费）
#      停下来等人确认：闸门没过就往下跑，后面的调用全部白花。
#
# 每一步的退出码写进状态文件（环境变量 PIPELINE_STATE，默认 out/pipeline_state.tsv）。
# `--resume` 读它，从第一个退出码不是 0 的步骤接着跑，已经成功的步骤不重跑。
# 长任务会打印进度与心跳（core/progress.py），超过心跳间隔仍无输出才是卡住了；
# 故障排查见 docs/DEPLOYMENT.md。

set -uo pipefail
cd "$(dirname "$0")/.."

# 单价不写在这里：两套单价（top3 均价、full_context 带前缀价）都由
# scripts/pipeline_plan.py 定义，bash 只负责问它。一个数抄在两处，改了一处另一处就会过期。

# 每一步的退出码，一行 `步骤号<TAB>退出码<TAB>结束时间`。
# 放在 out/（gitignore）下：它是本地这一次运行的状态，不是项目内容。
# 路径可以用环境变量覆盖，测试靠这个把它指到临时目录。
PIPELINE_STATE="${PIPELINE_STATE:-out/pipeline_state.tsv}"

# 记一步的结果。同一步重跑要覆盖旧记录而不是追加：追加的话 --resume 读到的是
# 最早那条（失败的那条），修好重跑成功之后还会再跑一遍。
record_step() {
  local n="$1" rc="$2"
  mkdir -p "$(dirname "$PIPELINE_STATE")"
  local tmp="${PIPELINE_STATE}.tmp"
  if [ -f "$PIPELINE_STATE" ]; then
    grep -v -P "^${n}\t" "$PIPELINE_STATE" > "$tmp" 2>/dev/null || true
  else
    : > "$tmp"
  fi
  printf '%s\t%s\t%s\n' "$n" "$rc" "$(date -Is)" >> "$tmp"
  sort -n -o "$PIPELINE_STATE" "$tmp"
  rm -f "$tmp"
}

# 步骤 3 的闸门结论（维持 full_context / 退回 hybrid）也写进状态文件：不落盘的话，
# 下一次 --resume 起来的步骤又会按 full_context 跑，而人早已决定退回。
#
# 存成一行 `#retriever_mode_decided\t<模式>\t<时间>`：`#` 开头，不会被 step_state
# 的 `^<步骤号>\t` 匹配到；只有三列步骤行的状态文件照常能读。
MODE_DECISION_KEY="#retriever_mode_decided"

record_mode_decision() {
  local mode="$1"
  mkdir -p "$(dirname "$PIPELINE_STATE")"
  local tmp="${PIPELINE_STATE}.tmp"
  if [ -f "$PIPELINE_STATE" ]; then
    grep -v -F "${MODE_DECISION_KEY}	" "$PIPELINE_STATE" > "$tmp" 2>/dev/null || true
  else
    : > "$tmp"
  fi
  printf '%s\t%s\t%s\n' "$MODE_DECISION_KEY" "$mode" "$(date -Is)" >> "$tmp"
  mv "$tmp" "$PIPELINE_STATE"
  echo ">>> 这个结论已写进 $PIPELINE_STATE：后面各步按 $mode 的口径估算成本。"
}

mode_decision() {
  [ -f "$PIPELINE_STATE" ] || return 0
  grep -F "${MODE_DECISION_KEY}	" "$PIPELINE_STATE" 2>/dev/null | tail -1 | cut -f2
}

# 某一步上次的退出码；没有记录时回 `none`。
step_state() {
  [ -f "$PIPELINE_STATE" ] || { echo none; return; }
  local line
  line=$(grep -P "^$1\t" "$PIPELINE_STATE" 2>/dev/null | tail -1) || true
  [ -n "$line" ] || { echo none; return; }
  echo "$line" | cut -f2
}

# --resume 的起点：第一个"上次不是退出码 0"的步骤（没有记录也算）。
# 全部是 0 时回一个比最大步骤号还大的数：那时 --resume 不跑任何步骤，
# 并且会明确说出来，而不是静默跑完 0 步。
first_unfinished_step() {
  while IFS='|' read -r n _ _ _ _ _; do
    [ "$(step_state "$n")" = "0" ] || { echo "$n"; return; }
  done < <(steps_in_order)
  echo 99
}

print_status() {
  echo "步骤 名称                     上次退出码  结束时间"
  echo "--------------------------------------------------------------------------"
  while IFS='|' read -r n _ name _ _ _; do
    local st line when
    st=$(step_state "$n")
    when="—"
    if [ -f "$PIPELINE_STATE" ]; then
      line=$(grep -P "^${n}\t" "$PIPELINE_STATE" 2>/dev/null | tail -1) || true
      [ -n "$line" ] && when=$(echo "$line" | cut -f3)
    fi
    case "$st" in
      none) st="未运行" ;;
      0) st="0（成功）" ;;
      10) st="10（人工中止）" ;;
      *) st="$st（失败）" ;;
    esac
    printf "%-4s %-24s %-11s %s\n" "$n" "$name" "$st" "$when"
  done < <(steps_in_order)
  echo "--------------------------------------------------------------------------"
  echo "状态文件：$PIPELINE_STATE"
  local decided
  decided=$(mode_decision)
  [ -n "$decided" ] && echo "步骤 3 的闸门结论：默认检索模式 = $decided（后面各步按它的口径估算成本）"
  local nxt
  nxt=$(first_unfinished_step)
  if [ "$nxt" = "99" ]; then
    echo "每一步都是退出码 0。--resume 不会跑任何步骤。"
  else
    echo "--resume 会从步骤 $nxt 开始。"
  fi
}

# 步骤号|检索模式|名称|预估调用数|人工确认|说明
#
# 表的顺序就是执行顺序，步骤号从 0 连续编号（--only / --from / --resume 与状态文件都按它寻址）。
# 步骤 3（full_context 检验）排在所有花钱的评测步骤之前：它决定默认检索模式是否成立，
# 闸门不通过要退回 hybrid，之后每一步的成本口径都随之改变。
#
# 检索模式每一步固定，不继承：不声明模式的步骤会跟着 effective_mode() 的默认值走，
# 而两套单价相差一个数量级以上，默认值一变，估算表却不会报错。映射和单价都在
# scripts/pipeline_plan.py。
#
# 「预估调用数」可以写 `auto:<文件>`：运行时由 scripts/pipeline_plan.py 从那份文件
# 读出（步骤 5 = eval/epsilon.json 三段 llm_calls 之和），不写死一个会过期的数。
STEPS=(
  "0|n/a|环境自检|0|no|零调用：pytest / ruff / 凭据核对 / 环境变量残留 / 数据文件是否齐全 / LLM_MODEL 是否在服务端模型清单里"
  "1|n/a|零调用验证|0|no|凭据机读 / 落盘证候表是否为当前代码的产物 / 本地语料规范化 / 切块验证（需人工查看原文）/ SDT 失分分析"
  "2|n/a|本地模型|2|no|启动 vLLM + verify_local_backend（直接调用 generate() 验证后端，不走检索层）"
  "3|full_context|full_context 检验|320|no|闸门：不通过则默认检索模式退回 hybrid，之后各步的成本口径随之改变。前缀规模（0 调用）+ 缓存命中率（2 次问诊）+ full_context 下的 E3/E4 + 字体子集化（0 调用）"
  "4|top3|role 填充率闸门|60|YES|不通过就停：填充率不够时，分层 ε 没有意义"
  "5|top3|噪声地板 ε|auto:eval/epsilon.json|no|预估调用数运行时从 eval/epsilon.json 读取（三段 llm_calls 之和）。固定 top3：eval/epsilon.json 在这一系下测得"
  "6|n/a|药理层抽取|2181|YES|预估 = 预过滤后保留的块数 + 每源 5 块试抽，以 verify_pharmacology_chunks 合计行的预估调用数为准，数据源变化后按它更新这一格。先 --limit-blocks 5 人工核对质量，再全量 --crosscheck。离线抽取，不走检索层"
  "7|top3|录制回放|278|no|record_fixtures（与其 --dry-run 打印的录制清单一致）+ verify_replay。产物与检索模式绑定（fixture 的键是 sha256(system)，full_context 下 system 含整份知识前缀），所以排在步骤 3 定下默认检索模式之后"
  "8|top3|评测|1200|no|最贵的一步：run_eval 四项 + SDT Test（写入台账）。固定 top3：E8 只遍历 TOP3_MODES，SDT 台账与 eval/RESULTS.md 的对照值都在这一系下测得"
  "9|top3|性能基准|38|no|bench_startup 冷/热各一次（0 调用）+ bench_consult 不开 ReAct ×3、开 ReAct ×1。固定 top3"
  "10|top3|消融|150|no|四组消融（A/B/C/D）在 10 条主诉上运行：每条 3+5+5+2=15 次 × 10 条 = 150，另加一次预热（不计入任何一组）。固定 top3：检索模式不是消融的变量，四组在同一模式下运行"
  "11|n/a|蒸馏（可选）|350|YES|可选，蒸馏数据只用于研究对照，不进在线服务；未设置 DISTILL_BASE_MODEL 时整步跳过。条数由 offline.distill_from_v4 --estimate 按预算上限计算"
)


# 步骤表，一行一步（格式同上），按执行顺序。
steps_in_order() {
  printf '%s\n' "${STEPS[@]}"
}

# 这一步要用哪个 RETRIEVER_MODE，并真的 export 出去。
#
# `n/a` 是 unset，不是"不管"：留着上一步的值，等于让一步本不受模式影响的作业
# 悄悄依赖上一步的设置。映射问 scripts/pipeline_plan.py（全项目一处），
# bash 这边不自己写 top3→hybrid。
apply_retriever_mode() {
  local step_mode="$1" want
  want=$(python3 -m scripts.pipeline_plan --retriever-mode "$step_mode") || {
    echo "步骤模式 $step_mode 解析失败，不跑这一步（按错的模式跑比不跑更贵）" >&2
    return 1
  }
  if [ -n "$want" ]; then
    export RETRIEVER_MODE="$want"
    echo ">>> 检索模式钉成 RETRIEVER_MODE=$want（步骤声明 $step_mode）"
  else
    unset RETRIEVER_MODE
    echo ">>> 这一步不走检索层（$step_mode），已清掉 RETRIEVER_MODE，不继承上一步"
  fi
}

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

DRY_RUN=0; FROM=0; ONLY=""; ASSUME_YES=0; RESUME=0; STATUS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --resume) RESUME=1 ;;
    --status) STATUS=1 ;;
    --from) FROM="${2:?--from 要一个步骤号}"; shift ;;
    --only) ONLY="${2:?--only 要一个步骤号}"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
  shift
done

# --resume 和 --from 一起传是自相矛盾的（一个说"读状态文件"，一个说"我指定"）。
# 直接拒绝而不是挑一个生效：挑一个生效的话，传错的人不会知道自己被忽略了。
if [ "$RESUME" = "1" ] && [ "$FROM" != "0" ]; then
  echo "--resume 和 --from 不能一起传：前者从状态文件算起点，后者是你指定起点。" >&2
  exit 2
fi
if [ "$RESUME" = "1" ] && [ -n "$ONLY" ]; then
  echo "--resume 和 --only 不能一起传。" >&2
  exit 2
fi

# 步骤表里「预估调用数」那一格 → 一个整数。`auto:<文件>` 交给 scripts/pipeline_plan.py
# 现读（bash 和 tests/test_run_pipeline.py 调的是同一个 resolve_calls）。
resolve_calls() {
  case "$1" in
    auto:*) python3 -m scripts.pipeline_plan --calls "$1" ;;
    *) echo "$1" ;;
  esac
}

# 峰谷分时的判定只有一处实现（core/usage.py::is_peak / peak_note），bash 不自己算时区：
# 时区或 UTC 偏移算错的表现是"按五折估的预算，实际按原价扣"。
peak_note() { python3 -c "from core.usage import peak_note; print(peak_note())"; }
is_peak() { python3 -c "import sys; from core.usage import is_peak; sys.exit(0 if is_peak() else 1)"; }

# 花钱的大头（药理层抽取、录制回放、评测、性能基准、full_context 检验、蒸馏）。
# 高峰时段启动它们只提醒、不阻止：有时就是得现在跑。
COSTLY_STEPS=" 3 6 7 8 9 11 "

warn_if_peak() {
  local n="$1"
  case "$COSTLY_STEPS" in
    *" $n "*) ;;
    *) return 0 ;;
  esac
  if is_peak; then
    printf '\033[33m>>> [步骤 %s] 现在是高峰时段，这一步花费较高。谷段（北京 12–14、18–09）五折。\033[0m\n' "$n"
    printf '\033[33m>>> 只提醒、不阻止：要等就 Ctrl-C，之后用 --resume 从这一步接着跑。\033[0m\n'
  fi
}

print_plan() {
  local total=0 total_yuan=0
  local -A calls_by_mode=() yuan_by_mode=() unit_by_mode=()
  local mode
  for mode in top3 full_context n/a; do
    unit_by_mode[$mode]=$(python3 -m scripts.pipeline_plan --unit-price "$mode")
  done
  echo "步骤 模式          名称                     预估调用  人工确认  说明"
  echo "--------------------------------------------------------------------------"
  while IFS='|' read -r n mode name calls gate note; do
    calls=$(resolve_calls "$calls")
    local yuan
    yuan=$(python3 -m scripts.pipeline_plan --cost "$mode" "$calls")
    total=$((total + calls))
    total_yuan=$(python3 -c "print(f'{$total_yuan + $yuan:.1f}')")
    calls_by_mode[$mode]=$(( ${calls_by_mode[$mode]:-0} + calls ))
    yuan_by_mode[$mode]=$(python3 -c "print(f'{${yuan_by_mode[$mode]:-0} + $yuan:.1f}')")
    printf "%-4s %-13s %-24s %8s  %-8s  %s\n" \
      "$n" "$mode" "$name" "$calls" "$gate" "$note"
  done < <(steps_in_order)
  echo "--------------------------------------------------------------------------"
  local ratio
  ratio=$(python3 -c "print(round(${unit_by_mode[full_context]} / ${unit_by_mode[top3]}))")
  echo "按检索模式分开算（两套单价相差约 ${ratio} 倍，只看总调用数估不出花费）："
  for mode in top3 full_context n/a; do
    local c="${calls_by_mode[$mode]:-0}" y="${yuan_by_mode[$mode]:-0}"
    printf "  %-13s %6s 次 × ¥%-8s ≈ ¥%s\n" "$mode" "$c" "${unit_by_mode[$mode]}" "$y"
  done
  echo "  合计 ${total} 次 ≈ ¥${total_yuan}（高峰价；谷段五折）"
  echo
  echo "单价来源：scripts/pipeline_plan.py（全项目一处）。top3 按录制回放那批调用的实际花费"
  echo "折算；full_context 是整份知识前缀（命中缓存）+ 一次输出，按 core/usage.py 的价目表算出。"
  echo "两者都是量级估算，不是账单。"
  echo
  echo "步骤 0/1/2 零调用或近零调用，先跑：免费能发现的问题先发现。"
  echo "步骤 3 检验 full_context 能否作为默认检索模式，闸门不通过就退回 hybrid，"
  echo "之后每一步的成本口径、步骤 7 录制的 fixture 都随之改变。"
  echo "步骤 6 药理层抽取必须在步骤 7 录制、步骤 8 评测之前（core/tools.py 读它落盘的数据）。"
  echo "步骤 9 性能基准量的是前面各步完成之后的系统；步骤 11（蒸馏）可以不做。"
  local decided
  decided=$(mode_decision)
  if [ -n "$decided" ]; then
    echo
    echo "已有步骤 3 的闸门结论：默认检索模式 = $decided。"
  else
    printf '\033[33m注意：状态文件里没有步骤 3 的闸门结论，现在跑步骤 7（录制）可能白录——\033[0m\n'
    printf '\033[33m  fixture 的键含 system 全文，检索模式一变就全部不命中。先跑步骤 3。\033[0m\n'
  fi
}

gate() {   # $1 = 步骤号, $2 = 要人确认什么
  if [ "$ASSUME_YES" = "1" ]; then
    echo ">>> [步骤 $1] 人工确认被 --yes 跳过：$2"
    return 0
  fi
  echo
  echo ">>> [步骤 $1] 人工确认：$2"
  echo ">>> 看完上面的输出，确认没问题再继续。输入 y 继续，其它任意键中止这一步。"
  read -r -p ">>> 继续？[y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

# SDT Test 的提交文件：步骤 8 写，步骤 1 的失分分析读，两处用同一个路径。
SDT_SUBMISSION="out/sdt_chain_v3.txt"

step_0() {
  # 全量测试分两遍跑。标了 real_embedding 的少数用例会真加载 sentence-transformers
  # 模型，跟其余用例放在同一个进程里，小内存机器上会被 OOM 杀掉（退出码 137，只留一个
  # Killed，看不出是哪条）。其余用例默认走假编码器；两遍之间 sleep 20，让上一遍的
  # 进程退干净、内存被回收。
  python -m pytest tests/ -q -m "not real_embedding" || return 1
  sleep 20
  python -m pytest tests/ -q -m real_embedding || return 1
  ruff check . || return 1
  python -m scripts.collect_results --check || return 1
  echo "--- 环境变量（期望：没有上次调试留下的残留）---"
  # 超时与 max_tokens 也要看：被设成很大的值时，一次挂死的调用要很久才会超时。
  env | grep -E "RETRIEVER_MODE|USE_REACT|EVAL_MODE|FAST_MODE|LLM_MODE|LORA_DIR|LLM_TIMEOUT|LLM_MAX_TOKENS" || echo "（干净）"
  echo "--- 数据文件 ---"
  for f in cases.json data/graph.json data/element_index.json data/case_triples.jsonl; do
    [ -e "$f" ] && echo "  ✓ $f" || echo "  ✗ $f 缺失（依赖它的步骤会失败）"
  done
  echo "--- LLM_MODEL 是否仍在服务端的模型清单里（查 /models，不是 LLM 调用，零成本）---"
  # 服务端不认识的模型名可能返回 HTTP 200 + 空响应体而不是 404：表现为每次调用返回
  # 空串 → 校验失败 → 重试耗尽 → LLMError，错误信息里看不出根因在模型名上。
  # 在产生任何花费之前把它挡掉。
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
    # 查不到不算失败：没网、没配 key、本地后端都会走到这里，而这一步是零成本自检，
    # 不该因为拿不到一份清单就挡住后面的步骤。
    print(f"  注意：查不到模型清单（{e}），跳过这一项。本地后端或没配 key 时属正常")
    raise SystemExit(0)
if want in names:
    print(f"  ✓ LLM_MODEL={want} 在服务端清单里（清单：{names}）")
    raise SystemExit(0)
print(f"  ✗ LLM_MODEL={want} 不在服务端清单里：{names}")
print("    服务端不认识的模型名可能返回 HTTP 200 + 空响应体，每次调用都会失败。"
      "改 .env 的 LLM_MODEL 再跑。")
raise SystemExit(1)
PY
}

step_1() {
  python -m scripts.collect_results || return 1
  echo
  echo "--- 落盘的证候表是否当前代码的产物（零调用，需要教材 markdown）---"
  echo "--- 退出码 3 = 拿不到教材，这一项没有核对，不等于核对通过 ---"
  python -m scripts.verify_generated_data
  rc_gen=$?
  if [ "$rc_gen" = "1" ]; then
    echo "落盘的证候表不是当前代码的产物。按上面的命令重新生成，图谱也要跟着重建。"
    return 1
  fi
  [ "$rc_gen" = "3" ] && echo "注意：这一项没有核对（没有教材 markdown）。clone TCM_Datasets 之后再跑一次。"
  echo
  echo "--- 本地语料规范化（幂等：第二遍没有事可做）---"
  python -m scripts.normalize_local_corpora || return 1
  echo
  echo "--- 切块验证：下面打出每个源预过滤后的前 3 块、被跳过的前 5 块原文，要人看 ---"
  echo "--- 最后一行「合计 … 真实抽取预估调用数 N」要跟本脚本步骤 6 的预估对得上 ---"
  python -m scripts.verify_pharmacology_chunks || return 1
  echo
  echo "--- 本地语料的切块验证：只看段落粒度，这三份都不进药理层抽取 ---"
  echo "--- （理由见 offline/local_corpora.py 的声明表；抽取引擎直接 --input 指到它们会被拒绝）---"
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
  echo "--- 输入 $SDT_SUBMISSION 是步骤 8 的产物，第一次跑必然跳过；"
  echo "--- 步骤 8 跑过一次之后这一项才有东西可分析。看到「跳过」不是配置错了。---"
  if [ -n "${SDT:-}" ] && [ -f "$SDT_SUBMISSION" ]; then
    python -m eval.sdt.run --sdt-dir "$SDT" --split Test --error-analysis "$SDT_SUBMISSION"
  else
    echo "跳过：需要 \$SDT 和 $SDT_SUBMISSION（步骤 8 的 SDT Test 跑过才有这个文件）"
  fi
}

step_2() {
  bash scripts/start_vllm.sh &
  echo "vLLM 在后台启动（日志位置见 scripts/start_vllm.sh）。等它就绪之后再验证——"
  echo "加载权重要几分钟，这段时间没有输出属正常：看日志文件的修改时间判断进展，不要 kill。"
  python -m scripts.verify_local_backend
}

step_3() {
  # 检验 full_context 能否作为默认检索模式。排在所有花钱的评测步骤之前：
  # ②③ 决定默认配置是否成立，只需要 cases.json 和真实 key，不依赖步骤 6~8 的产物；
  # 闸门不通过要退回 hybrid，越早知道越好，否则后面的调用都按一个不成立的默认配置运行。
  #
  # 这一步的 E3/E4 与步骤 8 的数不可比：检索模式、采样次数与推理档位都与 top3 系不同。
  # 结果在 eval/RESULTS.md 里单列一行（full_context 系），不覆盖 top3 系的表。
  echo "--- ① 前缀真实规模（0 调用）。判据：每位医家各自 ≤ 500K token，且没有章节被裁掉 ---"
  python -m core.context_prefix --report || return 1

  echo "--- ② 前缀缓存命中率。判据：第二次 ≥ 0.9（第一次必然接近 0，属正常） ---"
  python -m scripts.bench_consult --backend real --repeat 2 || return 1

  echo "--- ③ full_context 下的 E3/E4。判据：两个 change_rate 各自不低于闸门值 ---"
  echo "--- 跟 top3 系的 E3/E4（步骤 8）并列报，不相减：两者换掉的量不同 ---"
  RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4 || return 1
  # 闸门没过就立即停，并给出退路：full_context 当默认检索模式的前提是"全量语料让模型
  # 更认得出这是谁的医案"，这个前提由 E3/E4 闸门担保。阈值问 eval/run_eval.py 的
  # GATE_OUTPUT_CHANGE_RATE（全项目一处），这里只读报告、给退路。
  python - <<'GATE_PY' || return 1
import json, sys
from pathlib import Path
from eval.run_eval import GATE_OUTPUT_CHANGE_RATE as GATE

bad = []
for name, key in (("report_e3.json", "e3"), ("report_e4.json", "e4")):
    path = Path("eval") / name
    if not path.exists():
        bad.append(f"{name} 不在（这一步没跑完）")
        continue
    rate = (json.loads(path.read_text(encoding="utf-8")).get(key) or {}).get("change_rate")
    if rate is None:
        bad.append(f"{key}: change_rate 是 null（可用样本两侧检索全为空，闸门无法判定）")
    elif rate < GATE:
        bad.append(f"{key}: change_rate {rate:.3f} < {GATE}")
if bad:
    print("闸门未通过：" + "；".join(bad), file=sys.stderr)
    print("默认检索模式退回 hybrid：export RETRIEVER_MODE=hybrid", file=sys.stderr)
    print("（退回之后以 top3 系的数为准；full_context 系那一行写明闸门未通过）",
          file=sys.stderr)
    sys.exit(1)
print(f"E3/E4 两个 change_rate 都 ≥ {GATE}，full_context 可以继续当默认。")
GATE_PY

  echo "--- ④ 字体子集化（0 调用；原始字体随仓库提供，缺失时 --download 联网获取）。判据：运行时自检的「离线字体」一项为 ok ---"
  python -m scripts.subset_fonts --check-deps || {
    echo "（缺 fonttools：pip install \"fonttools[woff]\" brotli，然后重跑这一步）"
    return 1
  }
  python -m scripts.subset_fonts --download || return 1
  python -m scripts.subset_fonts || return 1
  echo "--- 四个子集由 web/app.css 与 web/product/app.css 的 @font-face 引用；运行时自检 ---"
  python -m scripts.preflight_runtime || true
}

step_4() {
  python -m scripts.verify_role_fill
}

step_5() {
  # 默认设置（S3_THINKING=enabled）那一套，写 eval/epsilon.json。
  python -m offline.estimate_epsilon --n-repeats 3 || return 1
  echo "--- 判据：ε_core < ε_online < ε_adjunct（不成立就如实报，不调参去凑）---"
  python -m scripts.collect_results | sed -n '/ε 分层/,/^$/p'
  # 再跑一套关掉思考的，写 eval/epsilon_s3_disabled.json。两套设置的 ε 不可比，
  # 所以是并列的两个文件，而不是同一个文件覆盖一次；有了这一套，才能回答
  # "思考模式多花的时间值不值"。这一套失败不让本步算失败：默认那一套已经落盘。
  echo "--- 再跑一套：S3_THINKING=disabled（并列对照，不覆盖上面那套）---"
  S3_THINKING=disabled python -m offline.estimate_epsilon --n-repeats 3 || \
    echo "（disabled 那一套没跑成。默认那一套已落盘，步骤 5 不因此算失败。）"
}

step_6() {
  echo "--- 先每个源抽 5 块看质量（这一步的输出要人逐条看）---"
  # 走批量入口：六个源的 --input/--source/--book 取自
  # offline/pharmacology_sources.EXPECTED_SOURCES，不在这里逐个手写。
  python -m scripts.run_pharmacology_extraction --dry-run || return 1
  python -m scripts.run_pharmacology_extraction --limit-blocks 5
}

step_6b() {
  python -m scripts.run_pharmacology_extraction --crosscheck
}

step_7() {
  python -m scripts.record_fixtures || return 1
  python -m scripts.verify_replay
}

step_8() {
  python -m eval.run_eval --queries-path tests/queries.txt --e3 --e4 --e8 --e9 || return 1
  echo "--- 把上面重跑出的数回写进 eval/RESULTS.md 的凭据记号，然后核对 ---"
  python -m scripts.collect_results --check
  echo
  echo "--- SDT Test：只在最终定型后跑，会写台账 eval/sdt/test_run_log.jsonl ---"
  if [ -n "${SDT:-}" ]; then
    python -m eval.sdt.run --sdt-dir "$SDT" --split Test --solver chain --out "$SDT_SUBMISSION"
  else
    echo "跳过：需要 \$SDT"
  fi
}

step_9() {
  # 排在药理层抽取之后：core/tools.py 读步骤 6 落盘的药理层数据，开 ReAct 的那一次
  # 基准如果在它之前跑，量到的是"工具返回数据文件不存在"的耗时，不是真实形态。
  echo "--- 启动耗时：冷（第一个进程） ---"
  python -m scripts.bench_startup || return 1
  echo "--- 启动耗时：热（第二个进程，应命中 embedding 磁盘缓存） ---"
  python -m scripts.bench_startup || return 1
  echo "--- 一次问诊，不开 ReAct ×3（预算 ≤ 90s，见 docs/DESIGN.md「性能预算」） ---"
  python -m scripts.bench_consult --backend real --repeat 3 --no-react || return 1
  echo "--- 一次问诊，开 ReAct ×1（预算 ≤ 240s） ---"
  python -m scripts.bench_consult --backend real --repeat 1 --react
}

step_10() {
  # 四组消融：每组只动一个开关（S3_MODE / S3_BEST_OF_N / S1S2_MERGED），其余是产品默认。
  # 假后端跑不出内容指标（见 eval/ablation/knobs.py），所以先用假后端验管道，再用真实后端出数。
  echo "--- ① 先用假后端验一遍管道（0 调用）。判据：四组的调用数是 3/5/5/2 ---"
  python -m eval.ablation --backend fake --limit 2 \
    --out eval/bench/ablation_fake.json || return 1

  echo "--- ② 真跑四组。判据：四组都有 n_ok == n_queries，且 A 组的三项指标不是 None ---"
  python -m eval.ablation --backend real --queries-path tests/queries.txt \
    --out eval/report_ablation.json || return 1
}

step_11() {
  # 从 deepseek-v4-pro 蒸馏推理链样本，再用 LoRA 训一个本地模型做研究对照。
  # 这一步可以整步不做：蒸馏结果不进在线服务。
  #
  # 规模由预算决定：蒸馏脚本默认按预算上限计算条数，不传 --limit 就不会超预算。
  if [ -z "${DISTILL_BASE_MODEL:-}" ]; then
    echo "跳过：没有设置 DISTILL_BASE_MODEL（LoRA 基座的 Hugging Face 仓库 id）。"
    echo "要跑这一步：DISTILL_BASE_MODEL=<仓库 id> bash scripts/run_pipeline.sh --only 11"
    return 0
  fi
  echo "--- ① 先估钱（0 调用）。判据：打印出的条数与花费跟打算花的一致 ---"
  python -m offline.distill_from_v4 --estimate || return 1
  gate 11 "上面估出的条数与花费可以接受吗？确认后按这个规模真跑（会花钱）" || {
    echo "[步骤 11] 人工中止，没有真跑"
    return 10
  }

  echo "--- ② 真跑。超过确认线的花费靠 --yes-spend 放行，上面的人工确认就是这一步的授权 ---"
  python -m offline.distill_from_v4 --yes-spend || return 1

  echo "--- ③ 训练计划（0 调用、不加载模型）。判据：train 条数不是 0 ---"
  python -m scripts.train_lora --data distill_v4 --base-model "$DISTILL_BASE_MODEL" --dry-run || return 1

  echo "--- ④ 训练依赖（真训要 GPU）---"
  python -m scripts.train_lora --data distill_v4 --base-model "$DISTILL_BASE_MODEL" --check-deps || {
    echo "（缺训练依赖：pip install -r requirements-train.txt，然后重跑这一步）"
    return 1
  }
  echo "    真训：python -m scripts.train_lora --data distill_v4 --base-model \"\$DISTILL_BASE_MODEL\""
  echo "    判据：heldout loss 不比 train 高出 20%"
  echo "--- ⑤ 蒸馏模型只跑一次 SDT Test 作为旁证，跟 deepseek-v4-pro 并列报、不覆盖 ---"
  echo "    命令：LLM_MODE=local_inproc LLM_MODEL_PATH=<基座权重目录> LORA_DIR=<train_lora 打印的目录> \\"
  echo "          python -m eval.sdt.run --sdt-dir \"\$SDT\" --split Test --solver chain --out out/sdt_chain_distill.txt"
  echo "    判据：台账 eval/sdt/test_run_log.jsonl 多一行，且 eval/RESULTS.md 那一行标明是蒸馏模型"
}

run_step() {
  local n="$1" name="$2" step_mode="${3:-n/a}"
  echo
  echo "=========================================================================="
  echo "[步骤 $n] $name    开始 $(date -Is)"
  echo "=========================================================================="
  warn_if_peak "$n"
  # 每一步自己钉模式，不看上一步留下什么。钉不上（步骤表里写了个不存在的模式）就不跑
  # 这一步，但照常记下非 0 退出码并进汇总：静默早退的话，汇总里看不到这一步，
  # 状态文件里也没有它失败过的记录。
  local mode_rc=0
  apply_retriever_mode "$step_mode" || mode_rc=$?
  local decided
  decided=$(mode_decision)
  if [ -n "$decided" ]; then
    echo ">>> 步骤 3 的闸门已有结论：默认检索模式 = $decided（本步按 $step_mode 的口径估算成本）"
  fi
  local rc=$mode_rc
  [ "$mode_rc" = "0" ] && case "$n" in
    0) step_0 || rc=$? ;;
    1) step_1 || rc=$? ;;
    2) step_2 || rc=$? ;;
    3) step_3 || rc=$?
       # 闸门的结论落盘：过了就维持 full_context，没过就退回 hybrid。
       if [ "$rc" = "0" ]; then
         record_mode_decision full_context
       else
         record_mode_decision hybrid
         echo ">>> 步骤 3 闸门未通过：后面各步按 top3/hybrid 的口径估算成本，"
         echo ">>>   且步骤 7 若已在 full_context 下录过 fixture，要重录（键含 system 全文）。"
       fi ;;
    4) step_4 || rc=$?
       if [ "$rc" = "0" ]; then
         gate 4 "role 填充率过 90% 的闸门了吗？不过就停：分层 ε 的三个数建立在它上面" \
           || { echo "[步骤 4] 人工中止"; rc=10; }
       fi ;;
    5) step_5 || rc=$? ;;
    6) step_6 || rc=$?
       if [ "$rc" = "0" ]; then
         if gate 6 "上面 5 条抽出来的三元组，s（药名）对得上原文吗？o 和 source_span 呢？"; then
           step_6b || rc=$?
         else
           echo "[步骤 6] 人工中止，没有跑全量"; rc=10
         fi
       fi ;;
    7) step_7 || rc=$? ;;
    8) step_8 || rc=$? ;;
    9) step_9 || rc=$? ;;
    10) step_10 || rc=$? ;;
    11) step_11 || rc=$? ;;
  esac
  echo "--------------------------------------------------------------------------"
  echo "[步骤 $n] $name    结束 $(date -Is)    退出码 $rc"
  RESULTS+=("$n|$name|$rc")
  # 每一步结束就落盘，而不是在汇总时统一写：容器被回收、终端被关掉时，已经跑完的
  # 步骤仍然有记录——这正是 --resume 要应对的场景。
  record_step "$n" "$rc"
  return 0   # 一步失败不影响后面的步骤，见文件头第 1 条
}

if [ "$STATUS" = "1" ]; then
  print_status
  exit 0
fi

echo "$(peak_note)"
echo
print_plan

# --resume 的起点在 --dry-run 退出之前算：`--resume --dry-run` 要能回答
# "会从哪一步开始"，这正是开跑前最想知道的一件事。
if [ "$RESUME" = "1" ]; then
  FROM=$(first_unfinished_step)
  echo
  print_status
  if [ "$FROM" = "99" ]; then
    # 明确说出来：每一步都成功过时 --resume 不跑任何步骤，静默退出会被当成"跑完了"。
    echo
    echo "--resume：没有需要续跑的步骤。要重跑某一步用 --only <步骤号>。"
    exit 0
  fi
  echo
  echo "--resume：从步骤 $FROM 开始（它之前的步骤上次都是退出码 0，不重跑）。"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "--dry-run：什么都没跑。去掉这个参数即开始执行。"
  exit 0
fi

# `--from N`：从步骤 N 开始，跑到最后。
if [ "$FROM" != "0" ] && ! steps_in_order | cut -d'|' -f1 | grep -qx "$FROM"; then
  echo "--from $FROM：步骤表里没有这个步骤号。" >&2
  exit 2
fi

# 步骤表从文件描述符 3 读，而不是标准输入：循环体里的人工确认（gate 里的 read）
# 要读的是终端，读标准输入的话会把步骤表的下一行当成回答吃掉，下一步也随之被跳过。
RESULTS=()
while IFS='|' read -r -u 3 n step_mode name calls gate_flag note; do
  if [ -n "$ONLY" ]; then
    [ "$n" = "$ONLY" ] || continue
  elif [ "$n" -lt "$FROM" ]; then
    continue
  fi
  run_step "$n" "$name" "$step_mode"
done 3< <(steps_in_order)

echo
echo "=========================================================================="
echo "汇总（退出码 0 = 这一步自己跑完了；10 = 人工在确认点中止）"
echo "=========================================================================="
failed=0
for r in "${RESULTS[@]}"; do
  IFS='|' read -r n name rc <<< "$r"
  printf "  步骤 %-2s %-24s 退出码 %s\n" "$n" "$name" "$rc"
  [ "$rc" = "0" ] || failed=1
done
if [ "$failed" = "0" ]; then
  echo "全部步骤退出码 0。"
else
  echo "有步骤没跑成。按 docs/DEPLOYMENT.md 的故障排查修掉，然后："
  echo "    bash scripts/run_pipeline.sh --resume"
  echo "它从第一个没成功的步骤接着跑，已经成功的步骤不重跑。"
  echo "状态记在 $PIPELINE_STATE，用 --status 能单独看。"
fi
exit "$failed"
