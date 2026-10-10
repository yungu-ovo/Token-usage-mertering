# Token 消耗看板 · 以 WorkBuddy 为试点的本地实现

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

本地优先的 LLM 用量 / 成本采集与看板。**只读本机会话数据，不调任何远端接口，不上传任何内容。**
本仓库是把 `Token消耗看板-产品方案.md`（产品设计）与开发计划落成可运行代码的实现，试点数据源是
本机 WorkBuddy 的会话转录。

- 版本：`0.3.0` ｜ 范围：**仅 P0**（信息架构里标 P1/P2 的一律不做）
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
  collect.py        采集流水线（解析→落库→回填→重算→巡检），服务与脚本共用
  aggregator.py     日汇总重算
  alerts.py         预算三档 + 烧钱速率 + 预测耗尽 + 采集健康度
  currency.py       静态汇率（不联网）
  scheduler.py      每小时巡检 + 每日清理 + 可选定时采集
  main.py           路由 + 静态前端挂载
  pricing/          价格表：默认价 / 别名 / 归一器 / 版本化读写
  queries/          kpi · trend · tokens · mix · insights · details · common
  sources/          workbuddy_jsonl.py（解析器）· workbuddy_trace.py（校验）
                    trace_cache.py（按会话缓存率回填缓存读，幂等）
web/dashboard.html  看板（读真实API）
scripts/
  ingest_workbuddy.py   采集入口（--inspect / --dry-run / --ingest / --verify /
                        --backfill-cache [--apply]）
  seed_demo.py          生成合成演示数据（独立库，可 --demo 启动）
  purge_bad_alerts.py   清理口径错误的历史告警（一次性修复工具）
  run_regression.py     一键回归（自管服务 + 临时库副本，五层全跑）
  smoke.py              HTTP 契约冒烟（44 项）
  render_check.mjs      前端渲染冒烟（Node DOM 桩，34 项）
  check_privacy.py      隐私红线检查（12 项）
  pilot_acceptance.py   试点验收七条（产出 data/试点验收报告.md）
tests/                  144 个 pytest 用例
dashboard.html          设计基线原型（静态示例数据，未接入 API，保留作对照）
Token消耗看板-产品方案.md   产品设计（竞品扫描 → 指标 → 功能 → 界面 → 风险）
overview.md             交付概览
LICENSE                 MIT 许可
```

---

## 二、快速开始

只有一份命令清单，**没有一键脚本**——曾经有过两个 `.bat`，但它们在开发机上
从未被执行验证过（沙箱拦 `cmd.exe`）。一个没跑过的"一键脚本"是风险不是便利，
所以删掉了，只留下面这些**逐条验证过**的命令。

```bash
PY=".venv/Scripts/python.exe"          # Linux/macOS: .venv/bin/python

# 1) 初始化库（幂等，重复执行安全）
"$PY" -c "from app import db; db.init_db()"

# 2) 采集：先看能采到什么，再真写
"$PY" scripts/ingest_workbuddy.py --inspect
"$PY" scripts/ingest_workbuddy.py --dry-run --limit 20
"$PY" scripts/ingest_workbuddy.py --ingest
"$PY" scripts/ingest_workbuddy.py --backfill-cache# 补缓存读（先演练）
"$PY" scripts/ingest_workbuddy.py --backfill-cache --apply   # 真写（幂等）

# 3) 启服务（默认 127.0.0.1:8787）
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

服务起来之后，看板顶栏那个「采集」按钮走的就是同一条流水线
（`POST /api/collect`），不必回到命令行。

想让它自己在后台定期采？默认**不开**——它会在后台定时读你的会话文件，
「服务自己读」和「你点一下才读」是两种不同的信任关系：

```bash
TUM_AUTO_COLLECT=1 TUM_AUTO_COLLECT_MINUTES=30 "$PY" -m uvicorn app.main:app
```

没有真实数据也想看全貌？用合成演示数据，一条命令即可：

```bash
"$PY" scripts/seed_demo.py          # 生成 data/demo.db（纯虚构数据）
TUM_DB_PATH=data/demo.db "$PY" -m uvicorn app.main:app      # 用演示库启动
```

