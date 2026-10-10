"""趋势区与 Token 分层区。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..money import apportion, dec, money_str
from .common import COST_EXPR, daily_costs, day_range, envelope, month_range


def trend(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """逐日成本折线。无数据的日子补 0，并带 has_data 标记。"""
    today_lo, today_hi = month_range()
    lo, hi = day_range(days)
    series = daily_costs(conn, lo, hi)

    # 基准线：日预算
    row = conn.execute("SELECT daily_usd_micro FROM budget WHERE id = 1").fetchone()
    daily_budget = dec(row["daily_usd_micro"]) / 1000000 if row else dec(0)

    # 7 日移动均值
    values = [float(item["cost"]) for item in series]
    moving: list[float | None] = []
    for i in range(len(values)):
        window = values[max(0, i - 6): i + 1]
        moving.append(sum(window) / len(window) if window else None)

    data_days = sum(1 for item in series if item["has_data"])

    # 逐日金额也做一次配平：折线各点之和恰好等于总额，避免「按天加总对不上月度数字」
    exact_total = sum(dec(item["cost"]) for item in series)
    display = apportion(exact_total, [dec(item["cost"]) for item in series])

    return envelope(
        conn,
        days=days,
        from_day=lo,
        to_day=hi,
        total_cost=money_str(sum(display) if display else dec(0)),
        daily_budget=money_str(daily_budget),
        data_days=data_days,
        points=[
            {
                "day": item["day"],
                "cost": money_str(display[i]),
                "requests": item["requests"],
                "has_data": item["has_data"],
                "moving_avg": None if moving[i] is None else f"{moving[i]:.2f}",
                "over_budget": bool(daily_budget and dec(item["cost"]) > daily_budget),
            }
            for i, item in enumerate(series)
        ],
        empty_note=(
            "所选区间内没有采集到数据"
            if data_days == 0
            else None
        ),
    )


def tokens_daily(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """逐日四层 token 堆叠。"""
    lo, hi = day_range(days)
    rows = conn.execute(
        "SELECT ts_day, "
        "COALESCE(SUM(input_tokens),0) AS i, "
        "COALESCE(SUM(output_tokens),0) AS o, "
        "COALESCE(SUM(cache_read_tokens),0) AS cr, "
        "COALESCE(SUM(cache_write_tokens),0) AS cw, "
        "COUNT(*) AS req "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ? GROUP BY ts_day ORDER BY ts_day",
        (lo, hi),
    ).fetchall()
    found = {r["ts_day"]: r for r in rows}

    import datetime as dt

    points: list[dict[str, Any]] = []
    cursor = dt.date.fromisoformat(lo)
    last = dt.date.fromisoformat(hi)
    while cursor <= last:
        key = cursor.isoformat()
        r = found.get(key)
        layer = {
            "input": int(r["i"]) if r else 0,
            "output": int(r["o"]) if r else 0,
            "cache_read": int(r["cr"]) if r else 0,
            "cache_write": int(r["cw"]) if r else 0,
        }
        points.append(
            {
                "day": key,
                "input": layer["input"],
                "output": layer["output"],
                "cache_read": layer["cache_read"],
                "cache_write": layer["cache_write"],
                "total": sum(layer.values()),
                "requests": int(r["req"]) if r else 0,
                "has_data": r is not None,
            }
        )
        cursor += dt.timedelta(days=1)

    grand = {k: sum(p[k] for p in points) for k in ("input", "output", "cache_read", "cache_write")}
    grand["total"] = sum(grand.values())

    return envelope(
        conn,
        days=days,
        from_day=lo,
        to_day=hi,
        points=points,
        totals=grand,
        empty_note=("所选区间内没有采集到数据" if grand["total"] == 0 else None),
    )
