import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from astrbot.api import logger

from .models import CacheMeta, ScheduleMeta, TZ_SHANGHAI


class CacheManager:
    """缓存管理：meta.json、图片缓存、profile 清理、调度状态、群组映射、期号判断。"""

    _CLEANUP_SAFE_DIRS = {"Cache", "Code Cache", "GPUCache"}

    def __init__(self, data_dir: Path, debug: bool = False):
        self._data_dir = data_dir
        self.debug = debug
        self._skip_cleanup_once = False

    # ── 期号判断 ──

    def get_today_issue_code(self) -> str:
        """返回今天对应的期号，格式 MMDD。"""
        return datetime.now(TZ_SHANGHAI).strftime("%m%d")

    def is_today_issue(self, issue_code: str) -> bool:
        """判断给定期号是否等于今天的 MMDD。"""
        today = self.get_today_issue_code()
        result = issue_code == today
        if self.debug:
            logger.debug(f"期号判断: issue_code={issue_code}, today={today}, match={result}")
        return result

    # ── 报告缓存 ──

    def get_cache_dir(self) -> Path:
        """返回当前期缓存目录，自动创建。"""
        p = self._data_dir / "cache" / "current"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_meta(self) -> CacheMeta | None:
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

    def save_meta(self, meta: CacheMeta) -> None:
        """保存 meta.json 到缓存目录。"""
        meta_file = self.get_cache_dir() / "meta.json"
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"meta.json 已保存: issue_code={meta.get('issue_code')}")

    def is_cache_valid_for_today(self, meta: CacheMeta) -> bool:
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

    def is_cache_sent(self, meta: CacheMeta) -> bool:
        """判断当前缓存是否已自动发送过。"""
        return bool(meta.get("sent", False))

    def mark_cache_sent(self, meta: CacheMeta) -> None:
        """标记当前缓存为已自动发送，并保存。"""
        meta["sent"] = True
        meta["sent_at"] = datetime.now(TZ_SHANGHAI).isoformat()
        self.save_meta(meta)
        logger.info(f"已标记 sent=True: issue_code={meta.get('issue_code')}")

    def clear_current_cache(self) -> None:
        """删除当前期缓存目录下所有文件。"""
        cache_dir = self.get_cache_dir()
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info(f"已清除旧缓存: {cache_dir}")
        cache_dir.mkdir(parents=True, exist_ok=True)

    # ── 安全暂存下载 ──

    @contextmanager
    def staged_download(self) -> Generator["_StagedDownload", None, None]:
        """提供一个临时目录用于暂存下载文件。

        用法::

            with self._cache.staged_download() as stage:
                # stage.dir  — Path, 暂存目录，下载文件到此处
                saved = await scraper.download_report_images(page, urls, stage.dir)
                # 全部成功后提交到正式缓存
                stage.commit(saved, meta_dict)

        如果在 commit 之前发生异常，暂存目录会被自动清理，
        不会在正式缓存中留下残留文件。
        """
        stage = _StagedDownload(self)
        try:
            yield stage
        except Exception:
            # 异常时暂存目录由 TemporaryDirectory 自动清理
            raise
        finally:
            stage._cleanup()

    def cleanup_stale_temps(self) -> None:
        """清理 data_dir 下残留的暂存目录（名称以 _download_ 开头）。
        在插件启动时调用，处理上次意外退出遗留的临时文件。
        """
        staging_root = self._data_dir / "staging"
        if not staging_root.exists():
            return
        for child in staging_root.iterdir():
            if child.is_dir():
                try:
                    shutil.rmtree(child)
                    logger.info(f"已清理残留暂存目录: {child.name}")
                except Exception as e:
                    logger.warning(f"清理残留暂存目录失败 {child.name}: {e}")
        # 如果 staging 目录本身已空，也删掉
        try:
            staging_root.rmdir()
        except OSError:
            pass

    def cleanup_debug_files(self) -> None:
        """清理 data_dir 下的调试截图和旧二维码文件。"""
        for name in ("debug_page.png", "debug_page.html", "qrcode.png"):
            p = self._data_dir / name
            if p.exists():
                try:
                    p.unlink()
                    logger.debug(f"已清理: {name}")
                except Exception as e:
                    logger.warning(f"清理 {name} 失败: {e}")

    # ── profile 目录管理 ──

    def ensure_profile_dir(self, profile_dir_override: str = "") -> Path:
        """确保 profile 目录存在并返回其路径。"""
        if profile_dir_override:
            p = Path(profile_dir_override)
        else:
            p = self._data_dir / "profile"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def reset_profile_dir(self, profile_dir_override: str = "") -> Path:
        """删除旧 profile 目录并重建空目录。
        同时设置 _skip_cleanup_once 标记，避免本次再额外执行 7 天缓存清理。"""
        profile = self.ensure_profile_dir(profile_dir_override)
        if profile.exists():
            shutil.rmtree(profile)
            logger.info(f"已删除旧 profile: {profile}")
        profile.mkdir(parents=True, exist_ok=True)
        logger.info(f"已重建空 profile: {profile}")
        self._skip_cleanup_once = True
        return profile

    # ── profile 缓存清理 ──

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

    def maybe_cleanup_profile_cache(self, cleanup_interval_days: int, profile_dir_override: str = "") -> None:
        """如果距上次清理超过 cleanup_interval_days 天，清理 profile 中的浏览器缓存目录。"""
        if self._skip_cleanup_once:
            logger.info("[缓存清理] 本次已执行 profile 重建，跳过 7 天缓存清理")
            self._skip_cleanup_once = False
            return

        meta = self._load_cleanup_meta()
        last_ts = meta.get("last_cleanup_ts", 0)
        now = time.time()
        elapsed_days = (now - last_ts) / 86400
        if elapsed_days < cleanup_interval_days:
            if self.debug:
                logger.debug(f"距上次 profile 缓存清理 {elapsed_days:.1f} 天，未到 {cleanup_interval_days} 天，跳过")
            return

        profile = self.ensure_profile_dir(profile_dir_override)
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

    def load_schedule_meta(self) -> ScheduleMeta:
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
                for k, v in defaults.items():
                    data.setdefault(k, v)
                return data
            except Exception as e:
                logger.warning(f"schedule_meta.json 解析失败: {e}")
        return dict(defaults)

    def save_schedule_meta(self, meta: ScheduleMeta) -> None:
        """保存调度状态。"""
        p = self._schedule_meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.debug:
            logger.debug("schedule_meta.json 已保存")

    # ── 登录锁与冷却 ──

    _LOGIN_COOLDOWN_DAYS = 7
    _LOGIN_STALE_SECONDS = 600  # login_in_progress 超过 10 分钟视为 stale

    def is_login_in_progress(self) -> bool:
        sched = self.load_schedule_meta()
        if not sched.get("login_in_progress", False):
            return False
        # 防止因崩溃导致 login_in_progress 永久卡 True
        started_ts = sched.get("login_started_ts", 0)
        if started_ts > 0 and (time.time() - started_ts) > self._LOGIN_STALE_SECONDS:
            logger.warning("[登录] login_in_progress 已超时，自动重置")
            sched["login_in_progress"] = False
            self.save_schedule_meta(sched)
            return False
        return True

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

    # ── 群组 UMO 映射 ──

    def _group_mapping_path(self) -> Path:
        return self._data_dir / "group_mapping.json"

    def load_group_mapping(self) -> dict:
        """加载群号 -> unified_msg_origin 的映射。"""
        p = self._group_mapping_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_group_mapping(self, mapping: dict) -> None:
        p = self._group_mapping_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


