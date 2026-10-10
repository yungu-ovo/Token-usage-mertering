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


def _placeholder_notes(conn: sqlite3.Connection) -> list[str]:
    """当前成本是否建立在占位价格上——是的话必须显式告诉用户（B-2）。

    WorkBuddy 的模型名是内部代号，公共价格表匹配不上，成本是用
    ``--apply-placeholder-prices`` 灌进来的**占位价格**算的。数字能验证
    链路通，但**不代表真实账单**。这件事不能只写在 README 里。

    判定依据是**价格版本串里含 placeholder**（占位价格专用版本号），
    而不是 ``source``——占位价格与用户手工覆盖在库里的 source 都是
    ``user``，只有版本串能区分这两者。用错信号会给出误导性的提示。
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT pricing_version) AS c FROM price_table "
        "WHERE pricing_version LIKE '%placeholder%'"
    ).fetchone()
    if not row or int(row["c"] or 0) == 0:
        return []
    return [
        "当前部分模型用的是<b>占位价格</b>（非厂商官方报价），"
        "成本数字仅用于验证链路<b>不代表真实账单</b>"
    ]


def _cache_provenance(
    conn: sqlite3.Connection, lo: str, hi: str, layers: dict[str, int]
) -> dict[str, Any]:
    """说清楚缓存数字**从哪来、覆盖了多少**（B-1）。

    这里有个容易误导人的坑：jsonl 不含缓存字段，缓存读是 traces 分摊来的，
    而 traces 只覆盖部分会话。如果直接用「全库缓存读 ÷ 全库输入」，
    未覆盖的请求会进分母却不进分子，命中率被**严重低估**
    （实测会显示 43%，而真实会话级缓存率约 95%）。

    所以这里给两个口径：
    * ``hit_rate``：只按**有 trace 支撑**的请求算，反映真实缓存表现；
    * ``coverage``：全库有多少比例的请求真的有 trace 支撑，必须跟着一起展示。
    """
    from ..sources.trace_cache import coverage_note

    prov = coverage_note(conn)
    cov = prov["coverage"]

    if layers["cache_read"] <= 0:
        return {
            "cache_source": "none",
            "cache_source_label": "未上报",
            "cache_coverage": float(cov),
            "cache_note": "该数据源不提供缓存 token 字段，故不给命中率数字",
        }

    # 有缓存支撑的样本内命中率（排除未覆盖请求对分母的污染）
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(cache_read_tokens),0) AS c "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ? AND cache_read_tokens > 0",
        (lo, hi),
    ).fetchone()
    sample_i = int(row["i"] or 0)
    sample_c = int(row["c"] or 0)
    sample_rate = (sample_c / sample_i) if sample_i else 0.0

    partial = cov < Decimal("1")
    return {
        "cache_source": "trace_estimate",
        "cache_source_label": "由 traces 估算",
        "cache_coverage": float(cov),
        "cache_coverage_requests": prov["covered_requests"],
        "cache_coverage_total": prov["total_requests"],
        "sample_hit_rate": sample_rate,
        "sample_hit_rate_label": f"{sample_rate * 100:.1f}%",
        "cache_estimated": True,
        "cache_note": (
            f"缓存读由会话级traces 按缓存率分摊估算；有 trace 支撑的请求占 "
            f"{float(cov) * 100:.0f}%（{prov['covered_requests']}/{prov['total_requests']}）。"
            + (
                "命中率按**有 trace 支撑的样本**计算——若用全库做分母，"
                "未覆盖请求会把数字压低到失真。"
                if partial
                else ""
            )
        ),
    }


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
    # 命中率分母只取「有 trace 支撑」的请求。缓存读是分摊来的，
    # 若拿全库输入当分母，未覆盖的请求会把命中率压到失真（实测 95% → 43%）。
    prov = _cache_provenance(conn, m_lo, m_hi, layers)
    sample_row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(cache_read_tokens),0) AS c "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ? AND cache_read_tokens > 0",
        (m_lo, m_hi),
    ).fetchone()
    if int(sample_row["c"] or 0) > 0 and int(sample_row["i"] or 0) > 0:
        hit = Decimal(int(sample_row["c"])) / Decimal(int(sample_row["i"]))
    else:
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
        **_cache_provenance(conn, m_lo, m_hi, layers),
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
        # 预算明细提到顶层：前端的预算设置弹窗要能直接回填日/月两个值，
        # 放进 north_star 里会让前端多绕一层，接口契约也变得不直观。
        budget_detail={
            "daily_usd": money_str(dec(budget_daily_micro) / 1000000),
            "monthly_usd": money_str(budget_monthly),
            "currency": currency,
        },
        placeholder_prices=_placeholder_notes(conn),
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
