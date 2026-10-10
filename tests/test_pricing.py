"""定价：双口径、Decimal 精度、别名归一（RK-03）、价格表版本化（RK-01）。"""

from __future__ import annotations

from decimal import Decimal

from app.ingest import ingest_items
from app.models import IngestItem
from app.pricing import UNKNOWN, build_normalizer, current_version, override, resolve


def _item(rid: str, *, model: str = "gpt-5", ts: str = "2026-10-05T10:00:00+08:00",
          cost: str | None = None, credit: str | None = None,
          inp: int = 1_000_000, out: int = 0, cr: int = 0, cw: int = 0):
    usage: dict = {"input_tokens": inp, "output_tokens": out,
                   "cache_read_tokens": cr, "cache_write_tokens": cw}
    if cost is not None:
        usage["cost"] = cost
    if credit is not None:
        usage["credit"] = credit
    return IngestItem.model_validate(
        {"request_id": rid, "ts": ts, "provider": "test", "model": model,
         "session_id": "s", "usage": usage}
    )


def _row(conn, rid: str):
    return conn.execute("SELECT * FROM usage_fact WHERE request_id = ?", (rid,)).fetchone()


# ------------------------------------------------------------------ 精度
def test_cost_stored_as_integer_micro_not_float(conn):
    ingest_items(conn, [_item("p1", inp=1_000_000)])
    conn.commit()

    row = _row(conn, "p1")
    assert isinstance(row["cost_usd_micro"], int)
    # gpt-5 输入价 1.25 /Mtok，100 万 token -> $1.25 -> 1_250_000 微美元
    assert row["cost_usd_micro"] == 1_250_000


def test_no_real_columns_anywhere(conn):
    """整库不允许出现 REAL 类型列——审计要求禁用浮点。"""
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    for table in tables:
        for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
            assert col["type"].upper() != "REAL", f"{table}.{col['name']} 是 REAL"


def test_decimal_does_not_introduce_float_error(dbpath):
    from app.money import dec, to_micro

    # 0.1 + 0.2 在浮点下是 0.30000000000000004
    assert dec(0.1) + dec(0.2) == Decimal("0.3")
    assert to_micro("0.000001") == 1
    assert to_micro(Decimal("0.0000005")) == 1  # 四舍五入
    assert dec(0.184) == Decimal("0.184")


# ------------------------------------------------------------------ 成本来源
def test_provider_cost_wins_over_pricing_table(conn):
    ingest_items(conn, [_item("p2", cost="9.99")])
    conn.commit()
    row = _row(conn, "p2")
    assert row["cost_source"] == "provider"
    assert row["cost_usd_micro"] == 9_990_000
    # API 等价口径仍然按价格表算，两者互不污染
    assert row["equivalent_cost_usd_micro"] == 1_250_000


def test_subscription_dual_caliber(conn):
    """订阅制：厂商口径 = 积分 × 折算率，API 等价口径 = 价格表。"""
    ingest_items(conn, [_item("p3", credit="100", inp=1_000_000)])
    conn.commit()
    row = _row(conn, "p3")
    assert row["billing_mode"] == "api"  # 默认值，未显式指定
    assert row["vendor_credit_micro"] == 100_000_000
    # 默认折算率 0.01 -> 100 积分 = $1.00
    assert row["cost_usd_micro"] == 1_000_000
    assert row["equivalent_cost_usd_micro"] == 1_250_000
    assert row["cost_source"] == "provider"


# ------------------------------------------------------------------ RK-03
def test_unknown_model_is_recorded_not_silently_charged(conn):
    result = ingest_items(conn, [_item("p4", model="totally-made-up-model")])
    conn.commit()

    row = _row(conn, "p4")
    assert row["model"] == UNKNOWN
    assert row["model_raw"] == "totally-made-up-model"
    assert row["cost_usd_micro"] == 0
    assert row["equivalent_cost_usd_micro"] is None
    assert "totally-made-up-model" in result["unknown_models"]

    ledger = conn.execute(
        "SELECT hit_count FROM unknown_model WHERE model_raw = 'totally-made-up-model'"
    ).fetchone()
    assert ledger["hit_count"] == 1


def test_alias_normalization(conn):
    normalizer = build_normalizer(conn)
    assert normalizer.apply("claude-4.5-sonnet").model == "claude-sonnet-4.5"
    assert normalizer.apply("Claude-Sonnet-4-5-20250929").model == "claude-sonnet-4.5"
    assert normalizer.apply("anthropic/claude-sonnet-4.5").model == "claude-sonnet-4.5"
    assert normalizer.apply("GPT5").model == "gpt-5"
    assert normalizer.apply("nonsense-xyz").recognized is False


