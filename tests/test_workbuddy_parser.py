"""WorkBuddy JSONL 解析器：字段映射、兜底、隐私白名单（RK-04）。

样本 ``tests/fixtures/wb_sample.jsonl`` 为**手工构造的脱敏数据**，
其中不含任何真实会话内容；第 1 条里那句 ``SENSITIVE_BODY_MUST_NEVER_BE_READ``
是故意埋的探针，用来证明正文不会被读出来。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.sources.workbuddy_jsonl import (
    WorkBuddyJsonlParser,
    field_report,
    has_usage,
    iter_json_lines,
    record_to_parsed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wb_sample.jsonl"

# 物理行数（含 1 行截断坏 JSON、1 行非对象数组、1 行空行）
EXPECTED_PHYSICAL_LINES = 16
# 有效记录数
EXPECTED_RECORDS = 9


@pytest.fixture(scope="module")
def raw_records() -> list[dict]:
    return list(iter_json_lines(FIXTURE))


@pytest.fixture(scope="module")
def parsed() -> dict[str, object]:
    """按 raw_id 索引的解析结果。"""
    out: dict[str, object] = {}
    for record in iter_json_lines(FIXTURE):
        item = record_to_parsed(record)
        if item is not None:
            out[item.raw_id] = item
    return out


# ------------------------------------------------------------------ 读取健壮性
def test_line_reader_survives_bad_lines(raw_records):
    """坏 JSON 行、非对象行、空行都不应中断读取。"""
    physical = sum(
        1 for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    assert physical == EXPECTED_PHYSICAL_LINES - 1  # 空行不计入
    # 15 行物理行里：1 坏 JSON + 1 非对象 + 1 空行 = 3 行被丢弃
    assert len(raw_records) == EXPECTED_PHYSICAL_LINES - 3


def test_non_object_lines_are_dropped(raw_records):
    assert all(isinstance(r, dict) for r in raw_records)
    assert not any("not an object" in json.dumps(r) for r in raw_records)


# ------------------------------------------------------------------ 三条取值链
def test_raw_usage_chain_wins(parsed):
    rec = parsed["msg-0001"]
    assert rec.input_tokens == 12000
    assert rec.output_tokens == 800
    assert rec.cache_read_tokens == 9000
    assert rec.cache_write_tokens == 1500
    assert rec.credit == Decimal("42.5")
    assert rec.model == "wb-model-a"
    assert rec.session_id == "sess-0001"
    assert rec.project == "alpha"
    assert rec.status == "success"
    assert rec.provider == "workbuddy"
    assert rec.billing_mode == "subscription"


def test_provider_usage_camel_case_chain(parsed):
    """rawUsage 缺失时回落到 providerData.usage.inputTokens。"""
    rec = parsed["msg-0002"]
    assert (rec.input_tokens, rec.output_tokens) == (5000, 1000)
    assert (rec.cache_read_tokens, rec.cache_write_tokens) == (0, 0)
    assert rec.credit is None
    assert rec.model == "wb-model-b"
    assert rec.project == "beta"


def test_message_usage_snake_case_chain(parsed):
    """前两处都缺失时回落到 message.usage.input_tokens。"""
    rec = parsed["msg-0003"]
    assert (rec.input_tokens, rec.output_tokens) == (700, 300)
    assert (rec.cache_read_tokens, rec.cache_write_tokens) == (200, 50)
    assert rec.model == "wb-model-c"
    # cwd 用正斜杠时也能取到目录名
    assert rec.project == "gamma"


def test_cached_tokens_alias(parsed):
    """OpenAI 风格的 cached_tokens 视为缓存读。"""
    rec = parsed["msg-0007"]
    assert (rec.input_tokens, rec.output_tokens) == (3000, 0)
    assert rec.cache_read_tokens == 2500
    assert rec.status == "error"
    # requestModelName 优先于 requestModelId
    assert rec.model == "wb-model-e"


def test_prompt_cache_hit_write_alias(parsed):
    """prompt_cache_hit_tokens / prompt_cache_write_tokens 变体。"""
    rec = parsed["msg-0010"]
    assert (rec.cache_read_tokens, rec.cache_write_tokens) == (6000, 1000)
    assert rec.credit == Decimal("12")


def test_zero_field_does_not_block_real_cache_value(parsed):
    """真实形状：cache_read_input_tokens 恒 0 存在时，不能阻断 prompt_cache_hit_tokens。

    这是线上踩过的坑（P0-A）：WorkBuddy 的 rawUsage 里 cache_read_input_tokens
    始终存在且恒为 0，而真实缓存读在 prompt_cache_hit_tokens（1,689/1,694 条
    非零，占输入 97.4%）。用 first_of 的「第一个非空」会把直报数据整体丢弃
    ——属于 RK-07 禁止的「静默少算」。
    """
    rec = parsed["msg-0013"]
    assert rec.input_tokens == 92879
    assert rec.output_tokens == 1534
    assert rec.cache_read_tokens == 90112
    assert rec.cache_write_tokens == 0
    assert rec.model == "Step-5-Preview"


def test_max_of_takes_group_maximum():
    """组内取最大：多拼写共存时取有证据的一侧（实测四组零分歧，安全）。"""
    from app.sources.base import max_of

    record = {
        "providerData": {
            "rawUsage": {
                "prompt_tokens": 100,
                "cache_read_input_tokens": 0,
                "prompt_cache_hit_tokens": 80,
            }
        }
    }
    assert max_of(record, "providerData.rawUsage.prompt_tokens") == 100
    assert max_of(
        record,
        "providerData.rawUsage.cache_read_input_tokens",
        "providerData.rawUsage.prompt_cache_hit_tokens",
    ) == 80
    assert max_of(record, "nonexistent.path", "also.missing") == 0


# ------------------------------------------------------------------ 时间
def test_millisecond_epoch_is_converted(parsed):
    """timestamp 是 13 位毫秒，必须换算成本地自然日，不能退化成今天。"""
    assert parsed["msg-0001"].ts.startswith("2026-10-05T10:00:00")
    assert parsed["msg-0002"].ts.startswith("2026-10-06T19:30:15")
    assert parsed["msg-0003"].ts.startswith("2026-10-07T11:05:00")


def test_iso_string_timestamp_is_accepted(parsed):
    assert parsed["msg-0008"].ts.startswith("2026-10-08T16:00:00")


# ------------------------------------------------------------------ 状态归一
@pytest.mark.parametrize(
    "raw_id,expected",
    [("msg-0011", "throttled"), ("msg-0012", "timeout")],
)
def test_status_mapping(parsed, raw_id, expected):
    assert parsed[raw_id].status == expected


# ------------------------------------------------------------------ 跳过规则
def test_only_useful_records_are_kept(parsed):
    assert len(parsed) == EXPECTED_RECORDS


def test_records_without_usage_are_skipped(parsed):
    assert "msg-0004" not in parsed  # 纯用户消息
    assert "msg-0009" not in parsed  # 只有 credit、无 token


def test_all_zero_usage_is_skipped(parsed):
    assert "msg-0005" not in parsed


def test_records_without_id_are_skipped(parsed):
    """无 messageId 也无法用 id 兜底时丢弃，避免产生假 request_id。"""
    ids = set(parsed)
    assert not any(i.startswith("msg-0006") for i in ids)


def test_has_usage_probe():
    assert has_usage({"providerData": {"rawUsage": {"prompt_tokens": 1}}}) is True
    assert has_usage({"providerData": {"rawUsage": {"credit": 5}}}) is False
    assert has_usage({}) is False


# ------------------------------------------------------------------ RK-04 隐私
def test_body_text_never_reaches_parsed_record(parsed):
    """结构性保证：ParsedRecord 里不可能出现正文。"""
    for rec in parsed.values():
        blob = json.dumps(rec.to_ingest_item(), ensure_ascii=False, default=str)
        assert "SENSITIVE_BODY_MUST_NEVER_BE_READ" not in blob
        assert "just a plain question" not in blob
        assert "content" not in rec.__dataclass_fields__
        assert "text" not in rec.__dataclass_fields__


def test_ingest_item_shape(parsed):
    item = parsed["msg-0001"].to_ingest_item()
    assert item["usage"]["input_tokens"] == 12000
    assert item["usage"]["credit"] == "42.5"  # Decimal 转字符串，不经浮点
    assert item["raw_id"] == "msg-0001"
    assert item.get("request_id") is None  # 由采集入口派生，解析层不臆造
    assert "model_raw" not in item  # 原始模型名由采集入口填充


def test_field_report_does_not_leak_values(raw_records):
    """诊断报告只报字段名，不报值。"""
    first = next(
        r for r in raw_records if '"SENSITIVE' in json.dumps(r, ensure_ascii=False)
    )
    report = field_report(first)
    assert report["has_usage"] is True
    assert "providerData.rawUsage.prompt_tokens" in report["present"]
    blob = json.dumps(report, ensure_ascii=False)
    assert "SENSITIVE_BODY_MUST_NEVER_BE_READ" not in blob


# ------------------------------------------------------------------ 发现
def test_discover_finds_jsonl(tmp_path):
    nested = tmp_path / "proj-x"
    nested.mkdir()
    (nested / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (nested / "b.txt").write_text("ignore me", encoding="utf-8")

    parser = WorkBuddyJsonlParser(home=tmp_path)
    found = parser.discover()
    assert [p.name for p in found] == ["a.jsonl"]


def test_discover_on_missing_dir_is_empty(tmp_path):
    parser = WorkBuddyJsonlParser(home=tmp_path / "nope")
    assert parser.discover() == []
