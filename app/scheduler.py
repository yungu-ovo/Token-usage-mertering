"""进程内定时调度。

用 APScheduler 的 ``AsyncIOScheduler``——纯 Python、无 broker。
**刻意不引入 Celery / Redis**：单机场景下那是纯粹的增加运维负担。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts
from .aggregator import prune_details
from .db import session

log = logging.getLogger("tum.scheduler")

_scheduler: Any = None
HOURLY_JOB_ID = "tum-hourly"
DAILY_JOB_ID = "tum-daily-prune"


def enabled() -> bool:
    return os.environ.get("TUM_SCHEDULER", "1") not in ("0", "false", "False")


def _hourly() -> None:
    try:
        with session() as conn:
            result = alerts.evaluate_scheduled(conn)
        log.info("每小时巡检完成: %s", result)
    except Exception:  # 调度任务不能把服务带崩
        log.exception("每小时巡检失败")


def _daily() -> None:
    try:
        with session() as conn:
            removed = prune_details(conn)
        log.info("明细清理完成，删除 %s 行", removed)
    except Exception:
        log.exception("明细清理失败")


def start() -> None:
    global _scheduler
    if not enabled() or _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        log.warning("APScheduler 未安装，定时巡检已禁用")
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_hourly, "interval", hours=1, id=HOURLY_JOB_ID,
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_daily, "cron", hour=3, minute=30, id=DAILY_JOB_ID,
                       max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("定时调度已启动（每小时巡检 + 每日清理）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_once() -> dict[str, int]:
    """手动跑一次巡检（给 /api/refresh 用）。"""
    with session() as conn:
        return alerts.evaluate_scheduled(conn)
