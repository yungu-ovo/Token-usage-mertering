"""试点验收：把「跑通」的七条定义做成可重复执行的检查。

对应计划 §十三「试点验收七条」。设计原则：

* **不碰生产库**：需要写数据的检查（去重、价格版本、别名归一）在临时复制的库上做；
  只读检查（端到端、双口径、隐私、聚合正确性）打真实服务与真实库。
* **可重复**：跑多少次结论都一样，不依赖执行顺序，不留副作用。
* **不当老好人**：任何一条不过就退出码非零，并写进报告。

用法::

    python scripts/pilot_acceptance.py
    python scripts/pilot_acceptance.py --base http://127.0.0.1:8787
    python scripts/pilot_acceptance.py --report data/试点验收报告.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable


# ------------------------------------------------------------------ 小工具
class Result:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, no: int, name: str, ok: bool, evidence: str, note: str = "") -> None:
        self.items.append(
            {"no": no, "name": name, "ok": bool(ok), "evidence": evidence, "note": note}
        )
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {no}. {name}")
        print(f"        {evidence}")
        if note:
            print(f"        {note}")

    @property
    def failed(self) -> list[dict]:
        return [i for i in self.items if not i["ok"]]


def opener():
    # 本机设了 HTTP_PROXY，直连 127.0.0.1 会被代理拦成 502
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(op, url: str, timeout: float = 15.0):
    with op.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def temp_db_copy(src: Path):
    """复制一份库用于可写检查，返回 (临时目录, 库路径)。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="tum-accept-"))
    dst = tmpdir / "usage.db"
    shutil.copy2(src, dst)
    # WAL 副文件也要带上，否则可能丢掉最近的写入
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return tmpdir, dst


# ------------------------------------------------------------------ 1 端到端
def check_end_to_end(r: Result, base: str, db: Path, op) -> dict:
    from app import config
    from app.sources.workbuddy_jsonl import WorkBuddyJsonlParser

    parser = WorkBuddyJsonlParser()
    files = parser.discover()
    jsonl_records = list(parser.parse_all())

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    uniq = conn.execute("SELECT COUNT(DISTINCT request_id) AS c FROM usage_fact").fetchone()["c"]
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
    conn.close()

    kpi = get_json(op, f"{base}/api/kpi")
    trend = get_json(op, f"{base}/api/trend?days=30")
    mtd = Decimal(kpi["north_star"]["mtd_cost"])

    ok = (
        len(files) > 0
        and len(jsonl_records) > 0
        and rows > 0
        and rows == uniq
        and meta.get("data_source") == "workbuddy"
        and mtd > 0
        and trend["total_cost"] != "0.00"
    )
    r.add(
        1,
        "端到端（jsonl → 解析 → 落库 → 聚合 → 接口）",
        ok,
        f"源文件 {len(files)} 个 / 解析 {len(jsonl_records):,} 条 / 落库 {rows:,} 行"
        f"（去重后唯一 {uniq:,}）/ data_source={meta.get('data_source')!r} / 本月成本 ${mtd}",
        "" if rows == uniq else "存在重复 request_id，说明去重没生效",
    )
    return {"rows": rows, "records": len(jsonl_records), "mtd": str(mtd)}


# ------------------------------------------------------------------ 2 RK-02 去重
def check_dedup(r: Result, src_db: Path) -> None:
    from app import config, db
    from app.ingest import ingest_items
    from app.models import IngestItem

    tmpdir, copy = temp_db_copy(src_db)
    saved = config.DB_PATH
    config.DB_PATH = copy
    try:
        # 同一 request_id 依次上报 output 100 → 300 → 200（流式写入的典型形态：
        # 同一条消息被反复落盘，token 数只增不减，中间还可能收到比已有值更小的快照）
        dup_id = "accept-dup"
        items = [
            IngestItem.model_validate(
                {
                    "request_id": dup_id,
                    "ts": "2026-10-09T12:00:00+08:00",
                    "provider": "workbuddy",
                    "model": "gpt-5",
                    "session_id": "accept-sess",
                    "usage": {"input_tokens": 10_000, "output_tokens": out},
                }
            )
            for out in (100, 300, 200)
        ]
        with db.session(copy) as conn:
            before = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
        with db.session(copy) as conn:
            ingest_items(conn, items)
        with db.session(copy) as conn:
            after = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
            kept = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_fact WHERE request_id = ?", (dup_id,)
            ).fetchone()["c"]
            out = conn.execute(
                "SELECT output_tokens FROM usage_fact WHERE request_id = ?", (dup_id,)
            ).fetchone()["output_tokens"]

        # 再原样重放一遍，必须是幂等 no-op
        with db.session(copy) as conn:
            ingest_items(conn, items)
        with db.session(copy) as conn:
            replay = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    finally:
        config.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok = after == before + 1 and kept == 1 and out == 300 and replay == after
    r.add(
        2,
        "RK-02 幂等去重（同 messageId 重放）",
        ok,
        f"同一 id 三次上报（output 100→300→200）后总行数 {before:,} → {after:,}"
        f"（+{after - before}，应为 +1），该 id 占 {kept} 行（应为 1）；"
        f"保留 output={out}（应为 300，即取最大值不取最后值）；原样重放后 {replay:,} 行（应不变）",
        "即同一条消息被反复落盘、或收到更小的快照，都不会重复计费也不会把已计的数字改小",
    )


