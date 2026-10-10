"""价格表的加载、版本化、解析与覆盖（RK-01）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..money import MICRO, dec
from .normalize import Normalizer

DEFAULT_PRICING_FILE = Path(__file__).resolve().parent / "default_pricing.json"
ALIASES_FILE = Path(__file__).resolve().parent / "aliases.json"

# 同一模型同一天命中多来源时的优先级（数字小者优先）
_SOURCE_PRIORITY = {"user": 0, "litellm": 1, "builtin": 2}


@dataclass(frozen=True)
class Price:
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    pricing_version: str
    source: str

    def cost_of(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> Decimal:
        """按四层 token 计算成本（USD）。

        分项各自量化后再求和，保证「四层分项之和 == 总额」守恒。
        """
        total = (
            Decimal(int(input_tokens or 0)) * self.input_per_mtok
            + Decimal(int(output_tokens or 0)) * self.output_per_mtok
            + Decimal(int(cache_read_tokens or 0)) * self.cache_read_per_mtok
            + Decimal(int(cache_write_tokens or 0)) * self.cache_write_per_mtok
        ) / MICRO
        return total


# ------------------------------------------------------------------ 加载内置价格表
def load_builtin() -> dict[str, Any]:
    return json.loads(DEFAULT_PRICING_FILE.read_text(encoding="utf-8"))


def load_aliases() -> dict[str, str]:
    if not ALIASES_FILE.exists():
        return {}
    return json.loads(ALIASES_FILE.read_text(encoding="utf-8")).get("aliases", {})


def seed_builtin(conn: sqlite3.Connection) -> None:
    """把内置价格表写入 price_table。

    用 ``INSERT OR IGNORE``：**老版本永不被覆盖**，这是 RK-01「调价不改历史成本」的基础。
    """
    data = load_builtin()
    version = data["pricing_version"]
    effective_from = data["effective_from"]
    for model, prices in data.get("models", {}).items():
        conn.execute(
            "INSERT OR IGNORE INTO price_table ("
            "  pricing_version, model, provider, effective_from, effective_to,"
            "  input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, source"
            ") VALUES (?, ?, '', ?, NULL, ?, ?, ?, ?, 'builtin')",
            (
                version,
                model,
                effective_from,
                str(prices.get("input", "0")),
                str(prices.get("output", "0")),
                str(prices.get("cache_read", "0")),
                str(prices.get("cache_write", "0")),
            ),
        )
    # 别名表
    for alias_raw, model in load_aliases().items():
        conn.execute(
            "INSERT OR IGNORE INTO model_alias (alias_raw, model, provider) VALUES (?, ?, '')",
            (alias_raw, model),
        )


def known_models(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT model FROM price_table").fetchall()
    return {r["model"] for r in rows}


def build_normalizer(conn: sqlite3.Connection) -> Normalizer:
    """别名来源：JSON 文件 + 库里的 model_alias 表（后者优先）。"""
    aliases = dict(load_aliases())
    for row in conn.execute("SELECT alias_raw, model FROM model_alias").fetchall():
        aliases[row["alias_raw"]] = row["model"]
    return Normalizer(aliases=aliases, known_models=known_models(conn))


# ------------------------------------------------------------------ 解析
def resolve(conn: sqlite3.Connection, model: str, ts_day: str) -> Price | None:
    """取该模型在 ``ts_day`` 当天生效的价格。命中不到返回 None。"""
    rows = conn.execute(
        "SELECT * FROM price_table "
        "WHERE model = ? AND effective_from <= ? "
        "  AND (effective_to IS NULL OR effective_to >= ?)",
        (model, ts_day, ts_day),
    ).fetchall()
    if not rows:
        return None

    # 先按「来源优先级」取最优（user > litellm > builtin），
    # 同优先级再取 effective_from 最新的那条
    best_priority = min(_SOURCE_PRIORITY.get(r["source"], 9) for r in rows)
    candidates = [r for r in rows if _SOURCE_PRIORITY.get(r["source"], 9) == best_priority]
    best = max(candidates, key=lambda r: r["effective_from"])

    return Price(
        model=best["model"],
        input_per_mtok=dec(best["input_per_mtok"]),
        output_per_mtok=dec(best["output_per_mtok"]),
        cache_read_per_mtok=dec(best["cache_read_per_mtok"]),
        cache_write_per_mtok=dec(best["cache_write_per_mtok"]),
        pricing_version=best["pricing_version"],
        source=best["source"],
    )


def current_version(conn: sqlite3.Connection, today: str | None = None) -> str:
    """当前生效的价格表版本。

    ``today`` 用**本地自然日**，不能用 SQLite 的 ``date('now')``——后者是 UTC，
    在本地 UTC+8 的 00:00~08:00 之间会退回前一天，从而选到旧版本的价格。
    """
    day = today or _today()
    row = conn.execute(
        "SELECT pricing_version, MAX(effective_from) AS ef FROM price_table "
        "WHERE effective_from <= ? GROUP BY pricing_version "
        "ORDER BY ef DESC LIMIT 1",
        (day,),
    ).fetchone()
    if row:
        return row["pricing_version"]
    row = conn.execute(
        "SELECT pricing_version FROM price_table ORDER BY effective_from DESC LIMIT 1"
    ).fetchone()
    return row["pricing_version"] if row else ""


# ------------------------------------------------------------------ 覆盖（RK-01）
def override(
    conn: sqlite3.Connection,
    model: str,
    input_per_mtok: Any,
    output_per_mtok: Any,
    cache_read_per_mtok: Any = 0,
    cache_write_per_mtok: Any = 0,
    effective_from: str | None = None,
    version: str | None = None,
) -> str:
    """写入一条 user 来源的价格。

    关键：**不修改已有行**，而是新增一个版本，并把旧版本的 ``effective_to``
    回填到新版本生效日的前一天。这样历史记录引用的旧版本仍然可解析，历史成本不变。
    """
    eff = effective_from or _today()
    ver = version or f"user-{eff}-{model}"

    prev = conn.execute(
        "SELECT pricing_version, effective_from FROM price_table "
        "WHERE model = ? AND source = 'user' AND effective_to IS NULL",
        (model,),
    ).fetchall()
    for row in prev:
        if row["effective_from"] < eff:
            conn.execute(
                "UPDATE price_table SET effective_to = ? "
                "WHERE pricing_version = ? AND model = ?",
                (_day_before(eff), row["pricing_version"], model),
            )

    conn.execute(
        "INSERT OR REPLACE INTO price_table ("
        "  pricing_version, model, provider, effective_from, effective_to,"
        "  input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, source"
        ") VALUES (?, ?, '', ?, NULL, ?, ?, ?, ?, 'user')",
        (
            ver,
            model,
            eff,
            str(dec(input_per_mtok)),
            str(dec(output_per_mtok)),
            str(dec(cache_read_per_mtok)),
            str(dec(cache_write_per_mtok)),
        ),
    )
    return ver


def _today() -> str:
    import datetime as _dt

    return _dt.date.today().isoformat()


def _day_before(day: str) -> str:
    import datetime as _dt

    d = _dt.date.fromisoformat(day)
    return (d - _dt.timedelta(days=1)).isoformat()


# ------------------------------------------------------------------ 未识别模型记账
def record_unknown(conn: sqlite3.Connection, model_raw: str) -> int:
    """记一次未识别命中，返回累计次数。"""
    conn.execute(
        "INSERT INTO unknown_model (model_raw) VALUES (?) "
        "ON CONFLICT(model_raw) DO UPDATE SET "
        "  last_seen = datetime('now'), hit_count = hit_count + 1",
        (model_raw or "",),
    )
    row = conn.execute(
        "SELECT hit_count FROM unknown_model WHERE model_raw = ?", (model_raw or "",)
    ).fetchone()
    return int(row["hit_count"]) if row else 1


def is_new_unknown(conn: sqlite3.Connection, model_raw: str) -> bool:
    """首次出现才返回 True —— 用于只告警一次。"""
    row = conn.execute(
        "SELECT hit_count FROM unknown_model WHERE model_raw = ?", (model_raw or "",)
    ).fetchone()
    return row is None


def list_unknown(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "model_raw": r["model_raw"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "hit_count": r["hit_count"],
        }
        for r in conn.execute(
            "SELECT * FROM unknown_model ORDER BY hit_count DESC"
        ).fetchall()
    ]
