"""LLM 抽象层：prompt 加载/渲染 + 后端封装。所有 LLM 调用必须经过这里，禁止在业务代码里直接 import openai。"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from string import Template
from typing import TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def load_prompt(name: str, version: str = "v1") -> dict:
    """读 prompts/{version}/{name}.yaml，返回 {system, notes} 字典。"""
    path = PROMPTS_ROOT / version / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template_str: str, **kwargs) -> str:
    """用 string.Template 渲染。用 safe_substitute 而不是 substitute：
    prompt 里常有 $var 没被传入的情况（比如可选段落），缺变量时不该抛异常。"""
    return Template(template_str).safe_substitute(**kwargs)


def strip_code_fence(text: str) -> str:
    """剥离 LLM 返回里可能带的 markdown 围栏（```json ... ``` 或 ``` ... ```）。
    做成模块级函数是为了能单独单元测试，不依赖网络。"""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


class LLMError(RuntimeError):
    """LLM 调用在重试耗尽后仍失败时抛出，携带足够定位问题的上下文。"""


class LLMBackend(ABC):
    @abstractmethod
    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        **kwargs,
    ) -> T:
        """给定 system/user 提示与目标 pydantic 模型，返回校验通过的模型实例。"""
        raise NotImplementedError


class OpenAICompatBackend(LLMBackend):
    """走 OpenAI 兼容接口（DeepSeek 等）。惰性创建 client，避免模块加载时就要求
    环境变量齐全（测试环境可能没有 LLM_API_KEY）。"""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=os.environ.get("LLM_API_KEY"),
                base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            )
        return self._client

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        **kwargs,
    ) -> T:
        model = os.environ.get("LLM_MODEL", "deepseek-chat")
        schema_hint = (
            f"\n\n你的回答必须是且只能是一个符合以下 JSON Schema 的 JSON 对象，"
            f"不要输出任何解释、前后缀或 markdown 围栏，只输出 JSON 本身：\n"
            f"{schema.model_json_schema()}"
        )
        full_system = system + schema_hint

        messages = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        last_raw = ""
        max_attempts = 3  # 首次 + 最多 2 次重试
        for attempt in range(max_attempts):
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    **kwargs,
                )
                raw = resp.choices[0].message.content or ""
                last_raw = raw
                cleaned = strip_code_fence(raw)
                return schema.model_validate_json(cleaned)
            except Exception as e:  # noqa: BLE001 - 需要捕获校验错误与网络错误统一重试
                last_error = e
                if attempt < max_attempts - 1:
                    # 第 2 次起把上次的校验错误回灌给模型，要求修正后重新输出。
                    messages.append({"role": "assistant", "content": last_raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"上一次输出未通过校验，错误信息：{e}\n"
                                "请修正后重新输出一个符合 schema 的 JSON 对象，"
                                "不要输出解释或围栏。"
                            ),
                        }
                    )

        raise LLMError(
            f"LLM 调用在 {max_attempts} 次尝试后仍失败。"
            f"schema={schema.__name__}, 最后错误={last_error}, "
            f"最后原始返回前 500 字={last_raw[:500]!r}"
        )


class VLLMBackend(LLMBackend):
    """正式阶段本地部署时启用，需核对 vLLM 版本 API；支持 guided_decoding 与 LoRA 热切换。

    demo 阶段不要求能跑通——这里只占位声明接口形状，让 get_backend() 的分支
    完整、将来切换时不用改调用方。真正接入时大致是：
      - 用 vllm.LLM 或 vllm 的 OpenAI 兼容 server（走 OpenAICompatBackend 复用即可）
      - guided_decoding 传 schema.model_json_schema() 做结构化约束，替代
        OpenAICompatBackend 里"提示词里塞 schema + json_object"的弱约束方式
      - LoRA 热切换通过 vllm 的 lora_request 参数，按 physician 选择不同 LoRA_DIR
    """

    def __init__(self) -> None:
        self._model_path = os.environ.get("LLM_MODEL_PATH")
        self._lora_dir = os.environ.get("LORA_DIR")

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        **kwargs,
    ) -> T:
        raise NotImplementedError(
            "VLLMBackend 尚未实现，正式阶段本地部署时补全（见类注释）。"
        )


def get_backend() -> LLMBackend:
    """按 LLM_MODE 环境变量返回后端实例，默认 api。"""
    mode = os.environ.get("LLM_MODE", "api")
    if mode == "local":
        return VLLMBackend()
    return OpenAICompatBackend()


_llm_singleton: LLMBackend | None = None


def get_llm() -> LLMBackend:
    """惰性单例。模块底部不创建全局实例，避免模块导入时就要求环境变量齐全。"""
    global _llm_singleton
    if _llm_singleton is None:
        _llm_singleton = get_backend()
    return _llm_singleton
