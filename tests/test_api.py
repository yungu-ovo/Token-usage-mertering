"""接口层：用真实 TestClient 打一遍，校验响应契约与几个易错点。

为什么不用直调路由函数：不带参数直调时拿到的是未解析的 ``Query`` 默认对象
（``days`` 会是 Query 实例而不是 int），既失真又容易误判。
``TestClient`` 走完整 FastAPI 参数解析，行为与线上一致。
真正的跨进程 HTTP 行为由 ``scripts/smoke.py`` 覆盖。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.ingest import ingest_items
from app.models import IngestItem

TODAY = dt.date.today().isoformat()
TS = f"{TODAY}T10:00:00+08:00"
ENVELOPE = {"generated_at", "pricing_version", "data_source", "credit_rate"}


@pytest.fixture()
def client(dbpath):
    """不进入 with 块 => 不触发 lifespan，避免测试期启动调度器。

    dbpath 夹具已经建库并播种了内置价格表。
    """
    return TestClient(main.app)


@pytest.fixture()
def client_wb(dbpath):
    with db.session(dbpath) as conn:
        db.set_meta(conn, "data_source", "workbuddy")
    return TestClient(main.app)


# ------------------------------------------------------------------ meta
def test_meta_reports_workbuddy_source(client_wb):
    """回归：曾经把默认值 'demo' 当成 key 传进 get_meta，导致 data_source 恒为空串。"""
    payload = client_wb.get("/api/meta").json()
    assert payload["data_source"] == "workbuddy"
    assert payload["app"]
    assert payload["pricing_version"] == "builtin-2026.10.1"
    assert payload["credit_rate"]["calibrated"] is False


def test_meta_data_source_key_actually_exists(client_wb):
    payload = client_wb.get("/api/meta").json()
    assert payload["data_source"] != "", "data_source 不该是空串——说明读错了 key"


def test_health(client):
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert isinstance(payload["usage_fact_rows"], int)


# ------------------------------------------------------------------ 信封一致性
@pytest.mark.parametrize(
    "path",
    ["/api/kpi", "/api/trend", "/api/tokens", "/api/mix", "/api/insights", "/api/requests"],
)
def test_dashboard_endpoints_share_one_envelope(client, path):
    """所有看板接口必须带同一套信封字段，前端据此渲染口径角标。"""
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    missing = ENVELOPE - set(resp.json())
    assert not missing, f"{path} 缺少信封字段 {sorted(missing)}"


# ------------------------------------------------------------------ 配置
def test_budget_round_trip(client):
    before = client.get("/api/budget").json()
    assert set(before) == {"daily_usd", "monthly_usd", "currency"}
    assert before["daily_usd"] == "10.00"

    after = client.put("/api/budget", json={"daily_usd": "7.50"}).json()
    assert after["daily_usd"] == "7.50"
    assert after["monthly_usd"] == before["monthly_usd"], "只改了日预算，月预算不该被动"
    assert client.get("/api/budget").json()["daily_usd"] == "7.50"


def test_budget_rejects_bad_value(client):
    resp = client.put("/api/budget", json={"daily_usd": "不是数字"})
    assert resp.status_code == 422


def test_pricing_payload_shape(client):
    payload = client.get("/api/pricing").json()
    assert payload["current_version"] == "builtin-2026.10.1"
    assert payload["models"], "内置价格表应当已播种"
    assert isinstance(payload["unknown_models"], list)


def test_currency_payload_shape(client):
    payload = client.get("/api/currency").json()
    assert "USD" in payload["supported"]
    assert payload["rates"] and payload["rates"][0]["base"] == "USD"


# ------------------------------------------------------------------ 查询参数
def _seed(client, dbpath, n_success=2, n_error=1):
    items = []
    for i in range(n_success + n_error):
        items.append(
            IngestItem.model_validate(
                {
                    "request_id": f"r{i}",
                    "ts": TS,
                    "provider": "workbuddy",
                    "model": "gpt-5",
                    "session_id": f"s{i}",
                    "usage": {"input_tokens": 1000},
                    "status": "error" if i >= n_success else "success",
                }
            )
        )
    with db.session(dbpath) as conn:
        ingest_items(conn, items)


def test_requests_filter_by_status(client, dbpath):
    _seed(client, dbpath)
    assert client.get("/api/requests", params={"limit": 10}).json()["total"] == 3

    err = client.get("/api/requests", params={"limit": 10, "status": "error"}).json()
    assert err["total"] == 1
    assert err["items"][0]["status"] == "error"


def test_requests_filter_by_unknown_model_is_empty_not_error(client, dbpath):
    _seed(client, dbpath)
    resp = client.get("/api/requests", params={"limit": 10, "model": "not-a-model"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_requests_pagination(client, dbpath):
    _seed(client, dbpath, n_success=5, n_error=0)
    page1 = client.get("/api/requests", params={"limit": 2, "offset": 0}).json()
    assert page1["limit"] == 2 and page1["has_more"] is True
    page3 = client.get("/api/requests", params={"limit": 2, "offset": 4}).json()
    assert page3["has_more"] is False


def test_requests_limit_upper_bound(client):
    """limit 上界由 Query 约束把守，超限应被拒绝而不是打垮数据库。"""
    assert client.get("/api/requests", params={"limit": 9999}).status_code == 422
    assert client.get("/api/requests", params={"limit": 0}).status_code == 422


def test_mix_dim_pattern_blocks_injection(client):
    """dim 直接拼进 SQL，必须由 pattern 白名单挡住非法值。"""
    assert client.get("/api/mix", params={"dim": "model"}).status_code == 200
    for bad in ["password", "1;DROP TABLE usage_fact", "model,project"]:
        assert client.get("/api/mix", params={"dim": bad}).status_code == 422, bad


def test_trend_days_bounds(client):
    assert client.get("/api/trend", params={"days": 7}).status_code == 200
    assert client.get("/api/trend", params={"days": 0}).status_code == 422
    assert client.get("/api/trend", params={"days": 999}).status_code == 422


def test_readonly_endpoints_reject_writes(client):
    assert client.put("/api/kpi", json={}).status_code == 405
    assert client.delete("/api/requests").status_code == 405


# ------------------------------------------------------------------ 缓存口径
def test_cache_insight_distinguishes_absent_from_zero(client, dbpath):
    """输入侧有大量 token 却没有缓存读 -> 应当说「未上报」，而不是「模型未定价」。"""
    with db.session(dbpath) as conn:
        ingest_items(
            conn,
            [
                IngestItem.model_validate(
                    {
                        "request_id": "c1",
                        "ts": TS,
                        "provider": "workbuddy",
                        "model": "gpt-5",
                        "session_id": "sc1",
                        "usage": {"input_tokens": 500_000},
                    }
                )
            ],
        )
    payload = client.get("/api/insights").json()
    ids = {i["id"] for i in payload["insights"]}
    assert "cache_absent" in ids, payload["insights"]
    item = next(i for i in payload["insights"] if i["id"] == "cache_absent")
    assert "未上报" in item["title"]
    assert "未定价" not in item["detail"]


# ------------------------------------------------------------------ 文案
def test_insight_text_carries_no_markdown_markers(client, dbpath):
    """前端按纯文本渲染，正文里混进 ** 或 ` 会原样显示成星号/反引号。"""
    _seed(client, dbpath)
    payload = client.get("/api/insights").json()
    for item in payload["insights"]:
        for field in ("title", "detail"):
            text = item.get(field) or ""
            for marker in ("**", "__", "`", "###"):
                assert marker not in text, f"{item['id']}.{field} 含 Markdown 标记 {marker!r}：{text[:80]}"