打开 <http://127.0.0.1:8787/> 即看板（`/` 会 307 跳到 `/dashboard.html`），
`/docs` 是自动生成的 OpenAPI 文档。

### 可调项（`TUM_*` 环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TUM_HOST` / `TUM_PORT` | `127.0.0.1` / `8787` | 只绑回环地址，不对外暴露 |
| `TUM_DB_PATH` | `data/usage.db` | 换库（演示库就用它指到 `data/demo.db`） |
| `TUM_DATA_DIR` | `data/` | 数据目录 |
| `TUM_WB_HOME` | `~/.workbuddy` | 只读的数据源根目录 |
| `TUM_BUDGET_DAILY` / `TUM_BUDGET_MONTHLY` | `10` / `300` | 初始预算（USD） |
| `TUM_CREDIT_TO_USD` | `0.01` | 积分折算率占位值，`calibrated=False` |
| `TUM_RETENTION_DAYS` | `90` | 明细保留天数，超期汇总进 `daily_agg` 后删除 |
| `TUM_AUTO_COLLECT` | `0` | 定时采集开关（**默认关**，见上文） |
| `TUM_AUTO_COLLECT_MINUTES` | `30` | 定时采集间隔 |
| `TUM_PARSE_SUCCESS_MIN` | `0.95` | 解析成功率告警阈值（RK-07） |
| `TUM_SCHEDULER` | `1` | 调度器总开关 |

---

## 三、数据流

```
~/.workbuddy/projects/**/*.jsonl   ──┐
  （主数据源：逐条请求，含 token 数）   │
                                    ├─→ 解析器（只取白名单字段）
~/.workbuddy/traces/*/trace_*.json ─┤         ↓
  （会话级补充：补缓存读 token）      ─→ 归一化（模型别名三级）
                                              ↓
                                     定价（按 ts_day选版本）
                                              ↓
                                usage_fact（幂等 UPSERT，微美元）
                                    ↑                │
                      traces 分摊回填 │                ↓
                      （缓存读，可重复跑）    daily_agg（日汇总）
                                              ↓
                                   FastAPI /api/*  →  web/dashboard.html
```

**缓存读为什么要单独回填**：`jsonl` 里没有任何缓存字段，而 traces 有会话级的
`totalCachedTokens`。两者粒度不同（请求级 vs 会话级），直接混灌会重复计数，
所以只把 trace 观测到的缓存量**按输入占比分摊给被 trace 实际覆盖的那批请求**，
未覆盖的请求保持 0——宁可少算，不拿没有证据的数字充数。

`--verify` 会把 jsonl 逐条求和与 traces 会话级汇总对照；只对 trace 留存完整
（覆盖 ≥98%）的会话判定，留存不全的会话标记为「不可比」而不是伪造一致。

**采集是同一条流水线**，`app/collect.py` 一个实现，脚本与 `POST /api/collect`
共用：解析 → 落库 → traces 回填缓存 → 重算聚合 → 告警巡检。
早先它只活在 `scripts/` 里，服务层想采也采不了，于是界面上那个「重新采集」
按钮其实只重算了聚合——数据永远停在最后一次手动跑脚本的时刻。

**数据有多旧必须看得见**：`/api/meta` 的 `data_freshness` 给出最新一条记录的时间
与滞后小时数，顶栏直接显示「数据截至 …（已滞后 N 天）」。旧数据和新数据在界面上
长得一模一样，不标出来就只能靠猜。

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
| **RK-07** | 数据源改格式**不许静默少算** | `sources/base.py` 的 `ParseStats` 统计解析成功率并写进 `meta`；低于 95% 触发 `parser` 告警，`/api/meta` 暴露版本与成功率 | `test_alerts.py` · `test_collect.py` |

另外四条工程纪律：

- **本地自然日 vs UTC**：价格版本与日切分一律用本地日期（`timeutil.today_local()`），
  禁止 SQLite 的 `date('now')`（UTC）—— UTC+8 的 00:00~08:00 会错选前一天。
  `test_current_version_never_asks_sqlite_for_utc_now` 用 `set_trace_callback` 守住。
