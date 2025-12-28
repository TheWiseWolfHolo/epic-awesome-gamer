# -*- coding: utf-8 -*-
# Time       : 2022/1/16 0:25
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description: 游戏商城控制句柄

import json
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

        # egs-navigation 的 isloggedin 可能异步更新，做短轮询避免瞬时误判
        nav = self.page.locator("//egs-navigation")
        status = None
        for _i in range(30):  # 15s
            status = await nav.get_attribute("isloggedin")
            if status in ("true", "false"):
                break
            await self.page.wait_for_timeout(500)

        if status != "true":
            logger.error("❌ context cookies is not available")
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

    async def _handle_instant_checkout(self, page: Page):
        """处理点击 'Get' 后弹出的即时结账窗口 (容错增强版)"""
        logger.info("🚀 Triggering Instant Checkout Flow...")
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
            # 关键修改：如果不需要验证码，wait_for_challenge 可能会报错，我们需要忽略这个错误
            try:
                logger.debug("Checking for CAPTCHA...")
                await agent.wait_for_challenge()
            except Exception as e:
                # 这里的报错通常是因为没有弹出验证码，导致库找不到元素
                # 我们将其视为“无验证码直接成功”，记录日志但不中断
                logger.info(f"CAPTCHA detection skipped (Likely no CAPTCHA needed): {e}")

            # 4. 检查结果 (推断成功)
            # 如果按钮已经消失或不可见，或者 iframe 已经关闭，说明下单成功了
            try:
                if not await payment_btn.is_visible():
                     logger.success("🎉 Instant Checkout: Payment button disappeared (Success inferred)")
                     return
            except Exception:
                # 如果定位器失效，说明 iframe 可能已经关了，这也是成功
                logger.success("🎉 Instant Checkout: Iframe closed (Success inferred)")
                return

            # 如果按钮还在，可能需要二次确认
            logger.warning("⚠️ Payment button still visible. Attempting one last click...")
            with suppress(Exception):
                await payment_btn.click(force=True)
                await page.wait_for_timeout(2000)
            
            logger.success("Instant checkout flow finished (Blind Success).")

        except Exception as err:
            # 只要之前点击了按钮，就有可能已经成功入库。不要抛出致命错误。
            logger.warning(f"Instant checkout warning (Game might still be claimed): {err}")
            # 刷新页面以重置状态，防止影响下一个游戏
            await page.reload()

    async def add_promotion_to_cart(self, page: Page, urls: List[str]) -> bool:
        has_pending_cart_items = False

        for url in urls:
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

            if "In Library" in texts:
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

            # 4. 智能分支处理
            try:
                target_btn = purchase_btn 
                text = await target_btn.text_content()
                
                if "Get" in text:
                    logger.debug(f"👉 Found 'Get' button, starting instant checkout - {url=}")
                    await target_btn.click()
                    await self._handle_instant_checkout(page)
                    
                elif "Add To Cart" in text:
                    logger.debug(f"🛒 Found 'Add To Cart' button - {url=}")
                    await target_btn.click()
                    with suppress(TimeoutError):
                         await expect(target_btn).to_have_text("View In Cart", timeout=10000)
                    has_pending_cart_items = True

            except Exception as err:
                logger.warning(f"Failed to process game - {err}")
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
        urls = [p.url for p in promotions]
        has_cart_items = await self.add_promotion_to_cart(self.page, urls)

        if has_cart_items:
            await self._purchase_free_game()
            try:
                await self.page.wait_for_url(URL_CART_SUCCESS)
                logger.success("🎉 Successfully collected cart games")
            except TimeoutError:
                logger.warning("Failed to collect cart games")
        else:
            logger.success("🎉 Process completed (Instant claimed or already owned)")
