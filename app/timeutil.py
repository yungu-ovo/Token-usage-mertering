"""时间解析。采集侧与采集入口共用，避免依赖方向颠倒。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import config

LOCAL_TZ = dt.timezone(dt.timedelta(hours=config.LOCAL_TZ_OFFSET_HOURS))


def parse_ts(raw: Any) -> tuple[str, str]:
    """把各种时间表示解析为 (ISO8601 本地时区, 本地自然日 YYYY-MM-DD)。

    支持：ISO8601（带/不带时区、带 Z）、Unix 秒、Unix 毫秒、纯数字字符串。
    无法识别时回落到当前时间。
    """
    moment = _to_datetime(raw)
    if moment is None:
        moment = dt.datetime.now(tz=LOCAL_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    local = moment.astimezone(LOCAL_TZ)
    return local.isoformat(), local.date().isoformat()


def _to_datetime(raw: Any) -> dt.datetime | None:
    if raw is None or raw == "":
        return None

    if isinstance(raw, bool):
        return None

    # 数值：需要区分秒与毫秒。WorkBuddy 的 timestamp 实测是 13 位毫秒。
    if isinstance(raw, (int, float)):
        return _from_epoch_number(float(raw))

    text = str(raw).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # 纯数字字符串同样可能是秒或毫秒
    if text.lstrip("-").isdigit():
        return _from_epoch_number(float(text))

    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


# 秒级 Unix 时间戳如今约 1.79e9，毫秒级约 1.79e12。用 1e11 作为分界。
_MS_THRESHOLD = 1e11


def _from_epoch_number(value: float) -> dt.datetime | None:
    """按数量级判断秒还是毫秒后再转换。"""
    if abs(value) >= _MS_THRESHOLD:
        value = value / 1000.0
    return _from_epoch(value)


def _from_epoch(seconds: float) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def today_local() -> dt.date:
    return dt.datetime.now(tz=LOCAL_TZ).date()


def now_local() -> dt.datetime:
    """当前**本地时区**时刻（带 tzinfo）。"""
    return dt.datetime.now(tz=LOCAL_TZ)


def now_local_str() -> str:
    """当前本地时间，格式化成 ``YYYY-MM-DD HH:MM:SS``。

    凡是**会展示给用户**的时间列，都必须由 Python 侧算好再写库，
    不能让 SQLite 用 ``datetime('now')`` 生成——那是 UTC，UTC+8 下会整整差 8 小时
    （A-3：告警时间戳曾因此全部早了 8 小时）。
    """
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def now_local_iso() -> str:
    """当前本地时间的 ISO8601 串（带 +08:00 偏移）。"""
    return now_local().isoformat(timespec="seconds")
