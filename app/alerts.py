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
from .db import get_budget, get_meta
from .money import dec, money_str, to_micro
from .queries.common import COST_EXPR, fetch_cost, month_range
from .timeutil import now_local, now_local_str

UNPRICED_KEY = "unpriced:models"
PARSER_KEY = "parser:success_rate"
# 状态型告警（不是事件型）：速率回落后解除，再超再触发。
# 早先按小时拼 key，每个小时都是一个新 key，alert_state 会无限膨胀且永不解除。
BURN_RATE_KEY = "burn_rate"

# 采集健康度的元信息键（由 app/collect.py 写入）
META_PARSE_RATE = "parse_success_rate"
META_PARSER_VERSION = "parser_version"
META_PARSER_SCANNED = "parse_scanned"
META_PARSER_USAGE_LINES = "parse_usage_lines"
META_PARSER_PARSED = "parse_parsed"


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

    now = now_local_str()
    conn.execute(
        "INSERT INTO alert_state (alert_key, level, triggered_at, cleared_at) "
        "VALUES (?, ?, ?, NULL) "
        "ON CONFLICT(alert_key) DO UPDATE SET "
        "  level = excluded.level, triggered_at = excluded.triggered_at, cleared_at = NULL",
        (key, level, now),
    )
    conn.execute(
        "INSERT INTO alert_event "
        "(alert_key, level, category, message, value_micro, threshold_micro, fired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            level,
            category,
            message,
            None if value is None else to_micro(value),
            None if threshold is None else to_micro(threshold),
            now,
        ),
    )
    return True


def clear(conn: sqlite3.Connection, key: str) -> None:
    """指标回落，解除激活态，允许下次再触发。

    A-1：光解除状态层不够——事件层那条未确认的告警还会一直挂在界面上，
    说一件已经不成立的事。所以这里同时把它标成「已自动解除」。
    """
    now = now_local_str()
    conn.execute(
        "UPDATE alert_state SET cleared_at = ? "
        "WHERE alert_key = ? AND cleared_at IS NULL",
        (now, key),
    )
    conn.execute(
        "UPDATE alert_event SET resolved_at = ? "
        "WHERE alert_key = ? AND acknowledged = 0 AND resolved_at IS NULL",
        (now, key),
    )


# ------------------------------------------------------------------ 自我校验
# 不变量：**一条「未确认且未自动解除」的告警，其 alert_key 必须仍处于激活态。**
# 反过来说，只要 key 不活跃了，那条告警就是在陈述一件已经不成立的事，
# 必须被标为已解除——上一轮的「845 条未定价（实际 0 条）」就是这么漏出来的。
def stale_open_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """返回「仍在展示、但问题已经不成立」的告警事件。正常应恒为空。"""
    rows = conn.execute(
        "SELECT e.id, e.alert_key, e.level, e.category, e.message, e.fired_at "
        "FROM alert_event e "
        "WHERE e.acknowledged = 0 AND e.resolved_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM alert_state s "
        "  WHERE s.alert_key = e.alert_key "
        "    AND s.triggered_at IS NOT NULL AND s.cleared_at IS NULL"
        ")"
    ).fetchall()
    return [dict(r) for r in rows]


def sweep_stale(conn: sqlite3.Connection) -> int:
    """把「已不成立但仍挂着」的告警标为自动解除。返回处理条数。"""
    stale = stale_open_events(conn)
    if not stale:
        return 0
    now = now_local_str()
    conn.execute(
        "UPDATE alert_event SET resolved_at = ? "
        "WHERE acknowledged = 0 AND resolved_at IS NULL AND id IN (%s)"
        % ",".join("?" for _ in stale),
        [now] + [r["id"] for r in stale],
    )
    return len(stale)


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
    if info["triggered"]:
        return fire(
            conn,
            BURN_RATE_KEY,
            "severe",
            "burn_rate",
            f"近 {config.BURN_RATE_WINDOW_HOURS} 小时成本 ${info['current']}，"
            f"为过去 {config.BURN_RATE_BASELINE_DAYS} 天同时段均值 ${info['baseline']} 的 "
            f"{info['ratio']:.1f} 倍",
            dec(info["current"]),
            dec(info["baseline"]),
        )
    # 速率回落后必须解除，否则这个 key 永远活跃、之后再也不会触发（A-1 同源问题）
    clear(conn, BURN_RATE_KEY)
    return False


