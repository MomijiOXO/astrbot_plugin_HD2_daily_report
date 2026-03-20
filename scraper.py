import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

from astrbot.api import logger

from .models import ReportResult


class BiliScraper:
    """B站爬虫：浏览器管理、DOM 解析、图片下载、登录。"""

    # ── B 站 DOM 选择器（集中管理，前端改版时只需改这里） ──

    # 动态列表页
    _SEL_DYN_ITEM = ".bili-dyn-item"
    """单条动态卡片。"""

    _SEL_DYN_TITLE = ".dyn-card-opus__title"
    """动态标题元素（包含 data-url 属性）。"""

    _SEL_DYN_PICS = ".dyn-card-opus__pics"
    """动态图片容器。"""

    _SEL_PIC_SOURCES = "source[srcset]"
    """图片容器内的 <source> 元素。"""

    _SEL_PIC_IMGS = "img[src]"
    """图片容器内的 <img> 元素。"""

    # 登录状态检测
    _SEL_UNLOGIN_HINT = ".unlogin-tip, .bili-dyn-login-tip"
    """未登录态提示元素。"""

    _SEL_AVATAR = ".header-avatar, .bili-avatar"
    """已登录头像元素（用于扫码成功检测）。"""

    # 快报标题匹配
    _RE_REPORT_TITLE = re.compile(r"^银河快报(\d{4})$")
    """银河快报标题正则，捕获组 1 为 MMDD 期号。"""

    # 登录页
    _BILI_LOGIN_URL = "https://passport.bilibili.com/login"
    _QR_SELECTORS = [
        ".login-scan-box img",
        ".qrcode-img img",
        ".login-scan-box",
        ".qrcode-img",
        ".login-panel",
    ]

    def __init__(
        self,
        data_dir: Path,
        profile_dir: Path,
        *,
        headless: bool = True,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        request_timeout: int = 30,
        max_images: int = 0,
    ):
        self._data_dir = data_dir
        self._profile_dir = profile_dir
        self._headless = headless
        self._user_agent = user_agent
        self._request_timeout = request_timeout
        self._max_images = max_images

        # 浏览器实例跟踪
        self._pw = None
        self._ctx = None

    # ── 浏览器生命周期 ──

    async def close_browser(self):
        """安全关闭当前跟踪的浏览器上下文和 Playwright 实例。"""
        if self._ctx is not None:
            try:
                await self._ctx.close()
                logger.info("已关闭浏览器上下文")
            except Exception as e:
                logger.warning(f"关闭浏览器上下文时异常: {e}")
            self._ctx = None
        if self._pw is not None:
            try:
                await self._pw.stop()
                logger.info("已停止 Playwright 实例")
            except Exception as e:
                logger.warning(f"停止 Playwright 实例时异常: {e}")
            self._pw = None

    async def launch_persistent_browser(self):
        """启动 Playwright persistent context 并返回 (playwright, context)。"""
        await self.close_browser()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
            user_agent=self._user_agent,
        )
        self._pw = pw
        self._ctx = ctx
        return pw, ctx

    # ── URL 处理 ──

    @staticmethod
    def normalize_image_url(url: str) -> str:
        """补全协议头，返回归一化后的图片 URL。"""
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return url

    def build_candidate_image_urls(self, url: str) -> list[str]:
        """对单个图片 URL 生成候选列表：原图（去 @ 后缀）+ 原始 URL，去重。"""
        url = self.normalize_image_url(url)
        candidates = []
        at_idx = url.find("@")
        if at_idx != -1:
            candidates.append(url[:at_idx])
        candidates.append(url)
        seen = set()
        result = []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

    # ── 页面解析 ──

    async def wait_for_report_cards(self, page):
        """等待 B 站 SPA 动态卡片渲染完成。"""
        timeout_ms = self._request_timeout * 1000
        logger.info("开始等待页面渲染...")
        try:
            await page.wait_for_selector(self._SEL_DYN_ITEM, timeout=timeout_ms)
            logger.info(f"等待成功: {self._SEL_DYN_ITEM} 已出现")
            return
        except Exception:
            logger.warning(f"{self._SEL_DYN_ITEM} 未出现，尝试兜底选择器 {self._SEL_DYN_TITLE}")

        try:
            await page.wait_for_selector(self._SEL_DYN_TITLE, timeout=timeout_ms)
            logger.info(f"等待成功: {self._SEL_DYN_TITLE} 已出现（兜底）")
            return
        except Exception:
            pass

        logger.error("等待失败: 动态内容未渲染")
        debug_dir = self._data_dir
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(debug_dir / "debug_page.png"), full_page=True)
            html = await page.content()
            (debug_dir / "debug_page.html").write_text(html, encoding="utf-8")
            title = await page.title()
            logger.error(f"调试信息 — url: {page.url}, title: {title}")
            logger.error(f"已保存 debug_page.png 和 debug_page.html 到 {debug_dir}")
        except Exception as e:
            logger.error(f"保存调试信息失败: {e}")
        raise TimeoutError("页面动态内容等待超时")

    async def parse_latest_report_from_page(self, page) -> ReportResult | None:
        """从渲染后的 DOM 中解析最新一期银河快报。"""
        items = await page.query_selector_all(self._SEL_DYN_ITEM)
        logger.info(f"{self._SEL_DYN_ITEM} 总数: {len(items)}")

        candidates: list[ReportResult] = []

        for item in items:
            title_el = await item.query_selector(self._SEL_DYN_TITLE)
            if not title_el:
                continue
            title_text = (await title_el.inner_text()).strip()
            m = self._RE_REPORT_TITLE.match(title_text)
            if not m:
                continue

            issue_code = m.group(1)

            data_url = await title_el.get_attribute("data-url") or ""
            if data_url.startswith("//"):
                data_url = "https:" + data_url

            raw_urls = []
            pics_container = await item.query_selector(self._SEL_DYN_PICS)
            if pics_container:
                # 优先使用 <source srcset>（通常质量更高）
                for src_el in await pics_container.query_selector_all(self._SEL_PIC_SOURCES):
                    srcset = await src_el.get_attribute("srcset") or ""
                    if srcset:
                        raw_urls.append(srcset.split(",")[0].split()[0])
                # 仅当 <source> 未获取到任何 URL 时，才回退到 <img src>
                if not raw_urls:
                    for img_el in await pics_container.query_selector_all(self._SEL_PIC_IMGS):
                        src = await img_el.get_attribute("src") or ""
                        if src:
                            raw_urls.append(src)

            seen_urls: set[str] = set()
            image_urls: list[str] = []
            for raw in raw_urls:
                normalized = self.normalize_image_url(raw)
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    image_urls.append(normalized)

            if self._max_images > 0:
                image_urls = image_urls[: self._max_images]

            candidates.append(ReportResult(
                matched_title=title_text,
                issue_code=issue_code,
                source_url=data_url,
                image_urls=image_urls,
            ))

        logger.info(f"匹配到银河快报标题的数量: {len(candidates)}")

        if not candidates:
            return None

        best = max(candidates, key=lambda c: c.issue_code)
        logger.info(f"最终选中: {best.matched_title} (issue_code={best.issue_code})")
        return best

    # ── 图片下载 ──

    async def download_image(self, page, url: str, file_path: Path) -> bool:
        """使用 Playwright 页面上下文下载单张图片，成功返回 True。"""
        try:
            resp = await page.context.request.get(url, timeout=self._request_timeout * 1000)
            if resp.ok:
                file_path.write_bytes(await resp.body())
                logger.info(f"下载成功: {file_path.name} <- {url}")
                return True
            logger.warning(f"下载失败 (HTTP {resp.status}): {url}")
        except Exception as e:
            logger.warning(f"下载异常: {url} -> {e}")
        return False

    async def download_report_images(self, page, image_urls: list[str], cache_dir: Path) -> list[Path]:
        """并行下载图片列表，返回成功保存的本地路径（保持原始顺序）。"""

        async def _download_one(idx: int, raw_url: str) -> Path | None:
            dest = cache_dir / f"{idx}.jpg"
            for candidate_url in self.build_candidate_image_urls(raw_url):
                if await self.download_image(page, candidate_url, dest):
                    return dest
            logger.warning(f"第 {idx} 张图片所有候选 URL 均下载失败")
            return None

        results = await asyncio.gather(
            *(_download_one(idx, url) for idx, url in enumerate(image_urls, start=1))
        )
        return [p for p in results if p is not None]

    # ── 登录检测 ──

    async def is_login_invalid(self, page, parsed_result: ReportResult | None) -> tuple[bool, str]:
        """判断当前页面是否处于登录失效状态，返回 (是否失效, 原因)。"""
        current_url = page.url
        if "passport.bilibili.com" in current_url or "login" in current_url.lower():
            return True, f"页面跳转到登录页: {current_url}"

        title = await page.title()
        title_lower = title.lower()
        if "登录" in title or "login" in title_lower:
            return True, f"页面标题含登录关键词: {title}"

        unlogin_el = await page.query_selector(self._SEL_UNLOGIN_HINT)
        if unlogin_el:
            return True, "页面存在未登录态提示元素"

        items = await page.query_selector_all(self._SEL_DYN_ITEM)
        if not items and parsed_result is None:
            # 页面无动态卡片，但如果检测到已登录头像则不判定为未登录
            avatar = await page.query_selector(self._SEL_AVATAR)
            if avatar:
                return False, ""
            return True, "页面无动态卡片且解析结果为空，可能未登录"

        return False, ""

    # ── QR 登录 ──

    async def open_login_page_and_capture_qrcode(self, page) -> Path:
        """在已有 page 上打开 B 站登录页，截取二维码并返回截图路径。"""
        logger.info(f"打开登录页: {self._BILI_LOGIN_URL}")
        await page.goto(self._BILI_LOGIN_URL, wait_until="domcontentloaded",
                        timeout=self._request_timeout * 1000)

        qr_path = self._data_dir / "qrcode.png"
        screenshot_taken = False

        for selector in self._QR_SELECTORS:
            try:
                el = await page.wait_for_selector(selector, timeout=8000)
                if el:
                    await el.screenshot(path=str(qr_path))
                    logger.info(f"二维码截图成功 (selector={selector})")
                    screenshot_taken = True
                    break
            except Exception:
                continue

        if not screenshot_taken:
            logger.warning("未定位到二维码元素，截取整页作为兜底")
            await page.screenshot(path=str(qr_path))

        return qr_path

    async def wait_for_login_success(self, page, timeout: float = 120,
                                     poll_interval: float = 3.0) -> bool:
        """轮询等待用户扫码登录成功。"""
        elapsed = 0.0
        logger.info(f"等待用户扫码登录 (timeout={timeout}s)...")
        while elapsed < timeout:
            current_url = page.url
            if "passport.bilibili.com" not in current_url:
                logger.info(f"扫码登录成功，页面已跳转: {current_url}")
                return True
            avatar = await page.query_selector(self._SEL_AVATAR)
            if avatar:
                logger.info("扫码登录成功，检测到头像元素")
                return True
            await page.wait_for_timeout(poll_interval * 1000)
            elapsed += poll_interval
        logger.warning(f"本轮等待扫码超时 ({timeout}s)")
        return False
