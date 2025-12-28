# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import json
import time
from contextlib import suppress
from json import JSONDecodeError
from typing import List

import httpx
from hcaptcha_challenger.agent import AgentV
from loguru import logger
from playwright.async_api import Page
from playwright.async_api import expect, TimeoutError, FrameLocator
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from models import OrderItem, Order
from models import PromotionGame
from settings import settings, RUNTIME_DIR

URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = (
    f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
)
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"
URL_ORDER_HISTORY = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"


URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_PRODUCT_BUNDLES = "https://store.epicgames.com/en-US/bundles/"


def get_promotions() -> List[PromotionGame]:
    """获取周免游戏数据"""
    def is_discount_game(prot: dict) -> bool | None:
        with suppress(KeyError, IndexError, TypeError):
            offers = prot["promotions"]["promotionalOffers"][0]["promotionalOffers"]
            for i, offer in enumerate(offers):
                if offer["discountSetting"]["discountPercentage"] == 0:
                    return True

    promotions: List[PromotionGame] = []

    resp = httpx.get(URL_PROMOTIONS, params={"local": "zh-CN"})

    try:
        data = resp.json()
    except JSONDecodeError as err:
        logger.error("Failed to get promotions", err=err)
        return []

    with suppress(Exception):
        cache_key = RUNTIME_DIR.joinpath("promotions.json")
        cache_key.parent.mkdir(parents=True, exist_ok=True)
        cache_key.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Get store promotion data and <this week free> games
    for e in data["data"]["Catalog"]["searchStore"]["elements"]:
        if not is_discount_game(e):
            continue

        try:
            e["url"] = f"{URL_PRODUCT_PAGE.rstrip('/')}/{e['offerMappings'][0]['pageSlug']}"
        except (KeyError, IndexError):
            if e.get("productSlug"):
                e["url"] = f"{URL_PRODUCT_PAGE.rstrip('/')}/{e['productSlug']}"
            else:
                logger.info(f"Failed to get URL: {e}")
                continue

        logger.info(e["url"])
        promotions.append(PromotionGame(**e))

    return promotions


class EpicAgent:
    def __init__(self, page: Page):
        self.page = page
        self.epic_games = EpicGames(self.page)
        self._promotions: List[PromotionGame] = []
        self._ctx_cookies_is_available: bool = False
        self._orders: List[OrderItem] = []
        self._namespaces: List[str] = []
        self._cookies = None

    async def _sync_order_history(self):
        if self._orders:
            return
        completed_orders: List[OrderItem] = []
        try:
            await self.page.goto("https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory")
            text_content = await self.page.text_content("//pre")
            data = json.loads(text_content)
            for _order in data["orders"]:
                order = Order(**_order)
                if order.orderType != "PURCHASE":
                    continue
                for item in order.items:
                    if not item.namespace or len(item.namespace) != 32:
                        continue
                    completed_orders.append(item)
        except Exception as err:
            logger.warning(err)
        self._orders = completed_orders

    async def _check_orders(self):
        await self._sync_order_history()
        self._namespaces = self._namespaces or [order.namespace for order in self._orders]
        self._promotions = [p for p in get_promotions() if p.namespace not in self._namespaces]

    async def _should_ignore_task(self) -> bool:
        self._ctx_cookies_is_available = False
        await self.page.goto(URL_CLAIM, wait_until="domcontentloaded")

        # 以账号 JSON API 探测登录态，避免 store 页 isloggedin 不更新导致误判
        status = None
        with suppress(Exception):
            nav = self.page.locator("//egs-navigation")
            status = await nav.get_attribute("isloggedin")

        logged_in = False
        try:
            resp = await self.page.request.get(URL_ORDER_HISTORY, timeout=15000)
            if resp.ok:
                content_type = (resp.headers.get("content-type") or "").lower()
                if "application/json" in content_type:
                    data = await resp.json()
                    logged_in = isinstance(data, dict) and ("orders" in data)
        except Exception:
            logged_in = False

        if not logged_in:
            cookie_count = 0
            cookie_names: List[str] = []
            with suppress(Exception):
                cookies = await self.page.context.cookies(URL_CLAIM)
                cookie_count = len(cookies)
                cookie_names = [
                    c.get("name") for c in cookies if isinstance(c, dict) and c.get("name")
                ]
            logger.error(
                "❌ not logged in (account API probe failed) | store_isloggedin={} url={} cookie_count={} cookie_names_sample={}",
                status,
                self.page.url,
                cookie_count,
                cookie_names[:10],
            )
            return False
        self._ctx_cookies_is_available = True
        await self._check_orders()
        if not self._promotions:
            return True
        return False

    async def collect_epic_games(self):
        if await self._should_ignore_task():
            logger.success("All week-free games are already in the library")
            return

        if not self._ctx_cookies_is_available:
            return

        if not self._promotions:
            await self._check_orders()

        if not self._promotions:
            logger.success("All week-free games are already in the library")
            return

        for p in self._promotions:
            pj = json.dumps({"title": p.title, "url": p.url}, indent=2, ensure_ascii=False)
            logger.debug(f"Discover promotion \n{pj}")

        if self._promotions:
            try:
                await self.epic_games.collect_weekly_games(self._promotions)
            except Exception as e:
                logger.exception(e)
        
        logger.debug("All tasks in the workflow have been completed")


