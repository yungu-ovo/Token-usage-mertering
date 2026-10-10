"""WorkBuddy trace 解析器（交叉校验源，**不用于灌数**）。

数据位置：``<WORKBUDDY_HOME>/traces/<workerPid>/trace_*.json``
结构为 ``{"trace": {...}, "spans": [...]}``，token 聚合在 ``trace`` 层。

这个类刻意**不继承** ``SourceParser``：trace 是会话级聚合，粒度比 ``usage_fact``
粗，只能用来验证 jsonl 逐条求和算得对不对，不能直接入库（会重复计数）。

同样只读，且只取数值字段；``trace.metadata`` 可能含会话信息，一律不读。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config


class WorkBuddyTraceParser:
    name = "workbuddy-trace"

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home or config.WORKBUDDY_TRACES_DIR)

    def discover(self) -> list[Path]:
        if not self.home.exists():
            return []
        return sorted(self.home.glob("*/trace_*.json"))

    def read_trace(self, path: Path) -> dict[str, Any] | None:
        """读取一个 trace 文件的数值摘要。失败返回 None。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        trace = data.get("trace")
        if not isinstance(trace, dict):
            return None

        session_id = trace.get("sessionId") or ""
        if not session_id:
            return None

        model_info = trace.get("modelInfo")
        if not isinstance(model_info, dict):
            model_info = {}

        return {
            "path": str(path),
            "session_id": str(session_id),
            "total_tokens": _int(trace.get("totalTokens")),
            "total_input_tokens": _int(model_info.get("totalInputTokens")),
            "total_output_tokens": _int(model_info.get("totalOutputTokens")),
            "total_cached_tokens": _int(model_info.get("totalCachedTokens")),
            "call_count": _int(model_info.get("callCount")),
            "models": [str(m) for m in (model_info.get("models") or [])],
            "agent": str(trace.get("agentName") or ""),
            "started_at": str(trace.get("startedAt") or ""),
            "ended_at": str(trace.get("endedAt") or ""),
            "status": str(trace.get("status") or ""),
        }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
