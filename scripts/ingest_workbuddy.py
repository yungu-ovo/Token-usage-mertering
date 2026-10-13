"""WorkBuddy 真实用量灌数脚本（试点）。

用法::

    python scripts/ingest_workbuddy.py --inspect          # 只统计，不写任何东西
    python scripts/ingest_workbuddy.py --dry-run --limit 20
    python scripts/ingest_workbuddy.py --ingest --since 2026-09-01
    python scripts/ingest_workbuddy.py --verify           # 与 traces 交叉校验
    python scripts/ingest_workbuddy.py --ingest --api     # 走 HTTP 打到本地服务

安全约束
--------
* **只读** ``~/.workbuddy/``，绝不写入该目录。
* 只按白名单字段取值，对话正文不会进入内存或落库（RK-04）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import alerts, collect as collect_mod, config  # noqa: E402
from app.db import init_db, session, tx  # noqa: E402
from app.sources.base import ALLOWED_PATHS, dig  # noqa: E402
from app.sources.workbuddy_jsonl import (  # noqa: E402
    WorkBuddyJsonlParser,
    has_usage,
    iter_json_lines,
    record_to_parsed,
)
from app.sources.workbuddy_trace import WorkBuddyTraceParser  # noqa: E402


def _money(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.6f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------ inspect
def cmd_inspect(args: argparse.Namespace) -> int:
    parser = WorkBuddyJsonlParser()
    files = parser.discover()
    print(f"数据目录        : {config.WORKBUDDY_PROJECTS_DIR}")
    print(f"JSONL 文件数    : {len(files)}")
    if not files:
        print("!! 没找到任何 .jsonl，无法继续")
        return 1

    total_lines = 0
    with_usage = 0
    parsable = 0
    type_counter: Counter[str] = Counter()
    path_hits: Counter[str] = Counter()
    models: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    projects: Counter[str] = Counter()
    days: Counter[str] = Counter()
    credit_seen = 0
    ts_missing = 0

    for path in files:
        for record in iter_json_lines(path):
            total_lines += 1
            type_counter[str(dig(record, "type"))] += 1
            if not has_usage(record):
                continue
            with_usage += 1
            for p in ALLOWED_PATHS:
                if dig(record, p) is not None:
                    path_hits[p] += 1
            parsed = record_to_parsed(record)
            if parsed is None:
                continue
            parsable += 1
            models[parsed.model] += 1
            tools[parsed.tool] += 1
            projects[parsed.project] += 1
            if not parsed.ts:
                ts_missing += 1
            else:
                days[parsed.ts[:10]] += 1
            if parsed.credit is not None:
                credit_seen += 1

    print(f"JSONL 总行数    : {total_lines}")
    print(f"含 usage 的行   : {with_usage}")
    print(f"可解析成记录    : {parsable}")
    print(f"其中 credit 非空: {credit_seen}")
    print(f"timestamp 缺失  : {ts_missing}")

    print("\n--- 白名单字段命中次数（top 20）---")
    for path, count in path_hits.most_common(20):
        print(f"  {count:>7}  {path}")

    print("\n--- 记录 type 分布 ---")
    for name, count in type_counter.most_common(10):
        print(f"  {count:>7}  {name}")

    print(f"\n--- 模型分布（{len(models)} 种）---")
    for name, count in models.most_common(30):
        print(f"  {count:>7}  {name!r}")

    print(f"\n--- tool 分布 ---")
    for name, count in tools.most_common(10):
        print(f"  {count:>7}  {name!r}")

    print(f"\n--- 项目分布 ---")
    for name, count in projects.most_common(20):
        print(f"  {count:>7}  {name!r}")

    if days:
        ordered = sorted(days)
        print(f"\n--- 日期范围 ---\n  {ordered[0]} .. {ordered[-1]}（共 {len(ordered)} 天）")

    print("\n--- 去重键唯一性（决定 MAX 合并是否会丢数据）---")
    _dedup_report(files)

    print("\n--- 交叉校验源 ---")
    trace_files = WorkBuddyTraceParser().discover()
    print(f"  trace 文件数  : {len(trace_files)}")
    return 0


def _dedup_report(files: list[Path]) -> None:
    """检查 messageId 是否唯一。

    如果一次 API 调用会写出多条共用 messageId 的记录（流式分片），
    那么 MAX 合并就是正确行为；如果 messageId 唯一，则不存在合并问题。
    """
    mid_counter: Counter[tuple[str, str]] = Counter()
    conv_counter: Counter[tuple[str, str]] = Counter()
    mid_examples: dict[tuple[str, str], list[dict]] = {}

    for path in files:
        for record in iter_json_lines(path):
            if not has_usage(record):
                continue
            sid = str(dig(record, "sessionId") or "")
            mid = str(dig(record, "providerData.messageId") or "")
            conv = str(dig(record, "providerData.conversationRequestId") or "")
            if mid:
                key = (sid, mid)
                mid_counter[key] += 1
                if mid_counter[key] <= 3:
                    mid_examples.setdefault(key, []).append(
                        {
                            "type": dig(record, "type"),
                            "in": dig(record, "providerData.rawUsage.prompt_tokens"),
                            "out": dig(record, "providerData.rawUsage.completion_tokens"),
                            "cr": dig(record, "providerData.rawUsage.cache_read_input_tokens"),
                        }
                    )
            if conv:
                conv_counter[(sid, conv)] += 1

    total = sum(mid_counter.values())
    print(f"  含 usage 记录数            : {total}")
    print(f"  唯一 (sessionId, messageId): {len(mid_counter)}")
    print(f"  唯一 (sessionId, convReqId): {len(conv_counter)}")
    dupes = {k: v for k, v in mid_counter.items() if v > 1}
    print(f"  重复的 messageId 组数      : {len(dupes)}")
    if dupes:
        print("  !! messageId 有重复，MAX 合并会生效。示例：")
        for key, count in list(dupes.items())[:3]:
            print(f"     {key[1][:16]}… x{count}")
            for ex in mid_examples.get(key, [])[:3]:
                print(f"        type={ex['type']} in={ex['in']} out={ex['out']} cr={ex['cr']}")
    else:
        print("  OK messageId 唯一，MAX 合并不会丢数据")


# ------------------------------------------------------------------ dry-run
def cmd_dry_run(args: argparse.Namespace) -> int:
    parser = WorkBuddyJsonlParser()
    records = list(parser.parse_all())
    if args.since:
        records = [r for r in records if r.ts[:10] >= args.since]
    if args.limit:
        records = records[: args.limit]

    print(f"待归一记录数: {len(records)}\n")
    header = f"{'时间':<20} {'模型':<26} {'项目':<22} {'in':>8} {'out':>7} {'cR':>9} {'cW':>7} {'credit':>10} {'状态':<9}"
    print(header)
    print("-" * len(header))
    for r in records:
        print(
            f"{r.ts[:19]:<20} {r.model[:25]:<26} {r.project[:21]:<22} "
            f"{r.input_tokens:>8} {r.output_tokens:>7} {r.cache_read_tokens:>9} "
            f"{r.cache_write_tokens:>7} {_money(r.credit):>10} {r.status:<9}"
        )

    total_in = sum(r.input_tokens for r in records)
    total_out = sum(r.output_tokens for r in records)
    total_cr = sum(r.cache_read_tokens for r in records)
    total_cw = sum(r.cache_write_tokens for r in records)
    print("\n--- 合计 ---")
    print(f"  input={total_in:,}  output={total_out:,}  cache_read={total_cr:,}  cache_write={total_cw:,}")
    print(f"  合计 token = {total_in + total_out + total_cr + total_cw:,}")

    # 归一 + 定价预演（只读 DB）
    init_db()
    from app.pricing import build_normalizer, resolve

    with session() as conn:
        normalizer = build_normalizer(conn)
        unknown: Counter[str] = Counter()
        priced: Counter[str] = Counter()
        unpriced: Counter[str] = Counter()
        total_cost = Decimal(0)
        for r in records:
            norm = normalizer.apply(r.model)
            if not norm.recognized:
                unknown[r.model] += 1
                continue
            priced[norm.model] += 1
            price = resolve(conn, norm.model, r.ts[:10])
            if price is None:
                unpriced[norm.model] += 1
                continue
            total_cost += price.cost_of(
                r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens
            )

    print("\n--- 归一与定价预演 ---")
    print(f"  可定价记录    : {sum(priced.values())}")
    print(f"  API 等价成本  : ${total_cost:.4f}")
    print(f"  已识别模型    : {dict(priced)}")
    if unpriced:
        print(f"  已识别但无价  : {dict(unpriced)}")
    if unknown:
        print(f"\n  !! 未识别模型（会进 __unknown__ 并告警，成本不计入）:")
        for name, count in unknown.most_common():
            print(f"       {count:>6}  {name!r}")
    return 0


# ------------------------------------------------------------------ 占位价格
# 实现已搬到 app/collect.py（B-1：服务层也要能跑这条流水线）。
# 这里保留同名入口，命令行行为不变。
def load_placeholder_config(path: Path | None = None):
    return collect_mod.load_placeholder_config(path)


def apply_placeholder_prices(path: Path | None = None) -> None:
    info = collect_mod.apply_placeholder_prices(path)
    source = info["source"]
    print("!! 已写入占位价格（source=user，版本串含 placeholder 字样）")
    print(f"   来源：{source}")
    if source.endswith("placeholder.example.json"):
        print("   ⚠ 用的是脱敏示例，里面没有你的真实模型名 → 真实数据会进 __unknown__。")
        print(f"     要计入成本，请把示例复制成 {collect_mod.PLACEHOLDER_LOCAL.name} 并填入真实代号。")
    print(f"   模型数：{info['models']}")
    print("!! 这些不是 WorkBuddy 的真实单价，请用 /api/pricing/override 覆盖。")


# ------------------------------------------------------------------ ingest
def cmd_ingest(args: argparse.Namespace) -> int:
    if args.apply_placeholder_prices:
        apply_placeholder_prices(getattr(args, "placeholder_file", None))

    incremental = bool(getattr(args, "incremental", False))
    if incremental and args.api:
        print("--incremental 与 --api 不兼容：增量扫描在本地库上进行，请去掉 --api")
        return 1

    records: list = []
    if not incremental:
        records, _stats = collect_mod.parse_records(since=args.since)
        if args.api:
            if not records:
                print("没有可写入的记录")
                return 1
            return _ingest_via_http(records, args)

    init_db()
    # 与调度器的采集轮互斥：排队等锁而不是撞 "database is locked"
    with collect_mod.COLLECT_LOCK:
        with tx() as conn:
            summary = collect_mod.collect(
                conn,
                since=args.since,
                backfill_cache=True,
                incremental=incremental,
            )

    mode = "增量" if incremental else "全量"
    print(f"[{mode}采集] 解析 {summary.get('parsed', 0)} 条记录")
    scan = summary.get("scan") or {}
    if scan:
        print(
            f"  扫描 {scan.get('files_seen', 0)} 个文件 · 新读 {scan.get('files_read', 0)} 个"
            f"（其中新文件 {scan.get('new_files', 0)}）· 未变化 {scan.get('unchanged_files', 0)} 个"
            f" · 截断重读 {scan.get('truncated_files', 0)} 个"
        )

    pinfo = summary.get("parser") or {}
    print("\n--- 解析器健康度（RK-07）---")
    print(f"  解析器版本    : {pinfo.get('parser')} v{pinfo.get('version')}")
    print(f"  扫描行数      : {pinfo.get('scanned', 0)}（坏行 {pinfo.get('bad_lines', 0)}）")
    print(f"  含用量记录    : {pinfo.get('usage_lines', 0)}")
    print(f"  成功解析      : {pinfo.get('parsed', 0)}")
    rate = pinfo.get("success_rate")
    print("  解析成功率    : " + ("—（无样本）" if not rate else f"{float(rate):.2%}"))
    if pinfo.get("skipped"):
        print(f"  跳过原因      : {pinfo['skipped']}")

    ing = summary.get("ingest") or {}
    print("\n--- 写入结果 ---")
    print(f"  接收 {ing.get('received', 0)}  新增 {ing.get('inserted', 0)}  "
          f"更新 {ing.get('updated', 0)}  去重忽略 {ing.get('ignored', 0)}")
    if ing.get("unknown_models_new"):
        print(f"  首次出现的未识别模型: {ing['unknown_models_new']}")
    if ing.get("unknown_models"):
        print(f"  涉及未识别模型共 {len(ing['unknown_models'])} 种")

    cache = summary.get("cache")
    if cache:
        print(f"\n  缓存回填      : {cache['requests']} 行 / "
              f"{cache['cache_read_tokens']:,} tokens（{cache['sessions']} 个会话）")

    print("\n--- 告警巡检 ---")
    for key, value in (summary.get("alerts") or {}).items():
        print(f"  {key:<16}: {value}")

    with session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, SUM(cost_usd_micro) AS c1, "
            "SUM(COALESCE(equivalent_cost_usd_micro,0)) AS c2, "
            "SUM(COALESCE(vendor_credit_micro,0)) AS cred, "
            "SUM(cache_read_tokens) AS cr, "
            "MIN(ts_day) AS lo, MAX(ts_day) AS hi FROM usage_fact"
        ).fetchone()
        priced = conn.execute(
            "SELECT COUNT(*) AS c FROM usage_fact WHERE model <> '__unknown__'"
        ).fetchone()["c"]
        print(f"\n  库中总行数    : {row['c']}（已定价 {priced}，未定价 {row['c'] - priced}）")
        print(f"  厂商口径合计  : ${row['c1'] / 1e6:.4f}")
        print(f"  API 等价合计  : ${row['c2'] / 1e6:.4f}")
        print(f"  积分合计      : {row['cred'] / 1e6:.2f}")
        print(f"  缓存读合计    : {row['cr'] or 0:,} tokens")
        print(f"  日期范围      : {row['lo']} .. {row['hi']}")

        if row["cred"] and row["c2"]:
            suggested = (row["c2"] / 1e6) / (row["cred"] / 1e6)
            print(
                f"\n  建议积分折算率: 1 积分 ≈ ${suggested:.4f}\n"
                f"    （推导：API 等价合计 ÷ 积分合计。这是个**交叉核对值**，\n"
                f"      取决于上面那组占位价格是否正确，确认后用\n"
                f"      PUT /api/pricing/credit-rate 写入。）"
            )
    return 0


def _to_item(record) -> object:
    from app.models import IngestItem

    return IngestItem(**record.to_ingest_item())


def _ingest_via_http(records: list, args: argparse.Namespace) -> int:
    import httpx

    url = args.api.rstrip("/") + "/api/ingest"
    payload = {"items": [r.to_ingest_item() for r in records]}
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    print(f"\n--- HTTP 写入结果 ---")
    print(f"  接收 {data['received']}  新增 {data['inserted']}  "
          f"更新 {data['updated']}  忽略 {data['ignored']}")
    if data.get("unknown_models_new"):
        print(f"  首次出现的未识别模型: {data['unknown_models_new']}")
    return 0


# ------------------------------------------------------------------ verify
def cmd_verify(args: argparse.Namespace) -> int:
    """jsonl 逐条求和 vs traces 逐文件求和 交叉校验。

    踩过的坑：一个会话会对应**多个** trace 文件（按 worker 进程/时间窗滚动切分），
    早先的实现只取了其中一个（还用了 max），于是得出「jsonl 是 trace 的 5.8 倍」
    这种吓人的结论。实际上把同会话所有 trace 文件加起来，覆盖完整的会话是**逐位相等**的
    —— 差异全部来自 trace 文件被滚动清理，而不是求和算错。
    """
    jsonl_parser = WorkBuddyJsonlParser()
    per_session_jsonl: dict[str, dict[str, int]] = {}
    for record in jsonl_parser.parse_all():
        slot = per_session_jsonl.setdefault(record.session_id, {"n": 0, "in": 0, "out": 0, "cache": 0})
        slot["n"] += 1
        slot["in"] += record.input_tokens
        slot["out"] += record.output_tokens
        slot["cache"] += record.cache_read_tokens + record.cache_write_tokens

    trace_parser = WorkBuddyTraceParser()
    per_session_trace: dict[str, dict[str, int]] = {}
    trace_files: dict[str, int] = {}
    for path in trace_parser.discover():
        info = trace_parser.read_trace(path)
        if info is None:
            continue
        sid = info["session_id"]
        slot = per_session_trace.setdefault(
            sid, {"n": 0, "calls": 0, "in": 0, "out": 0, "cache": 0, "files": 0}
        )
        slot["n"] += 1
        slot["calls"] += info["call_count"]
        slot["in"] += info["total_input_tokens"]
        slot["out"] += info["total_output_tokens"]
        slot["cache"] += info["total_cached_tokens"]
        slot["files"] += 1
        trace_files[sid] = slot["files"]

    print(f"jsonl 侧会话数: {len(per_session_jsonl)}")
    print(f"trace 侧会话数: {len(per_session_trace)}")
    print(f"trace 文件数  : {sum(trace_files.values())}（同会话多文件需先求和再比）")

    common = sorted(set(per_session_jsonl) & set(per_session_trace))
    print(f"共同会话数    : {len(common)}\n")

    if not common:
        print("两侧没有可比的共同会话")
        print(f"\njsonl 侧 token 合计: {sum(v['in'] + v['out'] for v in per_session_jsonl.values()):,}")
        print(f"trace 侧 token 合计: {sum(v['in'] + v['out'] for v in per_session_trace.values()):,}")
        return 0

    header = (
        f"{'会话':<38} {'覆盖':>7} {'jsonl':>14} {'trace':>14} {'差异':>12} {'差异率':>9}"
    )
    print(header)
    print("-" * len(header))

    comparable = 0
    within = 0
    skipped: list[str] = []
    COVERAGE_MIN = 0.98

    for sid in common:
        j = per_session_jsonl[sid]
        t = per_session_trace[sid]
        j_total = j["in"] + j["out"]
        t_total = t["in"] + t["out"]
        coverage = (t["calls"] / j["n"]) if j["n"] else 0.0

        if coverage < COVERAGE_MIN:
            skipped.append(sid)
            print(
                f"{sid[:37]:<38} {coverage:>6.1%} {j_total:>14,} {t_total:>14,} "
                f"{'—':>12} {'不可比':>9}  trace 仅留存 {t['calls']}/{j['n']} 次调用"
            )
            continue

        diff = j_total - t_total
        rate = (abs(diff) / t_total) if t_total else 0.0
        flag = "OK" if rate <= 0.02 else "!!"
        comparable += 1
        if rate <= 0.02:
            within += 1
        print(f"{sid[:37]:<38} {coverage:>6.1%} {j_total:>14,} {t_total:>14,} {diff:>+12,} {rate:>8.2%} {flag}")

    # trace 是会话级汇总，这里把两侧的缓存情况报出来做参照
    print()
    cached = sum(v["cache"] for v in per_session_trace.values())
    t_in = sum(v["in"] for v in per_session_trace.values())
    if t_in:
        print(f"trace 侧缓存读: {cached:,} / 输入 {t_in:,} = {cached / t_in:.1%}")
        print("（jsonl 侧自 P0-A 起有请求级直报缓存读 prompt_cache_hit_tokens，")
        print(" 与 trace 会话级汇总可互相印证；trace 留存不全时以 jsonl 直报为准）")

    print()
    print(f"可比会话（trace 覆盖 ≥{COVERAGE_MIN:.0%}）: {comparable}/{len(common)}")
    print(f"其中差异 ≤2%           : {within}/{comparable}")
    if skipped:
        print(f"不予比较（trace 留存不全）: {len(skipped)} 个会话")
    print(f"jsonl 侧合计: {sum(v['in'] + v['out'] for v in per_session_jsonl.values()):,}")
    print(f"trace 侧合计: {sum(v['in'] + v['out'] for v in per_session_trace.values()):,}")

    if comparable and within < comparable:
        print("\nVERIFY: FAIL  存在可比的会话但差异超过 2%，说明求和对不上")
        return 1
    if not comparable:
        print("\nVERIFY: 无法判定（所有共同会话的 trace 留存都不完整）")
        return 2
    print("\nVERIFY: PASS  所有可比会话的逐条求和与 trace 逐位一致")
    return 0


# ------------------------------------------------------------------ main
def cmd_backfill_cache(args: argparse.Namespace) -> int:
    """从 traces 回填缓存 token（B-1：jsonl 不含缓存字段）。

    默认只演练不写，加 ``--apply`` 才落库。
    """
    from app import db
    from app.sources.trace_cache import backfill

    apply = bool(getattr(args, "apply", False))
    with db.session() as conn:
        stats = backfill(conn, apply=apply)
        if not apply:
            conn.rollback()

    mode = "已写入" if apply else "演练（加 --apply 才落库）"
    print(f"== 缓存 token 回填 · {mode} ==")
    print(f"trace 覆盖会话：{stats['sessions']} 个")
    print(f"待补记录：{stats['requests']} 行")
    print(f"缓存读合计：{stats['cache_read_tokens']:,} tokens")
    print(f"其中片段覆盖（低可信）：{stats['low_coverage_sessions']} 个会话")
    if stats["samples"]:
        print("\n样本（按会话）：")
        print(f"  {'会话':<38}{'traces':>7}{'缓存率':>8}{'覆盖率':>8}{'记录':>7}")
        for s in stats["samples"]:
            flag = "" if s["trusted"] else "  ← 片段覆盖"
            print(
                f"  {s['session_id']:<38}{s['trace_files']:>7}"
                f"{s['rate_pct']:>7.1f}%{s['coverage_pct']:>7.1f}%{s['requests']:>7}{flag}"
            )
    print(
        "\n注意：命中率按**有 trace 支撑的请求**计算。若用全库输入做分母，"
        "未覆盖的请求会把数字压低到失真。"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 用量灌数（只读原始数据）")
    ap.add_argument("--inspect", action="store_true", help="只统计字段与分布，不写任何东西")
    ap.add_argument("--dry-run", action="store_true", help="解析并打印归一结果，不落库")
    ap.add_argument("--ingest", action="store_true", help="真正写入数据库（默认全量；配 --incremental 走水位增量）")
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="增量采集：只读文件水位后的新增行（常驻服务补采用；--ingest 默认全量）",
    )
    ap.add_argument("--verify", action="store_true", help="与 traces 交叉校验")
    ap.add_argument(
        "--backfill-cache",
        action="store_true",
        help="从 traces 回填缓存 token（默认演练，配 --apply 才落库）",
    )
    ap.add_argument("--apply", action="store_true", help="配合 --backfill-cache 真正写入")
    ap.add_argument("--since", default=None, help="只处理该日期之后的记录（YYYY-MM-DD）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条（配合 --dry-run）")
    ap.add_argument("--api", default=None, help="改为 POST 到这个地址，如 http://127.0.0.1:8787")
    ap.add_argument(
        "--apply-placeholder-prices",
        action="store_true",
        help="写入显式占位价格让成本链路可验证（不是真实单价，详见脚本内注释）",
    )
    ap.add_argument(
        "--placeholder-file",
        default=None,
        help="占位价格配置路径；缺省优先 app/pricing/placeholder.local.json，再退到脱敏示例",
    )
    args = ap.parse_args()

    if not any([args.inspect, args.dry_run, args.ingest, args.verify, args.backfill_cache]):
        args.inspect = True

    if args.backfill_cache:
        return cmd_backfill_cache(args)
    if args.inspect:
        return cmd_inspect(args)
    if args.dry_run:
        return cmd_dry_run(args)
    if args.verify:
        return cmd_verify(args)
    return cmd_ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