def test_known_model_name_must_not_be_stripped_by_version_cleaner(conn):
    """守住「价格表里有、却被清洗成未知模型」的假阴性。

    回归用例来自演示数据：``deepseek-v3`` 本身就是价格表的正式模型名，
    但 cleaners 的 ``[-_](latest|preview|stable|beta|v\\d+)$`` 会把 ``-v3``
    当成版本后缀剥掉，得到 ``deepseek`` 再判未知 → 成本被记成 0。
    """
    normalizer = build_normalizer(conn)
    nz = normalizer.apply("deepseek-v3")
    assert nz.recognized is True, "价格表里存在的模型名不应被判为未知"
    assert nz.model == "deepseek-v3"
    # 同类：以 -vN 结尾且本身就在价格表里的名字
    assert normalizer.apply("deepseek-v3.1").model == "deepseek-v3"


def test_raw_exact_match_beats_cleaning_for_every_priced_model(conn):
    """价格表里每个模型名，原样喂回去都必须精确命中自己（自洽性）。"""
    from app.pricing import known_models

    normalizer = build_normalizer(conn)
    for name in sorted(known_models(conn)):
        got = normalizer.apply(name)
        assert got.recognized is True, f"{name} 原样输入却未被识别"
        assert got.model == name, f"{name} 原样输入被改写成了 {got.model}"


# ------------------------------------------------------------------ RK-01
def test_pricing_override_does_not_change_history(conn):
    """调价后历史记录的成本必须原封不动。"""
    ingest_items(conn, [_item("p5", ts="2026-10-05T10:00:00+08:00")])
    conn.commit()
    before = _row(conn, "p5")
    assert before["cost_usd_micro"] == 1_250_000
    assert before["pricing_version"] == "builtin-2026.10.1"

    # 涨价 10 倍，从 2026-10-09 生效
    override(conn, "gpt-5", "12.50", "100.00", "1.25", "12.50",
             effective_from="2026-10-09")
    conn.commit()

    after = _row(conn, "p5")
    assert after["cost_usd_micro"] == before["cost_usd_micro"], "历史成本被改写了！"
    assert after["pricing_version"] == before["pricing_version"], "历史版本被改写了！"


def test_new_price_applies_to_new_records(conn):
    override(conn, "gpt-5", "12.50", "100.00", "1.25", "12.50",
             effective_from="2026-10-09")
    conn.commit()

    ingest_items(conn, [_item("p6", ts="2026-10-09T10:00:00+08:00")])
    conn.commit()
    row = _row(conn, "p6")
    assert row["cost_usd_micro"] == 12_500_000
    assert row["cost_source"] == "pricing_table"


def test_old_version_row_is_preserved(conn):
    """老版本的价格行不能被覆盖或删除。"""
    versions = {
        r["pricing_version"]
        for r in conn.execute(
            "SELECT DISTINCT pricing_version FROM price_table WHERE model='gpt-5'"
        ).fetchall()
    }
    assert "builtin-2026.10.1" in versions

    override(conn, "gpt-5", "1.00", "2.00", effective_from="2026-10-09")
    conn.commit()

    versions_after = {
        r["pricing_version"]
        for r in conn.execute(
            "SELECT DISTINCT pricing_version FROM price_table WHERE model='gpt-5'"
        ).fetchall()
    }
    assert "builtin-2026.10.1" in versions_after, "老版本被删了"


def test_resolve_by_effective_window(conn):
    override(conn, "gpt-5", "9.00", "90.00", effective_from="2026-10-09")
    conn.commit()

    old = resolve(conn, "gpt-5", "2026-10-01")
    new = resolve(conn, "gpt-5", "2026-10-09")
    assert old is not None and old.input_per_mtok == Decimal("1.25"), "旧日期应命中旧版本"
    assert new is not None and new.input_per_mtok == Decimal("9.00"), "新日期应命中新版本"


# ------------------------------------------------------------------ 时区边界
def test_current_version_uses_local_day_not_utc(conn):
    """``date('now')`` 是 UTC，本地 UTC+8 的凌晨会退回前一天，选到旧版本。"""
    from app import timeutil

    today = timeutil.today_local().isoformat()
    ver = override(conn, "gpt-5", "9.00", "90.00", effective_from=today)
    conn.commit()

    # 默认（不传日期）必须用本地自然日，命中今天生效的新版本
    assert current_version(conn) == ver
    assert current_version(conn, today=today) == ver

    # 新版本生效日之前的日期，应命中内置版本
    assert current_version(conn, today="2026-10-05") == "builtin-2026.10.1"


def test_current_version_never_asks_sqlite_for_utc_now(conn):
    """守住这个坑：价格版本选择不允许交给 SQLite 的 date('now')（UTC）。"""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        current_version(conn)
    finally:
        conn.set_trace_callback(None)

    offenders = [sql for sql in seen if "date('now')" in sql or "datetime('now')" in sql]
    assert not offenders, f"价格版本选择又用回 UTC 时间了: {offenders}"
