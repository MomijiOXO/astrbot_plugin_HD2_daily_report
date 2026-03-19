import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import AstrBotConfig, logger

_PLUGIN_NAME = "astrbot_plugin_bili_daily_report"

# 写死常量
TRIGGER_KEYWORD = "今日快报"
RELOGIN_KEYWORD = "bili_qrcode"
RELOGIN_RESET_KEYWORD = "bili_qrcode_reset"
SCHEDULE_FETCH_TIME = "03:00"
SCHEDULE_SEND_TIME = "09:00"
BROWSER_HEADLESS = True
REQUEST_TIMEOUT_SECONDS = 30
MAX_IMAGES_PER_REPORT = 0
PROFILE_DIR = ""
PROFILE_CLEANUP_INTERVAL_DAYS = 7

# 配置项默认值
DEFAULTS = {
    "schedule_send_time": SCHEDULE_SEND_TIME,
    "whitelist_groups": [],
    "blacklist_groups": [],
    "admin_user_ids": [],
    "enable_debug_log": False,
}

# 固定常量
TARGET_URL = "https://space.bilibili.com/3546777720457465/dynamic"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _get(config: dict, key: str):
    """从配置中读取值，缺失或为 None 时回退到默认值。"""
    val = config.get(key)
    return val if val is not None else DEFAULTS[key]


