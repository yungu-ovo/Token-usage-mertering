"""全局配置。所有可调项都支持环境变量覆盖。"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------- 应用
APP_NAME = "Token 消耗看板"
HOST = os.environ.get("TUM_HOST", "127.0.0.1")
PORT = int(os.environ.get("TUM_PORT", "8787"))

# ---------------------------------------------------------------- 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TUM_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.environ.get("TUM_DB_PATH", DATA_DIR / "usage.db"))
WEB_DIR = PROJECT_ROOT / "web"
PRICING_DIR = Path(__file__).resolve().parent / "pricing"

# ---------------------------------------------------------------- 时区
# 所有统计按本地时区的自然日切分
LOCAL_TZ_OFFSET_HOURS = 8
LOCAL_TZ_NAME = "Asia/Shanghai"

# ---------------------------------------------------------------- 采集：WorkBuddy 试点
# 只读目录，程序绝不写入
WORKBUDDY_HOME = Path(
    os.environ.get("TUM_WB_HOME", Path.home() / ".workbuddy")
)
WORKBUDDY_PROJECTS_DIR = WORKBUDDY_HOME / "projects"
WORKBUDDY_TRACES_DIR = WORKBUDDY_HOME / "traces"

# ---------------------------------------------------------------- 数据来源标记
# 决定前端顶栏是否显示「示例数据」角标。灌入真实 WorkBuddy 数据后由
# /api/refresh 之外的路径自动改写为 workbuddy。
DATA_SOURCE_DEMO = "demo"
DATA_SOURCE_WORKBUDDY = "workbuddy"

# ---------------------------------------------------------------- 计费口径
# WorkBuddy 按「积分」计费，数据里不含积分 -> 美元的换算关系。
# 这里只给占位默认值，且 calibrated=False，前端必须显示「待校准」角标（RK-06 / 缺口 12）。
DEFAULT_CREDIT_TO_USD_RATE = Decimal(
    os.environ.get("TUM_CREDIT_TO_USD", "0.01")
)
CREDIT_RATE_CALIBRATED_DEFAULT = False

# 明细保留天数，超期先汇总进 daily_agg 再删除
DETAIL_RETENTION_DAYS = int(os.environ.get("TUM_RETENTION_DAYS", "90"))

# ---------------------------------------------------------------- 预算默认值
DEFAULT_DAILY_BUDGET_USD = Decimal(os.environ.get("TUM_BUDGET_DAILY", "10"))
DEFAULT_MONTHLY_BUDGET_USD = Decimal(os.environ.get("TUM_BUDGET_MONTHLY", "300"))

# 预算告警三档
BUDGET_THRESHOLDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("0.75"), "warn"),
    (Decimal("0.90"), "severe"),
    (Decimal("1.00"), "exceeded"),
)

# 烧钱速率异常检测：window 小时的成本 vs 过去 N 天同一时段均值的倍数
BURN_RATE_WINDOW_HOURS = 2
BURN_RATE_BASELINE_DAYS = 14
BURN_RATE_MULTIPLIER = Decimal("3")

# 预测耗尽告警的提前量（天）
FORECAST_LEAD_DAYS = 7

# ---------------------------------------------------------------- 采集（B-1 / B-2 / P0-B）
# 自动检测与记录（P0-B）：**默认开启**。用户明确要求「只要在 WorkBuddy 中
# 调用了模型，就自动检测并记录」——服务常驻即自动采集，无需人工点按钮。
# 关闭：TUM_AUTO_COLLECT=0（顶栏会显示「监控已关闭」，采集退化为手动）。
#
# 双模扫描：
#   * 增量轮（WATCH_INTERVAL_SECONDS，默认 15s）：只读文件水位后的新增行，
#     毫秒级成本，做到「调用发生后秒级可查」；
#   * 校准轮（CALIBRATION_MINUTES，默认 60min）：全量重扫一次，负责
#     RK-07 的权威解析成功率分母、traces 回填、聚合表自愈。
AUTO_COLLECT = os.environ.get("TUM_AUTO_COLLECT", "1") not in ("0", "false", "False", "")
WATCH_INTERVAL_SECONDS = min(300, max(5, int(os.environ.get("TUM_WATCH_INTERVAL_SECONDS", "15"))))
# 兼容旧变量名：TUM_AUTO_COLLECT_MINUTES 现在表示校准轮间隔（曾是定时采集间隔）
CALIBRATION_MINUTES = min(1440, max(5, int(
    os.environ.get("TUM_CALIBRATION_MINUTES",
                   os.environ.get("TUM_AUTO_COLLECT_MINUTES", "60"))
)))

# 解析成功率下限（RK-07）：低于此值说明数据源格式变了，成本会被**少算**，必须告警
PARSE_SUCCESS_MIN = Decimal(os.environ.get("TUM_PARSE_SUCCESS_MIN", "0.95"))

# 月末预测的最少样本天数，低于此值标注「样本不足」
FORECAST_MIN_SAMPLE_DAYS = 3

# ---------------------------------------------------------------- 汇率（静态，不联网）
# 结构：{quote: (rate, rate_date)}，USD -> quote
DEFAULT_FX_RATES: dict[str, tuple[str, str]] = {
    "CNY": ("7.12", "2026-10-01"),
}
