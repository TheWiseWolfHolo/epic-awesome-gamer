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
URL_ORDER_HISTORY = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"


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

    async def _probe_account_logged_in(self, timeout_ms: float = 15000) -> bool:
        """
        用“账号 JSON API”探测是否已登录，比 store 页的 isloggedin 更可靠。
        - 已登录：通常返回 JSON，包含 orders 字段
        - 未登录：可能 302/401/403，或返回 HTML
        """
        try:
            resp = await self.page.request.get(URL_ORDER_HISTORY, timeout=timeout_ms)
            if not resp.ok:
                return False
            headers = resp.headers or {}
            content_type = (headers.get("content-type") or "").lower()
            if "application/json" not in content_type:
                return False
            data = await resp.json()
            return isinstance(data, dict) and ("orders" in data)
        except Exception as e:
            logger.debug(f"Probe account login failed: {type(e).__name__}: {e}")
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

            # ========================================================
            # 核心策略：主动轮询登录态 + captcha 作为后台任务
            # hCaptcha 可能：1)静默放行 2)要求解题 3)直接阻止
            # 无论哪种情况，我们都要持续检查是否已登录
            # ========================================================
            captcha_task = asyncio.create_task(agent.wait_for_challenge())
            login_signal_task = asyncio.create_task(self._is_login_success_signal.get())

            login_confirmed = False
            max_probe_time = 120.0  # 最多探测 2 分钟

            async def _probe_login_loop():
                """持续探测登录态，直到成功或超时"""
                start = asyncio.get_event_loop().time()
                probe_count = 0
                while asyncio.get_event_loop().time() - start < max_probe_time:
                    probe_count += 1
                    # 每 5 秒探测一次 API
                    if await self._probe_account_logged_in(timeout_ms=8000):
                        logger.success(f"Login confirmed by API probe (attempt #{probe_count})")
                        return True
                    await asyncio.sleep(5)
                return False

            probe_task = asyncio.create_task(_probe_login_loop())

            # 等待任一任务完成：登录信号 / API 探测成功 / captcha 完成
            done, pending = await asyncio.wait(
                {captcha_task, login_signal_task, probe_task},
                timeout=max_probe_time + 30,
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 检查结果
            if login_signal_task in done:
                logger.success("Login confirmed by analytics signal")
                login_confirmed = True
            elif probe_task in done:
                try:
                    login_confirmed = probe_task.result()
                except Exception:
                    pass
            elif captcha_task in done:
                # captcha 完成了，再给一点时间让登录完成
                try:
                    captcha_task.result()
                    logger.debug("Captcha task completed, waiting for login confirmation...")
                except Exception as e:
                    logger.warning(f"Captcha task error: {type(e).__name__}")
                # 等待登录信号或 API 确认
                await asyncio.sleep(3)
                if await self._probe_account_logged_in(timeout_ms=15000):
                    logger.success("Login confirmed by API probe after captcha")
                    login_confirmed = True

            # 取消所有挂起的任务
            for t in pending:
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t

            # 最终确认
            if not login_confirmed:
                # 最后一次尝试
                await self.page.wait_for_timeout(3000)
                if await self._probe_account_logged_in(timeout_ms=20000):
                    logger.success("Login confirmed by final API probe")
                    login_confirmed = True

            if not login_confirmed:
                raise RuntimeError("Login failed: all probes returned negative")

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

        for attempt in range(3):
            logger.debug(f"Login attempt {attempt + 1}/3")

            # 优先使用账号接口探测登录态，避免 store 页 isloggedin 长期不更新导致反复重登
            if await self._probe_account_logged_in(timeout_ms=15000):
                logger.success("Epic Games is already logged in (account API probe)")
                return True

            try:
                login_result = await self._login()
            except Exception as e:
                logger.warning(f"Login attempt {attempt + 1} raised exception: {type(e).__name__}: {e}")
                # 即使 _login() 抛异常，也要检查是否其实已登录
                await self.page.wait_for_timeout(2000)
                if await self._probe_account_logged_in(timeout_ms=15000):
                    logger.success("Login confirmed by API probe after exception")
                    return True
                continue

            if login_result:
                # 登录成功后用账号接口确认（比 store isloggedin 更可靠）
                if await self._probe_account_logged_in(timeout_ms=20000):
                    return True
                logger.warning("Login returned success but account API probe failed, will retry")

        # 最后一次尝试：可能已经登录了但 API 探测失败
        logger.warning("All login attempts exhausted, final probe...")
        if await self._probe_account_logged_in(timeout_ms=30000):
            logger.success("Final API probe succeeded")
            return True

        return False
