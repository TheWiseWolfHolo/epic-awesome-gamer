# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Literal

from .url import has_v1beta, has_v1beta_openai, join_url

LLMMode = Literal["openai", "gemini_native", "gemini_openai"]


def build_openai_models_url(base_url: str) -> str:
    return join_url(base_url, "models")


def build_openai_chat_completions_url(base_url: str) -> str:
    return join_url(base_url, "chat/completions")


def build_gemini_native_models_url(root: str) -> str:
    if has_v1beta_openai(root):
        raise ValueError("gemini_native 模式下 base_url 不应包含 /v1beta/openai")
    return join_url(root, "models") if has_v1beta(root) else join_url(root, "v1beta/models")


def build_gemini_native_generate_content_url(root: str, model: str) -> str:
    if has_v1beta_openai(root):
        raise ValueError("gemini_native 模式下 base_url 不应包含 /v1beta/openai")
    return (
        join_url(root, f"models/{model}:generateContent")
        if has_v1beta(root)
        else join_url(root, "v1beta", f"models/{model}:generateContent")
    )


def build_gemini_openai_base_url(root: str) -> str:
    # base_url = {root}/v1beta/openai/ （若已含则不重复添加）
    if has_v1beta_openai(root):
        return root
    if has_v1beta(root):
        return join_url(root, "openai")
    return join_url(root, "v1beta/openai")


def build_gemini_openai_models_url(root: str) -> str:
    return join_url(build_gemini_openai_base_url(root), "models")


def build_gemini_openai_chat_completions_url(root: str) -> str:
    return join_url(build_gemini_openai_base_url(root), "chat/completions")


