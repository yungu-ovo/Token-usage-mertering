"""采集入口：归一 -> 双口径定价 -> 原子 UPSERT 去重 -> 触发告警。

去重正确性完全依赖单条原子 UPSERT（见 ``_UPSERT``），**不是**先查后写。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from typing import Any, Iterable, Sequence

from . import config
from .aggregator import rebuild_day
from .db import get_credit_rate
from .money import dec, to_micro
from .pricing import (
    UNKNOWN,
    build_normalizer,
    is_new_unknown,
    record_unknown,
    resolve,
)
from .timeutil import parse_ts

# ------------------------------------------------------------------ SQL
_UPSERT = """
INSERT INTO usage_fact (
  request_id, ts, ts_day, provider, model_raw, model, tool, project, session_id,
  user_id, machine_id, billing_mode,
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  vendor_credit_micro, cost_usd_micro, equivalent_cost_usd_micro, cost_source,
  latency_ms, status, pricing_version, cache_source
) VALUES (
  :request_id, :ts, :ts_day, :provider, :model_raw, :model, :tool, :project, :session_id,
  :user_id, :machine_id, :billing_mode,
  :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
  :vendor_credit_micro, :cost_usd_micro, :equivalent_cost_usd_micro, :cost_source,
  :latency_ms, :status, :pricing_version, :cache_source
)
ON CONFLICT(request_id) DO UPDATE SET
  ts = excluded.ts,
  ts_day = excluded.ts_day,
  provider   = excluded.provider,
  model_raw  = excluded.model_raw,
  model      = excluded.model,
  tool       = excluded.tool,
  project    = excluded.project,
  session_id = excluded.session_id,
  -- 去重语义（RK-02）：以 output_tokens 较大的那条记录为准，取它的 input_tokens。
  -- cache 两层单独取 MAX：流式写入时 cache 字段常常后到，取 MAX 不会丢数据，
  -- 也不会重复计数（MAX 不累加）。
  --
  -- 为什么不整体逐字段取 MAX：实测 825 条里有 1 组重复 messageId 的两条记录
  -- input/output 互不包含（in 22221/out 239 vs in 25591/out 155），逐字段 MAX
  -- 会拼出一条现实中不存在的行。
  input_tokens = CASE WHEN excluded.output_tokens >= usage_fact.output_tokens
                      THEN excluded.input_tokens ELSE usage_fact.input_tokens END,
  output_tokens      = MAX(usage_fact.output_tokens,      excluded.output_tokens),
  cache_read_tokens  = MAX(usage_fact.cache_read_tokens,  excluded.cache_read_tokens),
  cache_write_tokens = MAX(usage_fact.cache_write_tokens, excluded.cache_write_tokens),
  -- P0-A：直报证据永远压倒估算。本行自带缓存读（rawUsage 直报）时，来源改写为
  -- direct；否则保留库里已有的来源（可能是上一次写入的估算值）。
  cache_source = CASE WHEN excluded.cache_read_tokens > 0
                      THEN 'direct' ELSE usage_fact.cache_source END,
  vendor_credit_micro = COALESCE(excluded.vendor_credit_micro, usage_fact.vendor_credit_micro),
  cost_usd_micro = CASE WHEN excluded.cost_source = 'provider'
                        THEN excluded.cost_usd_micro ELSE usage_fact.cost_usd_micro END,
  equivalent_cost_usd_micro = COALESCE(
      excluded.equivalent_cost_usd_micro, usage_fact.equivalent_cost_usd_micro),
  cost_source = CASE WHEN excluded.cost_source = 'provider'
                     THEN 'provider' ELSE usage_fact.cost_source END,
  latency_ms  = COALESCE(excluded.latency_ms, usage_fact.latency_ms),
  status      = excluded.status,
  pricing_version = COALESCE(excluded.pricing_version, usage_fact.pricing_version),
  updated_at  = datetime('now')
