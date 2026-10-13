"""进程内定时调度。

用 APScheduler 的 ``AsyncIOScheduler``——纯 Python、无 broker。
**刻意不引入 Celery / Redis**：单机场景下那是纯粹的增加运维负担。

P0-B 起采集是**双模**的：

* ``_watch``（增量轮，默认 15s）：只读文件水位后的新增行，毫秒级成本。
  这是「调用即记录」的主力——服务常驻即自动采集，无需人工干预。
* ``_calibrate``（校准轮，默认 60min）：全量重扫，负责 RK-07 的权威解析
  成功率分母、traces 缓存回填与聚合表自愈。

两轮都先抢 ``COLLECT_LOCK``（非阻塞）：抢不到就跳过本轮。
手动采集（用户点按钮）优先于定时轮——它是明确的用户意图，不该被排队阻塞。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import alerts, collect as collect_mod, config
from .aggregator import prune_details
from .db import session, tx
from .sources.incremental import prune_missing

log = logging.getLogger("tum.scheduler")

_scheduler: Any = None
HOURLY_JOB_ID = "tum-hourly"
DAILY_JOB_ID = "tum-daily-prune"
WATCH_JOB_ID = "tum-watch"
CALIBRATE_JOB_ID = "tum-calibrate"


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
            stale_marks = prune_missing(conn)
        log.info("明细清理完成，删除 %s 行；水位清理 %s 行", removed, stale_marks)
    except Exception:
        log.exception("每日清理失败")


def _run_collect(*, incremental: bool, job: str) -> None:
    """跑一轮采集。抢不到锁就跳过——下一轮马上再来，不堆积。"""
    if not collect_mod.COLLECT_LOCK.acquire(blocking=False):
        log.debug("%s 跳过：另一轮采集正在进行", job)
        return
    try:
        with tx() as conn:
            summary = collect_mod.collect(conn, incremental=incremental)
        log.info("%s 完成: %s", job, collect_mod.collect_stats_line(summary))
    except Exception:
        log.exception("%s 失败", job)
    finally:
        collect_mod.COLLECT_LOCK.release()


def _watch() -> None:
    """增量轮（P0-B）。同步 IO 跑在事件循环里会短暂阻塞请求；
    单机、采集量是「新增的几行」，毫秒级，可接受。
    """
    _run_collect(incremental=True, job="增量采集")


def _calibrate() -> None:
    """校准轮（P0-B）：全量重扫 + traces 回填 + 聚合自愈。"""
    _run_collect(incremental=False, job="校准采集")


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
            _watch, "interval", seconds=config.WATCH_INTERVAL_SECONDS,
            id=WATCH_JOB_ID, max_instances=1, coalesce=True,
        )
        _scheduler.add_job(
            _calibrate, "interval", minutes=config.CALIBRATION_MINUTES,
            id=CALIBRATE_JOB_ID, max_instances=1, coalesce=True,
        )
    _scheduler.start()
    if config.AUTO_COLLECT:
        log.info(
            "定时调度已启动（每小时巡检 + 每日清理 + 每 %ds 增量采集 + 每 %dmin 校准）",
            config.WATCH_INTERVAL_SECONDS, config.CALIBRATION_MINUTES,
        )
    else:
        log.info("定时调度已启动（每小时巡检 + 每日清理；自动采集已关闭，按需手动触发）")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_once() -> dict[str, int]:
    """手动跑一次巡检（给 /api/refresh 用）。"""
    with session() as conn:
        return alerts.evaluate_scheduled(conn)
