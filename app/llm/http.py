# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger


class LLMHTTPError(RuntimeError):
    pass


def response_json_checked(
    resp: httpx.Response,
    *,
    log_headers: bool = True,
    context: str | None = None,
) -> Any:
    """
    在尝试 resp.json() 前先检查：
    - status_code
    - Content-Type 是否包含 application/json
    - body 是否为空

    若 JSONDecodeError：
    - 日志输出：status_code、content-type、headers(可选)、resp.text 前 1000 字符
    """
    status_code = resp.status_code
    content_type = (resp.headers.get("content-type") or "").lower()
    body = resp.content or b""
    text_snippet = (resp.text or "")[:1000]

    ctx = f" | {context}" if context else ""

    # 1) status_code 可观测（不提前 raise，便于看到错误 body / HTML）
    if status_code >= 400:
        logger.warning(f"LLM HTTP status_code={status_code}{ctx} url={resp.request.url}")

    # 2) 空 body
    if not body:
        logger.error(
            "LLM HTTP 响应体为空{} status_code={} content_type={} url={}",
            ctx,
            status_code,
            content_type,
            resp.request.url,
        )
        raise LLMHTTPError("LLM HTTP 响应体为空（可能被网关/WAF 拦截或服务异常）")

    # 3) 非 JSON Content-Type
    if "application/json" not in content_type:
        logger.error(
            "LLM HTTP 非 JSON 响应{} status_code={} content_type={} url={} body_snippet={}",
            ctx,
            status_code,
            content_type,
            resp.request.url,
            text_snippet,
        )
        raise LLMHTTPError(
            f"LLM HTTP 非 JSON 响应（status_code={status_code}, content_type={content_type}）"
        )

    # 4) 解析 JSON
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        headers = dict(resp.headers) if log_headers else {}
        logger.error(
            "LLM HTTP JSONDecodeError{} status_code={} content_type={} url={} headers={} body_snippet={}",
            ctx,
            status_code,
            content_type,
            resp.request.url,
            headers,
            text_snippet,
        )
        raise

    # 5) 非 2xx 直接失败（但已拿到 JSON，方便诊断）
    if resp.is_error:
        logger.error(
            "LLM HTTP 错误响应{} status_code={} url={} json={}",
            ctx,
            status_code,
            resp.request.url,
            data,
        )
        raise LLMHTTPError(f"LLM HTTP 错误响应（status_code={status_code}）")

    return data