class EpicGames:
    def __init__(self, page: Page):
        self.page = page
        self._promotions: List[PromotionGame] = []
        # 记录未能“确认入库”的 URL，最后会让任务失败，避免假成功
        self._unverified_claims: List[str] = []

    @staticmethod
    def _normalize_url(url: str) -> str:
        return (url or "").strip()

    @staticmethod
    async def _is_in_library(page: Page) -> bool:
        """
        基于商品页右侧按钮文本判断是否已入库（en-US: In Library / Owned）。
        仅用于 UI 验证，不依赖 order history。
        """
        btn_list = page.locator("//aside//button")
        try:
            aside_btn_count = await btn_list.count()
        except TimeoutError:
            return False

        texts = ""
        for i in range(aside_btn_count):
            with suppress(Exception):
                btn = btn_list.nth(i)
                t = await btn.text_content()
                if t:
                    texts += t

        return ("In Library" in texts) or ("Owned" in texts)

    async def _verify_in_library(self, page: Page, url: str, timeout_s: float = 45.0) -> bool:
        """
        反复打开/刷新商品页，等待 UI 变为 In Library。
        用于确认结账/领取确实成功，而不是“盲推断”。
        """
        url = self._normalize_url(url)
        if not url:
            return False

        deadline = time.monotonic() + float(timeout_s)
        last_err: Exception | None = None

        while time.monotonic() < deadline:
            try:
                await page.goto(url, wait_until="domcontentloaded")
                if await self._is_in_library(page):
                    return True
            except Exception as e:
                last_err = e
            await page.wait_for_timeout(1500)

        if last_err:
            logger.debug(f"Verify in library failed with last error: {type(last_err).__name__}: {last_err}")
        return False

    @staticmethod
    async def _agree_license(page: Page):
        logger.debug("Agree license")
        with suppress(TimeoutError):
            await page.click("//label[@for='agree']", timeout=4000)
            accept = page.locator("//button//span[text()='Accept']")
            if await accept.is_enabled():
                await accept.click()

    @staticmethod
    async def _active_purchase_container(page: Page):
        logger.debug("Scanning for purchase iframe...")
        iframe_selector = "//iframe[contains(@id, 'webPurchaseContainer') or contains(@src, 'purchase')]"
        wpc = page.frame_locator(iframe_selector).first

        logger.debug("Looking for 'PLACE ORDER' button...")
        place_order_btn = wpc.locator("button", has_text="PLACE ORDER")
        confirm_btn = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
        
        try:
            await expect(place_order_btn).to_be_visible(timeout=15000)
            logger.debug("✅ Found 'PLACE ORDER' button via text match")
            return wpc, place_order_btn
        except AssertionError:
            pass
            
        try:
            await expect(confirm_btn).to_be_visible(timeout=5000)
            logger.debug("✅ Found button via CSS class match")
            return wpc, confirm_btn
        except AssertionError:
            logger.warning("Primary buttons not found in iframe.")
            raise AssertionError("Could not find Place Order button in iframe")

    @staticmethod
    async def _uk_confirm_order(wpc: FrameLocator):
        logger.debug("UK confirm order")
        with suppress(TimeoutError):
            accept = wpc.locator("//button[contains(@class, 'payment-confirm__btn')]")
            if await accept.is_enabled(timeout=5000):
                await accept.click()
                return True

    async def _handle_instant_checkout(self, page: Page, product_url: str) -> bool:
        """处理点击 'Get' 后弹出的即时结账窗口，并在最后强验证是否入库。"""
        product_url = self._normalize_url(product_url)
        logger.info("🚀 Triggering Instant Checkout Flow... url={}", product_url)
        agent = AgentV(page=page, agent_config=settings)

        try:
            # 1. 定位按钮
            wpc, payment_btn = await self._active_purchase_container(page)
            
            # 2. 点击下单 (必须强制点击)
            logger.debug(f"Clicking payment button: {await payment_btn.text_content()}")
            await payment_btn.click(force=True)
            
            # 给一点反应时间
            await page.wait_for_timeout(3000)
            
            # 3. 尝试处理验证码 (增加容错)
            # 关键修改：不再“盲推断成功”，而是以“入库验证”为准。
            # 某些情况下 challenge frame 会快速刷新/销毁，导致 Frame was detached；这里做轻量重试。
            captcha_solved_or_not_needed = False
            last_captcha_err: Exception | None = None
            for attempt in range(3):
                try:
                    logger.debug("Checking for CAPTCHA... attempt={}", attempt + 1)
                    await agent.wait_for_challenge()
                    captcha_solved_or_not_needed = True
                    break
                except Exception as e:
                    last_captcha_err = e
                    msg = str(e)
                    # 常见：没有验证码/找不到 frame（视为不需要验证码）
                    if "Cannot find a valid challenge frame" in msg or "captcha payload" in msg:
                        logger.info(f"CAPTCHA not detected (skip): {type(e).__name__}: {e}")
                        captcha_solved_or_not_needed = True
                        break
                    # 常见：frame 刷新导致短暂 detach，等一会再试
                    if "Frame was detached" in msg:
                        logger.warning(f"CAPTCHA frame detached, retrying: {type(e).__name__}: {e}")
                        await page.wait_for_timeout(1500)
                        continue
                    logger.warning(f"CAPTCHA solve error: {type(e).__name__}: {e}")
                    break

            if not captcha_solved_or_not_needed and last_captcha_err:
                logger.warning(
                    f"CAPTCHA solving did not finish cleanly: {type(last_captcha_err).__name__}: {last_captcha_err}"
                )

            # 4. 强验证：回到商品页确认是否已入库
            if product_url and await self._verify_in_library(page, product_url, timeout_s=60):
                logger.success("🎉 Instant checkout verified: In Library")
                return True

            # 仍未入库：保留现场用于外层重试/失败处理
            logger.error("❌ Instant checkout NOT verified (still not in library)")
            return False

        except Exception as err:
            # 只要之前点击了按钮，就有可能已经成功入库。不要抛出致命错误。
            logger.warning(f"Instant checkout warning (Game might still be claimed): {err}")
            # 刷新页面以重置状态，防止影响下一个游戏
            with suppress(Exception):
                await page.reload()
            if product_url and await self._verify_in_library(page, product_url, timeout_s=30):
                logger.success("🎉 Instant checkout verified after exception: In Library")
                return True
            return False

    async def add_promotion_to_cart(self, page: Page, urls: List[str]) -> bool:
        has_pending_cart_items = False

        for url in urls:
            url = self._normalize_url(url)
            if not url:
                continue
            await page.goto(url, wait_until="load")

            # 1. 处理弹窗
            try:
                continue_btn = page.locator("//button//span[text()='Continue']")
                if await continue_btn.is_visible(timeout=5000):
                    logger.debug("Found Content Warning, clicking Continue...")
                    await continue_btn.click()
            except Exception:
                pass 

            # 2. 检查库状态
            btn_list = page.locator("//aside//button")
            try:
                aside_btn_count = await btn_list.count()
            except TimeoutError:
                logger.warning(f"Failed to load game page buttons - {url=}")
                continue

            texts = ""
            for i in range(aside_btn_count):
                btn = btn_list.nth(i)
                texts += await btn.text_content()

            if "In Library" in texts or "Owned" in texts:
                logger.success(f"Already in the library - {url=}")
                continue

            # 3. 定位核心按钮
            purchase_btn = page.locator("//aside//button[@data-testid='purchase-cta-button']")
            try:
                purchase_status = await purchase_btn.text_content(timeout=5000)
            except TimeoutError:
                logger.warning(f"Could not find purchase button - {url=}")
                continue

            if "Buy Now" in purchase_status or ("Get" not in purchase_status and "Add To Cart" not in purchase_status):
                logger.warning(f"Not available for purchase - {url=}")
                continue

            # 4. 智能分支处理（Get: 即时结账 + 入库验证；Add To Cart: 走购物车）
            try:
                target_btn = purchase_btn
                text = (await target_btn.text_content()) or ""

                if "Get" in text:
                    claimed = False
                    for attempt in range(2):
                        logger.debug(
                            "👉 Found 'Get' button, starting instant checkout - attempt={}/2 url={}",
                            attempt + 1,
                            url,
                        )
                        await target_btn.click()
                        claimed = await self._handle_instant_checkout(page, product_url=url)
                        if claimed:
                            break
                        logger.warning(f"Instant checkout not verified, retrying - {url=}")
                        with suppress(Exception):
                            await page.reload(wait_until="domcontentloaded")
                        target_btn = page.locator("//aside//button[@data-testid='purchase-cta-button']")

                    if not claimed:
                        self._unverified_claims.append(url)
                        logger.error(f"❌ Claim not verified - {url=}")

                elif "Add To Cart" in text:
                    logger.debug(f"🛒 Found 'Add To Cart' button - {url=}")
                    await target_btn.click()
                    with suppress(TimeoutError):
                        await expect(target_btn).to_have_text("View In Cart", timeout=10000)
                    has_pending_cart_items = True

            except Exception as err:
                logger.warning(f"Failed to process game - {type(err).__name__}: {err}")
                self._unverified_claims.append(url)
                continue

        return has_pending_cart_items

    async def _empty_cart(self, page: Page, wait_rerender: int = 30) -> bool | None:
        has_paid_free = False
        try:
            cards = await page.query_selector_all("//div[@data-testid='offer-card-layout-wrapper']")
            for card in cards:
                is_free = await card.query_selector("//span[text()='Free']")
                if not is_free:
                    has_paid_free = True
                    wishlist_btn = await card.query_selector(
                        "//button//span[text()='Move to wishlist']"
                    )
                    await wishlist_btn.click()

            if has_paid_free and wait_rerender:
                wait_rerender -= 1
                await page.wait_for_timeout(2000)
                return await self._empty_cart(page, wait_rerender)
            return True
        except TimeoutError as err:
            logger.warning("Failed to empty shopping cart", err=err)
            return False

    async def _purchase_free_game(self):
        await self.page.goto(URL_CART, wait_until="domcontentloaded")
        logger.debug("Move ALL paid games from the shopping cart out")
        await self._empty_cart(self.page)

        agent = AgentV(page=self.page, agent_config=settings)
        await self.page.click("//button//span[text()='Check Out']")
        await self._agree_license(self.page)

        try:
            logger.debug("Move to webPurchaseContainer iframe")
            wpc, payment_btn = await self._active_purchase_container(self.page)
            logger.debug("Click payment button")
            await self._uk_confirm_order(wpc)
            await agent.wait_for_challenge()
        except Exception as err:
            logger.warning(f"Failed to solve captcha - {err}")
            await self.page.reload()
            return await self._purchase_free_game()

    @retry(retry=retry_if_exception_type(TimeoutError), stop=stop_after_attempt(2), reraise=True)
    async def collect_weekly_games(self, promotions: List[PromotionGame]):
        # 清空上一轮残留
        self._unverified_claims = []

        urls = [p.url for p in promotions]
        has_cart_items = await self.add_promotion_to_cart(self.page, urls)

        if has_cart_items:
            await self._purchase_free_game()
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS)
                logger.success("🎉 Successfully collected cart games")
            except TimeoutError:
                logger.warning("Failed to collect cart games")
        # 无论走哪条流程，最后都做一次“入库验证”，避免 Actions 误报成功
        verify_failed: List[str] = []
        for p in promotions:
            url = self._normalize_url(p.url)
            if not url:
                continue
            ok = await self._verify_in_library(self.page, url, timeout_s=30)
            if not ok:
                verify_failed.append(url)

        # 合并失败列表（即时结账阶段失败 + 最终验证失败）
        all_failed = list(dict.fromkeys(self._unverified_claims + verify_failed))
        if all_failed:
            logger.error("❌ Some games were NOT added to library (verified): {}", all_failed)
            raise RuntimeError(f"Claim not verified for: {all_failed}")

        logger.success("🎉 Process completed (verified in library)")