WHERE excluded.output_tokens >= usage_fact.output_tokens
RETURNING output_tokens;
"""

_LOCAL_TZ = dt.timezone(dt.timedelta(hours=config.LOCAL_TZ_OFFSET_HOURS))


# ------------------------------------------------------------------ 辅助
def build_request_id(
    provider: str, tool: str, session_id: str, raw_id: str | None
) -> str:
    """派生幂等键。加命名空间前缀避免与其他来源撞车。"""
    payload = "|".join([provider or "", tool or "", session_id or "", raw_id or ""])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]
    return f"{provider or 'src'}_{digest}"


# ------------------------------------------------------------------ 定价
def _price_item(
    conn: sqlite3.Connection,
    *,
    model: str,
    ts_day: str,
    usage: Any,
    billing_mode: str,
    credit_rate: Any,
) -> tuple[int, int | None, str, str | None]:
    """返回 (厂商口径微美元, API 等价微美元, cost_source, pricing_version)。

    双口径并行，互不污染：
    * 厂商口径：优先 `usage.cost`（API 制），其次 `usage.credit × 折算率`（订阅制）
    * API 等价口径：始终按价格表算，未知模型为 None
    """
    price = resolve(conn, model, ts_day)
    pricing_version = price.pricing_version if price else None

    api_equiv = None
    if price is not None:
        api_equiv = to_micro(
            price.cost_of(
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )
        )

    vendor_cost = usage.cost
    credit = usage.credit

    if vendor_cost is not None:
        # 厂商直接给了美元成本，以它为准
        return to_micro(dec(vendor_cost)), api_equiv, "provider", pricing_version

    if credit is not None and dec(credit) != 0:
        # 订阅制：积分按折算率换成名义美元
        cost = dec(credit) * dec(credit_rate)
        return to_micro(cost), api_equiv, "provider", pricing_version

    if api_equiv is not None:
        return api_equiv, api_equiv, "pricing_table", pricing_version

    # 未识别模型且厂商未给成本：两条口径都记 0，由调用方告警（RK-03）
    return 0, None, "pricing_table", pricing_version


# ------------------------------------------------------------------ 主流程
def ingest_items(
    conn: sqlite3.Connection,
    items: Sequence[Any],
    *,
    data_source: str | None = None,
) -> dict[str, Any]:
    """批量写入。调用方需在事务中执行（见 db.tx）。

    ``items`` 为 ``models.IngestItem`` 序列。
    """
    normalizer = build_normalizer(conn)
    credit_rate, _calibrated = get_credit_rate(conn)

    inserted = updated = ignored = 0
    results: list[dict[str, Any]] = []
    unknown_seen: list[str] = []
    unknown_new: list[str] = []
    touched_days: set[str] = set()

    for item in items:
        provider = (item.provider or "").strip()
        tool = (item.tool or "").strip()
        session_id = (item.session_id or "").strip()

        request_id = (item.request_id or "").strip() or build_request_id(
            provider, tool, session_id, item.raw_id
        )

        ts_iso, ts_day = parse_ts(item.ts)

        raw_model = (item.model or "").strip()
        norm = normalizer.apply(raw_model)
        model = norm.model

        if not norm.recognized:
            unknown_seen.append(raw_model or "(空)")
            if is_new_unknown(conn, raw_model):
                unknown_new.append(raw_model or "(空)")
            record_unknown(conn, raw_model)

        cost_micro, equiv_micro, cost_source, pricing_version = _price_item(
            conn,
            model=model,
            ts_day=ts_day,
            usage=item.usage,
            billing_mode=item.billing_mode,
            credit_rate=credit_rate,
        )

        # 仅用于精确报告 inserted / updated；去重正确性由 UPSERT 保证
        existed = (
            conn.execute(
                "SELECT 1 FROM usage_fact WHERE request_id = ?", (request_id,)
            ).fetchone()
            is not None
        )

        params = {
            "request_id": request_id,
            "ts": ts_iso,
            "ts_day": ts_day,
            "provider": provider,
            "model_raw": raw_model,
            "model": model,
            "tool": tool,
            "project": (item.project or "").strip(),
            "session_id": session_id,
            "user_id": (item.user_id or "local").strip(),
            "machine_id": (item.machine_id or "local").strip(),
            "billing_mode": item.billing_mode,
            "input_tokens": int(item.usage.input_tokens or 0),
            "output_tokens": int(item.usage.output_tokens or 0),
            "cache_read_tokens": int(item.usage.cache_read_tokens or 0),
            "cache_write_tokens": int(item.usage.cache_write_tokens or 0),
            # P0-A：本记录自带缓存读即直报证据；没有则留给 traces 分摊兜底。
            "cache_source": (
                "direct" if int(item.usage.cache_read_tokens or 0) > 0 else "trace_estimate"
            ),
            "vendor_credit_micro": (
                to_micro(item.usage.credit)
                if item.usage.credit is not None
                else None
            ),
            "cost_usd_micro": cost_micro,
            "equivalent_cost_usd_micro": equiv_micro,
            "cost_source": cost_source,
            "latency_ms": item.latency_ms,
            "status": item.status,
            "pricing_version": pricing_version,
        }

        written = conn.execute(_UPSERT, params).fetchall()
        if written:
            action = "updated" if existed else "inserted"
            if existed:
                updated += 1
            else:
                inserted += 1
            touched_days.add(ts_day)
        else:
            action = "ignored"
            ignored += 1

        results.append(
            {
                "request_id": request_id,
                "action": action,
                "model": model,
                "model_recognized": norm.recognized,
                "cost_source": cost_source,
                "cost_usd": str(dec(cost_micro) / 1000000),
                "equivalent_cost_usd": (
                    None if equiv_micro is None else str(dec(equiv_micro) / 1000000)
                ),
            }
        )

    # 重建受影响日期的聚合（幂等，重跑不会翻倍）
    for day in sorted(touched_days):
        rebuild_day(conn, day)

    if data_source:
        from .db import set_meta

        set_meta(conn, "data_source", data_source)

    return {
        "received": len(items),
        "inserted": inserted,
        "updated": updated,
        "ignored": ignored,
        "unknown_models": sorted(set(unknown_seen)),
        "unknown_models_new": sorted(set(unknown_new)),
        "results": results,
    }
