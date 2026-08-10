// 线上路径渲染测试：不用浏览器、不用后端、不用 AWS 凭证。
//
// ## 为什么单独有这一个
//
// scripts/ui/render_test.mjs 测的是**本地**路径：起一个 FastAPI，手动把 /api/catalog
// 的响应灌进渲染函数。线上根本不是这条路：
//
//   CloudFront 默认行为回源 S3，只有 /ask 走 VPC origin → 内网 ALB → Fargate relay。
//   relay 只实现 /health 和 /ask，所以 GET /api/catalog 落到 S3 拿 403/404。
//   前端因此要退到同域静态快照 ./catalog.json。
//
// 这条路我没法轻易人工验——要浏览器加 Cognito 登录。而它恰好是最容易静默错的一条：
// renderCatalog() 一旦没跑，欢迎页「背后的数据」那行**保持静态 HTML 原文**，也就是
// v1 的「35 明细表 + 4 治理表 · ~19万行」；页面照常渲染，不报任何错。上一轮就是被这类
// 静默兜底骗过去的（存活探针误打 POST /ask → 405 → 一直在放烘焙数据）。
//
// 所以这个测试直接跑真实的 boot()：设好 window.APP_CONFIG（等价于线上 config.js），
// 让 /api/catalog 返 404、./catalog.json 返真实的 web/catalog.json，然后断言页面上
// 出现的是 48 张表 / Redshift Serverless，而不是 39 张 / PostgreSQL。
//
// 用法：
//     node scripts/ui/render_test_prod.mjs
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CAT = join(ROOT, "web", "catalog.json");

if (!existsSync(CAT)) {
  console.error("✗ 缺 web/catalog.json —— 线上前端就是靠它拿元数据的。");
  console.error("  先生成：./backend/.venv/bin/python scripts/deploy/build_catalog_json.py");
  process.exit(1);
}
const catalog = JSON.parse(readFileSync(CAT, "utf8"));

const html = readFileSync(join(ROOT, "web", "index.html"), "utf8");
const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]);
const main = blocks[blocks.length - 1];

const sink = {};
function mkEl(tag = "div", sel = "?") {
  const el = {
    tagName: tag, _sel: sel, dataset: {}, style: {}, hidden: false,
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    getAttribute: () => null, setAttribute() {}, removeAttribute() {},
    appendChild(c) { (this.children ||= []).push(c); return c; },
    removeChild() {}, remove() {}, scrollIntoView() {},
    addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
    closest: () => null, focus() {}, blur() {}, click() {}, insertAdjacentHTML() {},
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
  };
  let _html = "", _text = "";
  Object.defineProperty(el, "innerHTML", {
    get: () => _html, set(v) { _html = String(v); sink[el.id || el._sel] = _html; },
  });
  Object.defineProperty(el, "textContent", {
    get: () => _text, set(v) { _text = String(v); sink[(el.id || el._sel) + ":text"] = _text; },
  });
  return el;
}
const registry = new Map();
const q = sel => {
  if (!registry.has(sel)) {
    const el = mkEl("div", sel);
    if (sel.startsWith("#")) el.id = sel.slice(1);
    registry.set(sel, el);
  }
  return registry.get(sel);
};

globalThis.document = {
  querySelector: q, getElementById: id => q("#" + id), querySelectorAll: () => [],
  createElement: t => mkEl(t, "created:" + t), documentElement: mkEl("html", "html"),
  addEventListener() {}, body: mkEl("body", "body"), title: "",
};
globalThis.localStorage = { getItem: () => "zh", setItem() {} };
globalThis.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {} }),
                       graphic: { LinearGradient: function () {} } };
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() {} };

// —— 线上的运行时环境 ——
// config.js（S3 上那份）的等价物：配了 askUrl，boot() 会短路进 live 模式。
globalThis.location = { protocol: "https:", origin: "https://d123456abcdef.cloudfront.net" };
globalThis.window = {
  addEventListener() {}, matchMedia: () => ({ matches: false }),
  APP_CONFIG: { authEnabled: true, region: "us-west-2", askUrl: "/ask" },
};

