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
    CAPTCHA_PAYLOAD_TIMEOUT: float = Field(
        default=float(os.getenv("CAPTCHA_PAYLOAD_TIMEOUT", os.getenv("RESPONSE_TIMEOUT", "90"))),
        description="等待 hCaptcha getcaptcha payload 的超时（秒），默认 90，可用 env 覆盖",
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


def _apply_hcaptcha_compat_patch() -> None:
    """
    修复 hcaptcha-challenger 上游硬编码导致的不稳定：
    - getcaptcha payload 等待超时写死 30s（在 Actions 环境容易不够）
    - challenge iframe 域名写死 newassets.hcaptcha.com（Epic/区域/版本变动会找不到 frame）
    """
    try:
        import asyncio
        from contextlib import suppress

        from hcaptcha_challenger.agent import challenger as hc
        from hcaptcha_challenger.models import RequestType, ChallengeTypeEnum

        # 1) 放宽 iframe selector（支持任意 hcaptcha 子域）
        orig_arm_init = hc.RoboticArm.__init__

        def patched_arm_init(self, page, config):  # type: ignore[no-redef]
            orig_arm_init(self, page, config)
            self._checkbox_selector = (
                "//iframe[contains(@src,'hcaptcha.com') and contains(@src, 'frame=checkbox')]"
            )
            self._challenge_selector = (
                "//iframe[contains(@src,'hcaptcha.com') and contains(@src, 'frame=challenge')]"
            )

        hc.RoboticArm.__init__ = patched_arm_init  # type: ignore[method-assign]

        # 2) 放宽 frame.url 匹配（避免只认 newassets.hcaptcha.com）
        async def patched_get_challenge_frame_locator(self) -> object | None:  # Frame | None
            def is_challenge_url(url: str) -> bool:
                u = (url or "").lower()
                return ("hcaptcha.com/captcha" in u) and ("frame=challenge" in u)

            # 深度优先查找
            def find_recursive(frame, depth: int, max_depth: int):
                if depth >= max_depth:
                    return None
                for child in getattr(frame, "child_frames", []) or []:
                    if is_challenge_url(getattr(child, "url", "")):
                        return child
                    found = find_recursive(child, depth + 1, max_depth)
                    if found is not None:
                        return found
                return None

            candidate = find_recursive(self.page.main_frame, 0, 6)
            if candidate is not None:
                with suppress(Exception):
                    challenge_view = candidate.locator("//div[@class='challenge-view']")
                    if await challenge_view.is_visible(timeout=1500):
                        return candidate
                return candidate

            # 扫描全量 frames
            for frame in self.page.frames:
                if is_challenge_url(getattr(frame, "url", "")):
                    with suppress(Exception):
                        challenge_view = frame.locator("//div[@class='challenge-view']")
                        if await challenge_view.is_visible(timeout=1500):
                            return frame
                    return frame

            hc.logger.error("Cannot find a valid challenge frame")
            return None

        hc.RoboticArm.get_challenge_frame_locator = patched_get_challenge_frame_locator  # type: ignore[method-assign]

        # 3) 让 payload 等待超时可配置（默认跟随 settings.CAPTCHA_PAYLOAD_TIMEOUT）
        async def patched_review_challenge_type(self) -> object:  # RequestType | ChallengeTypeEnum
            try:
                timeout = float(getattr(self.config, "CAPTCHA_PAYLOAD_TIMEOUT", 30.0))
                self._captcha_payload = await asyncio.wait_for(
                    self._captcha_payload_queue.get(), timeout=timeout
                )
                await self.page.wait_for_timeout(500)
            except asyncio.TimeoutError:
                hc.logger.error("Wait for captcha payload to timeout")
                self._captcha_payload = None

            self.robotic_arm.signal_crumb_count = None
            self.robotic_arm.captcha_payload = None
            if not self._captcha_payload:
                return await self.robotic_arm.check_challenge_type()

            try:
                request_type = self._captcha_payload.request_type
                tasklist = self._captcha_payload.tasklist
                tasklist_length = len(tasklist)
                self.robotic_arm.captcha_payload = self._captcha_payload
                match request_type:
                    case RequestType.IMAGE_LABEL_BINARY:
                        self.robotic_arm.signal_crumb_count = int(tasklist_length / 9)
                        return RequestType.IMAGE_LABEL_BINARY
                    case RequestType.IMAGE_LABEL_AREA_SELECT:
                        self.robotic_arm.signal_crumb_count = tasklist_length
                        max_shapes = self._captcha_payload.request_config.max_shapes_per_image
                        if not isinstance(max_shapes, int):
                            return await self.robotic_arm.check_challenge_type()
                        return (
                            ChallengeTypeEnum.IMAGE_LABEL_SINGLE_SELECT
                            if max_shapes == 1
                            else ChallengeTypeEnum.IMAGE_LABEL_MULTI_SELECT
                        )
                    case RequestType.IMAGE_DRAG_DROP:
                        self.robotic_arm.signal_crumb_count = tasklist_length
                        return (
                            ChallengeTypeEnum.IMAGE_DRAG_SINGLE
                            if len(tasklist[0].entities) == 1
                            else ChallengeTypeEnum.IMAGE_DRAG_MULTI
                        )

                hc.logger.warning(f"Unknown request_type: {request_type=}")
            except Exception as err:
                hc.logger.error(f"Error parsing challenge type: {err}")

            return await self.robotic_arm.check_challenge_type()

        hc.AgentV._review_challenge_type = patched_review_challenge_type  # type: ignore[method-assign]

        logger.info(
            "🧩 hcaptcha-challenger 兼容补丁已应用 | payload_timeout={}s",
            settings.CAPTCHA_PAYLOAD_TIMEOUT,
        )
    except Exception as e:
        logger.warning(f"⚠️ hcaptcha-challenger 兼容补丁加载失败: {e}")


_apply_hcaptcha_compat_patch()