@register(
    "astrbot_plugin_bili_daily_report",
    "椛椿",
    "B站今日快报 — 自动抓取B站动态并生成快报",
    "0.1.0",
)
class BiliDailyReportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir = Path(StarTools.get_data_dir(plugin_name=_PLUGIN_NAME))

        # 读取配置
        self.schedule_send_time: str = _get(config, "schedule_send_time")
        self.whitelist_groups: list = _get(config, "whitelist_groups")
        self.blacklist_groups: list = _get(config, "blacklist_groups")
        self.admin_user_ids: list = _get(config, "admin_user_ids")
        self.debug: bool = bool(_get(config, "enable_debug_log"))

        # 浏览器实例跟踪（用于安全关闭）
        self._pw = None
        self._ctx = None

    def ensure_profile_dir(self) -> Path:
        """确保 profile 目录存在并返回其路径。"""
        if PROFILE_DIR:
            p = Path(PROFILE_DIR)
        else:
            p = self._data_dir / "profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ── 权限与目标群辅助 ──

    def _safe_list(self, val) -> list:
        """将配置值安全转为字符串列表。"""
        if isinstance(val, list):
            return [str(v) for v in val]
        return []

    def get_admin_user_ids(self) -> list:
        return self._safe_list(self.admin_user_ids)

    def is_allowed_group(self, group_id: str) -> bool:
        """判断群组是否允许发送。
        - 黑名单优先：若在黑名单中则禁止
        - 白名单次之：若白名单非空且群组不在白名单中则禁止
        - 两者都为空：默认允许所有群组
        """
        gid_str = str(group_id)
        whitelist = set(self._safe_list(self.whitelist_groups))
        blacklist = set(self._safe_list(self.blacklist_groups))

        # 黑名单检查
        if gid_str in blacklist:
            return False

        # 白名单检查（只有白名单非空时才生效）
        if whitelist and gid_str not in whitelist:
            return False

        return True

    def get_all_group_ids(self) -> list:
        """获取所有已加入的群聊 ID 列表。"""
        try:
            group_list = self.context.get_group_list()
            return [str(g.get("group_id", g.get("id", ""))) for g in group_list if g]
        except Exception as e:
            logger.warning(f"[群聊列表] 获取全部群聊失败: {e}")
            return []

    def is_admin_user(self, user_id) -> bool:
        admins = self.get_admin_user_ids()
        result = str(user_id) in admins
        if self.debug:
            logger.debug(f"权限检查: user_id={user_id}, admins={admins}, result={result}")
        return result

    # ── 期号判断 ──

    def get_today_issue_code(self) -> str:
        """返回今天对应的期号，格式 MMDD。"""
        return datetime.now().strftime("%m%d")

    def is_today_issue(self, issue_code: str) -> bool:
        """判断给定期号是否等于今天的 MMDD。"""
        today = self.get_today_issue_code()
        result = issue_code == today
        if self.debug:
            logger.debug(f"期号判断: issue_code={issue_code}, today={today}, match={result}")
        return result

    async def _close_browser(self):
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
        """启动 Playwright persistent context 并返回 (playwright, context)。
        同时跟踪实例引用，供 _close_browser 使用。"""
        # 先确保之前的实例已关闭
        await self._close_browser()
        profile = self.ensure_profile_dir()
        pw = await async_playwright().start()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=BROWSER_HEADLESS,
            user_agent=USER_AGENT,
        )
        self._pw = pw
        self._ctx = ctx
        return pw, ctx

    def normalize_image_url(self, url: str) -> str:
        """补全协议头，返回归一化后的图片 URL。"""
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return url

    def build_candidate_image_urls(self, url: str) -> list[str]:
        """对单个图片 URL 生成候选列表：原图（去 @ 后缀）+ 原始 URL，去重。"""
        url = self.normalize_image_url(url)
        candidates = []
        # 去掉 @... 样式后缀得到原图
        at_idx = url.find("@")
        if at_idx != -1:
            candidates.append(url[:at_idx])
        candidates.append(url)
        # 去重保序
        seen = set()
        result = []
        for u in candidates:
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

    async def wait_for_report_cards(self, page):
        """等待 B 站 SPA 动态卡片渲染完成。"""
        timeout_ms = REQUEST_TIMEOUT_SECONDS * 1000
        logger.info("开始等待页面渲染...")
        try:
            await page.wait_for_selector(".bili-dyn-item", timeout=timeout_ms)
            logger.info("等待成功: .bili-dyn-item 已出现")
            return
        except Exception:
            logger.warning(".bili-dyn-item 未出现，尝试兜底选择器 .dyn-card-opus__title")

        try:
            await page.wait_for_selector(".dyn-card-opus__title", timeout=timeout_ms)
            logger.info("等待成功: .dyn-card-opus__title 已出现（兜底）")
            return
        except Exception:
            pass

        # 两个选择器都失败，保存调试信息
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

    async def parse_latest_report_from_page(self, page) -> dict | None:
        """从渲染后的 DOM 中解析最新一期银河快报。"""
        items = await page.query_selector_all(".bili-dyn-item")
        logger.info(f".bili-dyn-item 总数: {len(items)}")

        pattern = re.compile(r"^银河快报(\d{4})$")
        candidates = []

        for item in items:
            title_el = await item.query_selector(".dyn-card-opus__title")
            if not title_el:
                continue
            title_text = (await title_el.inner_text()).strip()
            m = pattern.match(title_text)
            if not m:
                continue

            issue_code = m.group(1)

            # source_url
            data_url = await title_el.get_attribute("data-url") or ""
            if data_url.startswith("//"):
                data_url = "https:" + data_url

            # image_urls — 限定在 .dyn-card-opus__pics 内
            raw_urls = []
            pics_container = await item.query_selector(".dyn-card-opus__pics")
            if pics_container:
                for src_el in await pics_container.query_selector_all("source[srcset]"):
                    srcset = await src_el.get_attribute("srcset") or ""
                    if srcset:
                        raw_urls.append(srcset.split(",")[0].split()[0])
                for img_el in await pics_container.query_selector_all("img[src]"):
                    src = await img_el.get_attribute("src") or ""
                    if src:
                        raw_urls.append(src)

            # 归一化 + 去重
            seen_urls = set()
            image_urls = []
            for raw in raw_urls:
                for candidate in self.build_candidate_image_urls(raw):
                    if candidate not in seen_urls:
                        seen_urls.add(candidate)
                        image_urls.append(candidate)

            if MAX_IMAGES_PER_REPORT > 0:
                image_urls = image_urls[: MAX_IMAGES_PER_REPORT]

            candidates.append({
                "matched_title": title_text,
                "issue_code": issue_code,
                "source_url": data_url,
                "image_urls": image_urls,
            })

        logger.info(f"匹配到银河快报标题的数量: {len(candidates)}")

        if not candidates:
            return None

        best = max(candidates, key=lambda c: c["issue_code"])
        logger.info(f"最终选中: {best['matched_title']} (issue_code={best['issue_code']})")
        return best

    def get_cache_dir(self) -> Path:
        """返回当前期缓存目录，自动创建。"""
        p = self._data_dir / "cache" / "current"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_meta(self) -> dict | None:
        """加载缓存 meta.json，解析失败返回 None。"""
        meta_file = self.get_cache_dir() / "meta.json"
        if not meta_file.exists():
            logger.debug("meta.json 不存在")
            return None
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            logger.debug(f"已加载 meta.json: issue_code={meta.get('issue_code')}")
            return meta
        except Exception as e:
            logger.warning(f"meta.json 解析失败: {e}")
            return None

    def save_meta(self, meta: dict) -> None:
        """保存 meta.json 到缓存目录。"""
        meta_file = self.get_cache_dir() / "meta.json"
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"meta.json 已保存: issue_code={meta.get('issue_code')}")

    def is_cache_valid(self, meta: dict, current_issue_code: str) -> bool:
        """判断缓存是否有效。"""
        if meta.get("issue_code") != current_issue_code:
            logger.info(f"期号不匹配: 缓存={meta.get('issue_code')}, 当前={current_issue_code}")
            return False
        cache_dir = self.get_cache_dir()
        for f in meta.get("files", []):
            if not (cache_dir / f).exists():
                logger.info(f"缓存文件缺失: {f}")
                return False
        return True

    def is_cache_valid_for_today(self, meta: dict) -> bool:
        """判断缓存是否为今天对应的有效期号，且本地文件完整。"""
        issue_code = meta.get("issue_code")
        if not issue_code:
            logger.info("缓存无效: issue_code 缺失")
            return False
        if not self.is_today_issue(issue_code):
            logger.info(f"缓存非今天: issue_code={issue_code}, today={self.get_today_issue_code()}")
            return False
        cache_dir = self.get_cache_dir()
        files = meta.get("files", [])
        if not files:
            logger.info("缓存无效: files 为空")
            return False
        for f in files:
            if not (cache_dir / f).exists():
                logger.info(f"缓存文件缺失: {f}")
                return False
        return True

    def is_cache_sent(self, meta: dict) -> bool:
        """判断当前缓存是否已自动发送过。"""
        return bool(meta.get("sent", False))

    def mark_cache_sent(self, meta: dict) -> None:
        """标记当前缓存为已自动发送，并保存。"""
        meta["sent"] = True
        meta["sent_at"] = datetime.now().isoformat()
        self.save_meta(meta)
        logger.info(f"已标记 sent=True: issue_code={meta.get('issue_code')}")

    def clear_current_cache(self) -> None:
        """删除当前期缓存目录下所有文件。"""
        cache_dir = self.get_cache_dir()
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info(f"已清除旧缓存: {cache_dir}")
        cache_dir.mkdir(parents=True, exist_ok=True)

    async def download_image(self, page, url: str, file_path: Path) -> bool:
        """使用 Playwright 页面上下文下载单张图片，成功返回 True。"""
        try:
            resp = await page.context.request.get(url, timeout=REQUEST_TIMEOUT_SECONDS * 1000)
            if resp.ok:
                file_path.write_bytes(await resp.body())
                logger.info(f"下载成功: {file_path.name} <- {url}")
                return True
            logger.warning(f"下载失败 (HTTP {resp.status}): {url}")
        except Exception as e:
            logger.warning(f"下载异常: {url} -> {e}")
        return False

    async def download_report_images(self, page, image_urls: list[str]) -> list[Path]:
        """按顺序下载图片列表，返回成功保存的本地路径。"""
        cache_dir = self.get_cache_dir()
        saved: list[Path] = []
        for idx, raw_url in enumerate(image_urls, start=1):
            dest = cache_dir / f"{idx}.jpg"
            candidates = self.build_candidate_image_urls(raw_url)
            ok = False
            for candidate_url in candidates:
                if await self.download_image(page, candidate_url, dest):
                    ok = True
                    break
            if ok:
                saved.append(dest)
            else:
                logger.warning(f"第 {idx} 张图片所有候选 URL 均下载失败")
        return saved

    async def is_login_invalid(self, page, parsed_result) -> tuple[bool, str]:
        """判断当前页面是否处于登录失效状态，返回 (是否失效, 原因)。"""
        current_url = page.url
        # URL 跳转到登录页
        if "passport.bilibili.com" in current_url or "login" in current_url.lower():
            return True, f"页面跳转到登录页: {current_url}"

        title = await page.title()
        title_lower = title.lower()
        if "登录" in title or "login" in title_lower:
            return True, f"页面标题含登录关键词: {title}"

        # 页面存在未登录态提示
        unlogin_el = await page.query_selector(".unlogin-tip, .bili-dyn-login-tip")
        if unlogin_el:
            return True, "页面存在未登录态提示元素"

        # 没有动态卡片且解析结果为空
        items = await page.query_selector_all(".bili-dyn-item")
        if not items and parsed_result is None:
            return True, "页面无动态卡片且解析结果为空，可能未登录"

        return False, ""

    async def reset_profile_dir(self) -> Path:
        """关闭浏览器后删除旧 profile 目录并重建空目录。
        同时设置 _skip_cleanup_once 标记，避免本次再额外执行 7 天缓存清理。"""
        await self._close_browser()
        profile = self.ensure_profile_dir()
        if profile.exists():
            shutil.rmtree(profile)
            logger.info(f"已删除旧 profile: {profile}")
        profile.mkdir(parents=True, exist_ok=True)
        logger.info(f"已重建空 profile: {profile}")
        self._skip_cleanup_once = True
        return profile

    # ── profile 缓存清理 ──

    _CLEANUP_SAFE_DIRS = {"Cache", "Code Cache", "GPUCache"}

    def _cleanup_meta_path(self) -> Path:
        return self._data_dir / "cleanup_meta.json"

    def _load_cleanup_meta(self) -> dict:
        p = self._cleanup_meta_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_cleanup_meta(self, meta: dict) -> None:
        p = self._cleanup_meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    async def maybe_cleanup_profile_cache(self) -> None:
        """如果距上次清理超过 cleanup_interval_days 天，清理 profile 中的浏览器缓存目录。
        - 只清理 _CLEANUP_SAFE_DIRS 中列出的目录（Cache / Code Cache / GPUCache）
        - 严禁删除 Cookies、Local Storage、IndexedDB 等登录态核心数据
        - 清理前先确保浏览器已关闭
        - 如果本次已执行过 profile 重建（_skip_cleanup_once 标记），则跳过
        """
        if getattr(self, "_skip_cleanup_once", False):
            logger.info("[缓存清理] 本次已执行 profile 重建，跳过 7 天缓存清理")
            self._skip_cleanup_once = False
            return

        meta = self._load_cleanup_meta()
        last_ts = meta.get("last_cleanup_ts", 0)
        now = time.time()
        elapsed_days = (now - last_ts) / 86400
        if elapsed_days < PROFILE_CLEANUP_INTERVAL_DAYS:
            if self.debug:
                logger.debug(f"距上次 profile 缓存清理 {elapsed_days:.1f} 天，未到 {PROFILE_CLEANUP_INTERVAL_DAYS} 天，跳过")
            return

        # 清理前确保浏览器已关闭，避免文件被占用
        await self._close_browser()

        profile = self.ensure_profile_dir()
        cleaned = []
        for name in self._CLEANUP_SAFE_DIRS:
            target = profile / name
            if target.exists() and target.is_dir():
                shutil.rmtree(target)
                cleaned.append(name)
        if cleaned:
            logger.info(f"已清理 profile 缓存目录: {', '.join(cleaned)}")
        else:
            logger.info("profile 缓存目录无需清理")

        meta["last_cleanup_ts"] = now
        self._save_cleanup_meta(meta)
        logger.info("cleanup_meta.json 已更新")

    # ── 调度状态 schedule_meta.json ──

    def _schedule_meta_path(self) -> Path:
        return self._data_dir / "schedule_meta.json"

    def load_schedule_meta(self) -> dict:
        """加载调度状态，缺失或损坏时返回默认结构。"""
        defaults = {
            "last_fetch_check_ts": 0,
            "last_fetch_result": "",
            "last_send_attempt_ts": 0,
            "last_send_result": "",
            "last_login_success_ts": 0,
            "login_in_progress": False,
            "login_started_ts": 0,
            "login_qrcode_refresh_count": 0,
        }
        p = self._schedule_meta_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                # 补齐缺失字段
                for k, v in defaults.items():
                    data.setdefault(k, v)
                return data
            except Exception as e:
                logger.warning(f"schedule_meta.json 解析失败: {e}")
        return dict(defaults)

    def save_schedule_meta(self, meta: dict) -> None:
        """保存调度状态。"""
        p = self._schedule_meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.debug:
            logger.debug("schedule_meta.json 已保存")

    # ── 登录锁与冷却 ──

    _LOGIN_COOLDOWN_DAYS = 7

    def is_login_in_progress(self) -> bool:
        sched = self.load_schedule_meta()
        return bool(sched.get("login_in_progress", False))

    def _set_login_in_progress(self, in_progress: bool):
        sched = self.load_schedule_meta()
        sched["login_in_progress"] = in_progress
        if in_progress:
            sched["login_started_ts"] = time.time()
            sched["login_qrcode_refresh_count"] = 0
            logger.info("[登录] 登录流程开始, login_in_progress=True")
        self.save_schedule_meta(sched)

    def _record_login_success(self):
        sched = self.load_schedule_meta()
        sched["login_in_progress"] = False
        sched["last_login_success_ts"] = time.time()
        self.save_schedule_meta(sched)
        logger.info(f"[登录] 登录成功, login_in_progress=False, "
                     f"refresh_count={sched.get('login_qrcode_refresh_count', 0)}")

    def _record_login_end(self):
        """登录流程结束（无论成功失败）释放锁。"""
        sched = self.load_schedule_meta()
        sched["login_in_progress"] = False
        self.save_schedule_meta(sched)
        logger.info(f"[登录] 登录流程结束, login_in_progress=False, "
                     f"refresh_count={sched.get('login_qrcode_refresh_count', 0)}")

    def is_login_recently_successful(self) -> bool:
        """最近一次登录成功是否在冷却期内。"""
        sched = self.load_schedule_meta()
        last_ts = sched.get("last_login_success_ts", 0)
        if last_ts <= 0:
            return False
        elapsed_days = (time.time() - last_ts) / 86400
        return elapsed_days < self._LOGIN_COOLDOWN_DAYS

    # ── 定时抓取 ──

    def _is_fetch_time_reached(self) -> bool:
        """判断当前是否已过今天的 schedule_fetch_time 且今天尚未执行过抓取。"""
        now = datetime.now()
        try:
            h, m = SCHEDULE_FETCH_TIME.split(":")
            fetch_hour, fetch_min = int(h), int(m)
        except (ValueError, AttributeError):
            fetch_hour, fetch_min = 3, 0

        if now.hour < fetch_hour or (now.hour == fetch_hour and now.minute < fetch_min):
            return False

        # 检查今天是否已经执行过
        sched = self.load_schedule_meta()
        last_ts = sched.get("last_fetch_check_ts", 0)
        if last_ts > 0:
            last_dt = datetime.fromtimestamp(last_ts)
            if last_dt.date() == now.date():
                return False
        return True

    async def scheduled_fetch(self):
        """定时抓取：检查页面是否有今天对应期号，有则下载并缓存。"""
        today = self.get_today_issue_code()
        logger.info(f"[定时抓取] 开始执行, today_issue={today}")
        sched = self.load_schedule_meta()
        sched["last_fetch_check_ts"] = time.time()

        pw = None
        try:
            await self.maybe_cleanup_profile_cache()
            pw, ctx = await self.launch_persistent_browser()
            page = await ctx.new_page()
            await page.goto(TARGET_URL, wait_until="domcontentloaded",
                            timeout=REQUEST_TIMEOUT_SECONDS * 1000)
            logger.info(f"[定时抓取] 页面已打开: {TARGET_URL}")

            try:
                await self.wait_for_report_cards(page)
            except TimeoutError:
                # 页面渲染超时 → 可能是页面异常或登录失效，属于异常类，需通知
                invalid, reason = await self.is_login_invalid(page, None)
                if invalid:
                    logger.error(f"[定时抓取] 页面渲染超时且登录失效: {reason}")
                    sched["last_fetch_result"] = f"timeout_login_invalid: {reason}"
                    self.save_schedule_meta(sched)
                    await self._notify_admins(
                        f"[定时抓取] 页面渲染超时且登录失效: {reason}\n"
                        f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                    )
                else:
                    logger.warning("[定时抓取] 页面渲染超时")
                    sched["last_fetch_result"] = "timeout"
                    self.save_schedule_meta(sched)
                    await self._notify_admins("[定时抓取] 页面渲染超时，请检查网络或页面状态")
                return

            report = await self.parse_latest_report_from_page(page)

            # 登录失效检测
            invalid, reason = await self.is_login_invalid(page, report)
            if invalid:
                logger.error(f"[定时抓取] 登录失效: {reason}")
                sched["last_fetch_result"] = f"login_invalid: {reason}"
                self.save_schedule_meta(sched)
                await self._notify_admins(
                    f"[定时抓取] B站登录态失效: {reason}\n"
                    f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                )
                return

            if not report:
                # 页面正常渲染但没有匹配的快报 → 未更新，不通知
                logger.info(f"[定时抓取] 未找到银河快报内容, today_issue={today}")
                sched["last_fetch_result"] = "no_report"
                self.save_schedule_meta(sched)
                return

            if not self.is_today_issue(report["issue_code"]):
                # 页面最新期号非今天 → 未更新，不通知
                logger.info(f"[定时抓取] 页面最新期号 {report['issue_code']} 非今天 {today}，跳过")
                sched["last_fetch_result"] = "not_today"
                self.save_schedule_meta(sched)
                return

            # 检查本地缓存是否已经是今天的
            meta = self.load_meta()
            if meta and self.is_cache_valid_for_today(meta):
                logger.info(f"[定时抓取] 今天的缓存已存在: {report['matched_title']}, sent={meta.get('sent')}")
                sched["last_fetch_result"] = "cache_hit"
                self.save_schedule_meta(sched)
                return

            # 下载新图片
            self.clear_current_cache()
            saved = await self.download_report_images(page, report["image_urls"])
            if not saved:
                logger.warning(f"[定时抓取] 图片下载失败, issue={report['issue_code']}")
                sched["last_fetch_result"] = "download_failed"
                self.save_schedule_meta(sched)
                await self._notify_admins(f"[定时抓取] 图片下载失败 (issue={report['issue_code']})")
                return

            new_meta = {
                "keyword": TRIGGER_KEYWORD,
                "issue_code": report["issue_code"],
                "matched_title": report["matched_title"],
                "source_url": report["source_url"],
                "fetched_at": datetime.now().isoformat(),
                "files": [p.name for p in saved],
                "sent": False,
                "sent_at": None,
            }
            self.save_meta(new_meta)
            logger.info(f"[定时抓取] 成功: {report['matched_title']}，{len(saved)} 张图片, sent=False")
            sched["last_fetch_result"] = "ok"
            self.save_schedule_meta(sched)

        except Exception as e:
            logger.error(f"[定时抓取] 异常: {e}")
            sched["last_fetch_result"] = f"error: {e}"
            self.save_schedule_meta(sched)
            await self._notify_admins(f"[定时抓取] 异常: {e}")
        finally:
            if pw:
                try:
                    await ctx.close()
                    await pw.stop()
                except Exception:
                    pass
                self._pw = None
                self._ctx = None

    async def _fetch_loop(self):
        """后台循环，每 60 秒检查一次是否到达抓取时间。"""
        while True:
            try:
                if self._is_fetch_time_reached():
                    await self.scheduled_fetch()
            except Exception as e:
                logger.error(f"[定时抓取循环] 异常: {e}")
            await asyncio.sleep(60)

    # ── 定时发送 ──

    def _is_send_time_reached(self) -> bool:
        """判断当前是否已过今天的 schedule_send_time 且今天尚未执行过发送。"""
        now = datetime.now()
        try:
            h, m = self.schedule_send_time.split(":")
            send_hour, send_min = int(h), int(m)
        except (ValueError, AttributeError):
            send_hour, send_min = 9, 0

        if now.hour < send_hour or (now.hour == send_hour and now.minute < send_min):
            return False

        sched = self.load_schedule_meta()
        last_ts = sched.get("last_send_attempt_ts", 0)
        if last_ts > 0:
            last_dt = datetime.fromtimestamp(last_ts)
            if last_dt.date() == now.date():
                return False
        return True

    async def _notify_admins(self, text: str):
        """私聊通知所有管理员。"""
        for admin_id in self.get_admin_user_ids():
            try:
                await self.context.send_message(admin_id, text)
                logger.info(f"[通知管理员] {admin_id}: {text[:50]}")
            except Exception as e:
                logger.error(f"[通知管理员] 发送失败 admin={admin_id}: {e}")

    async def _send_images_to_groups(self, files: list[Path]) -> tuple[list[str], list[str]]:
        """向所有目标群发送图片，返回 (成功群列表, 失败群列表)。
        根据白名单/黑名单过滤目标群组。
        """
        # 获取所有群聊并根据白名单/黑名单过滤
        all_groups = self.get_all_group_ids()
        if not all_groups:
            logger.warning("[定时发送] 无法获取群聊列表，跳过发送")
            return [], []

        target_groups = [gid for gid in all_groups if self.is_allowed_group(gid)]
        whitelist = self._safe_list(self.whitelist_groups)
        blacklist = self._safe_list(self.blacklist_groups)
        logger.info(f"[定时发送] 白名单={whitelist}, 黑名单={blacklist}, 目标群组={target_groups}")

        if not target_groups:
            logger.warning("[定时发送] 无目标群组（黑白名单过滤后），跳过发送")
            return [], []

        ok_groups = []
        fail_groups = []
        for gid in target_groups:
            try:
                for img_path in files:
                    await self.context.send_message(gid, img_path=str(img_path))
                ok_groups.append(gid)
                logger.info(f"[定时发送] 群 {gid} 发送成功，{len(files)} 张图")
            except Exception as e:
                fail_groups.append(gid)
                logger.error(f"[定时发送] 群 {gid} 发送失败: {e}")
        return ok_groups, fail_groups

    async def scheduled_send(self):
        """定时发送：检查缓存并发送到目标群。"""
        today = self.get_today_issue_code()
        logger.info(f"[定时发送] 开始执行, today_issue={today}")
        sched = self.load_schedule_meta()
        sched["last_send_attempt_ts"] = time.time()

        # 先检查本地缓存
        meta = self.load_meta()
        if meta and self.is_cache_valid_for_today(meta) and not self.is_cache_sent(meta):
            logger.info(f"[定时发送] 使用已有缓存: {meta.get('matched_title')}, sent={meta.get('sent')}")
            files = [self.get_cache_dir() / f for f in meta.get("files", [])]
            ok, fail = await self._send_images_to_groups(files)
            if ok:
                self.mark_cache_sent(meta)
                logger.info(f"[定时发送] 发送完成: 成功={len(ok)}群, 失败={len(fail)}群")
            if fail:
                await self._notify_admins(
                    f"[定时发送] 今日快报发送失败的群: {', '.join(fail)}"
                )
            sched["last_send_result"] = f"ok={len(ok)},fail={len(fail)}"
            self.save_schedule_meta(sched)
            return

        if meta and self.is_cache_valid_for_today(meta) and self.is_cache_sent(meta):
            logger.info(f"[定时发送] 今天已发送过，跳过 (sent_at={meta.get('sent_at')})")
            sched["last_send_result"] = "already_sent"
            self.save_schedule_meta(sched)
            return

        # 缓存不可用，现场抓取
        logger.info("[定时发送] 缓存不可用，尝试现场抓取")
        pw = None
        try:
            await self.maybe_cleanup_profile_cache()
            pw, ctx = await self.launch_persistent_browser()
            page = await ctx.new_page()
            await page.goto(TARGET_URL, wait_until="domcontentloaded",
                            timeout=REQUEST_TIMEOUT_SECONDS * 1000)

            try:
                await self.wait_for_report_cards(page)
            except TimeoutError:
                invalid, reason = await self.is_login_invalid(page, None)
                if invalid:
                    logger.error(f"[定时发送] 页面渲染超时且登录失效: {reason}")
                    sched["last_send_result"] = f"timeout_login_invalid: {reason}"
                    self.save_schedule_meta(sched)
                    await self._notify_admins(
                        f"[定时发送] 页面渲染超时且登录失效: {reason}\n"
                        f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                    )
                else:
                    logger.warning("[定时发送] 页面渲染超时")
                    sched["last_send_result"] = "fetch_timeout"
                    self.save_schedule_meta(sched)
                    await self._notify_admins("[定时发送] 页面渲染超时，请检查网络或页面状态")
                return

            report = await self.parse_latest_report_from_page(page)

            # 登录失效检测
            invalid, reason = await self.is_login_invalid(page, report)
            if invalid:
                logger.error(f"[定时发送] 登录失效: {reason}")
                sched["last_send_result"] = f"login_invalid: {reason}"
                self.save_schedule_meta(sched)
                await self._notify_admins(
                    f"[定时发送] B站登录态失效: {reason}\n"
                    f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                )
                return

            if not report:
                # 页面正常但无内容 → 异常（发送时间到了却没内容）
                logger.info(f"[定时发送] 未找到银河快报内容, today_issue={today}")
                sched["last_send_result"] = "no_report"
                self.save_schedule_meta(sched)
                await self._notify_admins(f"[定时发送] 未找到银河快报内容 (today_issue={today})")
                return

            if not self.is_today_issue(report["issue_code"]):
                # 最新期号非今天 → 页面未更新，不通知
                logger.info(f"[定时发送] 页面最新期号 {report['issue_code']} 非今天 {today}，跳过")
                sched["last_send_result"] = "not_today"
                self.save_schedule_meta(sched)
                return

            # 下载
            self.clear_current_cache()
            saved = await self.download_report_images(page, report["image_urls"])
            if not saved:
                logger.warning(f"[定时发送] 图片下载失败, issue={report['issue_code']}")
                sched["last_send_result"] = "download_failed"
                self.save_schedule_meta(sched)
                await self._notify_admins(f"[定时发送] 图片下载失败 (issue={report['issue_code']})")
                return

            new_meta = {
                "keyword": TRIGGER_KEYWORD,
                "issue_code": report["issue_code"],
                "matched_title": report["matched_title"],
                "source_url": report["source_url"],
                "fetched_at": datetime.now().isoformat(),
                "files": [p.name for p in saved],
                "sent": False,
                "sent_at": None,
            }
            self.save_meta(new_meta)

            # 发送
            ok, fail = await self._send_images_to_groups(saved)
            if ok:
                self.mark_cache_sent(new_meta)
                logger.info(f"[定时发送] 发送完成: 成功={len(ok)}群, 失败={len(fail)}群")
            if fail:
                await self._notify_admins(
                    f"[定时发送] 今日快报发送失败的群: {', '.join(fail)}"
                )
            sched["last_send_result"] = f"ok={len(ok)},fail={len(fail)}"
            self.save_schedule_meta(sched)

        except Exception as e:
            logger.error(f"[定时发送] 异常: {e}")
            sched["last_send_result"] = f"error: {e}"
            self.save_schedule_meta(sched)
            await self._notify_admins(f"[定时发送] 异常: {e}")
        finally:
            if pw:
                try:
                    await ctx.close()
                    await pw.stop()
                except Exception:
                    pass
                self._pw = None
                self._ctx = None

    async def _send_loop(self):
        """后台循环，每 60 秒检查一次是否到达发送时间。"""
        while True:
            try:
                if self._is_send_time_reached():
                    await self.scheduled_send()
            except Exception as e:
                logger.error(f"[定时发送循环] 异常: {e}")
            await asyncio.sleep(60)

    # ── 二维码登录 ──

    _BILI_LOGIN_URL = "https://passport.bilibili.com/login"
    _QR_SELECTORS = [
        ".login-scan-box img",       # 二维码图片
        ".qrcode-img img",
        ".login-scan-box",           # 二维码区域
        ".qrcode-img",
        ".login-panel",              # 登录面板兜底
    ]

    # ── 二维码登录核心方法 ──

    _LOGIN_TOTAL_TIMEOUT = 300      # 登录流程总时长上限 5 分钟
    _QR_SINGLE_TIMEOUT = 120        # 单张二维码有效期 120 秒
    _QR_MAX_REFRESH = 1             # 二维码最多自动刷新次数
    _QR_POLL_INTERVAL = 3.0         # 轮询间隔秒

    async def open_login_page_and_capture_qrcode(self, page) -> Path:
        """在已有 page 上打开 B 站登录页，截取二维码并返回截图路径。
        优先截取二维码区域；若定位不到则截取整个登录面板。"""
        logger.info(f"打开登录页: {self._BILI_LOGIN_URL}")
        await page.goto(self._BILI_LOGIN_URL, wait_until="domcontentloaded",
                        timeout=REQUEST_TIMEOUT_SECONDS * 1000)

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
        """轮询等待用户扫码登录成功。
        Args:
            timeout: 本轮等待上限秒数
            poll_interval: 轮询间隔秒
        Returns:
            True 表示登录成功，False 表示本轮超时未登录。
        """
        elapsed = 0.0
        logger.info(f"等待用户扫码登录 (timeout={timeout}s)...")
        while elapsed < timeout:
            current_url = page.url
            # 登录成功后 B 站会跳转离开 passport 页面
            if "passport.bilibili.com" not in current_url:
                logger.info(f"扫码登录成功，页面已跳转: {current_url}")
                return True
            # 检查页面是否出现已登录标志
            avatar = await page.query_selector(".header-avatar, .bili-avatar")
            if avatar:
                logger.info("扫码登录成功，检测到头像元素")
                return True
            await page.wait_for_timeout(poll_interval * 1000)
            elapsed += poll_interval
        logger.warning(f"本轮等待扫码超时 ({timeout}s)")
        return False

    async def start_bili_qrcode_login(self, event: AstrMessageEvent):
        """完整的二维码登录流程（异步生成器，通过 yield 向触发者发送消息）。

        流程：
        1. 打开登录页，截取二维码发送给管理员
        2. 轮询等待扫码（单张二维码 120s）
        3. 若超时，自动刷新一次二维码（最多刷新 _QR_MAX_REFRESH 次）
        4. 总流程 5 分钟仍未成功则放弃
        """
        logger.info("开始二维码登录流程")
        self._set_login_in_progress(True)
        flow_start = time.time()
        refresh_count = 0

        pw = None
        try:
            yield event.plain_result("已重建浏览器登录环境，正在获取二维码...")
            pw, ctx = await self.launch_persistent_browser()
            page = await ctx.new_page()

            # ── 第一次获取二维码 ──
            qr_path = await self.open_login_page_and_capture_qrcode(page)
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "请使用 Bilibili 手机客户端扫描上方二维码登录（120 秒内有效）"
            )

            while True:
                # 计算本轮可用等待时间：取二维码有效期与总剩余时间的较小值
                remaining = self._LOGIN_TOTAL_TIMEOUT - (time.time() - flow_start)
                if remaining <= 0:
                    break
                this_round = min(self._QR_SINGLE_TIMEOUT, remaining)

                success = await self.wait_for_login_success(
                    page, timeout=this_round, poll_interval=self._QR_POLL_INTERVAL
                )
                if success:
                    self._record_login_success()
                    logger.info("登录成功，profile 已保存登录态")
                    yield event.plain_result("B站登录成功，登录态已保存")
                    return

                # 本轮超时，检查是否还能刷新
                remaining = self._LOGIN_TOTAL_TIMEOUT - (time.time() - flow_start)
                if remaining <= 0 or refresh_count >= self._QR_MAX_REFRESH:
                    break

                # ── 自动刷新二维码 ──
                refresh_count += 1
                # 持久化刷新次数
                sched = self.load_schedule_meta()
                sched["login_qrcode_refresh_count"] = refresh_count
                self.save_schedule_meta(sched)
                logger.info(f"[登录] 二维码超时，自动刷新第 {refresh_count}/{self._QR_MAX_REFRESH} 次")
                yield event.plain_result(
                    f"二维码已失效，正在自动刷新（第 {refresh_count} 次）..."
                )
                qr_path = await self.open_login_page_and_capture_qrcode(page)
                yield event.image_result(str(qr_path))
                yield event.plain_result(
                    "新二维码已生成，请在 120 秒内扫描"
                )

            # 所有轮次均超时
            self._record_login_end()
            total_elapsed = int(time.time() - flow_start)
            logger.warning(f"二维码登录超时，总耗时 {total_elapsed}s，刷新 {refresh_count} 次")
            yield event.plain_result("二维码已超时，请重新获取")

        except Exception as e:
            self._record_login_end()
            logger.error(f"二维码登录流程异常: {e}")
            yield event.plain_result(f"二维码登录流程异常: {e}")
        finally:
            if pw:
                try:
                    await ctx.close()
                    await pw.stop()
                except Exception:
                    pass
                self._pw = None
                self._ctx = None

    async def initialize(self):
        logger.info("BiliDailyReport 插件已加载")
        if self.debug:
            logger.debug(f"配置: trigger={TRIGGER_KEYWORD}, "
                         f"headless={BROWSER_HEADLESS}, timeout={REQUEST_TIMEOUT_SECONDS}s, "
                         f"fetch={SCHEDULE_FETCH_TIME}, send={self.schedule_send_time}, "
                         f"cleanup_interval={PROFILE_CLEANUP_INTERVAL_DAYS}d")
        self._fetch_task = asyncio.create_task(self._fetch_loop())
        self._send_task = asyncio.create_task(self._send_loop())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = event.message_str.strip()

        if text == TRIGGER_KEYWORD:
            user_id = event.get_sender_id()
            today = self.get_today_issue_code()
            logger.info(f"[手动快报] 收到触发: user={user_id}, today_issue={today}")

            # 先检查本地缓存是否有今天对应期号
            meta = self.load_meta()
            if meta and self.is_cache_valid_for_today(meta):
                logger.info(f"[手动快报] 本地缓存命中: {meta.get('matched_title')}, sent={meta.get('sent')}")
                for f in meta.get("files", []):
                    yield event.image_result(str(self.get_cache_dir() / f))
                return

            # 本地没有，现场抓取
            pw = None
            try:
                await self.maybe_cleanup_profile_cache()
                logger.info("[手动快报] 启动浏览器...")
                pw, ctx = await self.launch_persistent_browser()
                page = await ctx.new_page()
                await page.goto(TARGET_URL, wait_until="domcontentloaded",
                                timeout=REQUEST_TIMEOUT_SECONDS * 1000)
                logger.info(f"[手动快报] 页面已打开: {TARGET_URL}")
                try:
                    await self.wait_for_report_cards(page)
                except TimeoutError:
                    invalid, reason = await self.is_login_invalid(page, None)
                    if invalid:
                        logger.warning(f"[手动快报] 登录失效: {reason}")
                        yield event.plain_result(
                            f"当前 Bilibili 登录态可能已失效，请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                        )
                        await self._notify_admins(
                            f"[手动快报] 登录失效 (user={user_id}): {reason}"
                        )
                        return
                    raise
                report = await self.parse_latest_report_from_page(page)

                # 登录失效检测
                invalid, reason = await self.is_login_invalid(page, report)
                if invalid:
                    logger.warning(f"[手动快报] 登录失效: {reason}")
                    yield event.plain_result(
                        f"当前 Bilibili 登录态可能已失效，请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                    )
                    await self._notify_admins(
                        f"[手动快报] 登录失效 (user={user_id}): {reason}"
                    )
                    return

                if not report or not self.is_today_issue(report["issue_code"]):
                    # 页面未更新 → 仅告知用户，不通知管理员
                    issue_info = report["issue_code"] if report else "无"
                    logger.info(f"[手动快报] 今日快报尚未更新, 最新issue={issue_info}, today={today}")
                    yield event.plain_result("今日快报尚未更新")
                    return

                # 下载并缓存
                self.clear_current_cache()
                saved = await self.download_report_images(page, report["image_urls"])
                if not saved:
                    logger.warning(f"[手动快报] 图片下载失败, issue={report['issue_code']}")
                    yield event.plain_result("图片下载失败")
                    await self._notify_admins(
                        f"[手动快报] 图片下载失败 (user={user_id}, issue={report['issue_code']})"
                    )
                    return
                new_meta = {
                    "keyword": TRIGGER_KEYWORD,
                    "issue_code": report["issue_code"],
                    "matched_title": report["matched_title"],
                    "source_url": report["source_url"],
                    "fetched_at": datetime.now().isoformat(),
                    "files": [p.name for p in saved],
                    "sent": False,
                    "sent_at": None,
                }
                self.save_meta(new_meta)
                logger.info(f"[手动快报] 抓取成功: {report['matched_title']}, {len(saved)} 张图片")

                # 发送图片给当前用户（不修改 sent）
                for img_path in saved:
                    yield event.image_result(str(img_path))
            except Exception as e:
                logger.error(f"[手动快报] 异常: {e}")
                yield event.plain_result(f"获取今日快报失败: {e}")
                await self._notify_admins(f"[手动快报] 异常 (user={user_id}): {e}")
            finally:
                if pw:
                    try:
                        await ctx.close()
                        await pw.stop()
                    except Exception:
                        pass
                    self._pw = None
                    self._ctx = None
        elif text == RELOGIN_KEYWORD:
            user_id = event.get_sender_id()
            if not self.is_admin_user(user_id):
                yield event.plain_result("仅管理员可执行此操作")
                return
            if self.is_login_in_progress():
                yield event.plain_result("当前已有登录流程进行中，请稍后再试")
                return
            if self.is_login_recently_successful():
                yield event.plain_result("当前账号已登录，请勿重复登录")
                return
            logger.info(f"收到重新登录触发: {text} (user={user_id})")
            async for result in self.start_bili_qrcode_login(event):
                yield result
        elif text == RELOGIN_RESET_KEYWORD:
            user_id = event.get_sender_id()
            if not self.is_admin_user(user_id):
                yield event.plain_result("仅管理员可执行此操作")
                return
            if self.is_login_in_progress():
                yield event.plain_result("当前已有登录流程进行中，请稍后再试")
                return
            logger.info(f"收到重置 profile 触发: {text} (user={user_id})")
            try:
                await self.reset_profile_dir()
                yield event.plain_result("已重置浏览器登录环境，请发送 bili_qrcode 重新扫码")
            except Exception as e:
                logger.error(f"重置 profile 失败: {e}")
                yield event.plain_result(f"重置登录环境失败: {e}")

    async def terminate(self):
        await self._close_browser()
        for task in ("_fetch_task", "_send_task"):
            t = getattr(self, task, None)
            if t:
                t.cancel()
        logger.info("BiliDailyReport 插件已卸载")
