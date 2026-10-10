"""进程内定时调度。

用 APScheduler 的 ``AsyncIOScheduler``——纯 Python、无 broker。
**刻意不引入 Celery / Redis**：单机场景下那是纯粹的增加运维负担。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts, collect as collect_mod, config
from .aggregator import prune_details
from .db import session, tx

log = logging.getLogger("tum.scheduler")

_scheduler: Any = None
HOURLY_JOB_ID = "tum-hourly"
DAILY_JOB_ID = "tum-daily-prune"
COLLECT_JOB_ID = "tum-collect"


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


def _collect() -> None:
    """定时采集（B-1）。默认**不开**：它会在后台定期读本机会话文件，
    「服务自己读」和「用户点一下才读」是两种不同的信任关系，该由用户显式开启。

    同步 IO 跑在事件循环里会短暂阻塞请求；本机单机、采集量不大，可接受。
    """
    try:
        with tx() as conn:
            summary = collect_mod.collect(conn)
        log.info("定时采集完成: %s", collect_mod.collect_stats_line(summary))
    except Exception:
        log.exception("定时采集失败")


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
    if config.AUTO_COLLECT:
        _scheduler.add_job(
            _collect, "interval", minutes=config.AUTO_COLLECT_INTERVAL_MINUTES,
            id=COLLECT_JOB_ID, max_instances=1, coalesce=True,
        )
    _scheduler.start()
    log.info(
        "定时调度已启动（每小时巡检 + 每日清理%s）",
        f" + 每 {config.AUTO_COLLECT_INTERVAL_MINUTES} 分钟采集"
        if config.AUTO_COLLECT else "，采集按需手动触发",
    )


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_once() -> dict[str, int]:
    """手动跑一次巡检（给 /api/refresh 用）。"""
    with session() as conn:
        return alerts.evaluate_scheduled(conn)
