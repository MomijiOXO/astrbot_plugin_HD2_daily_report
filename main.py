import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import AstrBotConfig, logger

from .cache import CacheManager
from .scraper import BiliScraper
from .models import CacheMeta, TZ_SHANGHAI
from .loadout import generate_random_loadout, generate_full_random_loadout, format_loadout

_PLUGIN_NAME = "astrbot_plugin_bili_daily_report"

# 写死常量
TRIGGER_KEYWORD = "今日快报"
RELOGIN_KEYWORD = "bili_qrcode"
SCHEDULE_SEND_TIME = "09:00"
BROWSER_HEADLESS = True
REQUEST_TIMEOUT_SECONDS = 30
MAX_IMAGES_PER_REPORT = 0
PROFILE_DIR = ""
PROFILE_CLEANUP_INTERVAL_DAYS = 7

# 配置项默认值
DEFAULTS = {
    "schedule_send_time": SCHEDULE_SEND_TIME,
    "target_groups": [],  # 定时推送目标群组列表
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


@dataclass
class _FetchOutcome:
    """抓取结果：_fetch_and_cache 的返回值。"""

    status: str
    """success | timeout | timeout_login_invalid | login_invalid
    | no_report | not_today | download_failed"""

    reason: str = ""
    """login_invalid 时的具体原因。"""

    meta: CacheMeta | None = None
    """success 时的缓存元数据。"""

    files: list[Path] = field(default_factory=list)
    """success 时的本地图片路径列表。"""

    issue_code: str = ""
    """解析到的期号（可能非今天）。"""


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
        self.target_groups: list = _get(config, "target_groups")
        self.admin_user_ids: list = _get(config, "admin_user_ids")
        self.debug: bool = bool(_get(config, "enable_debug_log"))

        # 服务实例
        self._cache = CacheManager(self._data_dir, self.debug)
        self._scraper = BiliScraper(
            data_dir=self._data_dir,
            profile_dir=self._cache.ensure_profile_dir(PROFILE_DIR),
            headless=BROWSER_HEADLESS,
            user_agent=USER_AGENT,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
            max_images=MAX_IMAGES_PER_REPORT,
        )

        # 群组 UMO 映射（群号 -> unified_msg_origin）
        self.group_umo_mapping: dict = self._cache.load_group_mapping()

        self._browser_lock = asyncio.Lock()  # 保护浏览器生命周期，防止并发竞态

        # --- APScheduler 定时任务 ---
        self.scheduler = AsyncIOScheduler(timezone=TZ_SHANGHAI)

        try:
            # 解析并校验发送时间
            parts = self.schedule_send_time.split(":")
            if len(parts) != 2:
                raise ValueError(f"时间格式应为 HH:MM，实际为: {self.schedule_send_time}")
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"时间值超出范围: {hour:02d}:{minute:02d}")
            # 添加定时发送任务
            self.scheduler.add_job(
                self.scheduled_send,
                'cron',
                hour=hour,
                minute=minute,
                id="daily_report_send_job"
            )
            # 启动调度器
            self.scheduler.start()
            logger.info(f"定时发送任务已创建: {self.schedule_send_time}")
        except Exception as e:
            logger.error(f"定时发送任务创建失败", exc_info=True)

    # ── 权限与目标群辅助 ──

    def _safe_list(self, val) -> list:
        """将配置值安全转为字符串列表。"""
        if isinstance(val, list):
            return [str(v) for v in val]
        return []

    def get_admin_user_ids(self) -> list:
        return self._safe_list(self.admin_user_ids)

    def get_target_groups(self) -> list:
        """获取配置的定时推送目标群组列表。"""
        return self._safe_list(self.target_groups)

    def is_admin_user(self, user_id) -> bool:
        admins = self.get_admin_user_ids()
        result = str(user_id) in admins
        if self.debug:
            logger.debug(f"权限检查: user_id={user_id}, admins={admins}, result={result}")
        return result

    # ── 群组 UMO 预加载与学习 ──

    def _learn_group_umo(self, event: AstrMessageEvent) -> None:
        """从消息事件中自动学习群号与 unified_msg_origin 的映射。"""
        umo = getattr(event, 'unified_msg_origin', None)
        group_id = event.get_group_id()
        if not umo or not group_id:
            if self.debug:
                logger.debug(f"[UMO学习] 跳过: umo={umo!r}, group_id={group_id!r}")
            return
        clean_gid = str(group_id)
        if self.group_umo_mapping.get(clean_gid) != umo:
            self.group_umo_mapping[clean_gid] = umo
            self._cache.save_group_mapping(self.group_umo_mapping)
            logger.info(f"[UMO学习] 群 {clean_gid} -> {umo}")

    @staticmethod
    def _extract_group_id(value: str) -> str:
        """从纯群号或 unified_msg_origin 格式中提取群号。
        例如 "957880653" -> "957880653"
             "aiocqhttp:GroupMessage:957880653" -> "957880653"
        """
        value = value.strip()
        if ":" in value:
            parts = value.split(":")
            return parts[-1]
        return value

    # ── 消息发送 ──

    # 平台类型 -> UMO 前缀
    _PLATFORM_CONFIGS: list[tuple] = []  # 延迟初始化，避免模块级导入

    @classmethod
    def _get_platform_configs(cls):
        """延迟导入并返回平台配置列表。"""
        if not cls._PLATFORM_CONFIGS:
            from astrbot.api.event.filter import PlatformAdapterType
            cls._PLATFORM_CONFIGS = [
                (PlatformAdapterType.AIOCQHTTP, "aiocqhttp:GroupMessage"),
                (PlatformAdapterType.QQOFFICIAL, "qq_official:GroupMessage"),
            ]
        return cls._PLATFORM_CONFIGS

    def _get_platforms(self) -> list[tuple]:
        """获取可用的平台实例列表，返回 [(platform, umo_prefix), ...]。"""
        results = []
        for adapter_type, umo_prefix in self._get_platform_configs():
            try:
                platform = self.context.get_platform(adapter_type)
            except Exception:
                continue
            if platform:
                results.append((platform, umo_prefix))
        return results

    @staticmethod
    def _get_call_action(platform):
        """从平台实例中提取 OneBot call_action 方法，找不到返回 None。"""
        client = getattr(platform, 'get_client', lambda: None)() or \
                 getattr(platform, 'client', None) or \
                 getattr(platform, 'bot', None)
        if not client:
            return None
        return getattr(client, 'call_action', None) or \
               getattr(getattr(client, 'api', None), 'call_action', None)

    def _guess_group_umo(self, group_id: str, platforms=None) -> str | None:
        """根据平台实例推断群组 UMO 格式。

        常见格式: aiocqhttp:GroupMessage:{group_id}
        """
        if platforms is None:
            platforms = self._get_platforms()
        for _platform, umo_prefix in platforms:
            umo = f"{umo_prefix}:{group_id}"
            logger.info(f"[UMO推断] 群 {group_id} -> {umo}")
            return umo
        return None

    async def _notify_admins(self, text: str):
        """私聊通知所有管理员。

        策略：先尝试 OneBot send_private_msg，失败则用 context.send_message 兜底。
        """
        platforms = self._get_platforms()

        for admin_id in self.get_admin_user_ids():
            sent = False

            # 策略1: OneBot send_private_msg
            for platform, _umo_prefix in platforms:
                call_action = self._get_call_action(platform)
                if not call_action:
                    continue
                try:
                    await call_action("send_private_msg", user_id=int(admin_id), message=text)
                    sent = True
                    break
                except Exception as e:
                    logger.debug(f"[通知管理员] OneBot发送失败 admin={admin_id}: {e}")

            # 策略2: context.send_message 兜底（需要 UMO 格式）
            if not sent:
                try:
                    await self.context.send_message(admin_id, text)
                    sent = True
                except Exception as e:
                    logger.debug(f"[通知管理员] context.send_message 兜底失败 admin={admin_id}: {e}")

            if sent:
                logger.info(f"[通知管理员] {admin_id}: {text[:50]}")
            else:
                logger.error(f"[通知管理员] 发送失败 admin={admin_id}")

    async def _send_images_to_groups(self, files: list[Path]) -> tuple[list[str], list[str]]:
        """向所有目标群发送图片，返回 (成功群列表, 失败群列表)。
        发送策略：
        1. 先尝试 OneBot call_action 直接发送
        2. 失败则用 context.send_message + 学习到的 UMO 兜底
        支持 target_groups 中填写纯群号或 unified_msg_origin 格式。
        """
        target_groups = self.get_target_groups()
        logger.info(f"[定时发送] 目标群组={target_groups}")

        if not target_groups:
            logger.warning("[定时发送] 未配置目标群组，跳过发送")
            return [], []

        platforms = self._get_platforms()

        ok_groups = []
        fail_groups = []
        for raw_gid in target_groups:
            clean_gid = self._extract_group_id(raw_gid)
            sent = False

            # 策略1: OneBot call_action 直接发送
            onebot_sent_count = 0
            if files and platforms:
                for img_path in files:
                    img_sent = False
                    for platform, _umo_prefix in platforms:
                        call_action = self._get_call_action(platform)
                        if not call_action:
                            continue
                        try:
                            await call_action("send_group_msg", group_id=int(clean_gid), message=[
                                {"type": "image", "data": {"file": f"file:///{img_path}"}}
                            ])
                            img_sent = True
                            break
                        except Exception as e:
                            logger.debug(f"[定时发送] OneBot发送失败 群{clean_gid}: {e}")
                    if img_sent:
                        onebot_sent_count += 1
                    else:
                        break
                sent = onebot_sent_count == len(files)

            # 策略2: context.send_message + UMO 兜底（仅在策略1完全未发送时使用，避免重复）
            if not sent and onebot_sent_count == 0:
                # 确定 UMO：配置中直接填了 UMO 格式 > 已学习的映射 > 根据平台自动推断
                if ":" in raw_gid:
                    umo = raw_gid
                else:
                    umo = self.group_umo_mapping.get(clean_gid)
                if not umo:
                    umo = self._guess_group_umo(clean_gid, platforms)
                if umo:
                    try:
                        for img_path in files:
                            chain = MessageChain().file_image(str(img_path))
                            await self.context.send_message(umo, chain)
                        sent = True
                        logger.info(f"[定时发送] 群 {clean_gid} 通过 UMO 兜底发送成功")
                    except Exception as e:
                        logger.warning(f"[定时发送] 群 {clean_gid} UMO兜底发送失败: {e}")
                else:
                    logger.warning(f"[定时发送] 群 {clean_gid} 无可用 UMO 映射，跳过兜底")

            if sent:
                ok_groups.append(clean_gid)
                logger.info(f"[定时发送] 群 {clean_gid} 发送成功，{len(files)} 张图")
            else:
                fail_groups.append(clean_gid)
                logger.error(f"[定时发送] 群 {clean_gid} 发送失败")

        return ok_groups, fail_groups

    # ── 核心抓取流程 ──

    async def _fetch_and_cache(self) -> _FetchOutcome:
        """启动浏览器，抓取并缓存最新快报。

        调用方须持有 _browser_lock，并在 finally 中调用 close_browser。
        """
        await self._scraper.close_browser()
        self._cache.maybe_cleanup_profile_cache(PROFILE_CLEANUP_INTERVAL_DAYS, PROFILE_DIR)
        _, ctx = await self._scraper.launch_persistent_browser()
        page = await ctx.new_page()
        await page.goto(TARGET_URL, wait_until="domcontentloaded",
                        timeout=REQUEST_TIMEOUT_SECONDS * 1000)

        try:
            await self._scraper.wait_for_report_cards(page)
        except TimeoutError:
            invalid, reason = await self._scraper.is_login_invalid(page, None)
            if invalid:
                return _FetchOutcome("timeout_login_invalid", reason=reason)
            return _FetchOutcome("timeout")

        report = await self._scraper.parse_latest_report_from_page(page)

        invalid, reason = await self._scraper.is_login_invalid(page, report)
        if invalid:
            return _FetchOutcome("login_invalid", reason=reason)

        if not report:
            return _FetchOutcome("no_report")

        if not self._cache.is_today_issue(report.issue_code):
            return _FetchOutcome("not_today", issue_code=report.issue_code)

        with self._cache.staged_download() as stage:
            saved = await self._scraper.download_report_images(
                page, report.image_urls, stage.dir
            )
            if not saved:
                return _FetchOutcome("download_failed", issue_code=report.issue_code)

            new_meta: CacheMeta = {
                "keyword": TRIGGER_KEYWORD,
                "issue_code": report.issue_code,
                "matched_title": report.matched_title,
                "source_url": report.source_url,
                "fetched_at": datetime.now(TZ_SHANGHAI).isoformat(),
                "sent": False,
                "sent_at": None,
            }
            final_paths = stage.commit(saved, new_meta)

        return _FetchOutcome(
            "success", meta=new_meta, files=final_paths, issue_code=report.issue_code,
        )

    # ── 定时发送 ──

    async def scheduled_send(self):
        """定时发送：检查缓存并发送到目标群。"""
        today = self._cache.get_today_issue_code()
        logger.info(f"[定时发送] 开始执行, today_issue={today}")
        sched = self._cache.load_schedule_meta()
        sched["last_send_attempt_ts"] = time.time()

        try:
            # 先检查本地缓存
            meta = self._cache.load_meta()
            if meta and self._cache.is_cache_valid_for_today(meta):
                logger.info(f"[定时发送] 使用已有缓存: {meta.get('matched_title')}")
                files = [self._cache.get_cache_dir() / f for f in meta.get("files", [])]
                ok, fail = await self._send_images_to_groups(files)
                if ok:
                    self._cache.mark_cache_sent(meta)
                    logger.info(f"[定时发送] 发送完成: 成功={len(ok)}群, 失败={len(fail)}群")
                if fail:
                    await self._notify_admins(
                        f"[定时发送] 今日快报发送失败的群: {', '.join(fail)}"
                    )
                sched["last_send_result"] = f"ok={len(ok)},fail={len(fail)}"
                self._cache.save_schedule_meta(sched)
                return

            # 缓存不可用，现场抓取
            logger.info("[定时发送] 缓存不可用，尝试现场抓取")
            async with self._browser_lock:
                try:
                    outcome = await self._fetch_and_cache()

                    if outcome.status == "timeout_login_invalid":
                        logger.error(f"[定时发送] 页面渲染超时且登录失效: {outcome.reason}")
                        sched["last_send_result"] = f"timeout_login_invalid: {outcome.reason}"
                        self._cache.save_schedule_meta(sched)
                        await self._notify_admins(
                            f"[定时发送] 页面渲染超时且登录失效: {outcome.reason}\n"
                            f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                        )
                        return

                    if outcome.status == "timeout":
                        logger.warning("[定时发送] 页面渲染超时")
                        sched["last_send_result"] = "fetch_timeout"
                        self._cache.save_schedule_meta(sched)
                        await self._notify_admins("[定时发送] 页面渲染超时，请检查网络或页面状态")
                        return

                    if outcome.status == "login_invalid":
                        logger.error(f"[定时发送] 登录失效: {outcome.reason}")
                        sched["last_send_result"] = f"login_invalid: {outcome.reason}"
                        self._cache.save_schedule_meta(sched)
                        await self._notify_admins(
                            f"[定时发送] B站登录态失效: {outcome.reason}\n"
                            f"请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                        )
                        return

                    if outcome.status == "no_report":
                        logger.info(f"[定时发送] 未找到银河快报内容, today_issue={today}")
                        sched["last_send_result"] = "no_report"
                        self._cache.save_schedule_meta(sched)
                        await self._notify_admins(f"[定时发送] 未找到银河快报内容 (today_issue={today})")
                        return

                    if outcome.status == "not_today":
                        logger.info(f"[定时发送] 页面最新期号 {outcome.issue_code} 非今天 {today}，跳过")
                        sched["last_send_result"] = "not_today"
                        self._cache.save_schedule_meta(sched)
                        return

                    if outcome.status == "download_failed":
                        logger.warning(f"[定时发送] 图片下载失败, issue={outcome.issue_code}")
                        sched["last_send_result"] = "download_failed"
                        self._cache.save_schedule_meta(sched)
                        await self._notify_admins(f"[定时发送] 图片下载失败 (issue={outcome.issue_code})")
                        return

                    # success
                    ok, fail = await self._send_images_to_groups(outcome.files)
                    if ok:
                        self._cache.mark_cache_sent(outcome.meta)
                        logger.info(f"[定时发送] 发送完成: 成功={len(ok)}群, 失败={len(fail)}群")
                    if fail:
                        await self._notify_admins(
                            f"[定时发送] 今日快报发送失败的群: {', '.join(fail)}"
                        )
                    sched["last_send_result"] = f"ok={len(ok)},fail={len(fail)}"
                    self._cache.save_schedule_meta(sched)

                finally:
                    await self._scraper.close_browser()

        except Exception as e:
            logger.error(f"[定时发送] 异常: {e}")
            sched["last_send_result"] = f"error: {e}"
            self._cache.save_schedule_meta(sched)
            await self._notify_admins(f"[定时发送] 异常: {e}")

    # ── 二维码登录编排 ──

    _LOGIN_TOTAL_TIMEOUT = 300      # 登录流程总时长上限 5 分钟
    _QR_SINGLE_TIMEOUT = 120        # 单张二维码有效期 120 秒
    _QR_MAX_REFRESH = 1             # 二维码最多自动刷新次数
    _QR_POLL_INTERVAL = 3.0         # 轮询间隔秒

    async def start_bili_qrcode_login(self, event: AstrMessageEvent):
        """完整的二维码登录流程（异步生成器，通过 yield 向触发者发送消息）。

        流程：
        1. 打开登录页，截取二维码发送给管理员
        2. 轮询等待扫码（单张二维码 120s）
        3. 若超时，自动刷新一次二维码（最多刷新 _QR_MAX_REFRESH 次）
        4. 总流程 5 分钟仍未成功则放弃
        """
        logger.info("开始二维码登录流程")
        flow_start = time.time()
        refresh_count = 0

        async with self._browser_lock:
            self._cache._set_login_in_progress(True)
            try:
                yield event.plain_result("已重建浏览器登录环境，正在获取二维码...")
                _, ctx = await self._scraper.launch_persistent_browser()
                page = await ctx.new_page()

                # ── 第一次获取二维码 ──
                qr_path = await self._scraper.open_login_page_and_capture_qrcode(page)
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

                    success = await self._scraper.wait_for_login_success(
                        page, timeout=this_round, poll_interval=self._QR_POLL_INTERVAL
                    )
                    if success:
                        self._cache._record_login_success()
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
                    sched = self._cache.load_schedule_meta()
                    sched["login_qrcode_refresh_count"] = refresh_count
                    self._cache.save_schedule_meta(sched)
                    logger.info(f"[登录] 二维码超时，自动刷新第 {refresh_count}/{self._QR_MAX_REFRESH} 次")
                    yield event.plain_result(
                        f"二维码已失效，正在自动刷新（第 {refresh_count} 次）..."
                    )
                    qr_path = await self._scraper.open_login_page_and_capture_qrcode(page)
                    yield event.image_result(str(qr_path))
                    yield event.plain_result(
                        "新二维码已生成，请在 120 秒内扫描"
                    )

                # 所有轮次均超时
                total_elapsed = int(time.time() - flow_start)
                logger.warning(f"二维码登录超时，总耗时 {total_elapsed}s，刷新 {refresh_count} 次")
                yield event.plain_result("二维码已超时，请重新获取")

            except Exception as e:
                logger.error(f"二维码登录流程异常: {e}")
                yield event.plain_result(f"二维码登录流程异常: {e}")
            finally:
                self._cache._record_login_end()
                await self._scraper.close_browser()

    # ── 生命周期 ──

    async def initialize(self):
        logger.info("BiliDailyReport 插件已加载")

        # 清理上次意外退出遗留的暂存目录和调试文件
        self._cache.cleanup_stale_temps()
        self._cache.cleanup_debug_files()

        if self.debug:
            logger.debug(f"配置: trigger={TRIGGER_KEYWORD}, "
                         f"headless={BROWSER_HEADLESS}, timeout={REQUEST_TIMEOUT_SECONDS}s, "
                         f"send={self.schedule_send_time}, target_groups={self.get_target_groups()}, "
                         f"cleanup_interval={PROFILE_CLEANUP_INTERVAL_DAYS}d")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """轻量级全局处理器：仅执行副作用（学习 UMO），不消费事件。"""
        self._learn_group_umo(event)

    @filter.command("快报群组ID")
    async def cmd_group_id(self, event: AstrMessageEvent):
        """查询当前群的 unified_msg_origin，方便用户配置 target_groups。"""
        self._learn_group_umo(event)
        umo = getattr(event, 'unified_msg_origin', None)
        group_id = event.get_group_id()
        if umo:
            msg = (f"当前会话的 unified_msg_origin:\n{umo}\n"
                   f"群号: {group_id or '未知'}\n"
                   f"可将群号或上方完整字符串添加到配置的 target_groups 中")
        else:
            msg = "无法获取当前会话的 unified_msg_origin"
        yield event.plain_result(msg)

    @filter.command("今日快报")
    async def cmd_daily_report(self, event: AstrMessageEvent):
        """手动触发今日快报：优先使用缓存，否则现场抓取。"""
        self._learn_group_umo(event)
        user_id = event.get_sender_id()
        today = self._cache.get_today_issue_code()
        logger.info(f"[手动快报] 收到触发: user={user_id}, today_issue={today}")

        # 先检查本地缓存是否有今天对应期号
        meta = self._cache.load_meta()
        if meta and self._cache.is_cache_valid_for_today(meta):
            logger.info(f"[手动快报] 本地缓存命中: {meta.get('matched_title')}, sent={meta.get('sent')}")
            for f in meta.get("files", []):
                yield event.image_result(str(self._cache.get_cache_dir() / f))
            return

        # 本地没有，现场抓取
        # NOTE: locked() 预检查是 UX 快速拒绝，与 async with 之间无实际挂起点，竞态不会发生
        if self._browser_lock.locked():
            yield event.plain_result("当前有其他任务正在使用浏览器，请稍后再试")
            return
        async with self._browser_lock:
            try:
                outcome = await self._fetch_and_cache()

                if outcome.status in ("timeout_login_invalid", "login_invalid"):
                    logger.warning(f"[手动快报] 登录失效: {outcome.reason}")
                    yield event.plain_result(
                        f"当前 Bilibili 登录态可能已失效，请发送 {RELOGIN_KEYWORD} 重新扫码登录"
                    )
                    await self._notify_admins(
                        f"[手动快报] 登录失效 (user={user_id}): {outcome.reason}"
                    )
                    return

                if outcome.status == "timeout":
                    yield event.plain_result("页面加载超时，请稍后再试")
                    return

                if outcome.status in ("no_report", "not_today"):
                    issue_info = outcome.issue_code or "无"
                    logger.info(f"[手动快报] 今日快报尚未更新, 最新issue={issue_info}, today={today}")
                    yield event.plain_result("今日快报尚未更新")
                    return

                if outcome.status == "download_failed":
                    logger.warning(f"[手动快报] 图片下载失败, issue={outcome.issue_code}")
                    yield event.plain_result("图片下载失败")
                    await self._notify_admins(
                        f"[手动快报] 图片下载失败 (user={user_id}, issue={outcome.issue_code})"
                    )
                    return

                # success
                logger.info(f"[手动快报] 抓取成功: issue={outcome.issue_code}, {len(outcome.files)} 张图片")
                for img_path in outcome.files:
                    yield event.image_result(str(img_path))

            except Exception as e:
                logger.error(f"[手动快报] 异常: {e}")
                yield event.plain_result(f"获取今日快报失败: {e}")
                await self._notify_admins(f"[手动快报] 异常 (user={user_id}): {e}")
            finally:
                await self._scraper.close_browser()

    @filter.command("bili_qrcode_reset")
    async def cmd_qrcode_reset(self, event: AstrMessageEvent):
        """重置浏览器登录环境（管理员命令）。"""
        user_id = event.get_sender_id()
        if not self.is_admin_user(user_id):
            yield event.plain_result("仅管理员可执行此操作")
            return
        if self._cache.is_login_in_progress():
            yield event.plain_result("当前已有登录流程进行中，请稍后再试")
            return
        logger.info(f"收到重置 profile 触发 (user={user_id})")
        async with self._browser_lock:
            try:
                await self._scraper.close_browser()
                self._cache.reset_profile_dir(PROFILE_DIR)
                yield event.plain_result("已重置浏览器登录环境，请发送 bili_qrcode 重新扫码")
            except Exception as e:
                logger.error(f"重置 profile 失败: {e}")
                yield event.plain_result(f"重置登录环境失败: {e}")

    @filter.command("bili_qrcode")
    async def cmd_qrcode_login(self, event: AstrMessageEvent):
        """触发 B 站二维码登录流程（管理员命令）。"""
        user_id = event.get_sender_id()
        if not self.is_admin_user(user_id):
            yield event.plain_result("仅管理员可执行此操作")
            return
        if self._cache.is_login_in_progress():
            yield event.plain_result("当前已有登录流程进行中，请稍后再试")
            return
        if self._cache.is_login_recently_successful():
            yield event.plain_result("当前账号已登录，请勿重复登录")
            return
        logger.info(f"收到重新登录触发 (user={user_id})")
        # NOTE: locked() 预检查是 UX 快速拒绝，竞态不影响正确性
        if self._browser_lock.locked():
            yield event.plain_result("当前有其他任务正在使用浏览器，请稍后再试")
            return
        async for result in self.start_bili_qrcode_login(event):
            yield result

    @filter.command("随机战备")
    async def cmd_random_loadout(self, event: AstrMessageEvent):
        """按规则随机生成绝地潜兵2战备配置。"""
        loadout = generate_random_loadout()
        yield event.plain_result(format_loadout(loadout))

    @filter.command("全随机战备")
    async def cmd_full_random_loadout(self, event: AstrMessageEvent):
        """完全随机生成绝地潜兵2战备配置，不遵循任何规则。"""
        loadout = generate_full_random_loadout()
        yield event.plain_result(format_loadout(loadout, full_random=True))

    async def terminate(self):
        await self._scraper.close_browser()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("BiliDailyReport 插件已卸载")
