# Token 消耗看板 · 以 WorkBuddy 为试点的本地实现

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

本地优先的 LLM 用量 / 成本采集与看板。**只读本机会话数据，不调任何远端接口，不上传任何内容。**
本仓库是把 `Token消耗看板-产品方案.md`（产品设计）与开发计划落成可运行代码的实现，试点数据源是
本机 WorkBuddy 的会话转录。

- 版本：`0.1.0` ｜ 范围：**仅 P0**（信息架构里标 P1/P2 的一律不做）
- 后端：Python 3.11+ / FastAPI / SQLite（WAL）/ APScheduler（进程内）
- 前端：单文件 HTML + 原生 JS + 内联 SVG，**零构建、零 CDN**
- 金额存储：**INTEGER 微美元**（1e-6 USD），全链路 `Decimal`，禁用二进制浮点

---

## 一、目录结构

```
app/
  config.py         全局配置（可用 TUM_* 环境变量覆盖）
  db.py             连接、建表、meta / budget / credit_rate 读写
  schema.sql        STRICT 表定义
  money.py          Decimal 工具 + 微美元换算 + apportion() 分摊
  timeutil.py       时间解析（秒/毫秒/ISO8601 → 本地自然日）
  models.py         Pydantic 请求/响应
  ingest.py         落库主流程（幂等 UPSERT / 归一 / 定价 / 汇总）
  aggregator.py     日汇总重算
  alerts.py         预算三档 + 烧钱速率 + 预测耗尽
  currency.py       静态汇率（不联网）
  scheduler.py      每小时巡检 + 每日清理
  main.py           路由 + 静态前端挂载
  pricing/          价格表：默认价 / 别名 / 归一器 / 版本化读写
  queries/          kpi · trend · tokens · mix · insights · details · common
  sources/          workbuddy_jsonl.py（解析器）
web/dashboard.html  看板（读真实 API）
scripts/
  ingest_workbuddy.py   采集入口（--inspect / --dry-run / --ingest / --verify）
  seed_demo.py          生成合成演示数据（独立库，可 --demo 启动）
  run_regression.py     一键回归（自管服务 + 临时库副本，五层全跑）
  smoke.py              HTTP 契约冒烟（35 项）
  render_check.mjs      前端渲染冒烟（Node DOM 桩，25 项）
  check_privacy.py      隐私红线检查（12 项）
  pilot_acceptance.py   试点验收七条（产出 data/试点验收报告.md）
  setup_env.bat         一键建 venv + 装依赖
  run_dev.bat           一键启动（默认真实库，--demo 走演示库）
tests/                  99 个 pytest 用例
dashboard.html          设计基线原型（静态示例数据，未接入 API，保留作对照）
Token消耗看板-产品方案.md   产品设计（竞品扫描 → 指标 → 功能 → 界面 → 风险）
overview.md             交付概览
LICENSE                 MIT 许可
```

---

## 二、快速开始

### 方式 A：一键脚本（Windows）

```bat
scripts\setup_env.bat              :: 建 .venv + 装依赖 + 初始化库 + 跑单测
scripts\run_dev.bat                :: 用真实库 data\usage.db 启动
scripts\run_dev.bat --demo         :: 用演示库 data\demo.db 启动（无真实数据时看全貌）
scripts\run_dev.bat --port 9000    :: 换端口
```

### 方式 B：直接用命令（已验证）

```bash
# 建好虚拟环境后（scripts/setup_env.bat 会自动建 .venv）
PY=".venv/Scripts/python.exe"          # Linux/macOS: .venv/bin/python

# 1) 初始化库（幂等，重复执行安全）
"$PY" -c "from app import db; db.init_db()"

# 2) 采集：先看能采到什么，再真写
"$PY" scripts/ingest_workbuddy.py --inspect
"$PY" scripts/ingest_workbuddy.py --dry-run --limit 20
"$PY" scripts/ingest_workbuddy.py --ingest

# 3) 启服务（默认 127.0.0.1:8787）
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

没有真实数据也想看全貌？用合成演示数据，一条命令即可：

```bash
"$PY" scripts/seed_demo.py          # 生成 data/demo.db（纯虚构数据）
"$PY" scripts/run_dev.bat --demo    # 或者直接用它启动
```

打开 <http://127.0.0.1:8787/> 即看板（`/` 会 307 跳到 `/dashboard.html`），
`/docs` 是自动生成的 OpenAPI 文档。

---

## 三、数据流

```
~/.workbuddy/projects/**/*.jsonl   ──┐
  （主数据源：逐条请求，含 token 数）   │
                                    ├─→ 解析器（只取白名单字段）
