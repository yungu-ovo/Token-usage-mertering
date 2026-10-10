"""WorkBuddy 会话转录解析器（主数据源）。

数据位置：``<WORKBUDDY_HOME>/projects/<cwd-slug>/<session-uuid>.jsonl``
一行一条消息记录，token 计数在 ``providerData.rawUsage`` / ``providerData.usage``
/ ``message.usage`` 三处之一（以 ``rawUsage`` 优先，字段最全且带 ``credit``）。

**只读**：本模块只用 ``open(path, 'r')`` 读文件，绝不写入该目录。
**隐私**：只按 ``ALLOWED_PATHS`` 取值，正文（content / arguments / output）
不会被读取，也不会进入 :class:`ParsedRecord`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from .. import config
from ..timeutil import parse_ts
from .base import (
    ALLOWED_PATHS,
    ParsedRecord,
    ParseStats,
    SourceParser,
    as_decimal,
    as_int,
    dig,
    first_of,
    map_status,
)

# token 字段的取值优先级：rawUsage（上游原始，最全） > usage（归一 camelCase） > message.usage
_IN_INPUT = (
    "providerData.rawUsage.prompt_tokens",
    "providerData.usage.inputTokens",
    "message.usage.input_tokens",
)
_IN_OUTPUT = (
    "providerData.rawUsage.completion_tokens",
    "providerData.usage.outputTokens",
    "message.usage.output_tokens",
)
_IN_CACHE_READ = (
    "providerData.rawUsage.cache_read_input_tokens",
    "providerData.rawUsage.prompt_cache_hit_tokens",
    "message.usage.cache_read_input_tokens",
    "providerData.rawUsage.cached_tokens",
)
_IN_CACHE_WRITE = (
    "providerData.rawUsage.cache_creation_input_tokens",
    "providerData.rawUsage.prompt_cache_write_tokens",
    "message.usage.cache_creation_input_tokens",
)


class WorkBuddyJsonlParser(SourceParser):
    name = "workbuddy-jsonl"
    # 字段链：rawUsage > usage > message.usage。上游改结构时递增此版本号。
    version = "1"

    def __init__(self, home: Path | None = None) -> None:
        super().__init__()
        self.home = Path(home or config.WORKBUDDY_PROJECTS_DIR)

    # -------------------------------------------------------------- 发现
    def discover(self) -> list[Path]:
        if not self.home.exists():
            return []
        return sorted(self.home.glob("**/*.jsonl"))

    # -------------------------------------------------------------- 解析
    def iter_records(self, path: Path) -> Iterator[ParsedRecord]:
        """逐条产出记录，同时把解析健康度记进 ``self.stats``（RK-07）。"""
        for record in iter_json_lines(path, self.stats):
            if not has_usage(record):
                continue
            self.stats.usage_lines += 1
            parsed = record_to_parsed(record)
            if parsed is None:
                # 走到这里只剩两种原因：全 0 或没有幂等 id
                self.stats.bump_skipped(
                    "missing_id" if not record_id(record) else "zero_tokens"
                )
                continue
            self.stats.parsed += 1
            yield parsed


def iter_json_lines(
    path: Path, stats: ParseStats | None = None
) -> Iterator[dict[str, Any]]:
    """逐行读 JSONL。坏行直接跳过，不中断整份文件。

    传入 ``stats`` 时顺带统计行数——这是解析成功率的分母来源。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                if stats is not None:
                    stats.scanned += 1
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    if stats is not None:
                        stats.bad_lines += 1
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def has_usage(record: dict[str, Any]) -> bool:
    """记录里是否存在任何 token 计数。"""
    for path in _IN_INPUT + _IN_OUTPUT + _IN_CACHE_READ + _IN_CACHE_WRITE:
        if dig(record, path) is not None:
            return True
    return False


def record_id(record: dict[str, Any]) -> str:
    """幂等键的原始 id。抽出来是为了让 iter_records 能区分「跳过的两种原因」。"""
    return str(
        first_of(
            record,
            "providerData.messageId",
            "providerData.conversationRequestId",
            "id",
        )
        or ""
    )


def record_to_parsed(record: dict[str, Any]) -> ParsedRecord | None:
    """把一条原始记录转成 :class:`ParsedRecord`；无用量的返回 None。"""
    if not has_usage(record):
        return None

    input_tokens = as_int(first_of(record, *_IN_INPUT))
    output_tokens = as_int(first_of(record, *_IN_OUTPUT))
    cache_read = as_int(first_of(record, *_IN_CACHE_READ))
    cache_write = as_int(first_of(record, *_IN_CACHE_WRITE))

    # 全 0 的记录没有信息量，跳过（避免污染 request_count）
    if input_tokens == 0 and output_tokens == 0 and cache_read == 0 and cache_write == 0:
        return None

    model = (
        first_of(
            record,
            "providerData.requestModelName",
            "providerData.model",
            "providerData.requestModelId",
        )
        or ""
    )
    raw_id = record_id(record)
    if not raw_id:
        return None

    cwd = str(dig(record, "cwd") or "")
    project = os.path.basename(cwd.rstrip("/\\")) or "unknown"

    # timestamp 实测是 Unix 秒级时间戳，这里统一规范成 ISO8601 本地时区
    ts_iso, _ts_day = parse_ts(dig(record, "timestamp"))

    return ParsedRecord(
        raw_id=str(raw_id),
        ts=ts_iso,
        model=str(model),
        session_id=str(dig(record, "sessionId") or ""),
        project=project,
        tool=str(dig(record, "providerData.agent") or "workbuddy"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        credit=as_decimal(dig(record, "providerData.rawUsage.credit")),
        status=map_status(dig(record, "status")),
        provider="workbuddy",
        billing_mode="subscription",
    )


def field_report(record: dict[str, Any]) -> dict[str, Any]:
    """诊断用：报告这条记录里哪些白名单路径真实命中。

    只返回字段名与是否存在，**不返回值本身**，避免把正文带出来。
    """
    present = [p for p in sorted(ALLOWED_PATHS) if dig(record, p) is not None]
    return {
        "type": dig(record, "type"),
        "present": present,
        "has_usage": has_usage(record),
    }