class _StagedDownload:
    """staged_download 上下文管理器的内部状态。

    提供一个临时目录 ``self.dir`` 供下载使用，
    调用 ``commit()`` 后将文件原子性地移入正式缓存目录。
    """

    def __init__(self, cache: CacheManager):
        self._cache = cache
        staging_root = cache._data_dir / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        self._tmpdir = tempfile.mkdtemp(dir=staging_root, prefix="_download_")
        self.dir = Path(self._tmpdir)
        self._committed = False

    def commit(self, saved_files: list[Path], meta: CacheMeta) -> list[Path]:
        """将暂存的文件移入正式缓存目录，写入 meta.json。

        返回文件在正式缓存目录中的路径列表。
        """
        self._cache.clear_current_cache()
        cache_dir = self._cache.get_cache_dir()
        final_paths = []
        for src in saved_files:
            dst = cache_dir / src.name
            shutil.move(str(src), str(dst))
            final_paths.append(dst)
        meta["files"] = [p.name for p in final_paths]
        self._cache.save_meta(meta)
        self._committed = True
        logger.info(f"暂存提交完成: {len(final_paths)} 个文件 -> {cache_dir}")
        return final_paths

    def _cleanup(self) -> None:
        """清理暂存目录。commit 后目录应已为空或仅剩空目录。"""
        if self.dir.exists():
            try:
                shutil.rmtree(self.dir)
                if not self._committed:
                    logger.info(f"已清理未提交的暂存目录: {self.dir.name}")
            except Exception as e:
                logger.warning(f"清理暂存目录失败: {e}")
