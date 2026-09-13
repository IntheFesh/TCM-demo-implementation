"""在**装了 vllm 的机器上**验证本地后端真的接上了。沙盒里跑不了（没有 vllm、
没有权重），所以做成脚本 + 退出码，不是 pytest 用例。

    export LLM_MODE=local            # 或 local_inproc
    export LLM_MODEL=tcm-local       # server 模式：跟 --served-model-name 一致
    export LLM_MODEL_PATH=/root/autodl-tmp/models/Qwen2.5-1.5B-Instruct
    export LORA_DIR=/root/autodl-tmp/lora      # 可选
    python -m scripts.verify_local_backend

退出码：0 = 全部通过；1 = 有闸门没过；2 = 配置不全（连试都试不了）。

    python -m scripts.verify_local_backend --show-prompt-budget

只算 prompt 的 token 预算、不发任何请求——scripts/start_vllm.sh 里
--max-model-len 的取值就是从这个输出推出来的，换了 prompt 之后重算一遍，
别让脚本里那个数字跟实际 prompt 悄悄脱节。

三道闸门：
  1. **能拿到合法输出**：一次 generate() 返回的对象通过 pydantic 校验。
  2. **guided_decoding 真的生效**：这次 generate() 只调用了后端一次。
     guided_json 生效时输出必然合法，不该触发 generate() 的重试；一旦重试
     说明那个键没被 vLLM 认（版本差异），是配置问题，必须暴露出来而不是
     "反正重试之后也能成"地静默退化。
  3. **LoRA 配置自洽**：LORA_DIR 设了的话，每位已注册医家都要有 adapter
     目录。复用 core/llm.py 的 _resolve_lora_path，不在这里另写一套判断。
"""
from __future__ import annotations

import argparse
import os
import sys

from pydantic import BaseModel, Field

from core.llm import (
    LLMError,
    VLLM_GUIDED_JSON_KEY,
    _resolve_lora_path,
    get_backend,
    load_prompt,
)
from core.physicians import PHYSICIANS
from core.react import MAX_STEPS
from core.schemas import S3Syndrome

LOCAL_MODES = ("local", "local_inproc")


class _Probe(BaseModel):
    """探针 schema：字段少，但**每个字段都有约束**——全字段可选的 schema
    连"模型有没有照 schema 走"都测不出来（随便一个 {} 都合法）。"""

    syndrome: str = Field(min_length=1)
    confidence: int = Field(ge=0, le=100)