# ------------------------------------------------------------------ 3 RK-03 别名归一
def check_alias(r: Result, src_db: Path) -> None:
    from app import config, db
    from app.ingest import ingest_items
    from app.models import IngestItem

    tmpdir, copy = temp_db_copy(src_db)
    saved = config.DB_PATH
    config.DB_PATH = copy
    try:
        with db.session(copy) as conn:
            result = ingest_items(
                conn,
                [
                    IngestItem.model_validate(
                        {
                            "request_id": f"accept-unknown-{i}",
                            "ts": "2026-10-09T12:00:00+08:00",
                            "provider": "workbuddy",
                            "model": name,
                            "session_id": "accept-sess2",
                            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000},
                        }
                    )
                    for i, name in enumerate(["某某不存在模型-v9", "Another Made Up Model"])
                ],
            )
        with db.session(copy) as conn:
            rows = conn.execute(
                "SELECT model, model_raw, cost_usd_micro, equivalent_cost_usd_micro, cost_source "
                "FROM usage_fact WHERE request_id LIKE 'accept-unknown-%' ORDER BY request_id"
            ).fetchall()
            ledger = conn.execute(
                "SELECT model_raw, hit_count FROM unknown_model ORDER BY model_raw"
            ).fetchall()
            insight = None
    finally:
        config.DB_PATH = saved

    # 前端/洞察侧：在临时库上跑一次洞察构建
    saved = config.DB_PATH
    config.DB_PATH = copy
    try:
        from app.queries import insights

        with db.session(copy) as conn:
            payload = insights.build(conn)
            insight = next(
                (i for i in payload["insights"] if i["id"] == "unpriced_models"), None
            )
    finally:
        config.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)

    all_unknown = all(r["model"] == "__unknown__" for r in rows)
    zero_cost = all(r["cost_usd_micro"] == 0 and r["equivalent_cost_usd_micro"] is None for r in rows)
    raw_kept = all(r["model_raw"] for r in rows)
    ledger_ok = len(ledger) >= 2
    ok = (
        len(rows) == 2
        and all_unknown
        and zero_cost
        and raw_kept
        and ledger_ok
        and insight is not None
        and result["unknown_models"]
    )
    r.add(
        3,
        "RK-03 别名归一（未识别模型不静默计费）",
        ok,
        f"2 条未知模型全部归入 __unknown__（{all_unknown}）；成本记 0 且 API 等价为 None（{zero_cost}）；"
        f"原始名保留（{raw_kept}）；台账 {len(ledger)} 条；洞察已生成（{insight is not None}）",
        "处置：既不猜单价也不丢弃，而是记为 0 + 台账 + 洞察提示补价格表",
    )


