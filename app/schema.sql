-- Token 消耗看板 · 数据库结构
-- 金额一律以「微美元整数」存储（1e-6 USD），禁止浮点。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================ 事实表
-- 看板的唯一数据源。一次 LLM 调用一行。
CREATE TABLE IF NOT EXISTS usage_fact (
  request_id        TEXT    PRIMARY KEY,        -- 幂等键 sha1(provider|tool|session_id|raw_id)
  ts                TEXT    NOT NULL,           -- ISO8601 带时区
  ts_day            TEXT    NOT NULL,           -- 本地自然日 YYYY-MM-DD，写时算好
  provider          TEXT    NOT NULL DEFAULT '',-- 供应商标识，WorkBuddy 为 'workbuddy'
  model_raw         TEXT    NOT NULL DEFAULT '',-- 原始模型名，用于别名溯源
  model             TEXT    NOT NULL,           -- 归一后模型名；未识别为 '__unknown__'
  tool              TEXT    NOT NULL DEFAULT '',-- 工具/agent
  project           TEXT    NOT NULL DEFAULT '',-- 项目/工作区
  session_id        TEXT    NOT NULL DEFAULT '',-- 会话
  user_id           TEXT    NOT NULL DEFAULT 'local',   -- 预留多用户，现在不改表
  machine_id        TEXT    NOT NULL DEFAULT 'local',   -- 预留多机器
  billing_mode      TEXT    NOT NULL DEFAULT 'api',     -- api | subscription
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  vendor_credit_micro INTEGER,                  -- 厂商原始积分 ×1e6，仅存档审计
  cost_usd_micro    INTEGER NOT NULL DEFAULT 0, -- 厂商口径成本（微美元）
  equivalent_cost_usd_micro INTEGER,            -- API 等价成本（微美元），口径二
  cost_source       TEXT    NOT NULL DEFAULT 'pricing_table', -- provider | pricing_table
  latency_ms        INTEGER,
  status            TEXT    NOT NULL DEFAULT 'success',
  pricing_version   TEXT,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- 看板只有四种查询形状，索引按此建
CREATE INDEX IF NOT EXISTS idx_fact_day         ON usage_fact(ts_day);
CREATE INDEX IF NOT EXISTS idx_fact_model_day   ON usage_fact(model, ts_day);
CREATE INDEX IF NOT EXISTS idx_fact_project_day ON usage_fact(project, ts_day);
CREATE INDEX IF NOT EXISTS idx_fact_session     ON usage_fact(session_id);
CREATE INDEX IF NOT EXISTS idx_fact_ts          ON usage_fact(ts DESC);

-- ============================================================ 永久聚合
-- 明细超 90 天先汇总到这里再删除；本表永不过期。
CREATE TABLE IF NOT EXISTS daily_agg (
  ts_day            TEXT    NOT NULL,
  model             TEXT    NOT NULL DEFAULT '',
  project           TEXT    NOT NULL DEFAULT '',
  tool              TEXT    NOT NULL DEFAULT '',
  provider          TEXT    NOT NULL DEFAULT '',
  billing_mode      TEXT    NOT NULL DEFAULT 'api',
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd_micro    INTEGER NOT NULL DEFAULT 0,
  equivalent_cost_usd_micro INTEGER NOT NULL DEFAULT 0,
  vendor_credit_micro INTEGER NOT NULL DEFAULT 0,
  req_count         INTEGER NOT NULL DEFAULT 0,
  fail_count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts_day, model, project, tool, provider, billing_mode)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_agg_day ON daily_agg(ts_day);

-- ============================================================ 价格表（版本化）
-- 老版本永不修改。调价 = 新增版本行 + 回填旧版本 effective_to。
CREATE TABLE IF NOT EXISTS price_table (
  pricing_version TEXT NOT NULL,
  model           TEXT NOT NULL,
  provider        TEXT NOT NULL DEFAULT '',
  effective_from  TEXT NOT NULL,
  effective_to    TEXT,                          -- NULL 表示仍在生效
  input_per_mtok       TEXT NOT NULL DEFAULT '0',  -- Decimal 字符串
  output_per_mtok      TEXT NOT NULL DEFAULT '0',
  cache_read_per_mtok  TEXT NOT NULL DEFAULT '0',
  cache_write_per_mtok TEXT NOT NULL DEFAULT '0',
  source          TEXT NOT NULL DEFAULT 'builtin', -- builtin | user | litellm
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (pricing_version, model)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_price_model_eff
  ON price_table(model, effective_from);

-- ============================================================ 模型别名归一
CREATE TABLE IF NOT EXISTS model_alias (
  alias_raw  TEXT PRIMARY KEY,
  model      TEXT NOT NULL,
  provider   TEXT NOT NULL DEFAULT ''
) STRICT;

-- 未识别模型桶：不静默计入成本，而是记下来并告警（RK-03）
CREATE TABLE IF NOT EXISTS unknown_model (
  model_raw  TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
  hit_count  INTEGER NOT NULL DEFAULT 1
) STRICT;

-- ============================================================ 预算
CREATE TABLE IF NOT EXISTS budget (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  daily_usd_micro   INTEGER NOT NULL,
  monthly_usd_micro INTEGER NOT NULL,
  currency          TEXT NOT NULL DEFAULT 'USD',
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- 积分 -> 美元换算率（未校准前前端带「待校准」角标）
CREATE TABLE IF NOT EXISTS credit_rate (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  rate       TEXT NOT NULL,
  calibrated INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

-- ============================================================ 汇率（静态）
CREATE TABLE IF NOT EXISTS fx_rate (
  rate_date TEXT NOT NULL,
  base      TEXT NOT NULL,
  quote     TEXT NOT NULL,
  rate      TEXT NOT NULL,
  PRIMARY KEY (rate_date, base, quote)
) STRICT;

-- ============================================================ 告警
-- alert_state 用于去重：同一 key 同周期只 fire 一次
CREATE TABLE IF NOT EXISTS alert_state (
  alert_key    TEXT PRIMARY KEY,
  level        TEXT NOT NULL DEFAULT '',
  triggered_at TEXT,
  cleared_at   TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS alert_event (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_key       TEXT NOT NULL,
  level           TEXT NOT NULL,
  category        TEXT NOT NULL DEFAULT '',
  message         TEXT NOT NULL DEFAULT '',
  value_micro     INTEGER,
  threshold_micro INTEGER,
  fired_at        TEXT NOT NULL DEFAULT (datetime('now')),
  acknowledged    INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX IF NOT EXISTS idx_alert_fired ON alert_event(fired_at DESC);

-- ============================================================ 元信息
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
) STRICT;
