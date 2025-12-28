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

    async def _wait_store_isloggedin_true(self, timeout_s: float = 30.0) -> bool:
        """
        Epic 的 <egs-navigation isloggedin> 可能先为 "false" 再异步更新为 "true"。
        这里统一等待直到观测到 "true"，避免瞬时误判。
        """
        nav = self.page.locator("//egs-navigation")
        status = None
        for _i in range(max(1, int(timeout_s * 2))):
            with suppress(Exception):
                status = await nav.get_attribute("isloggedin")
            if status == "true":
                return True
            await self.page.wait_for_timeout(500)
        logger.debug(
            "Store login not ready | isloggedin={} url={}",
            status,
            self.page.url,
        )
        return False

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

        # 这一步是“尽力而为”的兜底，不应无限循环阻塞主流程
        deadline = time.time() + 30  # 最多尝试 30s
        idle_rounds = 0

        while time.time() < deadline and self._is_refresh_csrf_signal.empty() and btn_ids:
            await self.page.wait_for_timeout(500)
            action_chains = btn_ids.copy()
            clicked_any = False
            for action in action_chains:
                with suppress(Exception):
                    reminder_btn = self.page.locator(action)
                    await expect(reminder_btn).to_be_visible(timeout=1000)
                    await reminder_btn.click(timeout=1000)
                    btn_ids.remove(action)
                    clicked_any = True

            if clicked_any:
                idle_rounds = 0
            else:
                idle_rounds += 1
                # 连续多轮都没任何按钮可点，直接结束（可能根本不需要该验证）
                if idle_rounds >= 10:
                    break

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

            async def _safe_click(selector: str):
                # Playwright click 在某些情况下仍可能卡住；失败时退化为 DOM click
                try:
                    await self.page.click(selector, no_wait_after=True, force=True, timeout=15000)
                except Exception as e:
                    logger.warning(f"Safe click fallback - selector={selector} err={type(e).__name__}: {e}")
                    await self.page.evaluate(
                        "(sel) => { const el = document.querySelector(sel); if (el) el.click(); }",
                        selector,
                    )

            # 2. 点击继续按钮
            # Playwright 的 click 可能会等待“导航完成”导致超时；这里不等待，改为显式等待下一步元素出现
            await _safe_click("#continue")

            # 3. 输入密码
            password_input = self.page.locator("#password")
            await expect(password_input).to_be_visible(timeout=15000)
            await password_input.clear()
            await password_input.type(settings.EPIC_PASSWORD.get_secret_value())

            # 4. 点击登录按钮，触发人机挑战值守监听器
            # Active hCaptcha checkbox
            await _safe_click("#sign-in")

            # 并发等待：登录成功信号 vs 验证码任务（避免 90s 过早超时导致“已快成功但被我们中断”）
            captcha_task = asyncio.create_task(agent.wait_for_challenge())
            login_task = asyncio.create_task(self._is_login_success_signal.get())

            overall_timeout = float(getattr(settings, "EXECUTION_TIMEOUT", 120.0)) + float(
                getattr(settings, "RESPONSE_TIMEOUT", 30.0)
            ) + 60.0

            done, pending = await asyncio.wait(
                {captcha_task, login_task},
                timeout=overall_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # case 1) 先拿到登录成功
            if login_task in done:
                # 登录成功后取消验证码任务（若仍在跑）
                if not captcha_task.done():
                    captcha_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await captcha_task

            # case 2) 先结束验证码任务（成功或失败），再给登录一点时间完成跳转
            elif captcha_task in done:
                with suppress(Exception):
                    captcha_task.result()

                try:
                    await asyncio.wait_for(login_task, timeout=60)
                except asyncio.TimeoutError:
                    # 兜底：有时登录成功但 analytics 信号没抓到，回到商店页检查 isloggedin
                    with suppress(Exception):
                        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                        if await self._wait_store_isloggedin_true(timeout_s=30):
                            logger.success("Login inferred by isloggedin=true (fallback)")
                        else:
                            raise asyncio.TimeoutError(
                                "Login success signal timeout after captcha task finished"
                            )

            # case 3) 都没完成：超时
            else:
                raise asyncio.TimeoutError("Login timeout (captcha/login task not finished)")

            # 收尾：取消仍挂起的任务
            for t in pending:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t
            logger.success("Login success")

            # 该验证是可选步骤：超时/失败不影响后续领取逻辑
            try:
                await asyncio.wait_for(self._handle_right_account_validation(), timeout=60)
                logger.success("Right account validation success")
            except asyncio.TimeoutError:
                logger.warning("Right account validation timeout, continue")
            except Exception as e:
                logger.warning(f"Right account validation skipped: {type(e).__name__}: {e}")

            return True
        except Exception as err:
            logger.warning(f"{type(err).__name__}: {err}")
            sr = SCREENSHOTS_DIR.joinpath("authorization")
            sr.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=sr.joinpath(f"login-{int(time.time())}.png"))
            return None

    async def invoke(self) -> bool:
        self.page.on("response", self._on_response_anything)

        for _ in range(3):
            await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")

            if await self._wait_store_isloggedin_true(timeout_s=30):
                logger.success("Epic Games is already logged in")
                return True

            if await self._login():
                # 登录成功后再回到商店页确认一次（避免 analytics 信号误判 / store 域未同步）
                with suppress(Exception):
                    await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")
                if await self._wait_store_isloggedin_true(timeout_s=30):
                    return True

        return False