def _estimate_tokens(text: str) -> int:
    """中文按 1 token/字、其余按 4 字符/token。**这是上界估计**：真实
    tokenizer 上中文常能压到 1.5-2 字/token，所以按这个数配 max-model-len
    偏保守、不会偏小。拿不到真实 tokenizer 时（沙盒没有权重）也能算。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4


def show_prompt_budget() -> int:
    """算出 max-model-len 该给多少，逐项列出来源。不发请求。"""
    s3_tpl = load_prompt("s3_syndrome")["system"]
    react_tpl = load_prompt("s3_react")["system"]
    schema_hint = str(S3Syndrome.model_json_schema())

    # core/react.py 把每步 observation 截断到这个长度才放进 history
    from core.react import MAX_OBSERVATION_CHARS

    rows = [
        ("s3_syndrome 模板", _estimate_tokens(s3_tpl)),
        ("generate() 拼进 system 的 schema 全文", _estimate_tokens(schema_hint)),
        ("三条参考医案（raw_excerpt 各约 300 字）", 900),
    ]
    s3_input = sum(t for _, t in rows)
    s3_output = 1500  # 带三个候选方的输出，实测量级
    react_history = MAX_STEPS * (MAX_OBSERVATION_CHARS + 120)  # observation + thought
    react_prompt = _estimate_tokens(react_tpl) + react_history + 300
    retry_overhead = s3_output + 120  # 每次重试回灌上轮输出 + 校验错误
    worst = s3_input + react_history // 2 + s3_output + 2 * retry_overhead

    print("prompt token 预算（中文 1:1、ASCII 4:1，上界估计）：")
    for label, tokens in rows:
        print(f"  {label:44s} ≈ {tokens:6d}")
    print(f"  {'S3 输入合计':44s} ≈ {s3_input:6d}")
    print(f"  {'S3 输出（三个候选方）':44s} ≈ {s3_output:6d}")
    print(f"  {'ReAct 最后一步的 prompt（MAX_STEPS=' + str(MAX_STEPS) + '）':44s} ≈ {react_prompt:6d}")
    print(f"  {'每次重试额外追加':44s} ≈ {retry_overhead:6d}")
    print(f"\n最坏路径（ReAct trace + S3 + 2 次重试）≈ {worst} tokens")
    print(f"→ --max-model-len 建议取 {1 << (worst - 1).bit_length()}"
          f"（{worst} 之上最近的 2 的幂，约 "
          f"{(1 << (worst - 1).bit_length()) / worst - 1:.0%} 余量）")
    print("  scripts/start_vllm.sh 里的值应当跟这一行一致；改了 prompt 就重算。")
    return 0


def _check_lora() -> list[str]:
    """LORA_DIR 设了就逐个医家查 adapter 目录。返回问题清单（空 = 通过）。"""
    lora_dir = os.environ.get("LORA_DIR")
    if not lora_dir:
        print("LoRA：LORA_DIR 未设置 → 跑基座模型（阶段五的 adapter 还没训时就是这样）")
        return []
    problems = []
    for pid in PHYSICIANS:
        try:
            path = _resolve_lora_path(lora_dir, pid)
        except LLMError as e:
            problems.append(str(e))
            continue
        print(f"LoRA：{pid} → {path}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验证本地 vLLM 后端真的能跑通")
    ap.add_argument("--show-prompt-budget", action="store_true",
                    help="只算 max-model-len 的 token 预算，不发请求")
    ap.add_argument("--physician", default=None,
                    help="用这位医家的 id 试一次带 LoRA 的调用（默认不带）")
    args = ap.parse_args(argv)

    if args.show_prompt_budget:
        return show_prompt_budget()

    mode = os.environ.get("LLM_MODE", "api")
    if mode not in LOCAL_MODES:
        print(f"LLM_MODE={mode!r} 不是本地模式。设成 {' 或 '.join(LOCAL_MODES)} 再跑。",
              file=sys.stderr)
        return 2

    backend = get_backend()
    print(f"后端：{backend.backend_id()}  模型：{backend.model_name()}")
    print(f"guided_decoding 参数名：{VLLM_GUIDED_JSON_KEY}"
          f"（VLLM_GUIDED_JSON_KEY 可覆盖，用于 vLLM 版本差异）")
    print(f"comparability_warning：{backend.comparability_warning()}")

    failures: list[str] = []
    failures += _check_lora()

    # 数一下 generate() 到底调了后端几次：闸门 2 要的就是这个数。
    calls = {"n": 0}
    original = backend._complete

    def counting_complete(*a, **kw):
        calls["n"] += 1
        return original(*a, **kw)

    backend._complete = counting_complete  # type: ignore[method-assign]

    print("\n发一次真实调用……")
    try:
        out = backend.generate(
            system="你是一个测试探针。给出任意一个证型名和 0-100 的置信度。",
            user="", schema=_Probe, physician=args.physician,
        )
    except LLMError as e:
        print(f"❌ 闸门 1（能拿到合法输出）未过：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"✅ 闸门 1：拿到合法输出 {out!r}")

    if calls["n"] == 1:
        print("✅ 闸门 2：一次调用就通过，guided_decoding 生效")
    else:
        failures.append(
            f"闸门 2 未过：generate() 调了后端 {calls['n']} 次才拿到合法输出。"
            f"guided_json（当前键名 {VLLM_GUIDED_JSON_KEY!r}）生效时输出必然合法、"
            "不该触发重试——这说明这个键没被 vLLM 认（版本差异）。"
            "核对 vLLM 版本的 extra_body 参数名，用 VLLM_GUIDED_JSON_KEY 改掉，"
            "不要因为「重试之后也能成」就放过：那等于白花两次调用，"
            "而且本地模型相对云端 API 的这个优势根本没用上。"
        )

    if failures:
        print("\n以下闸门没过：", file=sys.stderr)
        for f in failures:
            print(f"  ❌ {f}", file=sys.stderr)
        return 1
    print("\n全部闸门通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