# ------------------------------------------------------------------ 4 RK-01 价格版本
def check_pricing_history(r: Result, src_db: Path) -> None:
    from app import config, db
    from app.ingest import ingest_items
    from app.models import IngestItem
    from app.pricing import override

    tmpdir, copy = temp_db_copy(src_db)
    saved = config.DB_PATH
    config.DB_PATH = copy
    try:
        with db.session(copy) as conn:
            ingest_items(
                conn,
                [
                    IngestItem.model_validate(
                        {
                            "request_id": "accept-price-1",
                            "ts": "2026-10-05T12:00:00+08:00",
                            "provider": "workbuddy",
                            "model": "gpt-5",
                            "session_id": "accept-sess3",
                            "usage": {"input_tokens": 1_000_000},
                        }
                    )
                ],
            )
        with db.session(copy) as conn:
            before = conn.execute(
                "SELECT cost_usd_micro, equivalent_cost_usd_micro, pricing_version "
                "FROM usage_fact WHERE request_id = 'accept-price-1'"
            ).fetchone()

        # 涨价 10 倍，从 2026-10-09 生效；不重启进程也应不影响历史
        with db.session(copy) as conn:
            override(conn, "gpt-5", "12.50", "100.00", "1.25", "12.50", effective_from="2026-10-09")

        with db.session(copy) as conn:
            after = conn.execute(
                "SELECT cost_usd_micro, equivalent_cost_usd_micro, pricing_version "
                "FROM usage_fact WHERE request_id = 'accept-price-1'"
            ).fetchone()
            versions = [
                r["pricing_version"]
                for r in conn.execute(
                    "SELECT DISTINCT pricing_version FROM price_table WHERE model='gpt-5'"
                ).fetchall()
            ]
            new_row = conn.execute(
                "SELECT equivalent_cost_usd_micro FROM usage_fact WHERE request_id='accept-price-1'"
            ).fetchone()

        # 新记录按新价
        with db.session(copy) as conn:
            ingest_items(
                conn,
                [
                    IngestItem.model_validate(
                        {
                            "request_id": "accept-price-2",
                            "ts": "2026-10-09T12:00:00+08:00",
                            "provider": "workbuddy",
                            "model": "gpt-5",
                            "session_id": "accept-sess3",
                            "usage": {"input_tokens": 1_000_000},
                        }
                    )
                ],
            )
        with db.session(copy) as conn:
            fresh = conn.execute(
                "SELECT equivalent_cost_usd_micro FROM usage_fact WHERE request_id = 'accept-price-2'"
            ).fetchone()
    finally:
        config.DB_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)

    history_intact = (before["equivalent_cost_usd_micro"] == after["equivalent_cost_usd_micro"]
                      and before["pricing_version"] == after["pricing_version"])
    old_kept = "builtin-2026.10.1" in versions
    new_applied = fresh["equivalent_cost_usd_micro"] == 12_500_000
    ok = history_intact and old_kept and new_applied
    r.add(
        4,
        "RK-01 价格表版本化（调价不改写历史）",
        ok,
        f"历史记录价格版本 {before['pricing_version']} 与成本 {before['equivalent_cost_usd_micro']} 微美元"
        f" → 调价 10 倍后仍为 {after['equivalent_cost_usd_micro']} 微美元（{history_intact}）；"
        f"老版本行保留（{old_kept}）；2026-10-09 的新记录按新价 {fresh['equivalent_cost_usd_micro']} 微美元（{new_applied}）",
        "实现：override 只新增版本并回填旧版本 effective_to，永不 UPDATE 已有行",
    )


# ------------------------------------------------------------------ 5 RK-05 双口径
def check_dual_caliber(r: Result, op, base: str) -> None:
    kpi = get_json(op, f"{base}/api/kpi")
    ns = kpi["north_star"]
    has_both = "mtd_cost" in ns and "vendor_cost" in ns
    differ = ns["mtd_cost"] != ns["vendor_cost"]
    estimated_flagged = isinstance(ns.get("estimated"), bool) and "estimate_method" in ns
    credit_uncalibrated = kpi["credit_rate"]["calibrated"] is False
    ok = has_both and estimated_flagged and "cost_caliber_label" in ns
    r.add(
        5,
        "RK-05 双口径（API 等价 + 厂商积分折算）",
        ok,
        f"mtd_cost=${ns['mtd_cost']}（{ns['cost_caliber_label']}） / vendor_cost=${ns['vendor_cost']}"
        f"（{ns['vendor_cost_label']}）/ 两者不同={differ} / 未校准标记={credit_uncalibrated} / "
        f"预测带估算标注={estimated_flagged}",
        f"估算方法：{ns.get('estimate_method')}",
    )


# ------------------------------------------------------------------ 6 RK-04 隐私
def check_privacy(r: Result, db: Path) -> None:
    code, out = run([PY, "scripts/check_privacy.py", "--db", str(db)])
    tail = [ln for ln in out.strip().splitlines() if ln.startswith(("PRIVACY", "[FAIL]", "[PASS]"))]
    ok = code == 0 and any("PRIVACY: PASS" in ln for ln in tail)
    # 从 "PRIVACY: PASS  12/12 项通过" 里取出 "12/12"
    total = "?"
    for ln in tail:
        if "PRIVACY: PASS" in ln:
            hit = re.search(r"(\d+/\d+)", ln)
            if hit:
                total = hit.group(1)
            break
    r.add(
        6,
        "RK-04 隐私（库中无对话正文）",
        ok,
        f"check_privacy.py 退出码 {code}；{total} 项检查通过"
        + ("" if ok else f"；输出：{tail[-3:]}"),
        "检查覆盖：dataclass 结构 / 白名单 / 解析器源码 / 只读约束 / 文本列取值长度 / 正文特征词",
    )


