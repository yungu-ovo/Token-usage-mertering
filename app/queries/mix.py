"""构成区：按模型 / 项目 / 工具拆解成本。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..money import MICRO as MICRO_D
from ..money import apportion, dec, money_str
from .common import COST_EXPR, day_range, envelope

_ALLOWED_DIMS = {"model": "model", "project": "project", "tool": "tool", "session": "session_id"}


def by_dimension(
    conn: sqlite3.Connection, dim: str = "model", days: int = 30, top: int = 8
) -> dict[str, Any]:
    column = _ALLOWED_DIMS.get(dim)
    if column is None:
        raise ValueError(f"不支持的维度: {dim}")

    lo, hi = day_range(days)
    rows = conn.execute(
        f"SELECT {column} AS name, SUM({COST_EXPR}) AS cost_micro, "
        "  COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens),0) AS tokens, "
        "  COUNT(*) AS req "
        "FROM usage_fact WHERE ts_day BETWEEN ? AND ? "
        f"GROUP BY {column} ORDER BY cost_micro DESC, tokens DESC",
        (lo, hi),
    ).fetchall()

    # 注意：SUM 出来的是**微美元**，必须先换成美元再参与 share 计算，
    # 否则 share 会算成 1e-7 量级（曾经真的踩过这个坑）。
    exact_total = sum(dec(r["cost_micro"]) for r in rows) / MICRO_D
    total_tokens = sum(int(r["tokens"]) for r in rows)
    total_req = sum(int(r["req"]) for r in rows)

    # 先算出「要展示的项」的精确成本（含长尾合并），再统一量化，
    # 这样各项之和恰好等于总额——不是 61.29 vs 61.30。
    shown = list(rows[:top])
    tail = list(rows[top:])
    tail_cost = sum(dec(r["cost_micro"]) for r in tail) / MICRO_D if tail else dec(0)

    exact_costs = [dec(r["cost_micro"]) / MICRO_D for r in shown]
    if tail:
        exact_costs.append(tail_cost)

    display = apportion(exact_total, exact_costs)

    items: list[dict[str, Any]] = []
    for i, r in enumerate(shown):
        items.append(
            {
                "name": r["name"] or "(未标注)",
                "cost": money_str(display[i]),
                "share": float(exact_costs[i] / exact_total) if exact_total else 0.0,
                "tokens": int(r["tokens"]),
                "requests": int(r["req"]),
            }
        )

    # 长尾合并，避免图表被噪声占满
    if tail:
        items.append(
            {
                "name": f"其他 {len(tail)} 项",
                "cost": money_str(display[-1]),
                "share": float(tail_cost / exact_total) if exact_total else 0.0,
                "tokens": sum(int(r["tokens"]) for r in tail),
                "requests": sum(int(r["req"]) for r in tail),
                "is_remainder": True,
            }
        )

    return envelope(
        conn,
        dim=dim,
        days=days,
        from_day=lo,
        to_day=hi,
        total_cost=money_str(display and sum(display) or dec(0)),
        total_tokens=total_tokens,
        total_requests=total_req,
        items=items,
        empty_note=("所选区间内没有采集到数据" if not rows else None),
    )
