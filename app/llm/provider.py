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
from pydantic import BaseModel, ValidationError

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


def _json_only_instruction(schema: dict) -> str:
    """
    强制模型输出 JSON 的提示词（比依赖上游 prompt 更可靠）。

    说明：hcaptcha-challenger 有些系统提示（如 spatial/path.md）并不包含 JSON 输出格式要求，
    上游 GeminiProvider 依赖 response_schema/response_mime_type 来保证结构化输出。
    这里我们显式注入“只输出 JSON + schema”，避免模型输出解释性文本导致解析失败。
    """
    schema_text = json.dumps(schema, ensure_ascii=False)
    return (
        "IMPORTANT:\n"
        "- Return ONLY a single JSON object.\n"
        "- Do NOT include any explanation, markdown, code fences, or extra text.\n"
        "- The JSON MUST conform to this JSON Schema:\n"
        f"{schema_text}\n"
    )


_RE_XY_PAIR = re.compile(
    r"""
    x\s*[:=]\s*(?P<x>\d+)\s*[,，]\s*y\s*[:=]\s*(?P<y>\d+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_RE_PAREN_PAIR = re.compile(r"\(\s*(?P<x>\d+)\s*[,，]\s*(?P<y>\d+)\s*\)")


def _extract_xy_pairs(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    pairs: list[tuple[int, int]] = []
    for m in _RE_XY_PAIR.finditer(text):
        pairs.append((int(m.group("x")), int(m.group("y"))))
    for m in _RE_PAREN_PAIR.finditer(text):
        pairs.append((int(m.group("x")), int(m.group("y"))))
    # 去重且保持顺序
    seen: set[tuple[int, int]] = set()
    uniq: list[tuple[int, int]] = []
    for p in pairs:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def _salvage_from_text(
    *,
    text: str,
    response_schema: Type[ResponseT],
    user_prompt: str | None,
) -> dict | None:
    """
    当模型没有输出 JSON 时，尝试从自然语言中提取关键坐标并拼出符合 schema 的 JSON。
    这是兜底策略，避免少量模型/网关不支持 JSON 模式时直接失败。
    """
    fields = getattr(response_schema, "model_fields", {}) or {}
    challenge_prompt = user_prompt or ""

    xy_pairs = _extract_xy_pairs(text)

    # ImageDragDropChallenge: {challenge_prompt, paths:[{start_point:{x,y}, end_point:{x,y}}]}
    if "paths" in fields:
        if len(xy_pairs) >= 2:
            (sx, sy), (ex, ey) = xy_pairs[0], xy_pairs[1]
            return {
                "challenge_prompt": challenge_prompt,
                "paths": [{"start_point": {"x": sx, "y": sy}, "end_point": {"x": ex, "y": ey}}],
            }
        return None

    # ImageAreaSelectChallenge: {challenge_prompt, points:[{x,y}, ...]}
    if "points" in fields:
        if xy_pairs:
            return {
                "challenge_prompt": challenge_prompt,
                "points": [{"x": x, "y": y} for x, y in xy_pairs],
            }
        return None

    # ChallengeRouterResult: {challenge_prompt, challenge_type}
    if "challenge_type" in fields and isinstance(text, str):
        # 允许模型直接输出 image_drag_single / image_label_multi_select 等
        t = text.strip().strip("`").strip()
        if t:
            return {"challenge_prompt": challenge_prompt, "challenge_type": t}

    return None


def _normalize_for_schema(
    parsed: Any,
    *,
    response_schema: Type[ResponseT],
    user_prompt: str | None,
) -> Any:
    """
    对解析到的 JSON 做最小修正，让其更容易通过 Pydantic 校验：
    - 自动补全 challenge_prompt
    - 容错常见字段形态（例如把 start/end 提升到 paths）
    """
    fields = getattr(response_schema, "model_fields", {}) or {}

    if isinstance(parsed, dict):
        if "challenge_prompt" in fields and "challenge_prompt" not in parsed:
            parsed["challenge_prompt"] = user_prompt or ""

        # 允许直接输出 {start_point, end_point}
        if "paths" in fields and "paths" not in parsed and {"start_point", "end_point"} <= set(parsed.keys()):
            parsed = {
                "challenge_prompt": parsed.get("challenge_prompt", user_prompt or ""),
                "paths": [{"start_point": parsed["start_point"], "end_point": parsed["end_point"]}],
            }

        # 允许直接输出 {x, y}
        if "points" in fields and "points" not in parsed and {"x", "y"} <= set(parsed.keys()):
            parsed = {
                "challenge_prompt": parsed.get("challenge_prompt", user_prompt or ""),
                "points": [{"x": parsed["x"], "y": parsed["y"]}],
            }

    return parsed


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
        schema = response_schema.model_json_schema()
        json_instruction = _json_only_instruction(schema)

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
            # 强制 JSON 输出（避免模型输出解释文本）
            messages.append({"role": "system", "content": json_instruction})

            user_content: list[dict[str, Any]] = []
            for mime, b64 in image_payloads:
                user_content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )
            if user_prompt:
                user_content.append({"type": "text", "text": user_prompt})
            # 再补一层约束（部分网关会忽略 system）
            user_content.append({"type": "text", "text": json_instruction})

            messages.append({"role": "user", "content": user_content})
            payload = {
                "model": self._model,
                "messages": messages,
                # 尽量让输出稳定；如代理不支持可忽略
                "temperature": 0,
                # OpenAI JSON mode（若网关不支持会报 4xx，稍后会自动降级重试）
                "response_format": {"type": "json_object"},
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
            parts.append({"text": json_instruction})

            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            if description:
                payload["systemInstruction"] = {"parts": [{"text": description}, {"text": json_instruction}]}
            else:
                payload["systemInstruction"] = {"parts": [{"text": json_instruction}]}

        else:
            raise ValueError(f"不支持的 LLM_MODE: {mode}")

        context = f"{mode}"

        async def _request_json(_payload: dict[str, Any]) -> Any:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.post(url, headers=headers, json=_payload)
            return response_json_checked(resp, context=context)

        # 第一次尝试：带 response_format/json_instruction
        try:
            data = await _request_json(payload)
        except (LLMHTTPError, httpx.HTTPError) as e:
            # 部分 OpenAI 兼容网关不支持 response_format；降级重试一次（移除 response_format）
            if mode in ("openai", "gemini_openai") and isinstance(payload, dict) and "response_format" in payload:
                logger.warning(f"LLM 第一次请求失败，尝试降级重试（移除 response_format）| err={e}")
                payload2 = dict(payload)
                payload2.pop("response_format", None)
                data = await _request_json(payload2)
            else:
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

        # 解析 JSON（带兜底策略）
        parsed = _extract_first_json(text)

        # 兜底 1：从自然语言中提取坐标拼 JSON
        if parsed is None:
            parsed = _salvage_from_text(text=text, response_schema=response_schema, user_prompt=user_prompt)

        # 兜底 2：二次“修复”请求，让模型把刚才的输出转换成严格 JSON
        if parsed is None:
            logger.warning("LLM 输出非 JSON，执行二次修复请求（convert-to-json）")
            repair_prompt = (
                "Convert the following content into a single JSON object that conforms to the JSON schema.\n"
                "Return ONLY JSON.\n\n"
                "JSON Schema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n\n"
                "Content:\n"
                f"{text}\n"
            )

            if mode in ("openai", "gemini_openai"):
                repair_payload = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": "You are a strict JSON converter."},
                        {"role": "user", "content": repair_prompt},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                }
                try:
                    data2 = await _request_json(repair_payload)
                    text2 = _extract_text_from_openai_chat_completions(data2 if isinstance(data2, dict) else {})
                except Exception as e:
                    logger.error(f"LLM 二次修复请求失败: {e}")
                    text2 = None
            else:
                # gemini_native：不带图片、只做文本修复
                repair_payload = {
                    "contents": [{"role": "user", "parts": [{"text": repair_prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                    "systemInstruction": {"parts": [{"text": "You are a strict JSON converter."}]},
                }
                try:
                    data2 = await _request_json(repair_payload)
                    text2 = _extract_text_from_gemini_native(data2 if isinstance(data2, dict) else {})
                except Exception as e:
                    logger.error(f"LLM 二次修复请求失败: {e}")
                    text2 = None

            if text2:
                self._last_response_json = data2 if isinstance(data2, dict) else self._last_response_json
                self._last_response_text = text2
                parsed = _extract_first_json(text2)
                if parsed is None:
                    parsed = _salvage_from_text(
                        text=text2, response_schema=response_schema, user_prompt=user_prompt
                    )

        if parsed is None:
            raise ValueError(f"无法从 LLM 输出中解析 JSON：{text[:500]}")

        parsed = _normalize_for_schema(parsed, response_schema=response_schema, user_prompt=user_prompt)

        try:
            return response_schema.model_validate(parsed)
        except ValidationError as ve:
            # 最后再尝试一次从自然语言提取（防止缺字段）
            if isinstance(self._last_response_text, str):
                salvaged = _salvage_from_text(
                    text=self._last_response_text,
                    response_schema=response_schema,
                    user_prompt=user_prompt,
                )
                if salvaged is not None:
                    return response_schema.model_validate(salvaged)
            raise ve

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


