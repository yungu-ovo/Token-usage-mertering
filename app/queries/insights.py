"""洞察区：只输出数据真正支持得起的结论。

设计原则（对应看板方法论「看完知道下一步该干什么」）：
每条洞察都必须带一个可执行的 ``action``；没有数据支撑的类别直接不出，
不用占位文案凑数。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal
from typing import Any

from .. import config
from ..money import dec, money_str
from .common import COST_EXPR, day_range, envelope


def build(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    lo, hi = day_range(days)
    insights: list[dict[str, Any]] = []

    total_row = conn.execute(
        f"SELECT SUM({COST_EXPR}) AS c, COUNT(*) AS req FROM usage_fact "
        "WHERE ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    total_cost = dec(total_row["c"]) / 1000000
    total_req = int(total_row["req"] or 0)

    # ---------------------------------------------------------- 1. 未定价记录
    unpriced = conn.execute(
        "SELECT COUNT(*) AS c, COUNT(DISTINCT model_raw) AS kinds FROM usage_fact "
        "WHERE model = '__unknown__' AND ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    unpriced_count = int(unpriced["c"] or 0)
    if unpriced_count:
        kinds = conn.execute(
            "SELECT model_raw, COUNT(*) AS c FROM usage_fact WHERE model = '__unknown__' "
            "AND ts_day BETWEEN ? AND ? GROUP BY model_raw ORDER BY c DESC LIMIT 5",
            (lo, hi),
        ).fetchall()
        names = "、".join(f"{r['model_raw'] or '(空)'}（{r['c']} 次）" for r in kinds)
        insights.append(
            {
                "id": "unpriced_models",
                "severity": "danger",
                "title": f"未定价模型 · {unpriced_count} 条记录成本未计入",
                "detail": (
                    f"涉及 {int(unpriced['kinds'])} 种模型名，占本区间请求的 "
                    f"{(unpriced_count / total_req * 100 if total_req else 0):.1f}%。"
                    f"成本被记为 0，而不是猜一个数——这是 RK-03 要防止的静默错误。"
                    f" 主要来源：{names}"
                ),
                "action": {"label": "去补价格表", "kind": "open_pricing"},
            }
        )

    # ---------------------------------------------------------- 2. 缓存节省
    cache_row = conn.execute(
        "SELECT COALESCE(SUM(cache_read_tokens),0) AS cr, COALESCE(SUM(input_tokens),0) AS i "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    cr = int(cache_row["cr"])
    fresh = int(cache_row["i"])
    if cr + fresh > 0:
        hit = Decimal(cr) / Decimal(cr + fresh)
        from .common import price_lookup
        from ..caliber import cache_saved_usd

        model_rows = conn.execute(
            "SELECT model, COALESCE(SUM(cache_read_tokens),0) AS cr FROM usage_fact "
            "WHERE ts_day BETWEEN ? AND ? GROUP BY model",
            (lo, hi),
        ).fetchall()
        saved = cache_saved_usd(
            [(r["model"], int(r["cr"])) for r in model_rows], price_lookup(conn, hi)
        )
        if cr == 0:
            # 这里必须和「有缓存读但模型没定价」区分开：成因不同，用户该做的事也不同。
            # 输入侧有大量 token 却一条缓存读都没有，通常是数据源本身不上报该字段。
            insights.append(
                {
                    "id": "cache_absent",
                    "severity": "neutral",
                    "title": "本区间未上报任何缓存读 token",
                    "detail": (
                        f"输入侧共 {fresh:,} token，但缓存读为 0。"
                        f"这更可能是「数据源不上报缓存字段」，而不是真的完全没命中——"
                        f"两者对成本分析的含义完全不同，所以这里不给出命中率数字。"
                        f"待数据源补充缓存字段后，命中率与缓存节省会自动出现。"
                    ),
                    "action": {"label": "查看 Token 分层", "kind": "filter_cache"},
                }
            )
        elif dec(saved.value) > 0:
            insights.append(
                {
                    "id": "cache_savings",
                    "severity": "ok",
                    "title": f"缓存命中率 {hit * 100:.1f}% · 已省 ${money_str(saved.value)}",
                    "detail": (
                        f"缓存读 {cr:,} token，占输入侧 {hit * 100:.1f}%。"
                        f"若这些全部按全新输入计价，需多付 ${money_str(saved.value)}。"
                        f"口径：{saved.method}"
                    ),
                    "action": {"label": "查看缓存明细", "kind": "filter_cache"},
                }
            )
        else:
            insights.append(
                {
                    "id": "cache_savings",
                    "severity": "neutral",
                    "title": f"缓存命中率 {hit * 100:.1f}%（暂时算不出节省金额）",
                    "detail": (
                        f"缓存读 {cr:,} token，但涉及模型均未定价，"
                        f"无法计算「输入单价 − 缓存读单价」的差额。补上价格后此项会自动出现。"
                    ),
                    "action": {"label": "去补价格表", "kind": "open_pricing"},
                }
            )

    # ---------------------------------------------------------- 3. 失败请求成本
    fail_row = conn.execute(
        f"SELECT SUM({COST_EXPR}) AS c, COUNT(*) AS n FROM usage_fact "
        "WHERE status <> 'success' AND ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    fail_cost = dec(fail_row["c"]) / 1000000
    fail_n = int(fail_row["n"] or 0)
    if fail_n:
        insights.append(
            {
                "id": "failed_cost",
                "severity": "warn",
                "title": f"失败请求成本 · ${money_str(fail_cost)}（{fail_n} 次）",
                "detail": (
                    f"占本区间成本的 "
                    f"{(fail_cost / total_cost * 100) if total_cost else 0:.1f}%。"
                    f"这部分钱花掉了但没有产出，建议对长上下文请求加超时上限与指数退避。"
                ),
                "action": {"label": "查看失败请求", "kind": "filter_status:error"},
            }
        )

    # ---------------------------------------------------------- 4. 成本大头
    top_model = conn.execute(
        f"SELECT model, SUM({COST_EXPR}) AS c FROM usage_fact "
        "WHERE ts_day BETWEEN ? AND ? GROUP BY model ORDER BY c DESC LIMIT 1",
        (lo, hi),
    ).fetchone()
    top_project = conn.execute(
        f"SELECT project, SUM({COST_EXPR}) AS c FROM usage_fact "
        "WHERE ts_day BETWEEN ? AND ? GROUP BY project ORDER BY c DESC LIMIT 1",
        (lo, hi),
    ).fetchone()
    if top_model and dec(top_model["c"]) > 0:
        row = conn.execute(
            "SELECT COALESCE(AVG(output_tokens),0) AS avg_out, COUNT(*) AS n FROM usage_fact "
            "WHERE model = ? AND ts_day BETWEEN ? AND ?",
            (top_model["model"], lo, hi),
        ).fetchone()
        insights.append(
            {
                "id": "top_driver",
                "severity": "neutral",
                "title": (
                    f"成本大头 · {top_model['model']} ${money_str(dec(top_model['c']) / 1000000)}"
                    + (f" ／ {top_project['project']}" if top_project else "")
                ),
                "detail": (
                    f"该模型期均输出 {int(row['avg_out'])} token、共 {int(row['n'])} 次调用。"
                    f"如果其中存在「大模型干小活」的长尾，降级到更小的模型是见效最快的省钱手段。"
                ),
                "action": {"label": "按模型下钻", "kind": "drill_model:" + str(top_model["model"])},
            }
        )

    # ---------------------------------------------------------- 5. 烧钱速率
    burn = _burn_rate(conn)
    if burn["triggered"]:
        insights.append(
            {
                "id": "burn_rate",
                "severity": "danger",
                "title": f"烧钱速率异常 · 近 {config.BURN_RATE_WINDOW_HOURS} 小时成本是基线的 {burn['ratio']:.1f}×",
                "detail": (
                    f"当前 {config.BURN_RATE_WINDOW_HOURS} 小时花费 ${burn['current']}，"
                    f"而过去 {config.BURN_RATE_BASELINE_DAYS} 天同时段均值仅 ${burn['baseline']}。"
                    f"常见原因是 agent 陷入推理循环，建议核对对应会话。"
                ),
                "action": {"label": "查看该时段会话", "kind": "filter_recent"},
            }
        )

    # ---------------------------------------------------------- 6. 数据覆盖度
    data_days = conn.execute(
        "SELECT COUNT(DISTINCT ts_day) AS d FROM usage_fact WHERE ts_day BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()["d"]
    span = (dt.date.fromisoformat(hi) - dt.date.fromisoformat(lo)).days + 1
    if span and int(data_days or 0) < span:
        insights.append(
            {
                "id": "sparse_data",
                "severity": "neutral",
                "title": f"数据覆盖 {data_days}/{span} 天，趋势图有断点",
                "detail": (
                    "没有用量的日子成本按 0 计入，折线上的缺口是真实情况而非采集故障。"
                    "WorkBuddy 只保留本机产生的会话转录，历史范围取决于你开始使用的时点。"
                ),
                "action": {"label": "重新采集", "kind": "reingest"},
            }
        )

    return envelope(
        conn,
        days=days,
        from_day=lo,
        to_day=hi,
        total_cost=money_str(total_cost),
        total_requests=total_req,
        insights=insights,
        empty_note=("所选区间内没有采集到数据，无法生成洞察" if not insights else None),
    )


def _burn_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    """当前窗口成本 vs 过去 N 天同时段均值。"""
    window = config.BURN_RATE_WINDOW_HOURS
    now = dt.datetime.now()
    start = now - dt.timedelta(hours=window)

    cur = conn.execute(
        f"SELECT SUM({COST_EXPR}) AS c FROM usage_fact WHERE ts >= ?",
        (start.isoformat(),),
    ).fetchone()
    current = dec(cur["c"]) / 1000000

    baseline_total = dec(0)
    samples = 0
    for offset in range(1, config.BURN_RATE_BASELINE_DAYS + 1):
        day = (now - dt.timedelta(days=offset)).date()
        lo = dt.datetime.combine(day, start.time()).isoformat()
        hi = dt.datetime.combine(day, now.time()).isoformat()
        row = conn.execute(
            f"SELECT SUM({COST_EXPR}) AS c FROM usage_fact WHERE ts >= ? AND ts <= ?",
            (lo, hi),
        ).fetchone()
        value = dec(row["c"]) / 1000000
        if value > 0:
            samples += 1
        baseline_total += value

    baseline = baseline_total / config.BURN_RATE_BASELINE_DAYS
    triggered = (
        baseline > 0
        and samples >= 1
        and current > baseline * config.BURN_RATE_MULTIPLIER
    )
    ratio = float(current / baseline) if baseline > 0 else 0.0
    return {
        "triggered": triggered,
        "current": money_str(current),
        "baseline": money_str(baseline),
        "ratio": ratio,
        "samples": samples,
    }