- **展示用的时间必须由 Python 算**：会呈现给用户的时间列（告警的 `fired_at` 等）
  一律走 `timeutil.now_local_str()`。SQLite 的 `datetime('now')` 是 UTC，
  曾经让告警时间整整早了 8 小时。`test_alerts_module_never_lets_sqlite_generate_time` 守住。
- **告警不许说已经不成立的话**：一条「未确认且未自动解除」的告警，其 `alert_key`
  必须仍处于激活态。`clear()` 会同时把事件标为 `resolved_at`，`sweep_stale()` 再兜底
  收口跨周期残留 —— 曾经有一条「845 条未定价」的告警在价格补上之后还挂了整整一天。
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
| GET | `/api/meta` | 版本、数据来源、口径标签、汇率、**数据新鲜度**、**解析器健康度** |
| POST | `/api/ingest` | 批量上报（幂等键 `request_id`） |
| GET | `/api/kpi` | 北极星 + 今日 + Token 分层 + 缓存 + 单请求均价 + 健康度 + `budget_detail` / `placeholder_prices` |
| GET | `/api/trend?days=` | 趋势（日成本 / 预算线 / 移动均值 / 构成） |
| GET | `/api/tokens?days=` | Token 分层 |
| GET | `/api/mix?dim=model\|project\|tool\|session&days=` | 构成（各项之和精确等于总额） |
| GET | `/api/insights?days=` | 洞察卡片 |
| GET | `/api/requests?…&limit=&offset=&cache=read\|write` | 明细（可筛 status / model / 有缓存读的记录） |
| GET | `/api/drill/{model}/sessions` | 模型 → 会话下钻 |
| GET/PUT | `/api/budget` | 日 / 月预算与币种 |
| GET | `/api/pricing` | 当前价格表与版本 |
| PUT | `/api/pricing/override` | 新增价格版本（不动历史） |
| PUT | `/api/pricing/credit-rate` | 积分折算率 |
| GET | `/api/currency` | 支持的币种与汇率 |
| GET | `/api/alerts` · POST `/api/alerts/{id}/ack` | 告警列表 / 确认 |
| POST | `/api/refresh` | 重算聚合 + 跑一次告警巡检（**不采集**） |
| POST | `/api/collect` | **真采集**：解析本机会话 → 落库 → 回填缓存 → 重算 → 巡检（`?dry_run=1` 只解析不写库） |
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
| 单元测试 | `pytest -q` | **144 passed** |
| HTTP 契约冒烟 | `python scripts/smoke.py` | **PASS 52/52**（含跨端点口径一致性、构成之和=总额、告警 ack 往返、预算往返、告警三态与时间本地化、采集演练不写库、页面无假数据残留） |
| 前端渲染冒烟 | `node scripts/render_check.mjs` | **PASS 40/40**（Node DOM 桩跑页面脚本，断言 KPI/图表/表格/洞察/告警面板/新鲜度角标均已填充） |
| 隐私红线 | `python scripts/check_privacy.py` | **PASS 12/12** |
| 试点验收七条 | `python scripts/pilot_acceptance.py` | **PASS 7/7**，报告落在 `data/试点验收报告.md` |

常用开关：

```bash
"$PY" scripts/run_regression.py --only pytest,smoke   # 只跑部分层（报告名会带后缀，不覆盖完整报告）
"$PY" scripts/run_regression.py --live-db             # 直接打真实库，不做副本
"$PY" scripts/run_regression.py --base http://127.0.0.1:8787   # 复用已在跑的服务
```

> **判定是「退出码 + 正文」双条件**，不是只看退出码。某些环境（比如带删除保护的机器）
> 会在用例全过之后因为清理临时目录被拦而返回非 0；这时正文里的 `120 passed` 才是真相，
> 回归会标为 `PASS*通过（环境噪声）` 并在报告里写明原因——既不误报 FAIL，也不静默放过。
> 真有失败用例时（`N failed`）永远判 FAIL。

