// 前端渲染契约测试：不用浏览器、不用 jsdom，验证 web/index.html 的动态元数据渲染。
//
// ## 为什么需要它
//
// UI 原来把表清单、表数、行数、引擎名全部写死在 HTML 与 i18n 字典里。数据从 19 万涨到
// 8000 万、表数从 39 到 48 之后，这些数字全错——而且**错得不会报警**，因为它们是静态
// 文本。改成从 /api/catalog 动态读之后，风险换了个形态：接口字段名改了、语言分支写错、
// 重渲染钩子漏了，界面同样会静默显示错的东西。
//
// 这个测试就是那道闸。它实际抓到过一个差 10 倍的 bug：行数缩写按 `n/1e4` 算却配上
// 英文单位 `K`，7992 万被渲染成 "7,992K"（≈799 万）。单位错误比数字缺失危险得多，
// 因为它看起来是个正常数字。
//
// ## 做法
//
// 打一份最小 DOM 桩，把 index.html 的主 script 整块跑起来，捕获写入各元素的 innerHTML，
// 然后拿**真实接口响应**灌进去断言。跑得起来就说明没有未定义变量、字段名对得上；
// 断言则锁住关键事实（表数、行数量级、派生层徽标、治理面板内容）。
//
// 用法：
//     node scripts/ui/render_test.mjs [http://127.0.0.1:8000]
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:8000";
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const html = readFileSync(join(ROOT, "web", "index.html"), "utf8");
const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]);
const main = blocks[blocks.length - 1];        // 业务脚本是最后也最大的一块

const sink = {};                               // 选择器 → 被写入的 innerHTML

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
    get: () => _html,
    set(v) { _html = String(v); sink[el.id || el._sel] = _html; },
  });
  Object.defineProperty(el, "textContent", {
    get: () => _text,
    set(v) { _text = String(v); sink[(el.id || el._sel) + ":text"] = _text; },
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
globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false }) };
globalThis.location = { protocol: "http:", origin: BASE };
let langPref = "zh";
globalThis.localStorage = { getItem: () => langPref, setItem() {} };
globalThis.echarts = { init: () => ({ setOption() {}, resize() {}, dispose() {} }),
                       graphic: { LinearGradient: function () {} } };
// 屏蔽 boot() 的网络调用：它自带 try/catch，抛错会安静退到 baked 模式，
// 而我们要手动灌真实数据，不希望它自己去拉。
globalThis.fetch = async () => { throw new Error("blocked in test"); };
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() {} };
globalThis.setTimeout = () => 0;

const run = new Function(main + `
;globalThis.__X={renderCatalog,renderStatus,rowsShort,rowsPhrase,setLang,
  setCat:d=>{CATALOG=d},setHealth:h=>{HEALTH=h},boot:()=>{BOOTED=true;MODE='live'}};`);
run();
const X = globalThis.__X;

const [cat, health] = await Promise.all([
  fetch2(BASE + "/api/catalog"), fetch2(BASE + "/health"),
]);
async function fetch2(u) {
  const { default: http } = await import("node:http");
  return new Promise((res, rej) => {
    http.get(u, r => { let b = ""; r.on("data", c => b += c);
      r.on("end", () => { try { res(JSON.parse(b)); } catch (e) { rej(e); } }); })
      .on("error", rej);
  });
}

if (cat.error || !cat.totals) {
  console.error("✗ /api/catalog 不可用：" + (cat.error || "无 totals 字段"));
  console.error("  先起后端：bash backend/run.sh");
  process.exit(1);
}

const fails = [];
const must = (c, m) => { if (!c) fails.push(m); };
const pick = k => (sink[k] || "(未写入)").replace(/\s+/g, " ").trim();

X.boot(); X.setHealth(health); X.setCat(cat);
X.renderStatus(); X.renderCatalog();

const bar = pick("dbInfo"), sum = pick('[data-i18n="wm_data_span"]');
const doms = pick(".wm-domains"), gov = pick("govPanel");
console.log("顶栏     " + bar);
console.log("摘要     " + sum);
console.log("治理     " + gov.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200));

must(String(cat.totals.tables).length > 0 && bar.includes(String(cat.totals.tables)),
     `顶栏应含接口给的表数 ${cat.totals.tables}`);
must(cat.engine && bar.includes(cat.engine), `顶栏应含引擎名 ${cat.engine}`);
must(!/PostgreSQL/.test(bar) || cat.engine === "PostgreSQL",
     "顶栏不应出现与实际后端不符的 PostgreSQL");
must(/万行|rows/.test(sum), "摘要应含行数");
must(sum.includes(cat.source === "glue" ? "Glue Data Catalog" : "information_schema"),
     "摘要应标明元数据来源");
// 派生层徽标：这几层原来在 UI 上完全不存在，而"读文档才知道该用哪张"靠它们演示
const layers = Object.keys(cat.totals.by_layer || {}).filter(l => l !== "base" && l !== "meta");
for (const l of layers) must(doms.includes(l), `域卡片应显示 ${l} 层徽标`);
if ((cat.governance || {}).available) {
  must(gov.includes(`${cat.governance.granted_tables} / ${cat.totals.tables}`),
       `治理面板应显示 ${cat.governance.granted_tables} / ${cat.totals.tables}`);
  for (const t of cat.governance.ungranted_tables || [])
    must(gov.includes(t), `治理面板应列出未授权表 ${t}`);
  for (const m of cat.governance.masked || [])
    must(gov.includes(m.table), `治理面板应列出脱敏表 ${m.table}`);
}

// —— 切英文再验一遍：行数单位的阈值 bug 只在这条路上暴露 ——
// 中文用万/亿（1e4/1e8），英文用 K/M/B（1e3/1e6/1e9）。曾经拿 n/1e4 配 'K'，
// 7992 万渲染成 "7,992K"，差 10 倍。
langPref = "en"; X.setLang("en"); X.setCat(cat); X.renderCatalog();
const en = pick('[data-i18n="wm_data_span"]');
console.log("EN 摘要  " + en);
const r = cat.totals.rows;
if (r >= 1e6 && r < 1e9) {
  must(/M rows/.test(en), `英文摘要应用 M 作单位（${r} 行）`);
  must(!/K rows/.test(en), "英文摘要不应用 K（会差 1000 倍）");
}

if (fails.length) {
  console.log(`\n✗ ${fails.length} 项断言失败：`);
  fails.forEach(f => console.log("   - " + f));
  process.exit(1);
}
console.log("\n前端渲染契约 全部通过 ✅");
