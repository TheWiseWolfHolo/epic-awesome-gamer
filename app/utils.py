# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
from zoneinfo import ZoneInfo
from loguru import logger
from settings import settings

def timezone_filter(record):
    record["time"] = record["time"].astimezone(ZoneInfo("Asia/Shanghai"))
    return record

def patch_aihubmix():
    """适配 SecretStr 的强力拦截器"""
    if not settings.GEMINI_API_KEY:
        return
    
    try:
        from google import genai
        from google.genai import types
        
        orig_init = genai.Client.__init__
        
        def new_init(self, *args, **kwargs):
            # --- 修改：使用 .get_secret_value() 获取 API Key 字符串 ---
            kwargs['api_key'] = settings.GEMINI_API_KEY.get_secret_value()
            
            base_url = settings.GEMINI_BASE_URL.rstrip('/')
            if not base_url.endswith('/v1') and not base_url.endswith('/v1beta'):
                base_url = f"{base_url}/v1"
            
            kwargs['http_options'] = types.HttpOptions(base_url=base_url)
            logger.info(f"🚀 AiHubMix 强力拦截已激活 | 模型: {settings.GEMINI_MODEL} | 地址: {base_url}")
            orig_init(self, *args, **kwargs)
            
        genai.Client.__init__ = new_init
    except Exception as e:
        logger.error(f"拦截器加载失败: {e}")

def init_log(**sink_channel):
    patch_aihubmix()
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()
    logger.add(sink=sys.stdout, level=log_level, filter=timezone_filter)
    return logger

init_log()