# ------------------------------------------------------------------ 7 聚合正确性
def check_aggregation(r: Result) -> None:
    code, out = run([PY, "scripts/ingest_workbuddy.py", "--verify"], timeout=600)
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    verdict = next((ln for ln in lines if ln.startswith("VERIFY:")), "")
    comparable = next((ln for ln in lines if "可比会话" in ln), "")
    within = next((ln for ln in lines if "差异 ≤2%" in ln), "")
    cache_line = next((ln for ln in lines if "trace 侧缓存读" in ln), "")
    # 源脚本的输出为了对齐表格留了多余空格，报告里压掉
    comparable = re.sub(r"\s{2,}", " ", comparable).strip()
    within = re.sub(r"\s{2,}", " ", within).strip()
    ok = code == 0 and "PASS" in verdict
    r.add(
        7,
        "聚合正确性（jsonl 逐条求和 vs traces）",
        ok,
        "；".join(x for x in (verdict, comparable, within) if x),
        cache_line.rstrip("；; ") if cache_line else "",
    )


# ------------------------------------------------------------------ 报告
def write_report(path: Path, results: Result, meta: dict, base: str) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed = len(results.items) - len(results.failed)
    lines = [
        "# Token 消耗看板 · WorkBuddy 试点验收报告",
        "",
        f"- 执行时间：{now}",
        f"- 服务地址：{base}",
        f"- 数据来源：本机 `.workbuddy` 会话转录（只读解析，未访问任何远端接口）",
        f"- 结论：**{'全部通过' if not results.failed else f'{len(results.failed)} 项未通过'}**"
        f"（{passed}/{len(results.items)}）",
        "",
        "## 逐条结果",
        "",
        "| # | 验证项 | 结论 | 证据 |",
        "| --- | --- | --- | --- |",
    ]
    for item in results.items:
        lines.append(
            f"| {item['no']} | {item['name']} | {'通过' if item['ok'] else '**未通过**'} | {item['evidence']} |"
        )
    lines += ["", "## 备注", ""]
    for item in results.items:
        if item["note"]:
            lines.append(f"- **{item['no']}. {item['name']}**：{item['note']}")
    lines += [
        "",
        "## 试点规模",
        "",
        f"- 解析记录：{meta.get('records', '?')} 条",
        f"- 落库行数：{meta.get('rows', '?')} 行",
        f"- 本月累计（API 等价口径）：${meta.get('mtd', '?')}",
        "",
        "## 已知限制（不作为验收项，供后续排期）",
        "",
        "- jsonl 转录**不含缓存 token 字段**，因此库中 `cache_read_tokens` 恒为 0，"
        "看板的缓存命中率/缓存节省两项处于「未上报」状态。",
        "  交叉校验时发现 `traces/*.json` 里有 `totalCachedTokens`，样本显示缓存读占输入约 95%。"
        "若要把缓存指标做真，需要把 traces 作为会话级的补充数据源接入（注意与 jsonl 的口径差异，避免重复计数）。",
        "- 会话转录只保留本机历史，30 天窗口内仅 7 天有数据，趋势图存在真实断点。",
        "- WorkBuddy 的模型名均为内部代号，内置公共价格表无法匹配；当前成本数字基于"
        "**显式标注的占位价格**（`--apply-placeholder-prices`），仅用于验证成本链路，不代表真实账单。",
        "",
        "---",
        "",
        "本报告由 `scripts/pilot_acceptance.py` 自动生成，可重复执行且不修改生产库。",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 试点验收（七条）")
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--db", default=str(ROOT / "data" / "usage.db"))
    ap.add_argument("--report", default=str(ROOT / "data" / "试点验收报告.md"))
    args = ap.parse_args()

    base = args.base.rstrip("/")
    db = Path(args.db)
    op = opener()

    print(f"WorkBuddy 试点验收  base={base}  db={db}\n" + "=" * 72)

    try:
        get_json(op, f"{base}/api/health", timeout=5)
    except (urllib.error.URLError, OSError) as exc:
        print(f"\n服务不可达（{base}）：{exc}")
        print("请先启动：python -m uvicorn app.main:app --port 8787")
        return 1

    results = Result()
    meta = check_end_to_end(results, base, db, op)
    check_dedup(results, db)
    check_alias(results, db)
    check_pricing_history(results, db)
    check_dual_caliber(results, op, base)
    check_privacy(results, db)
    check_aggregation(results)

    write_report(Path(args.report), results, meta, base)

    print("=" * 72)
    bad = results.failed
    if bad:
        print(f"ACCEPTANCE: FAIL  {len(bad)}/{len(results.items)} 项未通过")
        for item in bad:
            print(f"  - {item['no']}. {item['name']}：{item['evidence']}")
        print(f"\n报告已写入 {args.report}")
        return 1
    print(f"ACCEPTANCE: PASS  {len(results.items)}/{len(results.items)} 项全部通过")
    print(f"报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
