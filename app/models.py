"""Pydantic 请求/响应模型。

金额字段刻意声明为宽松类型（str/float/int/Decimal 都收），由 ``money.dec()``
统一转换——**绝不**让二进制浮点进入计算链路。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

BillingMode = Literal["api", "subscription"]
ItemStatus = Literal["success", "error", "timeout", "throttled"]


class UsagePayload(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    # 厂商口径：二者给其一即可
    cost: Any | None = Field(
        default=None, description="厂商返回的美元成本。给了就以它为准（cost_source=provider）"
    )
    credit: Any | None = Field(
        default=None, description="厂商返回的积分原值。订阅制场景使用，按 credit_rate 折算"
    )


class IngestItem(BaseModel):
    request_id: str | None = Field(
        default=None,
        description="幂等键。缺省时由 sha1(provider|tool|session_id|raw_id) 派生",
    )
    ts: Any = Field(description="ISO8601 带时区，或 Unix 毫秒时间戳")
    provider: str = ""
    model: str = ""
    tool: str = ""
    project: str = ""
    session_id: str = ""
    user_id: str = "local"
    machine_id: str = "local"
    billing_mode: BillingMode = "api"
    usage: UsagePayload = Field(default_factory=UsagePayload)
    latency_ms: int | None = None
    status: ItemStatus = "success"
    # 原始记录 ID（如 WorkBuddy 的 messageId），用于派生 request_id
    raw_id: str | None = None


class IngestRequest(BaseModel):
    items: list[IngestItem] = Field(default_factory=list)


class IngestResult(BaseModel):
    request_id: str
    action: Literal["inserted", "updated", "ignored"]
    model: str
    model_recognized: bool
    cost_source: str
    cost_usd: str
    equivalent_cost_usd: str | None


class IngestSummary(BaseModel):
    received: int
    inserted: int
    updated: int
    ignored: int
    unknown_models: list[str]
    results: list[IngestResult]


class BudgetUpdate(BaseModel):
    daily_usd: Decimal | None = None
    monthly_usd: Decimal | None = None
    currency: str | None = None


class CreditRateUpdate(BaseModel):
    rate: Decimal
    calibrated: bool = True


class PricingOverride(BaseModel):
    model: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal = Decimal(0)
    cache_write_per_mtok: Decimal = Decimal(0)
    effective_from: str | None = None