// CloudFront 的行为：/api/catalog 回源 S3 → 该 key 不存在 → 403/404；
// ./catalog.json 存在 → 200。/ask 不会在 boot() 里被 GET（配了 askUrl 就不探针）。
const asked = [];
globalThis.fetch = async (u) => {
  asked.push(String(u));
  if (String(u).includes("/api/catalog")) {
    return { ok: false, status: 403, json: async () => { throw new Error("AccessDenied XML"); } };
  }
  if (String(u).endsWith("catalog.json")) {
    return { ok: true, status: 200, json: async () => catalog };
  }
  throw new Error("unexpected fetch: " + u);
};

new Function(main)();          // 脚本末尾会自己调 boot()

// boot() 是 async 且没被 await，等它把 loadCatalog 的链路跑完。
// 等的必须是 renderCatalog 的产物（摘要行），不能等 #dbInfo：boot() 里
// renderStatus() 是同步的，先一步就把 #dbInfo 写了，拿它当条件会在
// loadCatalog 还没发第二个请求时就退出，测出一堆假失败。
const WAIT_KEY = '[data-i18n="wm_data_span"]';
const t0 = Date.now();
while (!sink[WAIT_KEY] && Date.now() - t0 < 5000) {
  await new Promise(r => setTimeout(r, 10));
}

const fails = [];
const must = (c, m) => { if (!c) fails.push(m); };
const pick = k => (sink[k] || "(未写入)").replace(/\s+/g, " ").trim();
const strip = s => s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

const bar = pick("dbInfo");
const sum = pick('[data-i18n="wm_data_span"]');
const doms = pick(".wm-domains");
const gov = pick("govPanel");

console.log("请求顺序 " + asked.join("  →  "));
console.log("顶栏     " + strip(bar));
console.log("摘要     " + strip(sum));
console.log("治理     " + strip(gov).slice(0, 160));

// ① 兜底真的发生了：先试实时接口，404 后再取静态快照
must(asked.some(u => u.includes("/api/catalog")), "应先尝试实时接口 /api/catalog");
must(asked.some(u => u.endsWith("catalog.json")), "实时接口 404 后应退到 ./catalog.json");

// ② 顶栏是真数据，不是那个写死 PostgreSQL 的旧 i18n 串
must(bar.includes(String(catalog.totals.tables)),
     `顶栏应含表数 ${catalog.totals.tables}（拿不到元数据时会退回写死的旧串）`);
must(bar.includes(catalog.engine), `顶栏应含引擎名 ${catalog.engine}`);
must(!/PostgreSQL/.test(bar), "顶栏不应出现 PostgreSQL —— 那是 db_live_post 兜底串漏出来了");
must(!/35 明细表|39 张表/.test(bar + sum), "不应出现 v1 的写死表数（35 明细 / 39 张）");

// ③ 摘要标明来源，并且**说清这是快照而不是实时**
must(sum.includes("Glue Data Catalog"), "摘要应标明元数据来自 Glue Data Catalog");
must(/万行|亿/.test(sum), "摘要应含行数量级");
if (catalog.snapshot) {
  must(sum.includes("快照时间"), "快照必须标注「快照时间」，不能冒充实时");
  must(sum.includes((catalog.generated_at_utc || "").slice(0, 10)),
       "应显示快照生成日期");
}

// ④ 派生层徽标（演示"读文档才知道该用哪张表"的本体）
const layers = Object.keys(catalog.totals.by_layer || {})
  .filter(l => l !== "base" && l !== "meta");
for (const l of layers) must(doms.includes(l), `域卡片应显示 ${l} 层徽标`);

// ⑤ 治理面板
if ((catalog.governance || {}).available) {
  must(gov.includes(`${catalog.governance.granted_tables} / ${catalog.totals.tables}`),
       `治理面板应显示 ${catalog.governance.granted_tables} / ${catalog.totals.tables}`);
  for (const t of catalog.governance.ungranted_tables || [])
    must(gov.includes(t), `治理面板应列出未授权表 ${t}`);
}

if (fails.length) {
  console.log(`\n✗ ${fails.length} 项断言失败：`);
  fails.forEach(f => console.log("   - " + f));
  process.exit(1);
}
console.log("\n线上路径渲染契约 全部通过 ✅");
