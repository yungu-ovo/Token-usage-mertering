"""告警引擎。

三类：预算三档（75/90/100）、预测耗尽（补足「提前 ≥7 天预警」）、烧钱速率异常（3×）。

防重复：``alert_state`` 以 ``alert_key`` 去重，同一 key 在同一周期只 fire 一次；
指标回落到阈值以下时写入 ``cleared_at``，允许下一周期重新触发。
"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from decimal import Decimal
from typing import Any

from . import config
from .caliber import days_until_budget_exhausted, month_end_forecast
from .db import get_budget
from .money import dec, money_str, to_micro
from .queries.common import COST_EXPR, fetch_cost, month_range

UNPRICED_KEY = "unpriced:models"


# ------------------------------------------------------------------ 状态读写
def _is_active(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT triggered_at, cleared_at FROM alert_state WHERE alert_key = ?", (key,)
    ).fetchone()
    if row is None:
        return False
    return row["triggered_at"] is not None and row["cleared_at"] is None


def fire(
    conn: sqlite3.Connection,
    key: str,
    level: str,
    category: str,
    message: str,
    value: Decimal | None = None,
    threshold: Decimal | None = None,
) -> bool:
    """触发告警。已处于激活态则跳过，返回 False。"""
    if _is_active(conn, key):
        return False

    conn.execute(
        "INSERT INTO alert_state (alert_key, level, triggered_at, cleared_at) "
        "VALUES (?, ?, datetime('now'), NULL) "
        "ON CONFLICT(alert_key) DO UPDATE SET "
        "  level = excluded.level, triggered_at = datetime('now'), cleared_at = NULL",
        (key, level),
    )
    conn.execute(
        "INSERT INTO alert_event (alert_key, level, category, message, value_micro, threshold_micro) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            key,
            level,
            category,
            message,
            None if value is None else to_micro(value),
            None if threshold is None else to_micro(threshold),
        ),
    )
    return True


def clear(conn: sqlite3.Connection, key: str) -> None:
    """指标回落，解除激活态，允许下次再触发。"""
    conn.execute(
        "UPDATE alert_state SET cleared_at = datetime('now') "
        "WHERE alert_key = ? AND cleared_at IS NULL",
        (key,),
    )


# ------------------------------------------------------------------ 预算三档
def _thresholds(
    conn: sqlite3.Connection, scope: str, spent: Decimal, budget: Decimal, period: str
) -> int:
    if budget <= 0:
        return 0
    ratio = spent / budget
    fired = 0
    for threshold, level in config.BUDGET_THRESHOLDS:
        key = f"budget:{scope}:{period}:{int(threshold * 100)}"
        if ratio >= threshold:
            message = (
                f"{'本月' if scope == 'monthly' else '今日'}预算已用 "
                f"{ratio * 100:.1f}%（${money_str(spent)} / ${money_str(budget)}），"
                f"越过 {int(threshold * 100)}% 阈值"
            )
            if fire(conn, key, level, "budget", message, spent, budget):
                fired += 1
        else:
            clear(conn, key)
    return fired


def evaluate_budget(conn: sqlite3.Connection, today: dt.date | None = None) -> dict[str, Any]:
    """每次 ingest 后同步调用，开销很小（两次 SUM）。"""
    now = today or dt.date.today()
    daily_budget_micro, monthly_budget_micro, _currency = get_budget(conn)
    daily_budget = dec(daily_budget_micro) / 1000000
    monthly_budget = dec(monthly_budget_micro) / 1000000

    m_lo, m_hi = month_range(now)
    mtd_cost, _ = fetch_cost(conn, m_lo, m_hi)
    today_cost, _ = fetch_cost(conn, m_hi, m_hi)

    fired = _thresholds(conn, "monthly", mtd_cost, monthly_budget, now.strftime("%Y-%m"))
    fired += _thresholds(conn, "daily", today_cost, daily_budget, now.isoformat())

    forecast = _evaluate_forecast(conn, now, mtd_cost, monthly_budget)
    return {
        "fired": fired,
        "mtd_cost": money_str(mtd_cost),
        "today_cost": money_str(today_cost),
        "forecast": money_str(forecast["forecast"]),
    }


def _evaluate_forecast(
    conn: sqlite3.Connection, today: dt.date, mtd_cost: Decimal, monthly_budget: Decimal
) -> dict[str, Any]:
    """预测耗尽告警：按当前速度，剩余天数少于提前量时触发。"""
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    forecast = month_end_forecast(mtd_cost, today.day, days_in_month)
    runway = days_until_budget_exhausted(mtd_cost, monthly_budget, today.day, days_in_month)

    key = f"forecast:exhaust:{today.strftime('%Y-%m')}"
    days_left = int(runway.value)
    remaining_in_month = days_in_month - today.day

    if (
        forecast.value > monthly_budget
        and remaining_in_month >= 0
        and days_left < remaining_in_month
        and mtd_cost > 0
    ):
        fire(
            conn,
            key,
            "warn",
            "forecast",
            f"按当前日均速度，预算预计在 {days_left} 天后耗尽，"
            f"早于月末 {remaining_in_month} 天；月末预测 ${money_str(forecast.value)}"
            f"（超预算 ${money_str(forecast.value - monthly_budget)}）。{forecast.method}",
            forecast.value,
            monthly_budget,
        )
    else:
        clear(conn, key)

    return {"forecast": forecast.value, "runway_days": days_left}


# ------------------------------------------------------------------ 未定价模型
def evaluate_unpriced(conn: sqlite3.Connection) -> bool:
    """只要有未定价记录就告警一次（RK-03）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS c, COUNT(DISTINCT model_raw) AS k FROM usage_fact "
        "WHERE model = '__unknown__'"
    ).fetchone()
    count = int(row["c"] or 0)
    if count == 0:
        clear(conn, UNPRICED_KEY)
        return False
    return fire(
        conn,
        UNPRICED_KEY,
        "warn",
        "pricing",
        f"{count} 条记录的模型不在价格表内（{int(row['k'])} 种），"
        f"成本被记为 0 且已标记，不会静默计入汇总",
    )