~/.workbuddy/traces/*/trace_*.json ─┘         ↓
  （仅用于交叉校验，会话级）              归一化（模型别名三级）
                                              ↓
                                     定价（按 ts_day 选版本）
                                              ↓
                                usage_fact（幂等 UPSERT，微美元）
                                              ↓
                                  daily_agg（日汇总，供长周期查询）
                                              ↓
                                   FastAPI /api/*  →  web/dashboard.html
```

`--verify` 会把 jsonl 逐条求和与 traces 会话级汇总对照；只对 trace 留存完整
（覆盖 ≥98%）的会话判定，留存不全的会话标记为「不可比」而不是伪造一致。

---

## 四、五条硬约束（RK-01 ~ RK-06）

这些不是"最好有"，是实现里不可绕过的部分；每条都有对应测试。

| 编号 | 约束 | 落地位置 | 验证 |
| --- | --- | --- | --- |
| **RK-01** | 价格表版本化，**调价不改写历史** | `pricing/pricing_store.py`：`override()` 只新增版本并回填旧版本 `effective_to`，永不 UPDATE 已有行；每条用量记录存 `pricing_version` | `test_pricing.py` · 验收 #4 |
| **RK-02** | 流式重复写入**幂等去重** | `ingest.py`：`ON CONFLICT(request_id) DO UPDATE … WHERE excluded.output_tokens >= 现有值`，取最大值不取最后值 | `test_ingest*` · 验收 #2 |
| **RK-03** | 未识别模型**不静默计费** | `pricing/normalize.py` 四级归一；未命中 → `__unknown__` + `unknown_model` 台账 + 洞察提示，成本记 0、API 等价留空 | `test_pricing.py` · 验收 #3 |
| **RK-04** | 隐私：**库中不存在对话正文** | `sources/` 白名单 `ALLOWED_PATHS`；`ParsedRecord` 结构上就没有正文字段；全程只读 `open(..., "r")` | `check_privacy.py` · 验收 #6 |
| **RK-05** | **双口径**：API 等价成本 + 厂商积分折算 | `usage_fact` 同时存 `equivalent_cost_usd_micro` 与 `vendor_credit_micro`；积分折算率未校准时前端显著标注 | 验收 #5 |
| **RK-06** | 每个推断值都标**估算 + 方法** | `Estimated` 包装（`estimated` + `estimate_method`），预测 / 缓存节省 / 单请求均价均带标注 | `test_caliber.py` · 验收 #5 |

另外两条工程纪律：

- **本地自然日 vs UTC**：价格版本与日切分一律用本地日期（`timeutil.today_local()`），
  禁止 SQLite 的 `date('now')`（UTC）—— UTC+8 的 00:00~08:00 会错选前一天。
  `test_current_version_never_asks_sqlite_for_utc_now` 用 `set_trace_callback` 守住。
- **分摊不许有舍入漂移**：构成图各项之和必须**精确等于**总额，用最大余数法
  `money.apportion()` 而不是各自四舍五入（否则会出现 60.51 vs 61.30）。

---

## 五、隐私设计（零侵入、零外传）

- 采集侧只读 `<home>/projects/**/*.jsonl` 与 `traces/*/trace_*.json`，**从不写入** `.workbuddy` 任何路径。
- 解析器只取白名单字段（时间、模型、token 数、状态、延迟、会话/项目标识），
  prompt / completion / 工具入参等正文字段**结构上不可达**。
- 全程不调用远端接口；汇率是配置文件里的静态值。服务只绑 `127.0.0.1`。
- `check_privacy.py` 从五个角度验证：dataclass 字段、白名单、解析器源码 AST（含"有没有把
  `open()` 开成写模式"）、数据库列名与文本列取值长度、正文特征词探针。
  当前结果：**12/12 通过**，扫描 14,777 个文本值，最长 200 字符，无正文特征命中。

---

## 六、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/meta` | 版本、数据来源、口径标签、汇率 |
| POST | `/api/ingest` | 批量上报（幂等键 `request_id`） |
| GET | `/api/kpi` | 北极星 + 今日 + Token 分层 + 缓存 + 单请求均价 + 健康度 |
| GET | `/api/trend?days=` | 趋势（日成本 / 预算线 / 移动均值 / 构成） |
| GET | `/api/tokens?days=` | Token 分层 |
| GET | `/api/mix?dim=model\|project&days=` | 构成（各项之和精确等于总额） |
| GET | `/api/insights?days=` | 洞察卡片 |
| GET | `/api/requests?…&limit=&offset=` | 明细（可筛 status / model） |
| GET | `/api/drill/{model}/sessions` | 模型 → 会话下钻 |
| GET/PUT | `/api/budget` | 日 / 月预算与币种 |
| GET | `/api/pricing` | 当前价格表与版本 |
| PUT | `/api/pricing/override` | 新增价格版本（不动历史） |
| PUT | `/api/pricing/credit-rate` | 积分折算率 |
| GET | `/api/currency` | 支持的币种与汇率 |
| GET | `/api/alerts` · POST `/api/alerts/{id}/ack` | 告警列表 / 确认 |
| POST | `/api/refresh` | 触发一次采集 |
| GET | `/api/health` | 健康检查 |

只读端点对写方法返回 405；`limit` / `days` 越界返回 422；`dim` 走正则白名单防注入。

---

## 七、验证体系与当前结果

分五层，从快到慢。**一条命令跑完全部**：

```bash
"$PY" scripts/run_regression.py          # 默认：临时库副本 + 自管服务，跑完自动关闭
```

```
REGRESSION: PASS  5/5 层全部通过（总耗时 4.7s）
```

它自己挑空闲端口、用**临时复制的库**起一个实例、跑完就关——所以你正在看的 8787
实例不受影响，真实库也不会被写脏；报告落 `data/回归报告-v0.1.0.md`。

> **刚 clone 下来就能跑**：`data/` 是 gitignore 的，全新克隆里没有 `usage.db`。
> 这时回归会自动生成一份合成演示数据供各层使用，并把「试点验收七条」**显式跳过**
> （它校验的是真实试点数据，合成数据下不成立）。报告里会写明跳过原因，不会静默略过。
> 灌入自己的数据后重跑即可解除：
> `python scripts/ingest_workbuddy.py --ingest`。

| 层 | 单独命令 | 当前结果 |
| --- | --- | --- |
| 单元测试 | `pytest -q` | **99 passed** |
| HTTP 契约冒烟 | `python scripts/smoke.py` | **PASS 35/35**（含跨端点口径一致性、构成之和=总额、预算往返、页面无假数据残留） |
| 前端渲染冒烟 | `node scripts/render_check.mjs` | **PASS 25/25**（Node DOM 桩跑页面脚本，断言 KPI/图表/表格/洞察均已填充） |
| 隐私红线 | `python scripts/check_privacy.py` | **PASS 12/12** |
| 试点验收七条 | `python scripts/pilot_acceptance.py` | **PASS 7/7**，报告落在 `data/试点验收报告.md` |

常用开关：

```bash
"$PY" scripts/run_regression.py --only pytest,smoke   # 只跑部分层（报告名会带后缀，不覆盖完整报告）
"$PY" scripts/run_regression.py --live-db             # 直接打真实库，不做副本
"$PY" scripts/run_regression.py --base http://127.0.0.1:8787   # 复用已在跑的服务
```

`pilot_acceptance.py` 的三条设计原则：**不碰生产库**（写类检查在临时复制的库上做）、
**可重复**（跑多少次结论一样）、**不当老好人**（任何一条不过就退出码非零）。

---

## 八、试点实测（本机 WorkBuddy 数据）

| 指标 | 值 |
| --- | --- |
| 解析记录 / 落库行数 | 约 1,100 条 / **846 行**（去重后唯一；解析数随本机会话持续写入而增长，落库去重后稳定） |
| 本月累计（API 等价口径） | **$29.39** / 预算 $300（9.8%，时间进度 32%） |
| 厂商口径（积分折算） | $10.99（折算率**未校准**） |
| 月末预测 | $91.10（标注「估算」，方法：月内日均 × 31 天） |
| 预算可支撑 | 92 天 |
| Token | 输入 89.1M + 输出 0.65M |
| 单请求均价 | $0.045630 |
| 交叉校验 | 3 个可比会话逐位一致（差异 0.00%），其余因 trace 留存不全标记「不可比」 |

---

## 九、已知限制（都不是 bug，是当前数据源的客观边界）

1. **jsonl 不提供缓存 token 字段** → 库里 `cache_read_tokens` 恒为 0，看板的缓存命中率 /
   缓存节省显示为「—（未上报）」并附原因，**不显示成 0.0%**（那会误导成"缓存完全没命中"）。
   交叉校验时发现 `traces/*.json` 里有 `totalCachedTokens`，抽样显示缓存读约占输入的 95.5%。
   要把这两项做真，需把 traces 作为**会话级补充数据源**接入，并处理与 jsonl 的口径重叠避免重复计数。
2. **厂商的模型名是内部代号**，公共价格表无法匹配。当前成本数字基于**显式标注的占位价格**
   （`--apply-placeholder-prices`），只用于验证成本链路是否通，**不代表真实账单**。
   拿到真实积分单价后应走 `PUT /api/pricing/override`（只新增版本，不动历史）。
   代号 → 占位价格的映射放在 `app/pricing/placeholder.local.json`，**该文件不进版本库**；
   仓库里只有脱敏示例 `placeholder.example.json`，照它复制一份填入自己的模型名即可。
3. **30 天窗口内只有 7 天有数据**（本机历史有限），趋势图存在真实断点，不是采集失败。
4. `scripts/*.bat` 在开发机上无法执行（沙箱拦截 `cmd.exe`），
   逻辑已逐行复核，但**未实跑验证**；等价命令见第二章「方式 B」，均已验证。

---

## 十、P0 之外（明确不做）

多用户 / 多租户、远程上报、告警推送渠道（邮件/IM）、价格表自动同步、
预算的部门 / 项目多级分配、导出报表定时任务、移动端适配。
这些都留在产品方案的 P1/P2 列表里，不在本次实现范围。

---

## 十一、许可

[MIT](LICENSE) © 2026 yungu-ovo

可自由使用、修改、分发与商用，保留版权声明即可。软件按「原样」提供，不含任何担保。

需要注意的两点：

- 仓库中的**价格表**（`app/pricing/default_pricing.json`）是按公开列表价整理的占位数据，
  仅用于让成本链路可端到端验证，**不是任何厂商的官方报价**，也不代表真实账单。
- 采集侧只读本机数据，不随本项目分发任何第三方数据；使用者需自行确保对其数据源拥有读取权限。

---

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
