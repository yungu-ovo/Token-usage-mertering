"""查询层公用片段。"""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal
from typing import Any

from .. import config
from ..money import MICRO as MICRO_D
from ..money import dec, money_str
from ..pricing import resolve

# 看板主口径：优先 API 等价成本，没有则回落到厂商口径成本。
# 「未校准前北极星默认展示 API 等价口径」——见计划 §四。
COST_EXPR = "COALESCE(equivalent_cost_usd_micro, cost_usd_micro)"

# 厂商口径成本（积分折算或厂商直接给的美元）
VENDOR_COST_EXPR = "CASE WHEN cost_source = 'provider' THEN cost_usd_micro ELSE 0 END"

TOKEN_COLS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def day_range(days: int, end: dt.date | None = None) -> tuple[str, str]:
    """返回 [start, end] 的 ISO 日期，含首含尾。"""
    last = end or dt.date.today()
    first = last - dt.timedelta(days=max(1, days) - 1)
    return first.isoformat(), last.isoformat()


def month_range(today: dt.date | None = None) -> tuple[str, str]:
    d = today or dt.date.today()
    return d.replace(day=1).isoformat(), d.isoformat()


def fetch_cost(conn: sqlite3.Connection, lo: str, hi: str) -> tuple[Decimal, Decimal]:
    """返回 (主口径成本, 厂商口径成本)，单位 **美元**（已从微美元换算）。"""
    row = conn.execute(
        f"SELECT SUM({COST_EXPR}) AS c1, SUM({VENDOR_COST_EXPR}) AS c2 "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    return dec(row["c1"]) / MICRO_D, dec(row["c2"]) / MICRO_D


def daily_costs(conn: sqlite3.Connection, lo: str, hi: str) -> list[dict[str, Any]]:
    """逐日成本（美元）。没有数据的日子补 0，保证前端折线连续。"""
    rows = conn.execute(
        f"SELECT ts_day, SUM({COST_EXPR}) AS cost_micro, COUNT(*) AS req "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ? GROUP BY ts_day ORDER BY ts_day",
        (lo, hi),
    ).fetchall()
    found = {
        r["ts_day"]: {"cost": dec(r["cost_micro"]) / MICRO_D, "requests": int(r["req"])}
        for r in rows
    }

    out: list[dict[str, Any]] = []
    cursor = dt.date.fromisoformat(lo)
    last = dt.date.fromisoformat(hi)
    while cursor <= last:
        key = cursor.isoformat()
        item = found.get(key)
        out.append(
            {
                "day": key,
                "cost": item["cost"] if item else dec(0),
                "requests": item["requests"] if item else 0,
                "has_data": item is not None,
            }
        )
        cursor += dt.timedelta(days=1)
    return out


def envelope(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    """响应统一信封。"""
    from ..db import get_credit_rate, get_meta
    from ..pricing import current_version

    rate, calibrated = get_credit_rate(conn)
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "pricing_version": current_version(conn),
        "data_source": get_meta(conn, "data_source", config.DATA_SOURCE_DEMO),
        "credit_rate": {"rate": rate, "calibrated": calibrated},
        **extra,
    }


def money(value: Any, places: int = 2) -> str:
    return money_str(value, places)


def price_lookup(conn: sqlite3.Connection, on_day: str):
    """返回一个 model -> Price|None 的查价函数，供口径函数使用。"""
    cache: dict[str, Any] = {}

    def lookup(model: str):
        if model not in cache:
            cache[model] = resolve(conn, model, on_day)
        return cache[model]

    return lookup
