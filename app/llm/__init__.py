# -*- coding: utf-8 -*-
"""
LLM 调用层（项目自定义）

设计目标：
- 任意 base_url 可用，且绝不私自改写/重写用户提供的 base_url
- 支持三种模式：openai / gemini_native / gemini_openai
- 提供健壮的 URL 拼接、可观测的非 JSON 错误日志、以及启动 preflight
"""

from .provider import HcaptchaLLMProvider
from .preflight import preflight_llm

__all__ = [
    "HcaptchaLLMProvider",
    "preflight_llm",
]


