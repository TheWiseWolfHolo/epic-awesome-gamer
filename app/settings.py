# -*- coding: utf-8 -*-
import os
import sys
from pathlib import Path
from typing import Literal

# === 引入所需库 ===
from hcaptcha_challenger.agent import AgentConfig
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from loguru import logger

# --- 核心路径定义 ---
PROJECT_ROOT = Path(__file__).parent
VOLUMES_DIR = PROJECT_ROOT.joinpath("volumes")
LOG_DIR = VOLUMES_DIR.joinpath("logs")
USER_DATA_DIR = VOLUMES_DIR.joinpath("user_data")
RUNTIME_DIR = VOLUMES_DIR.joinpath("runtime")
SCREENSHOTS_DIR = VOLUMES_DIR.joinpath("screenshots")
RECORD_DIR = VOLUMES_DIR.joinpath("record")
HCAPTCHA_DIR = VOLUMES_DIR.joinpath("hcaptcha")

# === 配置类定义 ===
class EpicSettings(AgentConfig):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # [基础配置] API Key 建议使用 SecretStr 类型
    GEMINI_API_KEY: SecretStr | None = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY"),
        description="LLM 的 API Key（Gemini 官方 / OpenAI 兼容均可）",
    )
    
    GEMINI_BASE_URL: str = Field(
        default=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
        description="LLM Base URL（兼容旧变量；不会被代码私自改写）",
    )
    
    GEMINI_MODEL: str = Field(
        default=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        description="模型名称",
    )

    # ==========================================================
    # 关键：让“用户填什么模型就用什么模型”（不限制模型名）
    # - hcaptcha-challenger 上游对这些字段做了 Literal 白名单类型
    # - 这里强制覆盖为 str，并默认统一使用 GEMINI_MODEL
    # - 如需单独微调，也可分别设置同名环境变量覆盖
    # ==========================================================
    CHALLENGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: os.getenv("CHALLENGE_CLASSIFIER_MODEL")
        or os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        description="验证码任务分类模型（默认跟随 GEMINI_MODEL，可任意字符串）",
    )
    IMAGE_CLASSIFIER_MODEL: str = Field(
        default_factory=lambda: os.getenv("IMAGE_CLASSIFIER_MODEL")
        or os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        description="九宫格图像分类模型（默认跟随 GEMINI_MODEL，可任意字符串）",
    )
    SPATIAL_POINT_REASONER_MODEL: str = Field(
        default_factory=lambda: os.getenv("SPATIAL_POINT_REASONER_MODEL")
        or os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        description="点选/框选推理模型（默认跟随 GEMINI_MODEL，可任意字符串）",
    )
    SPATIAL_PATH_REASONER_MODEL: str = Field(
        default_factory=lambda: os.getenv("SPATIAL_PATH_REASONER_MODEL")
        or os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        description="拖拽路径推理模型（默认跟随 GEMINI_MODEL，可任意字符串）",
    )

    # ================================
    # LLM 调用层（用户可配置）
    # ================================
    LLM_MODE: Literal["openai", "gemini_native", "gemini_openai"] = Field(
        default=os.getenv("LLM_MODE", "gemini_native"),
        description="LLM 调用模式：openai / gemini_native / gemini_openai",
    )

    # 注意：优先使用 LLM_BASE_URL；未提供时向下兼容 GEMINI_BASE_URL
    LLM_BASE_URL: str = Field(
        default=os.getenv("LLM_BASE_URL", "")
        or os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
        description="LLM Base URL（严禁代码擅自改写/重写）。",
    )

    # 是否在启动时执行 LLM preflight（deploy.py 中调用）
    LLM_PREFLIGHT: bool = Field(
        default=os.getenv("LLM_PREFLIGHT", "true").lower() in {"1", "true", "yes", "y", "on"},
        description="启动时执行 LLM preflight/healthcheck（true/false）",
    )

    EPIC_EMAIL: str = Field(default_factory=lambda: os.getenv("EPIC_EMAIL"))
    EPIC_PASSWORD: SecretStr = Field(default_factory=lambda: os.getenv("EPIC_PASSWORD"))
    DISABLE_BEZIER_TRAJECTORY: bool = Field(default=True)

    # ================================
    # 超时（允许通过环境变量覆盖）
    # - 上游默认 RESPONSE_TIMEOUT=30 在 Actions 环境容易不够
    # ================================
    EXECUTION_TIMEOUT: float = Field(
        default=float(os.getenv("EXECUTION_TIMEOUT", "180")),
        description="验证码整体执行超时（秒），默认 180，可用 env 覆盖",
    )
    RESPONSE_TIMEOUT: float = Field(
        default=float(os.getenv("RESPONSE_TIMEOUT", "90")),
        description="等待验证码服务响应超时（秒），默认 90，可用 env 覆盖",
    )

    cache_dir: Path = HCAPTCHA_DIR.joinpath(".cache")
    challenge_dir: Path = HCAPTCHA_DIR.joinpath(".challenge")
    captcha_response_dir: Path = HCAPTCHA_DIR.joinpath(".captcha")

    ENABLE_APSCHEDULER: bool = Field(default=True)
    TASK_TIMEOUT_SECONDS: int = Field(default=900)
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_WORKER_CONCURRENCY: int = Field(default=1)
    CELERY_TASK_TIME_LIMIT: int = Field(default=1200)
    CELERY_TASK_SOFT_TIME_LIMIT: int = Field(default=900)

    @property
    def user_data_dir(self) -> Path:
        target_ = USER_DATA_DIR.joinpath(self.EPIC_EMAIL)
        target_.mkdir(parents=True, exist_ok=True)
        return target_

settings = EpicSettings()
settings.ignore_request_questions = ["Please drag the crossing to complete the lines"]

def _apply_llm_provider_patch() -> None:
    """
    将 hcaptcha-challenger 默认的 GeminiProvider 替换为本项目的通用 LLM Provider。

    目标：
    - 支持任意 base_url（严禁代码擅自改写/重写）
    - 支持 OpenAI 兼容 & Gemini 官方（native / openai）三种模式
    """
    if not settings.GEMINI_API_KEY:
        return

    try:
        from hcaptcha_challenger.tools.internal.base import Reasoner
        from llm.provider import HcaptchaLLMProvider

        def _create_default_provider(self):  # type: ignore[no-redef]
            return HcaptchaLLMProvider(
                api_key=str(self._api_key),
                model=str(self._model) if self._model else "",
                mode=settings.LLM_MODE,
                base_url=settings.LLM_BASE_URL,
            )

        Reasoner._create_default_provider = _create_default_provider  # type: ignore[method-assign]
        logger.info(
            "🚀 LLM Provider 补丁已应用 | mode: {} | base_url: {}",
            settings.LLM_MODE,
            settings.LLM_BASE_URL,
        )
    except Exception as e:
        logger.error(f"❌ LLM Provider 补丁加载失败: {e}")


_apply_llm_provider_patch()
