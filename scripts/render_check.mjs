/**
 * 前端逻辑冒烟：把 web/dashboard.html 里的那份脚本，放到一个最小 DOM 桩上真跑一遍。
 *
 * 为什么不用真浏览器：安装 agent-browser 要下 ~500MB Chromium。而这里的失败模式
 * （拼错属性、取到 null、字段名对不上接口）用 DOM 桩 + 真实接口就能全覆盖，
 * 成本却几乎为零。真浏览器只在最后出截图时用一次。
 *
 * 用法：
 *   node scripts/render_check.mjs                      # 默认打 http://127.0.0.1:8787
 *   node scripts/render_check.mjs http://127.0.0.1:9000
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const BASE = (process.argv[2] || "http://127.0.0.1:8787").replace(/\/$/, "");

/* ------------------------------------------------------------------ DOM 桩 */
function makeStyle() {
  return new Proxy({}, { get: () => "", set: () => true });
}

function makeEl(id = "") {
  const el = {
    id,
    _html: "",
    textContent: "",
    innerHTML: "",
    className: "",
    title: "",
    hidden: false,
    disabled: false,
    style: makeStyle(),
    children: [],
    getAttribute: () => null,
    setAttribute: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    appendChild: () => {},
    removeChild: () => {},
    insertBefore: () => {},
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 200 }),
    focus: () => {},
    click: () => {},
  };
  // innerHTML 用 getter/setter，才能断言渲染结果
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  return el;
}

const registry = new Map();
const touched = new Set();

const document = {
  getElementById(id) {
    touched.add(id);
    if (!registry.has(id)) registry.set(id, makeEl(id));
    return registry.get(id);
  },
  querySelector(sel) {
    if (!registry.has(sel)) registry.set(sel, makeEl(sel));
    return registry.get(sel);
  },
  querySelectorAll(sel) {
    if (sel.includes("seg button")) {
      return [7, 30, 90].map((n) => {
        const b = makeEl("range-" + n);
        b.getAttribute = (a) => (a === "data-range" ? String(n) : null);
        return b;
      });
    }
    return [];
  },
  createElement: () => makeEl("created"),
  body: { appendChild: () => {}, removeChild: () => {} },
  addEventListener: () => {},
};

globalThis.document = document;
globalThis.window = globalThis;
globalThis.URL.createObjectURL = () => "blob:stub";
globalThis.URL.revokeObjectURL = () => {};
globalThis.Blob = class { constructor() {} };
globalThis.alert = () => {};

/* ------------------------------------------------------------------ 执行 */
const html = await readFile(path.join(ROOT, "web", "dashboard.html"), "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (blocks.length !== 1) {
  console.error(`FAIL  页面里应当恰好有 1 个 script 块，实际 ${blocks.length} 个`);
  process.exit(1);
}

const problems = [];
process.on("unhandledRejection", (e) => problems.push("未处理的 Promise 拒绝: " + e));
process.on("uncaughtException", (e) => problems.push("未捕获异常: " + (e && e.stack || e)));

// 把 fetch 固定到被测服务，避免脚本里的相对路径在 Node 下失效
const realFetch = globalThis.fetch;
globalThis.fetch = (url, opts) =>
  realFetch(String(url).startsWith("http") ? url : BASE + url, opts);

const checks = [];
function check(name, ok, note = "") {
  checks.push({ name, ok: !!ok, note });
}

// 自检：接口先得通
const health = await realFetch(BASE + "/api/health").then((r) => r.json()).catch((e) => ({ err: String(e) }));
if (health.err) {
  console.error(`FAIL  接口不通（${BASE}）：${health.err}`);
  console.error("      请先启动服务：python -m uvicorn app.main:app --port 8787");
  process.exit(1);
}

// 用 Function 而不是 eval，保持严格模式语义接近 <script>
try {
  new Function(blocks[0])();
} catch (err) {
  console.error("FAIL  脚本执行即抛错：\n" + (err && err.stack || err));
  process.exit(1);
}

// 等渲染完成：盯着几个关键容器的内容
async function waitFor(predicate, ms = 20000, step = 200) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((r) => setTimeout(r, step));
  }
  return false;
}

