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
    dig,
    first_of,
    map_status,
    max_of,
)

# token 字段的取值优先级：rawUsage（上游原始，最全） > usage（归一 camelCase） > message.usage
# 注意用 max_of 而不是 first_of：组内是**同一概念的多种拼写**，某个拼写恒为 0
# （如 cache_read_input_tokens）时不能阻断回退到有值的拼写（prompt_cache_hit_tokens）。
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
    # 字段链版本：rawUsage > usage > message.usage；上游改结构时递增此版本号。
    # v2（P0-A）：四个 token 组从 first_of 改为组内取最大——恒 0 的
    # cache_read_input_tokens 不再阻断 prompt_cache_hit_tokens 的回退。
    # 采集侧按此版本号决定是否作废水位全量重扫（见 sources/incremental.py）。
    version = "2"

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
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parsed = consume_line(line, self.stats)
                    if parsed is not None:
                        yield parsed
        except OSError:
            return


def consume_line(line: str, stats: ParseStats | None) -> ParsedRecord | None:
    """消费一行文本：JSON 解析 → 用量判定 → 规范化，并维护 ``stats``。

    全量路径（``iter_records``）与增量路径（``parse_file_from``）共用，
    保证两条路径的统计口径完全一致（RK-07）。
    """
    text = line.strip()
    if not text or text[0] != "{":
        return None
    if stats is not None:
        stats.scanned += 1
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if stats is not None:
            stats.bad_lines += 1
        return None
    if not isinstance(obj, dict):
        return None
    if not has_usage(obj):
        return None
    if stats is not None:
        stats.usage_lines += 1
    parsed = record_to_parsed(obj)
    if parsed is None:
        # 走到这里只剩两种原因：全 0 或没有幂等 id
        if stats is not None:
            stats.bump_skipped("missing_id" if not record_id(obj) else "zero_tokens")
        return None
    if stats is not None:
        stats.parsed += 1
    return parsed


def parse_file_from(
    path: Path, offset: int, stats: ParseStats | None = None
) -> tuple[list[ParsedRecord], int]:
    """从 ``offset`` **字节**处读取新增的完整行，返回 ``(记录, 新 offset)``。

    半行保护：文件正被另一个进程写入时，末尾可能是不完整的 JSON。
    只消费以 ``\\n`` 结尾的行，``offset`` 只推进到最后一个完整行末——
    残行留给下一轮，否则它会被当成坏行跳过，那条用量永久丢失
    （正是 RK-07 禁止的「静默少算」）。

    为什么用二进制模式而不是文本模式的 ``tell()``：CPython 的
    ``TextIOWrapper.tell()`` 返回的是**不透明 cookie**（实测比真实字节位置
    大 1），拿它当字节偏移存库、与 ``st_size`` 比较都会错位。二进制模式下
    ``seek/tell`` 是精确字节语义，且 utf-8 多字节序列绝不包含 0x0A，
    按 ``\\n`` 切行再逐行解码永远安全。
    """
    records: list[ParsedRecord] = []
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return records, offset

    cut = chunk.rfind(b"\n")
    if cut == -1:
        # 没有任何完整新行（空追加或只有半行）
        return records, offset
    complete = chunk[: cut + 1]
    new_offset = offset + len(complete)

    for raw in complete.split(b"\n")[:-1]:
        parsed = consume_line(raw.decode("utf-8", errors="replace"), stats)
        if parsed is not None:
            records.append(parsed)
    return records, new_offset


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

    input_tokens = max_of(record, *_IN_INPUT)
    output_tokens = max_of(record, *_IN_OUTPUT)
    cache_read = max_of(record, *_IN_CACHE_READ)
    cache_write = max_of(record, *_IN_CACHE_WRITE)

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