# ------------------------------------------------------------------ 烧钱速率
def evaluate_burn_rate(conn: sqlite3.Connection) -> bool:
    from .queries.insights import _burn_rate

    info = _burn_rate(conn)
    key = f"burn_rate:{dt.datetime.now().strftime('%Y-%m-%dT%H')}"
    if info["triggered"]:
        return fire(
            conn,
            key,
            "severe",
            "burn_rate",
            f"近 {config.BURN_RATE_WINDOW_HOURS} 小时成本 ${info['current']}，"
            f"为过去 {config.BURN_RATE_BASELINE_DAYS} 天同时段均值 ${info['baseline']} 的 "
            f"{info['ratio']:.1f} 倍",
            dec(info["current"]),
            dec(info["baseline"]),
        )
    return False


# ------------------------------------------------------------------ 定时任务
def evaluate_scheduled(conn: sqlite3.Connection) -> dict[str, int]:
    """每小时跑一次：烧钱速率 + 预测耗尽 + 未定价模型。"""
    return {
        "budget": evaluate_budget(conn)["fired"],
        "burn_rate": int(evaluate_burn_rate(conn)),
        "unpriced": int(evaluate_unpriced(conn)),
    }


# ------------------------------------------------------------------ 读取
def recent(
    conn: sqlite3.Connection, limit: int = 50, unacked_only: bool = False
) -> list[dict[str, Any]]:
    clause = "WHERE acknowledged = 0" if unacked_only else ""
    rows = conn.execute(
        f"SELECT * FROM alert_event {clause} ORDER BY fired_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "key": r["alert_key"],
            "level": r["level"],
            "category": r["category"],
            "message": r["message"],
            "value": None if r["value_micro"] is None else money_str(dec(r["value_micro"]) / 1000000),
            "threshold": None if r["threshold_micro"] is None else money_str(dec(r["threshold_micro"]) / 1000000),
            "fired_at": r["fired_at"],
            "acknowledged": bool(r["acknowledged"]),
        }
        for r in rows
    ]


def acknowledge(conn: sqlite3.Connection, alert_id: int) -> bool:
    cur = conn.execute(
        "UPDATE alert_event SET acknowledged = 1 WHERE id = ?", (alert_id,)
    )
    return bool(cur.rowcount)