const txt = (id) => {
  const e = registry.get(id);
  return e ? (e._html || e.textContent || "") : "";
};

await waitFor(() => txt("mtd-cost") && txt("chart-trend").includes("<svg"));

/* ------------------------------------------------------------------ 断言 */
const ghostCheck = ["MODEL_SHARE", "PROJ_SHARE", "BLENDED", "var ROWS", "seeded("];
check("页面不再引用硬编码假数据", !ghostCheck.some((g) => html.includes(g)));

check("本月成本已渲染", /\$[\d,]+\.\d{2}/.test(txt("mtd-cost")), txt("mtd-cost").slice(0, 60));
check("预算已用比例已渲染", /%/.test(txt("mtd-pct")), txt("mtd-pct"));
check("月末预测已渲染", /\$/.test(txt("forecast")), txt("forecast"));
check("厂商口径（第二口径）已渲染", /\$/.test(txt("vendor-cost")), txt("vendor-cost"));
check("今日成本已渲染", /\$/.test(txt("today-cost")), txt("today-cost"));
check("Token 总量已渲染", txt("tok-total") !== "" && txt("tok-total") !== "0", txt("tok-total"));
check(
  "缓存卡片有明确口径（数字或显式未上报）",
  /%/.test(txt("hit-rate")) || txt("hit-meta").includes("未上报"),
  txt("hit-rate") + " / " + txt("hit-meta").slice(0, 50),
);
check("缓存卡片不静默显示 0.0%", txt("hit-rate") !== "0.0%", txt("hit-rate"));
check("单请求成本已渲染", /\$/.test(txt("per-req")), txt("per-req"));
check("请求数已渲染", txt("req-count") !== "0", txt("req-count"));

check("趋势图渲染为 SVG", txt("chart-trend").includes("<svg"), `${txt("chart-trend").length} 字节`);
check("趋势图有折线路径", txt("chart-trend").includes("<path"));
check("趋势提示含区间合计", txt("trend-hint").includes("区间合计"), txt("trend-hint"));
check("Token 分层图渲染为 SVG", txt("chart-tokens").includes("<svg"));
check("Token 分层图有矩形柱", (txt("chart-tokens").match(/<rect/g) || []).length > 4);

check("构成区（按模型）已渲染", txt("mix-model").includes("brow"), `${(txt("mix-model").match(/class="brow"/g) || []).length} 行`);
check("构成区（按项目）已渲染", txt("mix-proj").includes("brow"));
check("构成区合计已渲染", /\$/.test(txt("mix-model-total")), txt("mix-model-total"));

check("洞察区有卡片或有明确空态", txt("insights").includes('class="ins') || txt("insights").includes("empty"));
check(
  "洞察区未使用占位文案凑数",
  !/示例|Lorem|TODO|占位/.test(txt("insights")),
);

check("明细表有数据行", (txt("tbody").match(/<tr/g) || []).length > 0, `${(txt("tbody").match(/<tr/g) || []).length} 行`);
check("明细表页脚有成本汇总", txt("tbl-foot").includes("区间成本"), txt("tbl-foot").slice(0, 80));

const src = registry.get("chip-source");
check("数据来源角标已更新", !!src && !/加载中/.test(src.textContent), src && src.textContent);
const cal = registry.get("chip-caliber");
check("口径角标已标注", !!cal && /口径/.test(cal.textContent), cal && cal.textContent);

/* ------------------------------------------------------------------ 结果 */
console.log(`接口自检：${BASE}  ok=${health.ok}  usage_fact=${health.usage_fact_rows} 行\n`);
let failed = 0;
for (const c of checks) {
  if (!c.ok) failed++;
  console.log(`[${c.ok ? "PASS" : "FAIL"}] ${c.name}${c.note ? "  " + c.note : ""}`);
}
if (problems.length) {
  console.log("\n运行时问题：");
  problems.forEach((p) => console.log("  · " + p));
  failed += problems.length;
}

console.log();
if (failed) {
  console.log(`RENDER: FAIL  ${failed}/${checks.length + problems.length} 项不通过`);
  process.exit(1);
}
console.log(`RENDER: PASS  ${checks.length}/${checks.length} 项通过`);
