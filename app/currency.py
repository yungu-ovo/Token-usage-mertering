"""汇率：静态表 + 手填覆盖。**不联网**（local-first，离线时也要能显示）。

每个汇率都带 rate_date，切币种时前端必须把日期一起显示出来。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any

from . import config
from .money import dec, money_str


def list_rates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT rate_date, base, quote, rate FROM fx_rate ORDER BY rate_date DESC"
    ).fetchall()
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        quote = r["quote"]
        if quote in seen:
            continue
        seen[quote] = {
            "base": r["base"],
            "quote": quote,
            "rate": r["rate"],
            "rate_date": r["rate_date"],
        }
    return list(seen.values())


def get_rate(conn: sqlite3.Connection, quote: str) -> tuple[Decimal, str] | None:
    if quote.upper() == "USD":
        return Decimal(1), "—"
    row = conn.execute(
        "SELECT rate, rate_date FROM fx_rate WHERE quote = ? ORDER BY rate_date DESC LIMIT 1",
        (quote.upper(),),
    ).fetchone()
    if row is None:
        return None
    return dec(row["rate"]), row["rate_date"]


def set_rate(
    conn: sqlite3.Connection, quote: str, rate: Any, rate_date: str | None = None
) -> None:
    import datetime as dt

    day = rate_date or dt.date.today().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO fx_rate (rate_date, base, quote, rate) VALUES (?, 'USD', ?, ?)",
        (day, quote.upper(), str(dec(rate))),
    )


def convert(amount_usd: Any, rate: Decimal, places: int = 2) -> str:
    return money_str(dec(amount_usd) * rate, places)


def default_currency() -> str:
    return "USD"


def supported(conn: sqlite3.Connection) -> list[str]:
    return ["USD"] + sorted({r["quote"] for r in list_rates(conn)})


def fx_block(conn: sqlite3.Connection, quote: str | None = None) -> dict[str, Any]:
    """响应信封里的汇率块。"""
    target = (quote or default_currency()).upper()
    found = get_rate(conn, target)
    if found is None:
        fallback = config.DEFAULT_FX_RATES.get(target)
        if fallback:
            return {"rate": fallback[0], "rate_date": fallback[1], "quote": target,
                    "note": "内置静态汇率，非实时"}
        return {"rate": "1", "rate_date": "—", "quote": "USD", "note": "无该币种汇率，按 USD 显示"}
    rate, day = found
    return {"rate": str(rate), "rate_date": day, "quote": target}
