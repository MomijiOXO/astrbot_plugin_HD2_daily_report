"""插件内共享的数据结构定义与常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone, timedelta
from typing import TypedDict

# 插件统一使用的时区：北京时间 (UTC+8)
TZ_SHANGHAI = timezone(timedelta(hours=8))


# ── 爬虫解析结果 ──

@dataclass(frozen=True, slots=True)
class ReportResult:
    """从 B 站动态页面解析出的单期银河快报。"""

    matched_title: str
    """匹配到的动态标题，如 "银河快报0320"。"""

    issue_code: str
    """期号，格式 MMDD，如 "0320"。"""

    source_url: str
    """动态原文链接。"""

    image_urls: list[str] = field(default_factory=list)
    """快报图片 URL 列表（已去重、归一化）。"""


# ── 缓存 meta.json ──

class CacheMeta(TypedDict, total=False):
    """cache/current/meta.json 的结构。"""

    keyword: str
    issue_code: str
    matched_title: str
    source_url: str
    fetched_at: str
    files: list[str]
    sent: bool
    sent_at: str | None


# ── 调度状态 schedule_meta.json ──

class ScheduleMeta(TypedDict, total=False):
    """schedule_meta.json 的结构。"""

    last_fetch_check_ts: float
    last_fetch_result: str
    last_send_attempt_ts: float
    last_send_result: str
    last_login_success_ts: float
    login_in_progress: bool
    login_started_ts: float
    login_qrcode_refresh_count: int
