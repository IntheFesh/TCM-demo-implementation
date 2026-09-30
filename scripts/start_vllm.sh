#!/usr/bin/env bash
# 起 vLLM 的 OpenAI 兼容 server，供 LLM_MODE=local 使用。
#
#   bash scripts/start_vllm.sh
#
# 环境变量（跟 core/llm.py::VLLMBackend 读的是同一套，不要在这里另起一套名字）：
#   LLM_MODEL_PATH  必填，本地权重目录
#   LORA_DIR        可选，每位医家一个 adapter：$LORA_DIR/<physician_id>/
#   VLLM_PORT       可选，默认 8000（客户端那边是 LLM_BASE_URL）
#
# 起完之后客户端这样接：
#   export LLM_MODE=local
#   export LLM_MODEL=tcm-local                  # 跟下面 --served-model-name 一致
#   export LLM_MODEL_PATH=$LLM_MODEL_PATH        # manifest 记的是这个（权重路径）
#   python -m scripts.verify_local_backend       # 退出码 0 = 真的接上了
set -euo pipefail

: "${LLM_MODEL_PATH:?需要 LLM_MODEL_PATH 指向本地权重目录}"
PORT="${VLLM_PORT:-8000}"
SERVED_NAME="${LLM_MODEL:-tcm-local}"

# ---- 参数取值的依据 ----
#
# --max-model-len 16384：由
# `python -m scripts.verify_local_backend --show-prompt-budget` 现算（中文按
# 1 token/字、ASCII 按 4 字符/token 估，是上界）。它逐项列出 S3 模板、generate()
# 拼进 system 的 schema 全文、参考医案和 S3 输出的 token 数，再算最坏路径：
# 开 ReAct 之后 history 每步追加一条截断到 MAX_OBSERVATION_CHARS 的 observation
# （最多 MAX_STEPS 步），再叠上 generate() 的两次重试（每次把上轮输出和校验错误
# 一起回灌）。上下文长度不够时，"开了 ReAct 且触发重试"这条路径会被截断，而
# 截断的表现是 LLMTruncatedError 直接放弃这条主诉，不是慢一点。取值是最坏路径
# 之上最近的 2 的幂，留出余量；真实 tokenizer 上的数会比这个估算小（中文 1:1 是
# 上界），所以这是保守值。prompt 改了要重算——tests/test_verify_local_backend.py
# 核对这里的值与现算结果一致。
#
# --gpu-memory-utilization 0.85：给激活值和碎片留出约 15% 的显存。取值太高时，
#   长 prompt 碰上 KV cache 峰值可能 OOM。这个数跟显存大小有关、不是通用常量，
#   换显卡或换模型要重新试。
#
# --max-num-seqs 16：并发请求数上限。api/main.py 的 MAX_CONCURRENT_CONSULTS
#   默认 4，每次问诊最多有 len(physicians_enabled()) 位医家并行（当前是 3 位），
#   4×3=12，取 16 留余量。配大了不会更快（受显存和 KV cache 限制），只会在排队时
#   占更多显存。
ARGS=(
  --model "$LLM_MODEL_PATH"
  --served-model-name "$SERVED_NAME"
  --port "$PORT"
  --max-model-len 16384
  --gpu-memory-utilization 0.85
  --max-num-seqs 16
)

# LoRA：每位医家一个 adapter。--enable-lora 必须在启动时就给，之后不能
# 改；--lora-modules 把每个 adapter 注册成 <physician_id>=<路径>，客户端按请求
# 用 lora_request.lora_name 选。这里从 core/physicians.py 的注册表现读医家 id，
# 不在脚本里手抄一份清单——手抄的那份会在增减医家时漏改
# （docs/ARCHITECTURE.md §4：同一概念只有一处实现）。
if [[ -n "${LORA_DIR:-}" ]]; then
  ARGS+=(--enable-lora)
  LORA_MODULES=()
  while IFS= read -r pid; do
    if [[ -d "$LORA_DIR/$pid" ]]; then
      LORA_MODULES+=("$pid=$LORA_DIR/$pid")
    else
      # 不静默跳过：报错退出。缺一个 adapter 而 server 照样起来，结果就是
      # 那位医家的请求要么报错要么静默走基座——后者会让"这位医家用的是他
      # 自己的 LoRA"这句声称变成假的（跟 core/llm.py::_resolve_lora_path
      # 拒绝静默退化是同一条理由）。
      echo "错误：LORA_DIR=$LORA_DIR 里没有医家 $pid 的 adapter 目录（$LORA_DIR/$pid）。" >&2
      echo "      要么把它训好放进去，要么不要设 LORA_DIR（明确表示本次跑基座模型）。" >&2
      exit 2
    fi
  done < <(python -c "from core.physicians import PHYSICIANS; print('\n'.join(PHYSICIANS))")
  ARGS+=(--lora-modules "${LORA_MODULES[@]}")
fi

echo "启动 vLLM：模型=$LLM_MODEL_PATH  served-name=$SERVED_NAME  端口=$PORT"
echo "LoRA：${LORA_DIR:-未启用}"
exec python -m vllm.entrypoints.openai.api_server "${ARGS[@]}"
