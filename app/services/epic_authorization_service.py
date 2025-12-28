# -*- coding: utf-8 -*-
"""
@Time    : 2025/7/16 22:13
@Author  : QIN2DIM
@GitHub  : https://github.com/QIN2DIM
@Desc    :
"""
import asyncio
import json
import time
from contextlib import suppress

from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import expect, Page, Response

from settings import SCREENSHOTS_DIR, settings

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"


class EpicAuthorization:

    def __init__(self, page: Page):
        self.page = page

        self._is_login_success_signal = asyncio.Queue()
        self._is_refresh_csrf_signal = asyncio.Queue()

    async def _on_response_anything(self, r: Response):
        if r.request.method != "POST" or "talon" in r.url:
            return

        with suppress(Exception):
            result = await r.json()
            result_json = json.dumps(result, indent=2, ensure_ascii=False)

            if "/id/api/login" in r.url and result.get("errorCode"):
                logger.error(f"{r.request.method} {r.url} - {result_json}")
            elif "/id/api/analytics" in r.url and result.get("accountId"):
                self._is_login_success_signal.put_nowait(result)
            elif "/account/v2/refresh-csrf" in r.url and result.get("success", False) is True:
                self._is_refresh_csrf_signal.put_nowait(result)
            # else:
            #     logger.debug(f"{r.request.method} {r.url} - {result_json}")

    async def _handle_right_account_validation(self):
        """
        以下验证仅会在登录成功后出现
        Returns:

        """
        await self.page.goto("https://www.epicgames.com/account/personal", wait_until="networkidle")

        btn_ids = ["#link-success", "#login-reminder-prompt-setup-tfa-skip", "#yes"]

        # == 账号长期不登录需要做的额外验证 == #

        while self._is_refresh_csrf_signal.empty() and btn_ids:
            await self.page.wait_for_timeout(500)
            action_chains = btn_ids.copy()
            for action in action_chains:
                with suppress(Exception):
                    reminder_btn = self.page.locator(action)
                    await expect(reminder_btn).to_be_visible(timeout=1000)
                    await reminder_btn.click(timeout=1000)
                    btn_ids.remove(action)

    async def _login(self) -> bool | None:
        # 尽可能早地初始化机器人
        agent = AgentV(page=self.page, agent_config=settings)

        # {{< SIGN IN PAGE >}}
        logger.debug("Login with Email")

        try:
            point_url = "https://www.epicgames.com/account/personal?lang=en-US&productName=egs&sessionInvalidated=true"
            await self.page.goto(point_url, wait_until="domcontentloaded")

            # 1. 使用电子邮件地址登录
            email_input = self.page.locator("#email")
            await expect(email_input).to_be_visible(timeout=15000)
            await email_input.clear()
            await email_input.type(settings.EPIC_EMAIL)

            # 2. 点击继续按钮
            # Playwright 的 click 可能会等待“导航完成”导致超时；这里不等待，改为显式等待下一步元素出现
            await self.page.click("#continue", no_wait_after=True)

            # 3. 输入密码
            password_input = self.page.locator("#password")
            await expect(password_input).to_be_visible(timeout=15000)
            await password_input.clear()
            await password_input.type(settings.EPIC_PASSWORD.get_secret_value())

            # 4. 点击登录按钮，触发人机挑战值守监听器
            # Active hCaptcha checkbox
            await self.page.click("#sign-in", no_wait_after=True)

            # Active hCaptcha challenge（不要阻塞登录成功信号：有时不会出现验证码）
            captcha_task = asyncio.create_task(agent.wait_for_challenge())
            with suppress(Exception):
                # 若验证码任务提前失败，记录一下但不立刻终止（真正是否登录成功由后续信号决定）
                if captcha_task.done():
                    captcha_task.result()

            # Wait for the page to redirect
            await asyncio.wait_for(self._is_login_success_signal.get(), timeout=90)

            # 登录成功后取消验证码任务（若仍在跑）
            if not captcha_task.done():
                captcha_task.cancel()
                with suppress(asyncio.CancelledError):
                    await captcha_task
            logger.success("Login success")

            await asyncio.wait_for(self._handle_right_account_validation(), timeout=60)
            logger.success("Right account validation success")
            return True
        except Exception as err:
            logger.warning(f"{err}")
            sr = SCREENSHOTS_DIR.joinpath("authorization")
            sr.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=sr.joinpath(f"login-{int(time.time())}.png"))
            return None

    async def invoke(self) -> bool:
        self.page.on("response", self._on_response_anything)

        for _ in range(3):
            await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")

            # egs-navigation 的 isloggedin 可能异步更新，做短轮询避免瞬时误判
            nav = self.page.locator("//egs-navigation")
            status = None
            for _i in range(30):  # 15s
                status = await nav.get_attribute("isloggedin")
                if status in ("true", "false"):
                    break
                await self.page.wait_for_timeout(500)

            if status == "true":
                logger.success("Epic Games is already logged in")
                return True

            if await self._login():
                return True

        return False
