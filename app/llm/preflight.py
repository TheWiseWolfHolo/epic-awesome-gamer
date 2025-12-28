# -*- coding: utf-8 -*-
from __future__ import annotations

import httpx
from loguru import logger

from .endpoints import (
    LLMMode,
    build_gemini_native_models_url,
    build_gemini_openai_models_url,
    build_openai_models_url,
)
from .http import LLMHTTPError, response_json_checked


async def preflight_llm(
    *,
    mode: LLMMode,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 15.0,
) -> None:
    """
    启动 preflight/healthcheck：
    - openai: GET {base_url}/models
    - gemini_native: GET {root}/v1beta/models（若 base_url 已含 /v1beta 则不重复添加）
    - gemini_openai: GET {root}/v1beta/openai/models（若 base_url 已含则不重复添加）

    要求返回 JSON，否则直接失败并给出清晰错误（含响应片段）。
    """
    if not base_url:
        raise ValueError("LLM_BASE_URL 不能为空")
    if not api_key:
        raise ValueError("LLM API key 不能为空（请配置 GEMINI_API_KEY）")

    if mode == "openai":
        url = build_openai_models_url(base_url)
        headers = {"Authorization": f"Bearer {api_key}"}
    elif mode == "gemini_native":
        url = build_gemini_native_models_url(base_url)
        headers = {"x-goog-api-key": api_key}
    elif mode == "gemini_openai":
        url = build_gemini_openai_models_url(base_url)
        headers = {"Authorization": f"Bearer {api_key}"}
    else:
        raise ValueError(f"不支持的 LLM_MODE: {mode}")

    logger.info(f"🔎 LLM preflight | mode={mode} url={url}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        _ = response_json_checked(resp, context="preflight")
    except (LLMHTTPError, httpx.HTTPError) as e:
        logger.error(f"❌ LLM preflight 失败 | mode={mode} url={url} err={e}")
        raise

    logger.success(f"✅ LLM preflight OK | mode={mode}")