`pilot_acceptance.py` 的三条设计原则：**不碰生产库**（写类检查在临时复制的库上做）、
**可重复**（跑多少次结论一样）、**不当老好人**（任何一条不过就退出码非零）。

---

## 八、试点实测（本机 WorkBuddy 数据）

> 2026-10-11 采集后的快照。本机会话持续写入，数字会随采集增长——
> 这不是不稳定，是数据源本来就在动。

| 指标 | 值 |
| --- | --- |
| 落库行数 / 当月请求 | **1,633 行** / 1,431 次（幂等去重后唯一） |
| 本月累计（API 等价口径） | **$64.90** / 预算 $300（21.6%，时间进度 35%） |
| 厂商口径（积分折算） | $12.62（折算率**未校准**） |
| 月末预测 | $182.91（标注「估算」，方法：月内日均 × 31 天） |
| 预算可支撑 | 39 天 |
| Token | 输入 212.2M + 输出 1.0M + 缓存读 41.0M |
| **缓存命中率** | **88.8%**（traces 估算口径，覆盖 348/1633 = 21% 请求） |
| **缓存节省** | **$11.56**（标注「估算」，方法：Σ 缓存读 ×（输入单价 − 缓存读单价）） |
| 单请求均价 | $0.045355 |
| 解析成功率 | **100%**（解析器 v1，扫描 6,152 行 / 含用量 1,634 条） |
| 交叉校验 | 3 个可比会话逐位一致（差异 0.00%），其余因 trace 留存不全标记「不可比」 |

---

## 九、已知限制（都不是 bug，是当前数据源的客观边界）

1. **缓存 token 来自 traces 估算，不是数据源直报** —— `jsonl` 不提供缓存字段，
   现在由 `traces` 按会话缓存率分摊补齐（见 `app/sources/trace_cache.py`）。
   **命中率 88.8% 是「有 trace 支撑的样本」口径，覆盖 348/1633 = 21% 的请求**；
   若拿全库做分母，未覆盖请求会把数字压低到失真值，所以前端明确标注覆盖率。
   trace 只覆盖会话的部分调用（实测有低至 10% 的），这些会话的结论标为「低可信」。
   ```bash
   "$PY" scripts/ingest_workbuddy.py --backfill-cache           # 演练，看会补多少
   "$PY" scripts/ingest_workbuddy.py --backfill-cache --apply   # 真写（幂等，可重复跑）
   ```
2. **厂商的模型名是内部代号**，公共价格表无法匹配。当前成本数字基于**显式标注的占位价格**
   （`--apply-placeholder-prices`），只用于验证成本链路是否通，**不代表真实账单**。
   顶栏会常驻「口径提示条」说明这件事，不用翻 README。
   拿到真实积分单价后应走 `PUT /api/pricing/override`（只新增版本，不动历史）。
   代号 → 占位价格的映射放在 `app/pricing/placeholder.local.json`，**该文件不进版本库**；
   仓库里只有脱敏示例 `placeholder.example.json`，照它复制一份填入自己的模型名即可。
3. **30 天窗口内只有 7 天有数据**（本机历史有限），趋势图存在真实断点，不是采集失败。
4. **数据不会自己变新，除非你让它采** —— 定时采集默认关闭（`TUM_AUTO_COLLECT=1`
   才开）。关着的时候顶栏会把滞后天数标出来，点一下即可补采。
5. **数据源目前只有 WorkBuddy 一家** —— 产品方案里列的 Claude Code / Cursor 日志
   与厂商 Admin API 尚未接入。这是有意收窄的试点范围，不是遗漏；
   `SourceParser` 抽象已经就位，接第二个源时它才真正被检验。

---

## 十、P0 之外（明确不做）

多用户 / 多租户、远程上报、告警推送渠道（邮件/IM）、价格表自动同步、
预算的部门 / 项目多级分配、导出报表定时任务、移动端适配。
这些都留在产品方案的 P1/P2 列表里，不在本次实现范围。

（CSV 导出是个例外：它是月度复盘刚需，实现成本很低，前端「导出」按钮
直接导出当前区间的明细，已随看板一起交付。）

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
