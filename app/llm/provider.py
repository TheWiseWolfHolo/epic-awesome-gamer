# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, List, Type, TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel

from .endpoints import (
    LLMMode,
    build_gemini_native_generate_content_url,
    build_gemini_openai_chat_completions_url,
    build_openai_chat_completions_url,
)
from .http import LLMHTTPError, response_json_checked

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "image/png"


def _file_to_base64(path: Path) -> tuple[str, str]:
    mime = _guess_mime(path)
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return mime, b64


def _extract_first_json(text: str) -> Any | None:
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None

    # 1) 纯 JSON
    if s[0] in "{[":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

    # 2) ```json ... ```
    for pattern in (r"```json\s*([\s\S]*?)```", r"```\s*([\s\S]*?)```"):
        m = re.search(pattern, s, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = (m.group(1) or "").strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 3) 兜底：截取第一个 { ... } 尝试解析
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = s[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    return None


def _extract_text_from_openai_chat_completions(data: dict) -> str | None:
    try:
        choices = data.get("choices") or []
        first = choices[0] if choices else {}
        message = first.get("message") or {}
        content = message.get("content")
    except Exception:
        return None

    if isinstance(content, str):
        return content

    # 少数实现会把 content 作为 parts 列表
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        return "\n".join(texts).strip() if texts else None

    return None


def _extract_text_from_gemini_native(data: dict) -> str | None:
    try:
        candidates = data.get("candidates") or []
        first = candidates[0] if candidates else {}
        content = first.get("content") or {}
        parts = content.get("parts") or []
    except Exception:
        return None

    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(texts).strip() if texts else None


class HcaptchaLLMProvider:
    """
    hcaptcha-challenger 的 ChatProvider 实现（外部可替换）。

    支持三种模式（由 app/settings.py 注入）：
    - openai: POST {base_url}/chat/completions
    - gemini_native: POST {root}/v1beta/models/{model}:generateContent
    - gemini_openai: POST {root}/v1beta/openai/chat/completions
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        mode: LLMMode,
        base_url: str,
        timeout_seconds: float = 60.0,
    ):
        self._api_key = (api_key or "").strip()
        self._model = (model or "").strip()
        self._mode = mode
        self._base_url = (base_url or "").strip()
        self._timeout = httpx.Timeout(timeout_seconds)

        self._last_response_json: dict | None = None
        self._last_response_text: str | None = None

    @property
    def last_response_json(self) -> dict | None:
        return self._last_response_json

    async def generate_with_images(
        self,
        *,
        images: List[Path],
        response_schema: Type[ResponseT],
        user_prompt: str | None = None,
        description: str | None = None,
        **kwargs,
    ) -> ResponseT:
        if not self._api_key:
            raise ValueError("LLM API key 为空（请配置 GEMINI_API_KEY 或 LLM_API_KEY）")
        if not self._base_url:
            raise ValueError("LLM base_url 为空（请配置 LLM_BASE_URL 或 GEMINI_BASE_URL）")

        mode = self._mode

        # 读取图片并转 base64（inline，不依赖 file upload）
        valid_images = [Path(p) for p in images if p]
        image_payloads: list[tuple[str, str]] = []
        for p in valid_images:
            if not p.exists():
                continue
            image_payloads.append(_file_to_base64(p))

        # 请求
        url: str
        headers: dict[str, str]
        payload: dict[str, Any]

        if mode in ("openai", "gemini_openai"):
            url = (
                build_openai_chat_completions_url(self._base_url)
                if mode == "openai"
                else build_gemini_openai_chat_completions_url(self._base_url)
            )
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }

            messages: list[dict[str, Any]] = []
            if description:
                messages.append({"role": "system", "content": description})

            user_content: list[dict[str, Any]] = []
            for mime, b64 in image_payloads:
                user_content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )
            if user_prompt:
                user_content.append({"type": "text", "text": user_prompt})

            messages.append({"role": "user", "content": user_content})
            payload = {
                "model": self._model,
                "messages": messages,
                # 尽量让输出稳定；如代理不支持可忽略
                "temperature": 0,
            }

        elif mode == "gemini_native":
            if not self._model:
                raise ValueError("gemini_native 模式必须提供 model")
            url = build_gemini_native_generate_content_url(self._base_url, self._model)
            headers = {
                "x-goog-api-key": self._api_key,
                "Content-Type": "application/json",
            }

            parts: list[dict[str, Any]] = []
            for mime, b64 in image_payloads:
                # Gemini REST: inlineData.mimeType/data（官方 JSON 命名为 lowerCamelCase）
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
            if user_prompt:
                parts.append({"text": user_prompt})

            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            if description:
                payload["systemInstruction"] = {"parts": [{"text": description}]}

        else:
            raise ValueError(f"不支持的 LLM_MODE: {mode}")

        context = f"{mode}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.post(url, headers=headers, json=payload)
            data = response_json_checked(resp, context=context)
        except (LLMHTTPError, httpx.HTTPError) as e:
            logger.error(f"LLM 请求失败 | mode={mode} url={url} err={e}")
            raise

        if isinstance(data, dict):
            self._last_response_json = data

        # 提取文本
        text: str | None
        if mode in ("openai", "gemini_openai"):
            text = _extract_text_from_openai_chat_completions(data if isinstance(data, dict) else {})
        else:
            text = _extract_text_from_gemini_native(data if isinstance(data, dict) else {})

        if not text:
            raise ValueError(f"LLM 响应中未找到可解析文本：{str(data)[:500]}")

        self._last_response_text = text

        # 解析 JSON
        parsed = _extract_first_json(text)
        if parsed is None:
            raise ValueError(f"无法从 LLM 输出中解析 JSON：{text[:500]}")

        return response_schema.model_validate(parsed)

    def cache_response(self, path: Path) -> None:
        """将最后一次 LLM 原始响应缓存到文件，便于排查。"""
        if not self._last_response_json and not self._last_response_text:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "mode": self._mode,
                "base_url": self._base_url,
                "model": self._model,
                "response_json": self._last_response_json,
                "response_text": self._last_response_text,
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to cache LLM response: {e}")


