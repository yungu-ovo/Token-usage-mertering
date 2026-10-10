"""KPI 区：北极星 + 4 张一级指标。"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from decimal import Decimal
from typing import Any

from .. import config
from ..caliber import (
    cache_hit_rate,
    cache_saved_usd,
    days_until_budget_exhausted,
    month_bounds,
    month_end_forecast,
    per_request_cost,
)
from ..db import get_budget, get_credit_rate
from ..money import dec, money_str
from .common import COST_EXPR, day_range, envelope, fetch_cost, month_range, price_lookup


def build(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    today = dt.date.today()
    m_lo, m_hi = month_range(today)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed = today.day

    mtd_cost, vendor_cost = fetch_cost(conn, m_lo, m_hi)
    budget_daily_micro, budget_monthly_micro, currency = get_budget(conn)
    budget_monthly = dec(budget_monthly_micro) / 1000000
    _rate, calibrated = get_credit_rate(conn)

    # ---- 北极星 ----
    forecast = month_end_forecast(mtd_cost, days_elapsed, days_in_month)
    pct = (mtd_cost / budget_monthly) if budget_monthly else Decimal(0)
    time_pct = Decimal(days_elapsed) / Decimal(days_in_month)
    runway = days_until_budget_exhausted(mtd_cost, budget_monthly, days_elapsed, days_in_month)

    north_star = {
        "mtd_cost": money_str(mtd_cost),
        "cost_caliber": "api_equivalent",
        "cost_caliber_label": "API 等价成本",
        "vendor_cost": money_str(vendor_cost),
        "vendor_cost_calibrated": calibrated,
        "vendor_cost_label": "厂商口径（积分折算）",
        "budget": money_str(budget_monthly),
        "currency": currency,
        "pct": float(pct),
        "pct_label": f"{(pct * 100):.1f}%",
        "time_pct": float(time_pct),
        "time_pct_label": f"{(time_pct * 100):.0f}%",
        "forecast_cost": money_str(forecast.value),
        "forecast_over_budget": money_str(
            forecast.value - budget_monthly if forecast.value > budget_monthly else Decimal(0)
        ),
        "estimated": forecast.is_estimated,
        "estimate_method": forecast.method,
        "sample_days": days_elapsed,
        "days_in_month": days_in_month,
        "runway_days": int(runway.value),
        "runway_method": runway.method,
    }

    # ---- 今日 vs 近 N 日均值 ----
    today_cost, _ = fetch_cost(conn, m_hi, m_hi)
    avg_lo, avg_hi = day_range(days, end=today)
    window_cost, _ = fetch_cost(conn, avg_lo, avg_hi)
    avg_cost = window_cost / Decimal(days) if days else Decimal(0)
    delta = ((today_cost - avg_cost) / avg_cost) if avg_cost else Decimal(0)

    today_block = {
        "cost": money_str(today_cost),
        "avg_days": days,
        "avg_cost": money_str(avg_cost),
        "delta_pct": float(delta),
        "delta_pct_label": f"{'+' if delta >= 0 else ''}{(delta * 100):.1f}%",
    }

    # ---- Token 四层（本月） ----
    tok = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
        "COALESCE(SUM(cache_read_tokens),0) AS cr, COALESCE(SUM(cache_write_tokens),0) AS cw "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ?",
        (m_lo, m_hi),
    ).fetchone()
    layers = {
        "input": int(tok["i"]),
        "output": int(tok["o"]),
        "cache_read": int(tok["cr"]),
        "cache_write": int(tok["cw"]),
    }
    layers["total"] = sum(layers.values())

    # ---- 缓存 ----
    hit = cache_hit_rate(layers["cache_read"], layers["input"])
    rows = conn.execute(
        "SELECT model, COALESCE(SUM(cache_read_tokens),0) AS cr FROM usage_fact "
        "WHERE ts_day BETWEEN ? AND ? GROUP BY model",
        (m_lo, m_hi),
    ).fetchall()
    saved = cache_saved_usd(
        [(r["model"], int(r["cr"])) for r in rows],
        price_lookup(conn, m_hi),
    )
    cache_block = {
        "hit_rate": float(hit),
        "hit_rate_label": f"{(hit * 100):.1f}%",
        "cache_read_tokens": layers["cache_read"],
        "saved_usd": money_str(saved.value),
        "estimated": saved.is_estimated,
        "estimate_method": saved.method,
    }

    # ---- 单请求均成本 ----
    req = conn.execute(
        "SELECT COUNT(*) AS c FROM usage_fact WHERE ts_day BETWEEN ? AND ?", (m_lo, m_hi)
    ).fetchone()
    requests = int(req["c"])
    per_req = per_request_cost(mtd_cost, requests)

    # ---- 数据健康度：未识别模型（RK-03） ----
    unknown = conn.execute(
        "SELECT COUNT(*) AS c FROM usage_fact WHERE model = '__unknown__' "
        "AND ts_day BETWEEN ? AND ?",
        (m_lo, m_hi),
    ).fetchone()
    unknown_count = int(unknown["c"])

    return envelope(
        conn,
        north_star=north_star,
        today=today_block,
        tokens=layers,
        cache=cache_block,
        per_request={
            "cost": money_str(per_req, 6),
            "requests": requests,
            "estimated": False,
            "estimate_method": "本月总成本 ÷ 本月请求数（含失败请求）",
        },
        health={
            "unpriced_records": unknown_count,
            "unpriced_ratio": float(
                Decimal(unknown_count) / Decimal(requests) if requests else 0
            ),
            "note": (
                "这些记录的模型不在价格表内，成本被记为 0 并已告警，不会被静默计入"
                if unknown_count
                else "所有记录均已定价"
            ),
        },
    )
