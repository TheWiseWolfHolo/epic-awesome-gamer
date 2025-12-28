# -*- coding: utf-8 -*-
from __future__ import annotations

import posixpath
from urllib.parse import urlsplit, urlunsplit


def join_url(base_url: str, *paths: str) -> str:
    """
    安全拼接 URL：
    - 处理 base_url 有/无尾部斜杠
    - 处理 base_url 自带 path 前缀（如 https://api.xxx.com/proxy）
    - 避免出现双斜杠或路径丢失

    注意：此函数不会“改写 base_url”，只返回拼接后的新 URL。
    """
    if base_url is None:
        raise ValueError("base_url 不能为空")

    base_url = str(base_url).strip()
    if not base_url:
        raise ValueError("base_url 不能为空")

    parts = urlsplit(base_url)
    base_path = (parts.path or "").rstrip("/")

    clean_parts = [str(p).strip("/") for p in paths if p is not None and str(p).strip("/") != ""]
    new_path = posixpath.join(base_path, *clean_parts) if clean_parts else base_path

    # 绝对 URL 必须确保 path 以 / 开头
    if parts.scheme and parts.netloc:
        if not new_path.startswith("/"):
            new_path = "/" + new_path if new_path else "/"

    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


def _path_segments(url: str) -> list[str]:
    path = urlsplit(url).path or ""
    return [seg for seg in path.split("/") if seg]


def has_v1beta(url: str) -> bool:
    """判断 base_url 的 path 中是否已经包含 /v1beta（作为路径段）。"""
    return "v1beta" in _path_segments(url)


def has_v1beta_openai(url: str) -> bool:
    """判断 base_url 的 path 中是否已经包含 /v1beta/openai（连续路径段）。"""
    segs = _path_segments(url)
    for i, seg in enumerate(segs):
        if seg == "v1beta" and i + 1 < len(segs) and segs[i + 1] == "openai":
            return True
    return False