# ------------------------------------------------------------------ 采集健康度
def evaluate_parser(conn: sqlite3.Connection) -> bool:
    """解析成功率低于阈值就告警（RK-07）。

    数据源改了日志格式时，解析器会**静默地少解析**，看板上表现为"今天成本很低"，
    用户只会以为是自己没怎么用。这是必然发生且无感知的失败，必须有告警。
    """
    rate_text = get_meta(conn, META_PARSE_RATE)
    if not rate_text:
        clear(conn, PARSER_KEY)
        return False

    try:
        rate = dec(rate_text)
    except Exception:
        return False

    scanned = int(get_meta(conn, META_PARSER_USAGE_LINES) or 0)
    parsed = int(get_meta(conn, META_PARSER_PARSED) or 0)
    version = get_meta(conn, META_PARSER_VERSION) or "unknown"

    if scanned <= 0:
        clear(conn, PARSER_KEY)
        return False

    if rate < config.PARSE_SUCCESS_MIN:
        return fire(
            conn,
            PARSER_KEY,
            "warn",
            "parser",
            f"解析器（{version}）成功率 {rate * 100:.1f}%，低于 {config.PARSE_SUCCESS_MIN * 100:.0f}%："
            f"{scanned} 条含用量记录只解析出 {parsed} 条。"
            f"通常是数据源改了日志格式，成本会被**少算**，请核对后再信看板上的数字",
            rate,
            config.PARSE_SUCCESS_MIN,
        )
    clear(conn, PARSER_KEY)
    return False


# ------------------------------------------------------------------ 采集进程健康度（P0-B）
STALL_KEY = "collector:stalled"
# 超过 3 倍轮询间隔（且至少 5 分钟）没有采集轮完成，就认为调度死了。
# 注意区分解「真没用模型」和「采集坏了」：本判断只看**采集轮有没有在跑**，
# 轮次在跑但扫不到新数据是正常空转，不告警。
STALL_MIN_MINUTES = 5


def evaluate_collector(conn: sqlite3.Connection) -> bool:
    """自动采集开着，但采集轮很久没完成——调度线程可能死了（P0-B）。"""
    if not config.AUTO_COLLECT:
        clear(conn, STALL_KEY)
        return False

    last = get_meta(conn, "last_collect_at")
    if not last:
        # 从未采集过：不告警（数据新鲜度已经在提示「先去采一次」）
        clear(conn, STALL_KEY)
        return False

    threshold_min = max(STALL_MIN_MINUTES, config.WATCH_INTERVAL_SECONDS * 3 / 60)
    try:
        moment = dt.datetime.fromisoformat(str(last))
    except (TypeError, ValueError):
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now_local().tzinfo)
    age_min = (now_local() - moment).total_seconds() / 60

    if age_min >= threshold_min:
        return fire(
            conn,
            STALL_KEY,
            "warn",
            "collector",
            f"自动采集已开启，但已经 {age_min:.0f} 分钟没有完成过采集轮"
            f"（阈值 {threshold_min:.0f} 分钟）。调度可能已停止，"
            "这期间的调用不会被记录——请重启服务或点「采集」补采",
        )
    clear(conn, STALL_KEY)
    return False


# ------------------------------------------------------------------ 定时任务
def evaluate_scheduled(conn: sqlite3.Connection) -> dict[str, int]:
    """每小时跑一次：预算 + 烧钱速率 + 预测耗尽 + 未定价 + 采集健康度。"""
    result = {
        "budget": evaluate_budget(conn)["fired"],
        "burn_rate": int(evaluate_burn_rate(conn)),
        "unpriced": int(evaluate_unpriced(conn)),
        "parser": int(evaluate_parser(conn)),
        "collector": int(evaluate_collector(conn)),
    }
    # 兜底：任何一条「还挂着但已经不成立」的告警，在这里统一收口
    result["stale_resolved"] = sweep_stale(conn)
    return result


# ------------------------------------------------------------------ 读取
def recent(
    conn: sqlite3.Connection, limit: int = 50, unacked_only: bool = False
) -> list[dict[str, Any]]:
    # 「待处理」= 既没被用户确认，也没被系统判定为已恢复（A-1）
    clause = "WHERE acknowledged = 0 AND resolved_at IS NULL" if unacked_only else ""
    rows = conn.execute(
        f"SELECT * FROM alert_event {clause} ORDER BY fired_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_event_to_item(r) for r in rows]


def _event_to_item(r: sqlite3.Row) -> dict[str, Any]:
    acked = bool(r["acknowledged"])
    resolved = r["resolved_at"] if "resolved_at" in r.keys() else None
    if acked:
        state = "acknowledged"
    elif resolved:
        state = "resolved"
    else:
        state = "open"
    return {
        "id": r["id"],
        "key": r["alert_key"],
        "level": r["level"],
        "category": r["category"],
        "message": r["message"],
        "value": None if r["value_micro"] is None else money_str(dec(r["value_micro"]) / 1000000),
        "threshold": None if r["threshold_micro"] is None else money_str(dec(r["threshold_micro"]) / 1000000),
        "fired_at": r["fired_at"],
        "acknowledged": acked,
        "resolved_at": resolved,
        # 前端据此分三态展示：未处理 / 已确认 / 已自动解除（A-1）
        "state": state,
    }


def acknowledge(conn: sqlite3.Connection, alert_id: int) -> bool:
    cur = conn.execute(
        "UPDATE alert_event SET acknowledged = 1 WHERE id = ?", (alert_id,)
    )
    return bool(cur.rowcount)
