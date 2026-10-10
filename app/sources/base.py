"""采集适配器基类与隐私白名单（RK-04）。

设计要点：解析结果用 :class:`ParsedRecord` 表示，而这个 dataclass **没有**
任何承载对话正文的字段。就算原始 JSON 里有 ``content`` / ``arguments`` /
``output``，也不可能被带出来——这是结构性的保证，不依赖评审时记得逐条检查。
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

# 只允许从原始记录里取这些路径。任何新增字段都必须先加进这里。
ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "timestamp",
        "type",
        "status",
        "sessionId",
        "cwd",
        "id",
        "providerData.messageId",
        "providerData.conversationRequestId",
        "providerData.model",
        "providerData.requestModelId",
        "providerData.requestModelName",
        "providerData.agent",
        "providerData.usage.inputTokens",
        "providerData.usage.outputTokens",
        "providerData.usage.totalTokens",
        "providerData.rawUsage.prompt_tokens",
        "providerData.rawUsage.completion_tokens",
        "providerData.rawUsage.total_tokens",
        "providerData.rawUsage.cached_tokens",
        "providerData.rawUsage.cache_read_input_tokens",
        "providerData.rawUsage.cache_creation_input_tokens",
        "providerData.rawUsage.prompt_cache_hit_tokens",
        "providerData.rawUsage.prompt_cache_write_tokens",
        "providerData.rawUsage.completion_thinking_tokens",
        "providerData.rawUsage.credit",
        "message.usage.input_tokens",
        "message.usage.output_tokens",
        "message.usage.total_tokens",
        "message.usage.cache_read_input_tokens",
        "message.usage.cache_creation_input_tokens",
    }
)


def dig(record: dict[str, Any], path: str) -> Any:
    """按点分路径取值，任一层不是 dict 就返回 None。"""
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def first_of(record: dict[str, Any], *paths: str) -> Any:
    """按顺序取第一个非空值。"""
    for path in paths:
        value = dig(record, path)
        if value is not None and value != "":
            return value
    return None


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def as_decimal(value: Any) -> Decimal | None:
    """注意：先转字符串再构造 Decimal，避免浮点误差（缺口 14）。"""
    if value is None or value == "":
        return None
    from ..money import dec

    return dec(value)


def map_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in ("", "success", "ok", "completed", "done", "finished"):
        return "success"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "throttl" in text or "rate" in text and "limit" in text:
        return "throttled"
    return "error"


@dataclass(frozen=True)
class ParsedRecord:
    """规范化后的用量记录。

    **刻意不含任何正文相关字段** —— 这是 RK-04 的结构性保证。
    """

    raw_id: str
    ts: str
    model: str
    session_id: str
    project: str
    tool: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    credit: Decimal | None = None
    status: str = "success"
    provider: str = "workbuddy"
    billing_mode: str = "subscription"

    def to_ingest_item(self) -> dict[str, Any]:
        d = asdict(self)
        usage = {
            "input_tokens": d.pop("input_tokens"),
            "output_tokens": d.pop("output_tokens"),
            "cache_read_tokens": d.pop("cache_read_tokens"),
            "cache_write_tokens": d.pop("cache_write_tokens"),
            "credit": None if d.pop("credit") is None else str(self.credit),
        }
        d["usage"] = usage
        return d


class SourceParser(abc.ABC):
    """采集适配器接口。实现必须**只读**原始数据。"""

    name: str = "base"

    @abc.abstractmethod
    def discover(self) -> list[Path]:
        """列出可解析的原始文件。"""

    @abc.abstractmethod
    def iter_records(self, path: Path) -> Iterator[ParsedRecord]:
        """逐条产出规范化记录。无用量记录应被跳过。"""

    def parse_all(self, files: Iterable[Path] | None = None) -> Iterator[ParsedRecord]:
        for path in files if files is not None else self.discover():
            yield from self.iter_records(path)
