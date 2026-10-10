"""明细区：分页 + 过滤 + 会话下钻。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..money import dec, money_str
from .common import COST_EXPR, VENDOR_COST_EXPR, envelope

_SELECT_COLS = (
    "request_id, ts, ts_day, provider, model, model_raw, tool, project, session_id, "
    "billing_mode, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "vendor_credit_micro, cost_usd_micro, equivalent_cost_usd_micro, cost_source, status"
)


def _row_to_item(r: sqlite3.Row) -> dict[str, Any]:
    headline = r["equivalent_cost_usd_micro"]
    if headline is None:
        headline = r["cost_usd_micro"]
    return {
        "request_id": r["request_id"],
        "ts": r["ts"],
        "day": r["ts_day"],
        "provider": r["provider"],
        "model": r["model"],
        "model_raw": r["model_raw"],
        "tool": r["tool"],
        "project": r["project"],
        "session_id": r["session_id"],
        "billing_mode": r["billing_mode"],
        "input_tokens": int(r["input_tokens"]),
        "output_tokens": int(r["output_tokens"]),
        "cache_read_tokens": int(r["cache_read_tokens"]),
        "cache_write_tokens": int(r["cache_write_tokens"]),
        "cost": money_str(dec(headline) / 1000000, 6),
        "vendor_credit": (
            None if r["vendor_credit_micro"] is None
            else money_str(dec(r["vendor_credit_micro"]) / 1000000, 6)
        ),
        "cost_source": r["cost_source"],
        "status": r["status"],
        "priced": r["model"] != "__unknown__",
    }


def requests(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    model: str | None = None,
    project: str | None = None,
    tool: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []

    if model:
        where.append("model = ?")
        params.append(model)
    if project:
        where.append("project = ?")
        params.append(project)
    if tool:
        where.append("tool = ?")
        params.append(tool)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if status:
        where.append("status = ?")
        params.append(status)
    if days:
        import datetime as dt

        lo = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
        where.append("ts_day >= ?")
        params.append(lo)

    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM usage_fact {clause}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM usage_fact {clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    agg = conn.execute(
        f"SELECT SUM({COST_EXPR}) AS c1, SUM({VENDOR_COST_EXPR}) AS c2, "
        "COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0) AS tok "
        f"FROM usage_fact {clause}",
        params,
    ).fetchone()

    return envelope(
        conn,
        total=int(total),
        limit=limit,
        offset=offset,
        has_more=(offset + len(rows)) < int(total),
        sum_cost=money_str(dec(agg["c1"]) / 1000000),
        sum_vendor_cost=money_str(dec(agg["c2"]) / 1000000),
        sum_tokens=int(agg["tok"] or 0),
        items=[_row_to_item(r) for r in rows],
    )


def sessions_for_model(
    conn: sqlite3.Connection, model: str, days: int = 30, top: int = 20
) -> dict[str, Any]:
    """P1 归因下钻：某模型 -> 会话列表。"""
    import datetime as dt

    lo = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        f"SELECT session_id, project, tool, SUM({COST_EXPR}) AS cost_micro, "
        "  COUNT(*) AS req, "
        "  COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens),0) AS tok, "
        "  MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM usage_fact WHERE model = ? AND ts_day >= ? "
        "GROUP BY session_id ORDER BY cost_micro DESC LIMIT ?",
        (model, lo, top),
    ).fetchall()
    return envelope(
        conn,
        model=model,
        items=[
            {
                "session_id": r["session_id"],
                "project": r["project"],
                "tool": r["tool"],
                "cost": money_str(dec(r["cost_micro"]) / 1000000),
                "requests": int(r["req"]),
                "tokens": int(r["tok"]),
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
            }
            for r in rows
        ],
    )
