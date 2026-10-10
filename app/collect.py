"""采集流水线（B-1）。

早先这条流水线只存在于 ``scripts/ingest_workbuddy.py`` 里，**服务层想采也采不了**——
于是界面上那个「重新采集」按钮只能重算聚合，数据永远停在最后一次手动跑脚本的时刻（A-2）。

这里把它抽成服务层可直接调用的函数；脚本退化为薄 CLI 包装，两边共用同一条路径，
不会再出现「脚本能采、服务不能采」的分裂。

隐私约束不变：全程只读 ``<home>/.workbuddy``，只按白名单取值（RK-04）。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import alerts, config, db
from .aggregator import rebuild_all
from .ingest import ingest_items
from .models import IngestItem
from .sources.base import ParseStats, ParsedRecord
from .sources.trace_cache import backfill
from .sources.workbuddy_jsonl import WorkBuddyJsonlParser
from .timeutil import now_local, now_local_str

# ---------------------------------------------------------------- 占位价格配置
# WorkBuddy 的模型名是内部代号，公开价格表查不到。为了让成本链路可端到端验证，
# 提供一组**显式占位价格**（source=user，版本串带 placeholder）。
#
# ⚠ 这些不是真实单价，只用于验证链路；拿到真实单价后走 PUT /api/pricing/override。
#
# 代号表刻意**不进版本库**：
#   placeholder.local.json    本机真实代号（已 gitignore）
#   placeholder.example.json  脱敏示例（进版本库，供照抄结构）
PLACEHOLDER_LOCAL = config.PRICING_DIR / "placeholder.local.json"
PLACEHOLDER_EXAMPLE = config.PRICING_DIR / "placeholder.example.json"
PLACEHOLDER_VERSION_FALLBACK = "user-placeholder"

STALE_HOURS = 24  # 超过这个时长没新数据，顶栏就该提示「已滞后」


def load_placeholder_config(
    path: Path | str | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, str], str, str]:
    """返回 ``(prices, aliases, version, 来源说明)``。

    默认优先本机 ``placeholder.local.json``，缺失时退到脱敏示例。
    """
    if path is not None:
        candidates: list[tuple[Path, str]] = [(Path(path), f"指定文件 {path}")]
    else:
        candidates = [
            (PLACEHOLDER_LOCAL, "本机配置 placeholder.local.json"),
            (PLACEHOLDER_EXAMPLE, "脱敏示例 placeholder.example.json"),
        ]

    for candidate, label in candidates:
        if not candidate.exists():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        prices = {
            str(k): {kk: str(vv) for kk, vv in v.items()}
            for k, v in (payload.get("prices") or {}).items()
        }
        aliases = {str(k): str(v) for k, v in (payload.get("aliases") or {}).items()}
        version = str(payload.get("version") or PLACEHOLDER_VERSION_FALLBACK)
        return prices, aliases, version, label

    raise SystemExit(
        "找不到占位价格配置。请任选其一：\n"
        f"  * 把 {PLACEHOLDER_EXAMPLE.name} 复制成 {PLACEHOLDER_LOCAL.name} 并填入你的模型名\n"
        "  * 或显式指定：--placeholder-file <路径>"
    )


def apply_placeholder_prices(path: Path | str | None = None) -> dict[str, Any]:
    """写入占位价格与别名。已有同名 user 行会被覆盖（版本号固定）。"""
    from .pricing import override

    prices, aliases, version, source = load_placeholder_config(path)
    if not prices:
        raise SystemExit(f"占位价格配置为空：{source}")

    with db.tx() as conn:
        for raw, canonical in aliases.items():
            conn.execute(
                "INSERT INTO model_alias (alias_raw, model, provider) VALUES (?, ?, '') "
                "ON CONFLICT(alias_raw) DO UPDATE SET model = excluded.model",
                (raw, canonical),
            )
        for model, price in prices.items():
            override(
                conn,
                model=model,
                input_per_mtok=price["input"],
                output_per_mtok=price["output"],
                cache_read_per_mtok=price["cache_read"],
                cache_write_per_mtok=price["cache_write"],
                effective_from="2026-01-01",
                version=f"{version}:{model}",
            )
    return {"models": len(prices), "source": source, "version": version}


# ---------------------------------------------------------------- 解析
def build_parser(home: Path | str | None = None) -> WorkBuddyJsonlParser:
    return WorkBuddyJsonlParser(home=Path(home) if home else None)


def parse_records(
    since: str | None = None, home: Path | str | None = None
) -> tuple[list[ParsedRecord], ParseStats]:
    """解析出待入库的记录，并顺带产出解析健康度（RK-07）。"""
    parser = build_parser(home)
    records = list(parser.parse_all())
    if since:
        records = [r for r in records if r.ts[:10] >= since]
    return records, parser.stats


def _to_item(record: ParsedRecord) -> IngestItem:
    return IngestItem(**record.to_ingest_item())


def _record_parser_stats(conn: sqlite3.Connection, stats: ParseStats) -> dict[str, Any]:
    """把解析健康度写进 meta，供告警与 /api/meta 读取。"""
    info = stats.as_dict()
    db.set_meta(conn, alerts.META_PARSER_VERSION, str(info["version"]))
    db.set_meta(conn, alerts.META_PARSER_SCANNED, str(info["scanned"]))
    db.set_meta(conn, alerts.META_PARSER_USAGE_LINES, str(info["usage_lines"]))
    db.set_meta(conn, alerts.META_PARSER_PARSED, str(info["parsed"]))
    db.set_meta(
        conn,
        alerts.META_PARSE_RATE,
        "" if info["success_rate"] is None else str(info["success_rate"]),
    )
    db.set_meta(conn, "last_collect_at", now_local_str())
    return info


# ---------------------------------------------------------------- 数据新鲜度
def data_freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    """库里最新的数据有多旧（A-4）。

    没有这个，用户看到的是一个"看起来很新"的看板——旧数据和新数据在界面上
    长得一模一样，只有数字会悄悄说谎。
    """
    row = conn.execute(
        "SELECT MAX(ts) AS ts, MAX(ts_day) AS day, COUNT(*) AS n FROM usage_fact"
    ).fetchone()
    count = int(row["n"] or 0) if row else 0
    latest_ts = row["ts"] if row else None
    latest_day = row["day"] if row else None

    if not count or not latest_ts:
        return {
            "has_data": False,
            "row_count": 0,
            "last_record_ts": None,
            "last_record_day": None,
            "freshness_hours": None,
            "is_stale": False,
            "note": "库中还没有任何用量记录",
        }

    hours = _hours_since(latest_ts)
    return {
        "has_data": True,
        "row_count": count,
        "last_record_ts": latest_ts,
        "last_record_day": latest_day,
        "freshness_hours": None if hours is None else round(hours, 1),
        "is_stale": bool(hours is not None and hours >= STALE_HOURS),
        "note": "" if hours is None else _freshness_note(hours),
    }


def _hours_since(ts: str) -> float | None:
    try:
        moment = dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now_local().tzinfo)
    return (now_local() - moment).total_seconds() / 3600.0


def _freshness_note(hours: float) -> str:
    if hours >= STALE_HOURS:
        return f"数据已滞后 {hours / 24:.1f} 天，点「采集」拉取最新用量"
    if hours >= 1:
        return f"最新数据距今 {hours:.1f} 小时"
    return f"最新数据距今 {hours * 60:.0f} 分钟"


# ---------------------------------------------------------------- 主流程
def collect(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    home: Path | str | None = None,
    placeholder_file: Path | str | None = None,
    apply_placeholder: bool = False,
    backfill_cache: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """跑完一整条采集流水线。调用方负责事务（推荐 ``db.tx()``）。

    顺序：解析 →（可选占位价格）→ 幂等落库 → traces 回填缓存 → 重算聚合 → 告警巡检。
    ``dry_run=True`` 只解析不写库，用于演练与接口自检。
    """
    records, stats = parse_records(since=since, home=home)
    parser_info = stats.as_dict()

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "parsed": len(records),
        "parser": parser_info,
        "placeholder": None,
        "ingest": None,
        "cache": None,
        "agg_rows": 0,
        "alerts": {},
    }

    if dry_run:
        # 演练不碰数据库：连 meta 都不写，避免"看一眼"就改了状态
        summary["note"] = "演练模式：只解析未写库"
        return summary

    _record_parser_stats(conn, stats)

    if apply_placeholder:
        summary["placeholder"] = apply_placeholder_prices(placeholder_file)

    if not records:
        summary["note"] = "没有可写入的记录"
        # 即便没有新记录也要巡检：解析成功率可能刚刚跌破阈值
        summary["agg_rows"] = rebuild_all(conn)
        summary["alerts"] = alerts.evaluate_scheduled(conn)
        return summary

    items = [_to_item(r) for r in records]
    ingest_summary = ingest_items(
        conn, items, data_source=config.DATA_SOURCE_WORKBUDDY
    )
    summary["ingest"] = ingest_summary

    if backfill_cache:
        summary["cache"] = backfill(conn, home, apply=True)

    summary["agg_rows"] = rebuild_all(conn)
    summary["alerts"] = alerts.evaluate_scheduled(conn)
    summary["freshness"] = data_freshness(conn)
    return summary


def collect_stats_line(summary: dict[str, Any]) -> str:
    """给人看的一行摘要（脚本与日志共用）。"""
    ing = summary.get("ingest") or {}
    return (
        f"解析 {summary.get('parsed', 0)} 条 · 新增 {ing.get('inserted', 0)} "
        f"更新 {ing.get('updated', 0)} 忽略 {ing.get('ignored', 0)}"
    )


__all__ = [
    "apply_placeholder_prices",
    "build_parser",
    "collect",
    "collect_stats_line",
    "data_freshness",
    "load_placeholder_config",
    "parse_records",
    "STALE_HOURS",
]
