"""接口冒烟：逐个打全部 API，校验响应契约。

用法::

    python scripts/smoke.py                # 默认打 http://127.0.0.1:8787
    python scripts/smoke.py --base http://127.0.0.1:9000

为什么要自己写 HTTP 客户端：本机设了 ``HTTP_PROXY``，直连 127.0.0.1 会被
代理拦成 502，所以这里显式禁用代理。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any

# 每个 API 必须出现在响应里的顶层字段（契约，前端依赖这些字段）
REQUIRED_KEYS: dict[str, set[str]] = {
    "/api/meta": {"generated_at", "data_source", "pricing_version", "credit_rate"},
    "/api/kpi": {"generated_at", "north_star", "today", "tokens", "cache", "per_request", "health"},
    "/api/trend": {"generated_at", "points", "total_cost", "daily_budget"},
    "/api/tokens": {"generated_at", "days", "points", "totals"},
    "/api/mix": {"generated_at", "dim", "items", "total_cost"},
    "/api/insights": {"generated_at", "insights", "total_cost"},
    "/api/requests": {"generated_at", "total", "items", "sum_cost"},
    "/api/budget": {"daily_usd", "monthly_usd", "currency"},
    "/api/pricing": {"current_version", "models", "unknown_models"},
    "/api/currency": {"supported", "rates"},
    "/api/alerts": {"items"},
}

SIMPLE_GETS = ["/api/health", *REQUIRED_KEYS.keys()]


def _opener() -> urllib.request.OpenerDirector:
    # 关键：空 ProxyHandler 表示不走任何代理
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(
    opener,
    url: str,
    timeout: float = 10.0,
    method: str = "GET",
    body: Any = None,
) -> tuple[int, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body_text)
        except json.JSONDecodeError:
            return exc.code, body_text
    except urllib.error.URLError as exc:
        return 0, f"{type(exc.reason).__name__}: {exc.reason}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--quiet", action="store_true", help="只输出不通过的接口")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    opener = _opener()
    failures: list[str] = []
    checks = 0

    def report(ok: bool, label: str, note: str = "") -> None:
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(label)
        if not args.quiet or not ok:
            print(f"[{'PASS' if ok else 'FAIL'}] {label:<46} {note}")

    # ---------------------------------------------------------- 只读接口
    for path in SIMPLE_GETS:
        status, payload = fetch(opener, base + path)
        ok = status == 200 and isinstance(payload, dict)
        note = str(status)
        if ok:
            missing = REQUIRED_KEYS.get(path, set()) - set(payload)
            if missing:
                ok, note = False, f"缺字段 {sorted(missing)}"
            else:
                note = str(status)
        else:
            note = f"{status} {str(payload)[:150]}"
        report(ok, f"GET {path}", note)

    # ---------------------------------------------------------- 带参数接口
    param_checks = [
        ("/api/requests?limit=3&offset=0", lambda p: isinstance(p.get("items"), list) and len(p["items"]) <= 3),
        ("/api/requests?model=gpt-5", lambda p: isinstance(p.get("items"), list)),
        ("/api/requests?status=error", lambda p: isinstance(p.get("items"), list)),
        ("/api/trend?days=7", lambda p: len(p.get("points", [])) == 7),
        ("/api/tokens?days=7", lambda p: len(p.get("points", [])) == 7),
        ("/api/mix?dim=project", lambda p: p.get("dim") == "project"),
        ("/api/mix?dim=tool", lambda p: p.get("dim") == "tool"),
        ("/api/mix?dim=session&top=3", lambda p: p.get("dim") == "session"),
        ("/api/drill/gpt-5/sessions", lambda p: isinstance(p.get("items"), list)),
        ("/api/alerts?limit=5", lambda p: isinstance(p.get("items"), list)),
    ]
    for path, predicate in param_checks:
        status, payload = fetch(opener, base + path)
        ok = status == 200 and isinstance(payload, dict) and predicate(payload)
        report(ok, f"GET {path}", str(status))

    # ---------------------------------------------------------- 口径一致性
    # 三个出口必须报同一个数，所以必须用**同一个时间窗**（30 天）。
    # 任何一个漏了微美元换算，汇总就会比明细大 100 万倍。
    _, mix_payload = fetch(opener, base + "/api/mix?dim=model&days=30")
    _, req_payload = fetch(opener, base + "/api/requests?days=30&limit=1")
    _, trend_payload = fetch(opener, base + "/api/trend?days=30")
    if all(isinstance(p, dict) for p in (mix_payload, req_payload, trend_payload)):
        numbers = {
            "mix.total_cost": mix_payload["total_cost"],
            "requests.sum_cost": req_payload["sum_cost"],
            "trend.total_cost": trend_payload["total_cost"],
        }
        report(
            len(set(numbers.values())) == 1,
            "口径一致性 mix / requests / trend(30d)",
            json.dumps(numbers, ensure_ascii=False),
        )
        # 构成区 share 必须归一
        shares = sum(r.get("share", 0) for r in mix_payload.get("items", []))
        report(
            not mix_payload.get("items") or abs(shares - 1.0) < 1e-6,
            "构成区 share 归一到 1",
            f"sum={shares:.6f}",
        )
        # 构成区各项成本之和必须等于总额
        item_sum = sum(Decimal(r["cost"]) for r in mix_payload.get("items", []))
        report(
            item_sum == Decimal(mix_payload["total_cost"]),
            "构成区各项之和 = 总额",
            f"{item_sum} vs {mix_payload['total_cost']}",
        )
        # 折线各点之和必须等于总额
        point_sum = sum(Decimal(p["cost"]) for p in trend_payload.get("points", []))
        report(
            point_sum == Decimal(trend_payload["total_cost"]),
            "折线各点之和 = 总额",
            f"{point_sum} vs {trend_payload['total_cost']}",
        )

    # ---------------------------------------------------------- 写接口往返
    status, before = fetch(opener, base + "/api/budget")
    if status == 200 and isinstance(before, dict):
        original_daily = before["daily_usd"]
        original_monthly = before["monthly_usd"]
        probe = "12.34"
        put_status, put_body = fetch(
            opener,
            base + "/api/budget",
            method="PUT",
            body={"daily_usd": probe, "monthly_usd": original_monthly},
        )
        get_status, after = fetch(opener, base + "/api/budget")
        round_trip = (
            put_status == 200
            and get_status == 200
            and isinstance(after, dict)
            and str(after.get("daily_usd")) == probe
        )
        report(round_trip, "PUT/GET /api/budget 往返", f"{put_status}/{get_status} {str(put_body)[:80]}")

        # 还原，避免冒烟脚本留下副作用
        fetch(
            opener,
            base + "/api/budget",
            method="PUT",
            body={"daily_usd": original_daily, "monthly_usd": original_monthly},
        )
        _, restored = fetch(opener, base + "/api/budget")
        report(
            isinstance(restored, dict) and str(restored.get("daily_usd")) == str(original_daily),
            "还原 /api/budget",
            f"daily={restored.get('daily_usd') if isinstance(restored, dict) else restored}",
        )
    else:
        report(False, "GET /api/budget", f"{status} {str(before)[:120]}")

    # ---------------------------------------------------------- 只读接口不可写
    status, _ = fetch(opener, base + "/api/kpi", method="PUT", body={})
    report(status in (404, 405), "PUT /api/kpi 应被拒绝", str(status))

    # ---------------------------------------------------------- 告警（C-1）
    # 修复前告警引擎一直在写库，但前端从不调这个接口 —— P0 的「预算与告警」
    # 在界面上是不闭环的。这里把契约钉死：列表、确认、以及未确认过滤。
    status, body = fetch(opener, base + "/api/alerts?limit=5")
    report(
        status == 200 and isinstance(body.get("items"), list),
        "GET /api/alerts 返回列表",
        f"{status} {len(body.get('items', [])) if isinstance(body, dict) else '?'} 条",
    )
    status, body = fetch(opener, base + "/api/alerts?unacked=true&limit=5")
    items = body.get("items", []) if isinstance(body, dict) else []
    report(
        status == 200 and all(not i.get("acknowledged") for i in items),
        "GET /api/alerts?unacked=true 只返回未确认项",
        f"{len(items)} 条",
    )
    if items:
        first = items[0]
        ack_id = first.get("id")
        status, body = fetch(opener, base + f"/api/alerts/{ack_id}/ack", method="POST")
        report(
            status == 200 and body.get("acknowledged") is True,
            "POST /api/alerts/{id}/ack 确认告警",
            f"id={ack_id} → {status}",
        )
        # 确认后不该再出现在 unacked 列表里
        _, after = fetch(opener, base + "/api/alerts?unacked=true&limit=50")
        after_ids = {i.get("id") for i in (after.get("items", []) if isinstance(after, dict) else [])}
        report(ack_id not in after_ids, "已确认告警不再出现在未确认列表", f"id={ack_id}")
    else:
        report(True, "当前无未确认告警，跳过 ack 往返")
        report(True, "当前无未确认告警，跳过 ack 后复查")
    status, _ = fetch(opener, base + "/api/alerts/999999/ack", method="POST")
    report(status == 404, "确认不存在的告警应404", str(status))

    # ---------------------------------------------------------- 缓存筛选（C-3）
    # 洞察卡的 filter_cache 动作现在真去筛，这个参数必须被后端认下来
    status, body = fetch(opener, base + "/api/requests?cache=read&limit=5")
    ok = status == 200 and isinstance(body.get("items"), list)
    if ok:
        ok = all(int(i.get("cache_read_tokens", 0)) > 0 for i in body["items"])
    report(ok, "GET /api/requests?cache=read 只返回有缓存读的记录", f"{status} {len(body.get('items', [])) if isinstance(body, dict) else '?'} 条")
    status, _ = fetch(opener, base + "/api/requests?cache=bogus")
    report(status == 422, "cache 参数走白名单校验", str(status))

    # ---------------------------------------------------------- 口径字段（B-2）
    status, kpi_body = fetch(opener, base + "/api/kpi?days=7")
    detail = kpi_body.get("budget_detail") if isinstance(kpi_body, dict) else None
    report(
        status == 200
        and isinstance(detail, dict)
        and "daily_usd" in detail
        and "monthly_usd" in detail,
        "GET /api/kpi 返回 budget_detail（供界面回填预算）",
        str(detail)[:90],
    )
    report(
        status == 200 and isinstance(kpi_body.get("placeholder_prices"), list),
        "GET /api/kpi 返回 placeholder_prices（占位价格提示）",
        str(kpi_body.get("placeholder_prices"))[:90] if isinstance(kpi_body, dict) else "",
    )

    # ---------------------------------------------------------- 金额口径一致性
    # 告警文案里的金额与 KPI 必须是同一口径的同一量级。
    # 修复前库里残留过「本月预算已用 3663766.7%」这种被放大 1e6 倍的历史告警，
    # 说明告警层曾把微美元当美元用；这条断言防止它再发生。
    _, alerts_all = fetch(opener, base + "/api/alerts?limit=50")
    mtd = kpi_body.get("mtd_cost") if isinstance(kpi_body, dict) else None
    if mtd and alerts_all.get("items"):
        real = float(mtd)
        bad = []
        for a in alerts_all["items"]:
            if not str(a.get("message", "")).startswith("本月预算已用"):
                continue
            import re as _re

            m = _re.search(r"（\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*/", a["message"])
            if not m:
                continue
            claimed = float(m.group(1).replace(",", ""))
            if real > 0 and claimed > real * 1000:
                bad.append(a["id"])
        report(not bad, "告警金额与 KPI 同口径（无1e6 量级放大）", f"KPI=${real} 异常={bad}")

    # ---------------------------------------------------------- 前端页面
    # 根路径应重定向到看板，且页面必须真的去调 API（不是硬编码假数据）
    try:
        with opener.open(base + "/", timeout=10) as resp:
            page = resp.read().decode("utf-8")
            final_url = resp.geturl()
        report(
            resp.status == 200 and "/dashboard.html" in final_url,
            "GET / 重定向到看板",
            final_url,
        )
        report(
            "/api/kpi" in page and "/api/trend" in page and "<?xml" not in page,
            "看板页面调用真实 API",
            f"{len(page)} 字节",
        )
        for ghost in ("MODEL_SHARE", "PROJ_SHARE", "BLENDED", "var ROWS"):
            report(ghost not in page, f"页面已无硬编码假数据 {ghost}")
    except Exception as exc:  # noqa: BLE001
        report(False, "GET / 看板页面", f"{type(exc).__name__}: {exc}")

    print()
    if failures:
        print(f"SMOKE: FAIL  {len(failures)}/{checks} 项不通过")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"SMOKE: PASS  {checks}/{checks} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
