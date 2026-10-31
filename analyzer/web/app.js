/* A股数据分析仪表盘 —— 前端逻辑（原生 JS + ECharts） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const UP = "#ef4b57";      // 红涨
const DOWN = "#2ebd85";    // 绿跌
const FLAT = "#8b98ab";
const ACCENT = "#4f8ef7";
const ANNUALIZE = 244;
const PALETTE = ["#4f8ef7", "#f0a03c", "#2ebd85", "#b07cf7", "#ef4b57", "#3cc7dd", "#e8d05a"];
const STOCK_HISTORY_MAX = 10;   // 搜索历史最多保留条数

const state = {
  marketDays: 365,     // 市场页区间（图表与排行榜共用）
  stockCode: null,
  stockData: null,
  ai: null,            // 当前个股的 AI 分析报告（正在展示的）
  aiByCode: {},        // code -> 报告 payload（前端缓存：轮询完成/切股后不丢，切回直显）
  aiLoading: false,    // （已废弃，保留字段防旧引用）改用 aiPolling 按 code 跟踪
  aiPolling: {},       // code -> job_id（该股生成任务轮询中；"submitting" = POST 在途）
  stockDays: 365,
  rankView: "stock",   // 排行榜视图: stock 个股 | board 板块
  rankBoard: "",       // 板块筛选（board_code，"" = 全部；行业/概念均可）
  rankMarket: "",      // 市场板块筛选（sh_main/sz_main/cyb/kcb/bj，"" = 全部）
  boardType: "industry",  // 板块视图类型: industry 行业 | concept 概念
  conceptAvailable: false, // 是否有概念板块数据
  boardsLoaded: false, // 板块下拉是否已填充
  fund: null,
  fundMode: "quarter",
  watchlist: [],  // [{code,name,latest_price,pct_chg,total_mv,fund,info}]
  wlNewsCode: null,  // 当前消息面板展示的股票
  wlDetail: new Set(),  // 已展开明细行的自选股
  wlVal: {},         // code -> 估值快照（明细行缓存）
  stockTrades: [],     // 当前个股的买卖流水 [{id,code,date,side,price,shares,note}]
  tradeByIdx: {},      // K 线下标 -> 当日交易（tooltip 用）
  tradesByCode: {},    // code -> 流水（自选股持仓列用）
  // K线周期: "60" 时 | "d" 日 | "w" 周 | "m" 月（周/月由前端聚合日线，时走 /api/hourly）
  klineFreq: lsGet("klineFreq", "d"),
  klineView: null,     // 当前 K 线图的数据视图（renderKlineChart 时重建）
  tradeDetailOpen: lsGet("tradeDetailOpen", true),   // K 线下方交易明细表是否展开
  wlCollapsed: lsGet("wlCollapsed", false),          // 自选股列表是否折叠
  // 搜索历史（搜索框旁的快捷 chips）：[[code, name], ...] 最近在前，单条可删
  stockHistory: (() => {
    const h = lsGet("stockHistory", []);
    return Array.isArray(h)
      ? h.filter((x) => Array.isArray(x) && typeof x[0] === "string" && typeof x[1] === "string")
      : [];
  })(),
  // 交易记录页（CRUD）
  tradesPage: {
    scope: "all",       // all | watchlist
    q: "",              // 搜索词
    editing: null,      // 正在编辑的 id（null = 新增模式）
    editingCode: null,  // 编辑模式下的股票代码
    formSide: "buy",    // 表单当前方向
    trades: [],         // 全部流水
    meta: {},           // code -> {name, latest_price}
    batchRows: [],      // 批量解析结果（待导入）
    batchCode: null,    // 批量目标股票
  },
  // 费用估算模型（localStorage 持久化）：佣金‱ / 每笔最低元；印花/过户为市场固定值
  feeCfg: (() => {
    const def = { comm: 5, min: 0, stamp: 0.05, transfer: 0.001 };
    try {
      return { ...def, ...JSON.parse(localStorage.getItem("feeCfg") || "{}") };
    } catch (_) { return def; }
  })(),
};

function wlHas(code) { return state.watchlist.some((w) => w.code === code); }

/** localStorage 读写（失败静默，如隐私模式）。 */
function lsGet(key, def) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? def : JSON.parse(v);
  } catch (_) { return def; }
}
function lsSet(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch (_) {}
}

let META = null;
const charts = {};

/* ============================== 工具 ============================== */

async function api(path) {
  const r = await fetch(path);
  let j = null;
  try { j = await r.json(); } catch (_) {}
  if (r.status === 401 && j && j.auth) showAuth("登录已过期，请重新登录");
  if (!r.ok) throw new Error((j && j.error) || r.statusText);
  return j;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fmtPct(x, d = 2) {
  if (x == null || !isFinite(x)) return "—";
  const v = x * 100;
  return (v > 0 ? "+" : "") + v.toFixed(d) + "%";
}
function pctCls(x) { return x > 0.0001 ? "up" : x < -0.0001 ? "down" : "flat"; }
function fmtMv(yuan) {
  if (yuan == null || !isFinite(yuan)) return "—";
  if (yuan >= 1e12) return (yuan / 1e12).toFixed(2) + " 万亿";
  if (yuan >= 1e8) return (yuan / 1e8).toFixed(0) + " 亿";
  return (yuan / 1e4).toFixed(0) + " 万";
}
function fmtAmt(yi) {
  if (yi == null || !isFinite(yi)) return "—";
  return yi >= 10000 ? (yi / 10000).toFixed(2) + " 万亿" : yi.toFixed(0) + " 亿";
}
function fmtYuan(v) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + " 万亿";
  if (a >= 1e8) return (v / 1e8).toFixed(1) + " 亿";
  if (a >= 1e4) return (v / 1e4).toFixed(1) + " 万";
  return v.toFixed(0);
}
function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function shiftDate(dateStr, days) {
  // 只取日期部分（时K线标签形如 "2026-10-23 14:00"，直接拼会得到非法日期）
  const d = new Date(String(dateStr).slice(0, 10) + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}
function periodStart(days) {
  if (!META) return null;
  return days > 0 ? shiftDate(META.date_max, days) : META.date_min;
}
function normalize(arr) {
  const base = arr.find((v) => v != null && v > 0);
  if (base == null) return arr.map(() => null);
  return arr.map((v) => (v == null ? null : +((v / base) * 100).toFixed(3)));
}
function movingAvg(arr, n) {
  const out = new Array(arr.length).fill(null);
  let sum = 0;
  for (let i = 0; i < arr.length; i++) {
    sum += arr[i] || 0;
    if (i >= n) sum -= arr[i - n] || 0;
    out[i] = i >= n - 1 ? +(sum / n).toFixed(3) : null;
  }
  return out;
}

/* ============================== 图表基础 ============================== */

const AXIS = {
  axisLine: { lineStyle: { color: "#2a3342" } },
  axisTick: { show: false },
  axisLabel: { color: "#8b98ab", fontSize: 11 },
  splitLine: { lineStyle: { color: "rgba(42,51,66,.55)" } },
};
const TOOLTIP = {
  trigger: "axis",
  axisPointer: { type: "cross", label: { backgroundColor: "#2a3342" } },
  backgroundColor: "rgba(22,27,36,.95)",
  borderColor: "#2a3342",
  textStyle: { color: "#dce3ee", fontSize: 12 },
};

function mkChart(id) {
  const el = document.getElementById(id);
  if (charts[id]) charts[id].dispose();
  const c = echarts.init(el);
  charts[id] = c;
  return c;
}

/* ============================== 市场概览 ============================== */

async function loadMarket() {
  const start = periodStart(state.marketDays);
  const m = await api(`/api/market?start=${start}&end=${META.date_max}`);
  renderMarketCards(m);
  renderIndexChart(m);
  renderBreadthChart(m);
  renderAmountChart(m);
  renderNavChart(m);
}

function renderMarketCards(m) {
  const i = m.dates.length - 1;
  const adv = m.adv[i], dec = m.dec[i], flat = m.flat[i];
  const ret = m.mean_ret[i], amt = m.amount_yi[i];
  const hs = m.indexes["000300.SH"];
  let hsTxt = "—", hsCls = "flat", hsSub = "";
  if (hs) {
    const c = hs.close;
    let li = c.length - 1, pi = li - 1;
    while (li >= 0 && c[li] == null) li--;
    while (pi >= 0 && c[pi] == null) pi--;
    if (li >= 0 && pi >= 0 && c[pi] > 0) {
      const chg = c[li] / c[pi] - 1;
      hsTxt = c[li].toFixed(2);
      hsCls = pctCls(chg);
      hsSub = fmtPct(chg);
    }
  }
  const total = adv + dec + flat;
  $("#market-cards").innerHTML = `
    <div class="card"><div class="k">上涨 / 下跌家数（${m.dates[i]}）</div>
      <div class="v"><span class="up">${adv.toLocaleString()}</span> <span class="flat">/</span> <span class="down">${dec.toLocaleString()}</span></div>
      <div class="s">平盘 ${flat} · 涨跌比 ${dec ? (adv / dec).toFixed(2) : "—"}</div></div>
    <div class="card"><div class="k">全市场平均涨跌</div>
      <div class="v ${pctCls(ret)}">${fmtPct(ret)}</div>
      <div class="s">等权口径</div></div>
    <div class="card"><div class="k">全市场成交额</div>
      <div class="v">${amt != null ? fmtAmt(amt) : "—"}</div>
      <div class="s">当日</div></div>
    <div class="card"><div class="k">沪深300</div>
      <div class="v ${hsCls}">${hsTxt}</div>
      <div class="s ${hsCls}">${hsSub}</div></div>
  `;
}

function renderIndexChart(m) {
  const c = mkChart("chart-indices");
  const series = Object.entries(m.indexes).map(([code, idx], k) => ({
    name: idx.name,
    type: "line",
    data: normalize(idx.close),
    showSymbol: false,
    lineStyle: { width: 1.6 },
    itemStyle: { color: PALETTE[k % PALETTE.length] },
    emphasis: { focus: "series" },
  }));
  c.setOption({
    color: PALETTE,
    tooltip: { ...TOOLTIP, valueFormatter: (v) => (v == null ? "—" : v.toFixed(2)) },
    legend: { top: 0, textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    grid: { left: 56, right: 18, top: 34, bottom: 58 },
    xAxis: { type: "category", data: m.dates, ...AXIS },
    yAxis: { type: "log", ...AXIS, axisLabel: { ...AXIS.axisLabel, formatter: (v) => v.toFixed(0) } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 12, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } }],
    series,
  });
}

function renderBreadthChart(m) {
  const c = mkChart("chart-breadth");
  c.setOption({
    tooltip: TOOLTIP,
    legend: { top: 0, data: ["上涨家数", "下跌家数"], textStyle: { color: "#8b98ab", fontSize: 11 } },
    grid: { left: 64, right: 18, top: 34, bottom: 58 },
    xAxis: { type: "category", data: m.dates, ...AXIS },
    yAxis: { type: "value", ...AXIS },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 12, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } }],
    series: [
      { name: "上涨家数", type: "line", data: m.adv, showSymbol: false, lineStyle: { width: 1, color: UP }, itemStyle: { color: UP }, areaStyle: { color: "rgba(239,75,87,.22)" } },
      { name: "下跌家数", type: "line", data: m.dec, showSymbol: false, lineStyle: { width: 1, color: DOWN }, itemStyle: { color: DOWN }, areaStyle: { color: "rgba(46,189,133,.22)" } },
    ],
  });
}

function renderAmountChart(m) {
  const c = mkChart("chart-amount");
  c.setOption({
    tooltip: { ...TOOLTIP, valueFormatter: (v) => (v == null ? "—" : fmtAmt(v)) },
    grid: { left: 70, right: 18, top: 24, bottom: 58 },
    xAxis: { type: "category", data: m.dates, ...AXIS },
    yAxis: { type: "value", ...AXIS },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 12, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } }],
    series: [{
      name: "成交额", type: "line", data: m.amount_yi, showSymbol: false,
      lineStyle: { width: 1.4, color: ACCENT }, itemStyle: { color: ACCENT },
      areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(79,142,247,.35)" }, { offset: 1, color: "rgba(79,142,247,.02)" }] } },
    }],
  });
}

function renderNavChart(m) {
  const c = mkChart("chart-nav");
  const eqw = normalize(m.nav_eqw);
  const hs300 = m.indexes["000300.SH"] ? normalize(m.indexes["000300.SH"].close) : null;
  const series = [{
    name: "等权全市场", type: "line", data: eqw, showSymbol: false,
    lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT },
  }];
  if (hs300) series.push({
    name: "沪深300", type: "line", data: hs300, showSymbol: false,
    lineStyle: { width: 1.4, type: "dashed", color: "#f0a03c" }, itemStyle: { color: "#f0a03c" },
  });
  c.setOption({
    tooltip: { ...TOOLTIP, valueFormatter: (v) => (v == null ? "—" : v.toFixed(2)) },
    legend: { top: 0, textStyle: { color: "#8b98ab", fontSize: 11 } },
    grid: { left: 56, right: 18, top: 34, bottom: 58 },
    xAxis: { type: "category", data: m.dates, ...AXIS },
    yAxis: { type: "log", ...AXIS, axisLabel: { ...AXIS.axisLabel, formatter: (v) => v.toFixed(0) } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 12, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } }],
    series,
  });
}

/* ============================== 自选股 ============================== */

async function loadWatchlist() {
  try {
    const d = await api("/api/watchlist");
    state.watchlist = d.codes || [];
    state.syncing = d.syncing || [];
    renderWatchlist();
    // 持仓列：异步补齐（不阻塞自选列表首屏）
    loadTradePositions();
    // 当前股票页的星标按钮状态同步
    const sb = $("#star-btn");
    if (sb && state.stockCode) {
      const on = wlHas(state.stockCode);
      sb.classList.toggle("on", on);
      sb.innerHTML = on ? "★ 已自选" : "☆ 加自选";
      sb.title = on ? "移出自选" : "加入自选";
    }
  } catch (err) {
    toast("自选股加载失败: " + err.message);
  }
}

async function loadTradePositions() {
  try {
    const d = await api("/api/trades");
    const byCode = {};
    for (const t of d.trades || []) (byCode[t.code] = byCode[t.code] || []).push(t);
    state.tradesByCode = byCode;
    renderWatchlist();
  } catch (_) { /* 持仓列可选，失败静默 */ }
}

/** 移动加权平均：持仓股数 / 均价 / 已实现盈亏 / 浮动盈亏（lastPrice 为最新真实价）。 */
function computePosition(ts, lastPrice) {
  let held = 0, avg = 0, realized = 0;
  const list = [...(ts || [])].sort((a, b) =>
    (a.date + a.created < b.date + b.created) ? -1 : 1);
  for (const t of list) {
    if (t.side === "buy") {
      avg = (avg * held + t.price * t.shares) / (held + t.shares);
      held += t.shares;
    } else {
      realized += (t.price - avg) * Math.min(t.shares, held);
      held = Math.max(0, held - t.shares);
    }
  }
  const unrealized = (lastPrice != null && held) ? (lastPrice - avg) * held : 0;
  return { held, avg_cost: held ? avg : null, realized, unrealized, n_trades: (ts || []).length };
}

function renderWatchlist() {
  const box = $("#watchlist-box");
  const wl = state.watchlist;
  applyWlCollapsed();
  if (!wl.length) { box.style.display = "none"; return; }
  box.style.display = "block";
  $("#wl-count").textContent = wl.length;
  const tbody = $("#wl-table tbody");
  tbody.innerHTML = wl.map((w) => {
    const f = w.fund;
    const pos = computePosition(state.tradesByCode[w.code], w.latest_price);
    const posCells = pos.n_trades
      ? `<td class="num">${pos.held ? pos.held.toLocaleString() + " 股" : "已清仓"}</td>
         <td class="num" style="color:var(--text-dim)">${pos.avg_cost != null ? fmtNum(pos.avg_cost) : "—"}${pos.held ? `<span class="${pctCls(pos.unrealized)}"> ${fmtSignedNum(pos.unrealized)}</span>` : ""}</td>`
      : `<td class="num" style="color:var(--text-dim)">—</td><td class="num" style="color:var(--text-dim)">—</td>`;
    const fundCells = f
      ? `<td>${periodLabel(f.period, "full")}</td>
      <td class="num">${fmtYuan(f.rev)}</td>
      <td class="num ${pctCls(f.rev_yoy)}">${fmtPct(f.rev_yoy, 1)}</td>
      <td class="num">${fmtYuan(f.net_profit)}</td>
      <td class="num ${pctCls(f.net_profit_yoy)}">${fmtPct(f.net_profit_yoy, 1)}</td>
      <td class="num">${f.roe != null ? (f.roe * 100).toFixed(1) + "%" : "—"}</td>`
      : `<td colspan="6"><button class="mini-btn" data-fund="${w.code}" style="margin-left:0" title="从同花顺抓取并缓存财报（约 1~2 秒）">抓取财报</button></td>`;
    return `
    <tr data-code="${w.code}">
      <td class="wl-drag" title="拖动调整顺序">⠿</td>
      <td style="color:var(--text-dim)">${w.code}</td>
      <td>${w.name}</td>
      <td class="num">${w.latest_price != null ? w.latest_price.toFixed(2) : "—"}</td>
      <td class="num" style="color:var(--text-dim)">${w.hfq_close != null ? w.hfq_close.toFixed(2) : "—"}</td>
      <td class="num ${pctCls(w.pct_chg)}">${fmtPct(w.pct_chg)}</td>
      <td class="num" style="color:var(--text-dim)">${fmtMv(w.total_mv)}</td>
      <td class="num" style="color:var(--text-dim)">${
        w.minute_bars != null
          ? `${w.minute_bars} · ${w.minute_last.slice(5)}`
          : (state.syncing || []).includes(w.code)
            ? "同步中…"
            : "—"
      }</td>
      ${posCells}
      ${fundCells}
      <td class="num">
        <button class="mini-btn" data-detail="${w.code}" title="展开/收起：完整财报 + 个股信息 + 估值"
          ${state.wlDetail.has(w.code) ? 'style="color:var(--accent);border-color:var(--accent)"' : ""}>详情</button>
        <button class="mini-btn" data-news="${w.code}" title="查看最新消息（东方财富，30 分钟缓存）">📰 消息</button>
        <button class="mini-btn" data-rm="${w.code}" title="移除">✕</button>
      </td>
    </tr>${state.wlDetail.has(w.code) ? wlDetailRow(w) : ""}`;
  }).join("");
  tbody.querySelectorAll("tr[data-code]").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      if (e.target.closest(".wl-drag")) return;   // 拖拽手柄不触发跳转
      pickStock(tr.dataset.code);
    })
  );
  bindWatchlistDnD(tbody);
  tbody.querySelectorAll("[data-rm]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleWatch(b.dataset.rm); })
  );
  tbody.querySelectorAll("[data-news]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); loadWlNews(b.dataset.news, false); })
  );
  tbody.querySelectorAll("[data-detail]").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleWlDetail(b.dataset.detail); })
  );
  tbody.querySelectorAll("[data-fund]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      b.textContent = "抓取中…"; b.disabled = true;
      try {
        await api("/api/fundamental?code=" + b.dataset.fund);
        toast("财报已抓取: " + b.dataset.fund);
        await loadWatchlist();
      } catch (err) {
        toast(err.message);
        b.textContent = "抓取财报"; b.disabled = false;
      }
    })
  );
  // 消息面板对应的股票被移除后隐藏面板
  if (state.wlNewsCode && !wl.some((w) => w.code === state.wlNewsCode)) {
    state.wlNewsCode = null;
    $("#wl-news").style.display = "none";
  }
  // 明细行对应的股票被移除后收起
  state.wlDetail.forEach((c) => {
    if (!wl.some((w) => w.code === c)) state.wlDetail.delete(c);
  });
}

/* ---------- 自选股折叠 ---------- */

function applyWlCollapsed() {
  const box = $("#watchlist-box");
  const btn = $("#wl-collapse");
  if (!box || !btn) return;
  box.classList.toggle("collapsed", !!state.wlCollapsed);
  btn.textContent = state.wlCollapsed ? "▸" : "▾";
  btn.title = state.wlCollapsed ? "展开自选股列表" : "折叠自选股列表";
}

/* ---------- 自选股拖拽排序（HTML5 DnD，顺序持久化到 watchlist.json） ---------- */

let wlDragCode = null;   // 当前拖动中的自选代码

function bindWatchlistDnD(tbody) {
  // 拖拽只从行首 ⠿ 手柄发起（按下时才置 draggable，避免影响行内文本选择/按钮点击）
  tbody.querySelectorAll("tr[data-code]").forEach((tr) => {
    const handle = tr.querySelector(".wl-drag");
    if (!handle) return;
    handle.addEventListener("mousedown", () => { tr.draggable = true; });
    handle.addEventListener("mouseup", () => { tr.draggable = false; });
    tr.addEventListener("dragstart", (e) => {
      wlDragCode = tr.dataset.code;
      tr.classList.add("wl-dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", wlDragCode); } catch (_) {}
    });
    tr.addEventListener("dragend", () => {
      tr.draggable = false;
      wlDragCode = null;
      clearWlDropHints(tbody);
    });
  });
  // 事件委托：主行与明细行都能接收（明细行折算到其主行之后）；tbody 级只绑定一次
  if (!tbody._wlDnDBound) {
    tbody._wlDnDBound = true;
    tbody.addEventListener("dragover", (e) => {
      if (!wlDragCode) return;
      const target = wlDropTarget(tbody, e.target);
      if (!target) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const before = wlDropBefore(target.tr, e.clientY);
      clearWlDropHints(tbody);
      if (target.code !== wlDragCode)
        target.tr.classList.add(before ? "wl-drop-above" : "wl-drop-below");
    });
    tbody.addEventListener("drop", (e) => {
      if (!wlDragCode) return;
      const target = wlDropTarget(tbody, e.target);
      if (!target) return;
      e.preventDefault();
      const dragCode = wlDragCode;
      const before = wlDropBefore(target.tr, e.clientY);
      const targetCode = target.code;
      wlDragCode = null;
      clearWlDropHints(tbody);
      if (targetCode !== dragCode) reorderWatchlist(dragCode, targetCode, before);
    });
  }
}

/** 拖放落点 -> 目标主行 {tr, code}；明细行归属其主行（视为“之后”）。 */
function wlDropTarget(tbody, el) {
  const tr = el && el.closest ? el.closest("tr") : null;
  if (!tr) return null;
  if (tr.dataset.code) return { tr, code: tr.dataset.code };
  const forCode = tr.dataset.detailFor;
  if (!forCode) return null;
  const main = tbody.querySelector(`tr[data-code="${forCode}"]`);
  return main ? { tr: main, code: forCode } : null;
}

/** 指针在行上半部=插到目标之前，下半部=之后。 */
function wlDropBefore(tr, clientY) {
  const r = tr.getBoundingClientRect();
  return clientY < r.top + r.height / 2;
}

function clearWlDropHints(tbody) {
  tbody.querySelectorAll(".wl-drop-above, .wl-drop-below")
    .forEach((tr) => tr.classList.remove("wl-drop-above", "wl-drop-below"));
}

/** 本地先重排（乐观更新），POST reorder 持久化；失败回滚。 */
async function reorderWatchlist(dragCode, targetCode, before) {
  const codes = state.watchlist.map((w) => w.code);
  const from = codes.indexOf(dragCode);
  let to = codes.indexOf(targetCode);
  if (from < 0 || to < 0) return;
  codes.splice(from, 1);
  to = codes.indexOf(targetCode);
  if (to < 0) return;
  if (!before) to += 1;
  codes.splice(to, 0, dragCode);
  const prev = state.watchlist;
  state.watchlist = codes.map((c) => prev.find((w) => w.code === c)).filter(Boolean);
  renderWatchlist();
  try {
    await apiPost("/api/watchlist", { action: "reorder", codes });
    toast("自选顺序已保存");
  } catch (err) {
    state.watchlist = prev;
    renderWatchlist();
    toast("自选顺序保存失败: " + err.message);
  }
}

async function loadWlNews(code, refresh) {
  const w = state.watchlist.find((x) => x.code === code);
  state.wlNewsCode = code;
  const box = $("#wl-news"), list = $("#wl-news-list"), hint = $("#wl-news-hint");
  box.style.display = "block";
  $("#wl-news-title").textContent = `最新消息 · ${w ? w.name : ""} ${code}`;
  hint.textContent = "";
  list.innerHTML = `<div class="hint" style="padding:10px 4px">加载中…${refresh ? "（强制刷新，绕过缓存）" : ""}</div>`;
  try {
    const d = await api(`/api/news?code=${code}${refresh ? "&refresh=1" : ""}`);
    hint.textContent = `更新于 ${d.updated}${d.stale ? "（网络异常，显示旧缓存）" : ""} · 来源：东方财富 · 点标题可打开原文`;
    list.innerHTML = (d.items || []).length
      ? d.items.map((it) => `
        <div class="news-item">
          <div class="news-time">${escHtml(it.time || "")}</div>
          <div class="news-body">
            ${it.url
              ? `<a class="news-title" href="${it.url}" target="_blank" rel="noopener">${escHtml(it.title)}</a>`
              : `<span class="news-title">${escHtml(it.title)}</span>`}
            ${it.source ? `<span class="news-src">${escHtml(it.source)}</span>` : ""}
            ${it.digest ? `<div class="news-digest">${escHtml(it.digest)}</div>` : ""}
          </div>
        </div>`).join("")
      : `<div class="hint" style="padding:10px 4px">暂无新闻</div>`;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    list.innerHTML = `<div class="hint" style="padding:10px 4px">加载失败：${escHtml(err.message)}</div>`;
  }
}

/* ---------- 自选股明细行：完整财报 + 个股信息 + 估值 ---------- */

function yoyEm(v) {
  if (v == null) return "";
  return ` <em class="${pctCls(v)}">${(v > 0 ? "+" : "") + (v * 100).toFixed(1)}%</em>`;
}

function wlDetailRow(w) {
  const f = w.fund, inf = w.info || {};
  const pc = (v) => (v == null ? "<b>—</b>"
    : `<b class="${pctCls(v)}">${(v > 0 ? "+" : "") + (v * 100).toFixed(1)}%</b>`);
  let week = "—";
  if (inf.week52_high != null && inf.week52_low != null && w.latest_price != null) {
    const span = inf.week52_high - inf.week52_low;
    const pos = span > 0 ? Math.round(((w.latest_price - inf.week52_low) / span) * 100) : null;
    week = `${inf.week52_low.toFixed(2)} ~ ${inf.week52_high.toFixed(2)}${pos != null ? ` · 位置 ${pos}%` : ""}`;
  }
  const infoSec = `
    <div class="wl-dsec">
      <div class="wl-dsec-t">个股信息<span class="wl-dsec-s">本地数据</span></div>
      <div class="wl-dgrid">
        <span class="kv"><i>流通市值</i><b>${fmtMv(inf.float_mv)}</b></span>
        <span class="kv"><i>换手率</i><b>${inf.turnover != null ? (inf.turnover * 100).toFixed(2) + "%" : "—"}</b></span>
        <span class="kv"><i>52周区间</i><b>${week}</b></span>
        <span class="kv"><i>年初至今</i>${pc(inf.ytd_chg)}</span>
        <span class="kv"><i>1年涨跌幅</i>${pc(inf.y1y_chg)}</span>
      </div>
    </div>`;
  const fundSec = f ? `
    <div class="wl-dsec">
      <div class="wl-dsec-t">财务（${periodLabel(f.period, "full")}）<span class="wl-dsec-s">同花顺财务摘要 · 累计口径</span></div>
      <div class="wl-dgrid">
        <span class="kv"><i>营业总收入</i><b>${fmtYuan(f.rev)}${yoyEm(f.rev_yoy)}</b></span>
        <span class="kv"><i>净利润</i><b>${fmtYuan(f.net_profit)}${yoyEm(f.net_profit_yoy)}</b></span>
        <span class="kv"><i>扣非净利润</i><b>${fmtYuan(f.kdj_profit)}${yoyEm(f.kdj_profit_yoy)}</b></span>
        <span class="kv"><i>EPS（元）</i><b>${f.eps != null ? f.eps.toFixed(2) : "—"}</b></span>
        <span class="kv"><i>ROE</i><b>${f.roe != null ? (f.roe * 100).toFixed(1) + "%" : "—"}</b></span>
        <span class="kv"><i>毛利率</i><b>${f.gross_margin != null ? (f.gross_margin * 100).toFixed(1) + "%" : "—"}</b></span>
        <span class="kv"><i>净利率</i><b>${f.net_margin != null ? (f.net_margin * 100).toFixed(1) + "%" : "—"}</b></span>
        <span class="kv"><i>资产负债率</i><b>${f.debt_ratio != null ? (f.debt_ratio * 100).toFixed(1) + "%" : "—"}</b></span>
      </div>
    </div>`
    : `
    <div class="wl-dsec">
      <div class="wl-dsec-t">财务<span class="wl-dsec-s">无缓存</span></div>
      <div class="wl-dgrid"><span class="hint">无财报缓存，请先点上方「抓取财报」</span></div>
    </div>`;
  const valSec = `
    <div class="wl-dsec">
      <div class="wl-dsec-t">估值<span class="wl-dsec-s" id="vl-sub-${w.code}">加载中…</span></div>
      <div class="wl-dgrid" id="vl-grid-${w.code}"><span class="hint">加载中…</span></div>
    </div>`;
  const head = document.querySelector("#wl-table thead tr");
  const colspan = head ? head.children.length : 16;
  return `
    <tr class="wl-detail-row" data-detail-for="${w.code}">
      <td colspan="${colspan}"><div class="wl-detail">${infoSec}${valSec}${fundSec}</div></td>
    </tr>`;
}

function toggleWlDetail(code) {
  if (state.wlDetail.has(code)) state.wlDetail.delete(code);
  else state.wlDetail.add(code);
  renderWatchlist();
  if (state.wlDetail.has(code)) loadWlVal(code);
}

async function loadWlVal(code) {
  const grid = document.getElementById("vl-grid-" + code);
  if (!grid) return;
  if (state.wlVal[code]) { renderWlVal(code, state.wlVal[code]); return; }
  try {
    const d = await api("/api/valuation?code=" + code);
    state.wlVal[code] = d;
    renderWlVal(code, d);
  } catch (err) {
    const sub = document.getElementById("vl-sub-" + code);
    if (sub) sub.textContent = "";
    grid.innerHTML = `<span class="hint">暂不可用：${escHtml(err.message)}</span>`;
  }
}

function renderWlVal(code, d) {
  const grid = document.getElementById("vl-grid-" + code);
  if (!grid) return;
  const num = (v, dec = 2) => (v == null ? "—" : v.toFixed(dec));
  const sub = document.getElementById("vl-sub-" + code);
  if (sub) sub.textContent = `${d.date || ""}${d.stale ? "（旧缓存）" : ""} · 东方财富估值分析`;
  const kv = (k, v) => `<span class="kv"><i>${k}</i><b>${v}</b></span>`;
  grid.innerHTML =
    kv("PE(TTM)", num(d.pe_ttm)) +
    kv("PE(静)", num(d.pe_static)) +
    kv("PB", num(d.pb)) +
    kv("PEG", num(d.peg)) +
    kv("PS", num(d.ps)) +
    kv("PCF", num(d.pcf));
}

async function toggleWatch(code) {
  try {
    const action = wlHas(code) ? "remove" : "add";
    const d = await apiPost("/api/watchlist", { action, code });
    const newCodes = d.codes || [];
    const kept = state.watchlist.filter((w) => newCodes.includes(w.code));
    const ordered = newCodes
      .map((c) => kept.find((w) => w.code === c))
      .filter(Boolean);
    // 新加入的项补齐名称/价格（服务端只返回 codes）
    for (const c of newCodes) {
      if (ordered.some((w) => w.code === c)) continue;
      const rows = await api("/api/stocks?q=" + c);
      const hit = rows.find((r) => r.code === c);
      if (hit) ordered.push(hit);
    }
    state.watchlist = ordered;
    renderWatchlist();
    const on = newCodes.includes(code);
    const sb = $("#star-btn");
    if (sb && state.stockCode === code) {
      sb.classList.toggle("on", on);
      sb.innerHTML = on ? "★ 已自选" : "☆ 加自选";
      sb.title = on ? "移出自选" : "加入自选";
    }
    toast(on ? `已加入自选: ${code}` : `已移除自选: ${code}`);
    // 加入后分钟线在后台下载，延时刷新几轮让状态列就位
    if (on) [4000, 10000, 20000].forEach((t) => setTimeout(loadWatchlist, t));
  } catch (err) { toast(err.message); }
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let j = null;
  try { j = await r.json(); } catch (_) {}
  if (r.status === 401 && j && j.auth && !path.startsWith("/api/auth/"))
    showAuth("登录已过期，请重新登录");
  if (!r.ok) throw new Error((j && j.error) || r.statusText);
  return j;
}

/* ============================== 登录 / 多用户 ============================== */

const AUTH = {
  enabled: false,   // 服务端是否启用登录（/api/auth/status）
  user: null,
  mode: "login",    // login | register
};

function setUser(name) {
  AUTH.user = name;
  $("#user-name").textContent = name;
  $("#hdr-user").style.display = "inline-flex";
}

function showAuth(msg) {
  AUTH.enabled = true;
  const ov = $("#auth-overlay");
  ov.style.display = "block";
  $("#auth-err").textContent = msg || "";
  setTimeout(() => $("#auth-user").focus(), 50);
}

function bindAuth() {
  $$("#auth-tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      AUTH.mode = b.dataset.mode;
      $$("#auth-tabs button").forEach((x) => x.classList.toggle("active", x === b));
      $("#auth-pass2").style.display = AUTH.mode === "register" ? "block" : "none";
      $("#auth-pass").autocomplete = AUTH.mode === "register" ? "new-password" : "current-password";
      $("#auth-submit").textContent = AUTH.mode === "login" ? "登 录" : "注 册";
      $("#auth-err").textContent = "";
    }));
  $("#auth-submit").addEventListener("click", authSubmit);
  ["#auth-user", "#auth-pass", "#auth-pass2"].forEach((sel) =>
    $(sel).addEventListener("keydown", (e) => { if (e.key === "Enter") authSubmit(); }));
  $("#logout-btn").addEventListener("click", async () => {
    try { await apiPost("/api/auth/logout", {}); } catch (_) {}
    location.reload();
  });
}

async function authSubmit() {
  const user = $("#auth-user").value.trim();
  const pass = $("#auth-pass").value;
  const pass2 = $("#auth-pass2").value;
  const errEl = $("#auth-err");
  errEl.textContent = "";
  if (!user || !pass) { errEl.textContent = "请输入用户名和密码"; return; }
  if (AUTH.mode === "register") {
    if (!/^[a-zA-Z0-9_-]{2,32}$/.test(user)) { errEl.textContent = "用户名应为 2~32 位字母/数字/_/-"; return; }
    if (pass.length < 6) { errEl.textContent = "密码至少 6 位"; return; }
    if (pass !== pass2) { errEl.textContent = "两次输入的密码不一致"; return; }
  }
  const btn = $("#auth-submit");
  btn.disabled = true;
  try {
    await apiPost(AUTH.mode === "login" ? "/api/auth/login" : "/api/auth/register",
                  { username: user, password: pass });
    location.reload();   // 登录/注册成功后整页刷新走正常启动
  } catch (e) {
    errEl.textContent = e.message;
    btn.disabled = false;
  }
}

/* ============================== 个股分析 ============================== */

// 无自选股/自选第一只无数据时，个股页默认展示的回退股票
const PRESET_STOCKS = [
  ["600519.SH", "贵州茅台"],
  ["300750.SZ", "宁德时代"],
  ["000001.SZ", "平安银行"],
  ["600036.SH", "招商银行"],
  ["002594.SZ", "比亚迪"],
  ["688981.SH", "中芯国际"],
];

function bindSearch() {
  const input = $("#stock-search");
  const sug = $("#search-suggest");
  let timer, lastRows = [];

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(doSearch, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && lastRows.length) pickStock(lastRows[0].code, { history: true });
    if (e.key === "Escape") sug.style.display = "none";
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search-box")) sug.style.display = "none";
  });

  async function doSearch() {
    const q = input.value.trim();
    if (!q) { sug.style.display = "none"; return; }
    try {
      lastRows = await api("/api/stocks?q=" + encodeURIComponent(q));
    } catch (err) { toast(err.message); return; }
    if (!lastRows.length) { sug.style.display = "none"; return; }
    sug.innerHTML = lastRows
      .map((r) => `<div class="item" data-code="${r.code}">
          <span>${wlHas(r.code) ? '<span class="wl-star" title="已在自选">★</span> ' : ""}<span class="code">${r.code}</span> <span class="nm">${r.name}</span></span>
          <span class="sg-price">
            <span>${r.latest_price != null ? r.latest_price.toFixed(2) : "—"} <span class="${pctCls(r.pct_chg || 0)}">${fmtPct(r.pct_chg)}</span></span>
            <span class="sg-real">后复权 ${r.hfq_close != null ? r.hfq_close.toFixed(2) : "—"}</span>
          </span>
        </div>`)
      .join("");
    sug.style.display = "block";
    sug.querySelectorAll(".item").forEach((el) =>
      el.addEventListener("click", () => pickStock(el.dataset.code, { history: true }))
    );
  }
}

/* ---------- 搜索历史（搜索框旁的快捷 chips，单条可删，localStorage 记忆） ---------- */

/** 记录一次搜索：去重后置顶，超限淘汰最旧。 */
function recordStockHistory(code, name) {
  if (!code || !name) return;
  const h = state.stockHistory.filter((x) => x[0] !== code);
  h.unshift([code, name]);
  state.stockHistory = h.slice(0, STOCK_HISTORY_MAX);
  lsSet("stockHistory", state.stockHistory);
  renderChips();
}

/** 删除一条搜索记录。 */
function removeStockHistory(code) {
  state.stockHistory = state.stockHistory.filter((x) => x[0] !== code);
  lsSet("stockHistory", state.stockHistory);
  renderChips();
}

function renderChips() {
  const box = $("#stock-chips");
  if (!box) return;
  const h = state.stockHistory;
  box.innerHTML = h.length
    ? h.map(([code, name]) => `
      <span class="chip" data-code="${escHtml(code)}" title="${escHtml(code)} · 点击查看，✕ 删除该记录">
        ${escHtml(name)}<i class="chip-x" data-del="${escHtml(code)}" title="删除该搜索记录">✕</i>
      </span>`).join("")
    : "";
  box.querySelectorAll(".chip").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest(".chip-x")) return;   // 删除按钮不触发跳转
      pickStock(el.dataset.code, { history: true });   // 重新搜索 -> 置顶
    }));
  box.querySelectorAll(".chip-x").forEach((el) =>
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      removeStockHistory(el.dataset.del);
    }));
}

/** 选中并展示一只股票。opts.history=true 时记入搜索历史（搜索框/chips 入口）。 */
async function pickStock(code, opts = {}) {
  try {
    const d = await api("/api/kline?code=" + code);
    state.stockCode = code;
    state.stockData = d;
    state.stockTrades = [];
    state.klineView = null;
    ["mf", "gm", "cd", "bb"].forEach((s) => setSubDot(s, 0));   // 清上一只股票的信号角标
    $("#stock-search").value = "";
    $("#search-suggest").style.display = "none";
    $("#stock-toolbar").style.display = "flex";
    renderStockHead(d);
    renderKlineChart();
    renderTradeTable();      // 先清空上一只股票的交易表
    updateTradeSummary();
    updateStockCards();
    loadTrades(code);
    loadCompare();
    loadMultiFactor();
    loadGarch();
    loadChip();
    loadBB();
    loadChips();
    loadFund();
    loadFlow();          // 资金流子标签（内含公告表加载）
    loadAnnounce();
    resetAIPanel();      // AI 面板重置：本会话已生成的报告直接展示，后端在途任务恢复轮询
    if (opts.history) recordStockHistory(code, d.name);
    return true;
  } catch (err) { toast(err.message); return false; }
}

function renderStockHead(d) {
  const last = d.dates.length - 1;
  const chg = d.pct_chg[last];
  const starred = wlHas(d.code);
  const qfq = d.price_unit === "qfq";
  const hfqTxt = d.hfq_close != null ? d.hfq_close.toFixed(2) : "—";
  $("#stock-head").innerHTML = `
    <span>${d.name}</span> <span class="stock-head-code" style="color:var(--text-dim)">${d.code}</span>
    <span class="price ${pctCls(chg || 0)}">${(d.close[last] ?? 0).toFixed(2)}</span>
    <span class="tag-hfq">${qfq ? "前复权" : "后复权"}</span>
    <span class="${pctCls(chg || 0)}">${fmtPct(chg)}</span>
    <span style="color:var(--text-dim);font-size:12.5px">${d.dates[last]}</span>
    ${qfq ? `<span style="color:var(--text-dim);font-size:12.5px" title="最新价=真实价，历史按最新复权因子折算">后复权 ${hfqTxt}</span>` : ""}
    <button id="star-btn" class="star-btn ${starred ? "on" : ""}" title="${starred ? "移出自选" : "加入自选"}">
      ${starred ? "★ 已自选" : "☆ 加自选"}
    </button>`;
  $("#star-btn").addEventListener("click", () => toggleWatch(d.code));
}

function sliceRecent(days) {
  const d = state.stockData;
  if (!d || days <= 0) return d ? { dates: d.dates, close: d.close } : null;
  const start = shiftDate(d.dates[d.dates.length - 1], days);
  let lo = 0, hi = d.dates.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (d.dates[mid] < start) lo = mid + 1; else hi = mid; }
  return { dates: d.dates.slice(lo), close: d.close.slice(lo) };
}

function calcStats(close) {
  const vals = close.filter((v) => v != null && v > 0);
  if (vals.length < 2) return null;
  const first = vals[0], last = vals[vals.length - 1];
  const n = vals.length;
  const ret = last / first - 1;
  const annRet = Math.pow(last / first, ANNUALIZE / n) - 1;
  let maxdd = 0, peak = first;
  for (const v of vals) { peak = Math.max(peak, v); maxdd = Math.min(maxdd, v / peak - 1); }
  const r = [];
  for (let i = 1; i < vals.length; i++) r.push(vals[i] / vals[i - 1] - 1);
  const mean = r.reduce((a, b) => a + b, 0) / r.length;
  const std = Math.sqrt(r.reduce((a, b) => a + (b - mean) ** 2, 0) / r.length);
  const vol = std * Math.sqrt(ANNUALIZE);
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(ANNUALIZE) : null;
  return { ret, annRet, maxdd, vol, sharpe, n };
}

function updateStockCards() {
  const d = state.stockData;
  if (!d) return;
  const s = sliceRecent(state.stockDays);
  const st = s ? calcStats(s.close) : null;
  const last = d.dates.length - 1;
  const chg = d.pct_chg[last];
  const label = state.stockDays > 0 ? `近${state.stockDays}日` : "全部数据";
  $("#stock-cards").innerHTML = `
    <div class="card"><div class="k">最新收盘（${d.dates[last]} · ${d.price_unit === "qfq" ? "前复权=真实价" : "后复权"}）</div>
      <div class="v ${pctCls(chg || 0)}">${(d.close[last] ?? 0).toFixed(2)}</div>
      <div class="s"><span class="${pctCls(chg || 0)}">${fmtPct(chg)}</span>${d.price_unit === "qfq" && d.hfq_close != null ? ` · 后复权 ${d.hfq_close.toFixed(2)}` : ""}</div></div>
    <div class="card"><div class="k">总市值</div>
      <div class="v">${fmtMv(d.total_mv)}</div>
      <div class="s">流通 ${fmtMv(d.float_mv)}</div></div>
    <div class="card"><div class="k">区间涨跌幅（${label}）</div>
      <div class="v ${pctCls(st ? st.ret : null)}">${st ? fmtPct(st.ret) : "—"}</div>
      <div class="s">年化 ${st ? fmtPct(st.annRet, 1) : "—"}</div></div>
    <div class="card"><div class="k">年化波动率</div>
      <div class="v">${st ? (st.vol * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">夏普 ${st && st.sharpe != null ? st.sharpe.toFixed(2) : "—"}</div></div>
    <div class="card"><div class="k">最大回撤</div>
      <div class="v down">${st ? (st.maxdd * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">${st ? st.n + " 个交易日" : ""}</div></div>
  `;
}

/* ---------- 统计区间 -> 图表显示范围同步 ---------- */

// 图表日期轴注册表: chartId -> 类目日期数组。render 完成后注册，
// 「统计区间」切换时统一把各图 dataZoom 缩放到同一时间范围。
const chartDates = {};

/** 注册并立即应用当前统计区间（须在 setOption 之后调用，此时 dataZoom 才可用）。 */
function registerChartDates(id, dates) {
  if (!Array.isArray(dates) || !dates.length) return;
  chartDates[id] = dates;
  applyPeriodToChart(id);
}

/** state.stockDays 个自然日前在 dates 中的起始下标（0/全部 -> 0）。 */
function periodStartIdx(dates) {
  const days = state.stockDays;
  if (!days || days <= 0 || !dates || !dates.length) return 0;
  const start = shiftDate(dates[dates.length - 1], days);
  let lo = 0, hi = dates.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (dates[mid] < start) lo = mid + 1; else hi = mid; }
  return lo;
}

function applyPeriodToChart(id) {
  const c = charts[id], dates = chartDates[id];
  if (!c || !dates || !dates.length) return;
  const start = Math.max(0, Math.round((periodStartIdx(dates) / dates.length) * 100));
  c.dispatchAction({ type: "dataZoom", start, end: 100 });
}

/** 统计区间切换后调用: 所有已注册图表（K线/走势对比/模型图）缩放到新区间。 */
function syncChartPeriods() {
  Object.keys(chartDates).forEach(applyPeriodToChart);
}

/* ============================== K线（时/日/周/月 周期切换） ============================== */

const KLINE_FREQS = ["60", "d", "w", "m"];

/** ISO 年-周键（周一为一周起点），周K线分组用。 */
function isoWeekKey(dateStr) {
  const dt = new Date(dateStr + "T00:00:00Z");
  const day = dt.getUTCDay() || 7;
  dt.setUTCDate(dt.getUTCDate() + 4 - day);   // 本周四所在日历周
  const y = dt.getUTCFullYear();
  const week = Math.ceil(((dt - Date.UTC(y, 0, 1)) / 86400000 + 1) / 7);
  return y + "-W" + String(week).padStart(2, "0");
}

/**
 * 日线数据 -> 当前周期的 K 线视图。
 * 日: 直接透传；周/月: 按 ISO 周 / 自然月聚合（开=首日开、收=末日收、高/低=区间极值、量额=区间和）。
 * endIdx[i] = 第 i 根K线覆盖的最后一个日线下标（交易日 -> K线下标映射用）。
 */
function buildKlineView(d, freq) {
  if (freq === "d") {
    return {
      freq: "d",
      labels: d.dates, tipLabels: d.dates, barDates: d.dates,
      endIdx: d.dates.map((_, i) => i),
      open: d.open, high: d.high, low: d.low, close: d.close,
      volume: d.volume, amount: d.amount, pct_chg: d.pct_chg,
    };
  }
  const monthly = freq === "m";
  const keyOf = monthly ? (s) => s.slice(0, 7) : isoWeekKey;
  const labels = [], tipLabels = [], barDates = [], endIdx = [];
  const open = [], high = [], low = [], close = [], volume = [], amount = [];
  let curKey = null, firstIdx = -1, hi = 0, lo = 0, volSum = 0, amtSum = 0, amtAllNull = true;
  const closeBucket = (lastIdx) => {
    if (curKey === null) return;
    labels.push(monthly ? curKey : d.dates[lastIdx]);
    tipLabels.push(monthly ? curKey : `${d.dates[firstIdx]} ~ ${d.dates[lastIdx]}`);
    barDates.push(d.dates[lastIdx]);
    endIdx.push(lastIdx);
    open.push(d.open[firstIdx]);
    high.push(hi); low.push(lo);
    close.push(d.close[lastIdx]);
    volume.push(volSum);
    amount.push(amtAllNull ? null : amtSum);
  };
  for (let i = 0; i < d.dates.length; i++) {
    const key = keyOf(d.dates[i]);
    if (key !== curKey) {
      closeBucket(i - 1);
      curKey = key; firstIdx = i;
      hi = -Infinity; lo = Infinity; volSum = 0; amtSum = 0; amtAllNull = true;
    }
    hi = Math.max(hi, d.high[i] ?? -Infinity);
    lo = Math.min(lo, d.low[i] ?? Infinity);
    volSum += d.volume[i] || 0;
    if (d.amount[i] != null) { amtSum += d.amount[i]; amtAllNull = false; }
  }
  closeBucket(d.dates.length - 1);
  const pct_chg = close.map((v, i) =>
    i > 0 && close[i - 1] > 0 && v != null ? v / close[i - 1] - 1 : null);
  return { freq, labels, tipLabels, barDates, endIdx, open, high, low, close, volume, amount, pct_chg };
}

/** /api/hourly 响应 -> 时K线视图（60分钟K线，真实价）。 */
function buildHourlyView(r) {
  const pct = r.close.map((v, i) =>
    i > 0 && r.close[i - 1] != null && v != null ? v / r.close[i - 1] - 1 : null);
  return {
    freq: "60",
    labels: r.dates, tipLabels: r.dates,
    barDates: r.dates.map((s) => String(s).slice(0, 10)),
    endIdx: null,   // 时K线不映射日级交易
    open: r.open, high: r.high, low: r.low, close: r.close,
    volume: r.volume, amount: r.amount, pct_chg: pct,
    rangeHint: `本地分钟线覆盖 ${r.n_days} 个交易日` +
      (r.days && r.days.length ? `（${r.days[0]} ~ ${r.days[r.days.length - 1]}）` : ""),
  };
}

function setKlineFreq(freq) {
  if (!KLINE_FREQS.includes(freq)) freq = "d";
  state.klineFreq = freq;
  lsSet("klineFreq", freq);
  $$("#kline-freq button").forEach((b) => b.classList.toggle("active", b.dataset.freq === freq));
  renderKlineChart();
}

function updateKlineTitle() {
  const f = state.klineFreq;
  $("#kline-title").textContent =
    f === "60" ? "时K线（60分钟 · 真实价）"
    : f === "w" ? "周K线（前复权 · 最新价=真实价）"
    : f === "m" ? "月K线（前复权 · 最新价=真实价）"
    : "日K线（前复权 · 最新价=真实价）";
  $("#kline-click-hint").textContent =
    f === "w" ? "🖱 点周K线看该周末日分时"
    : f === "m" ? "🖱 点月K线看该月末日分时"
    : "🖱 点K线看当日分时";
}

function renderKlineChart() {
  const d = state.stockData;
  if (!d) return;
  if (state.klineFreq === "60") { renderHourlyKline(); return; }
  drawKlineChart(buildKlineView(d, state.klineFreq));
}

/** 时K线：本地 1 分钟线聚合成 60 分钟K线（/api/hourly），无分钟数据时回退日线。 */
async function renderHourlyKline() {
  const code = state.stockCode;
  if (!code || !state.stockData) return;
  let r;
  try {
    r = await api("/api/hourly?code=" + code);
    if (state.stockCode !== code) return;   // 已切到其他股票
    if (!r.ok) throw new Error(r.error || "无本地分钟线");
  } catch (err) {
    toast("时K线不可用: " + err.message);
    setKlineFreq("d");   // 回退到日K线
    return;
  }
  if (state.klineFreq !== "60" || state.stockCode !== code) return;
  drawKlineChart(buildHourlyView(r));
}

function drawKlineChart(v) {
  state.klineView = v;
  updateKlineTitle();
  $("#kline-range").textContent = v.rangeHint || "";
  const c = mkChart("chart-kline");
  const ohlc = v.labels.map((_, i) => [v.open[i], v.close[i], v.low[i], v.high[i]]);
  const vol = v.labels.map((_, i) => ({
    value: v.volume[i],
    itemStyle: { color: (v.close[i] ?? 0) >= (v.open[i] ?? 0) ? "rgba(239,75,87,.75)" : "rgba(46,189,133,.75)" },
  }));
  c.setOption({
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: "rgba(22,27,36,.95)", borderColor: "#2a3342",
      textStyle: { color: "#dce3ee", fontSize: 12 },
      formatter: (items) => {
        const i = items[0].dataIndex;
        const [o, cl, l, h] = ohlc[i] || [];
        const chg = v.pct_chg[i];
        const amt = v.amount[i];
        const tradesHtml = (state.tradeByIdx[i] || []).map((t) => {
          const word = t.side === "buy" ? "买入" : "卖出";
          return `<br><span class="${t.side === "buy" ? "up" : "down"}">${t.side === "buy" ? "▲" : "▼"} ${word}</span> `
            + `<b>${t.price.toFixed(2)}</b> × ${t.shares.toLocaleString()} 股`
            + (t.note ? `（${escHtml(t.note)}）` : "");
        }).join("");
        return `<b>${v.tipLabels[i]}</b><br>
          开 <b>${o?.toFixed(2)}</b>　高 <b class="up">${h?.toFixed(2)}</b><br>
          低 <b class="down">${l?.toFixed(2)}</b>　收 <b class="${pctCls(chg)}">${cl?.toFixed(2)}</b><br>
          涨跌 <b class="${pctCls(chg)}">${fmtPct(chg)}</b>　量 <b>${((v.volume[i] || 0) / 10000).toFixed(1)} 万手</b>
          ${amt != null ? `<br>额 <b>${fmtAmt(amt / 1e8)}</b>` : ""}${tradesHtml}`;
      },
    },
    legend: { top: 0, data: ["K线", "MA5", "MA20", "MA60", "MA120", "买入", "卖出"], textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 60, right: 18, top: 34, height: "58%" },
      { left: 60, right: 18, top: "72%", height: "18%" },
    ],
    xAxis: [
      { type: "category", data: v.labels, ...AXIS, gridIndex: 0, axisLabel: { show: false } },
      { type: "category", data: v.labels, ...AXIS, gridIndex: 1 },
    ],
    yAxis: [
      { type: "value", scale: true, ...AXIS, gridIndex: 0 },
      { type: "value", ...AXIS, gridIndex: 1, splitLine: { show: false }, axisLabel: { ...AXIS.axisLabel, formatter: (val) => (val >= 10000 ? (val / 10000).toFixed(0) + "万" : val) } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], height: 16, bottom: 8, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } },
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        cursor: "pointer",   // 点击可看分时
      },
      { name: "MA5", type: "line", data: movingAvg(v.close, 5), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1 } },
      { name: "MA20", type: "line", data: movingAvg(v.close, 20), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1 } },
      { name: "MA60", type: "line", data: movingAvg(v.close, 60), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1 } },
      { name: "MA120", type: "line", data: movingAvg(v.close, 120), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1 } },
      { name: "成交量", type: "bar", data: vol, xAxisIndex: 1, yAxisIndex: 1 },
      // 买卖标记：数据由 loadTrades -> renderTradeMarkers 填充（按 id 定位合并）
      {
        id: "trade-buy", name: "买入", type: "scatter", data: [], xAxisIndex: 0, yAxisIndex: 0,
        symbol: "triangle", symbolSize: 13, z: 12, cursor: "pointer",
        itemStyle: { color: UP, borderColor: "rgba(255,255,255,.85)", borderWidth: 1 },
      },
      {
        id: "trade-sell", name: "卖出", type: "scatter", data: [], xAxisIndex: 0, yAxisIndex: 0,
        symbol: "triangle", symbolRotate: 180, symbolSize: 13, z: 12, cursor: "pointer",
        itemStyle: { color: DOWN, borderColor: "rgba(255,255,255,.85)", borderWidth: 1 },
      },
    ],
  });
  // 区间同步: K 线缩放到当前统计区间（默认 1 年）
  registerChartDates("chart-kline", v.labels);
  // 点击K线 -> 该K线对应交易日（周/月为区间末日）的 1 分钟分时弹窗
  c.on("click", (p) => {
    if (p.seriesName === "K线" && p.dataIndex != null && v.barDates[p.dataIndex]) {
      openMinuteModal(v.barDates[p.dataIndex]);
    }
  });
  renderTradeMarkers();
}

/* ============================== 当日分时（日K线点击 -> 1 分钟线） ============================== */

let minuteSeq = 0;   // 弹窗请求序号：慢响应回来时若已查看其他日期/已关闭则丢弃

async function openMinuteModal(date) {
  const seq = ++minuteSeq;
  state.minuteDate = date;
  const d = state.stockData;
  $("#minute-modal").classList.add("open");
  $("#minute-title").textContent = `${d ? d.name : ""} ${state.stockCode} · ${date} 分时`;
  $("#minute-sub").textContent = "加载中…";
  $("#minute-cards").innerHTML = "";
  $("#minute-hint").textContent = "";
  $("#minute-sync").style.display = "none";
  if (charts["chart-minute"]) { charts["chart-minute"].dispose(); delete charts["chart-minute"]; }
  $("#chart-minute").innerHTML = `<div class="loading">加载中…</div>`;
  try {
    const r = await api(`/api/minute?code=${state.stockCode}&date=${date}`);
    if (seq !== minuteSeq || !$("#minute-modal").classList.contains("open")) return;
    renderMinuteModal(r);
  } catch (err) {
    if (seq !== minuteSeq || !$("#minute-modal").classList.contains("open")) return;
    $("#minute-sub").textContent = "";
    $("#minute-hint").textContent = "分时加载失败: " + err.message;
    $("#minute-sync").style.display = "";
  }
}

function renderMinuteModal(r) {
  const syncBtn = $("#minute-sync");
  syncBtn.textContent = r.syncing ? "同步中…" : "⟳ 立即拉取";
  syncBtn.disabled = !!r.syncing;
  syncBtn.style.display = r.syncing || (!r.ok && r.fetchable) ? "" : "none";
  if (r.syncing) {
    // 同步进行中: 5 秒后自动重查（完成后停在结果；同步结束（无论成败）停止轮询）
    const code = state.stockCode, date = state.minuteDate;
    setTimeout(() => {
      if ($("#minute-modal").classList.contains("open")
          && state.stockCode === code && state.minuteDate === date) {
        openMinuteModal(date);
      }
    }, 5000);
  }
  if (!r.ok) {
    $("#minute-sub").textContent = "";
    $("#minute-cards").innerHTML = "";
    if (charts["chart-minute"]) { charts["chart-minute"].dispose(); delete charts["chart-minute"]; }
    $("#chart-minute").innerHTML = "";
    $("#minute-hint").textContent = r.error || "无分钟数据";
    return;
  }
  $("#minute-sub").textContent = `${r.n} 根 1 分钟K线（真实价）`;

  // ---- 当日摘要卡 ----
  const nums = (a) => (a || []).filter((v) => v != null);
  const o = r.open[0], cl = r.close[r.close.length - 1];
  const h = Math.max(...nums(r.high)), l = Math.min(...nums(r.low));
  const vwapLast = [...r.vwap].reverse().find((v) => v != null);
  const vol = r.volume.reduce((a, b) => a + (b || 0), 0);
  const amt = r.amount.reduce((a, b) => a + (b || 0), 0);
  const chg = r.prev_close != null && cl != null ? cl / r.prev_close - 1 : null;
  const amp = r.prev_close != null ? (h - l) / r.prev_close : null;
  $("#minute-cards").innerHTML = `
    <div class="card"><div class="k">开盘</div><div class="v">${o != null ? o.toFixed(2) : "—"}</div>
      <div class="s">昨收 ${r.prev_close != null ? r.prev_close.toFixed(2) : "—"}</div></div>
    <div class="card"><div class="k">最高 / 最低</div>
      <div class="v" style="font-size:16px"><span class="up">${h.toFixed(2)}</span> / <span class="down">${l.toFixed(2)}</span></div>
      <div class="s">振幅 ${amp != null ? (amp * 100).toFixed(2) + "%" : "—"}</div></div>
    <div class="card"><div class="k">收盘（分时口径）</div>
      <div class="v ${pctCls(chg)}">${cl != null ? cl.toFixed(2) : "—"}</div>
      <div class="s">${chg != null ? `较昨收 ${fmtPct(chg)}` : "无昨收基准"}</div></div>
    <div class="card"><div class="k">均价（VWAP）</div>
      <div class="v">${vwapLast != null ? vwapLast.toFixed(2) : "—"}</div>
      <div class="s">累计额 / 累计量</div></div>
    <div class="card"><div class="k">成交量</div>
      <div class="v">${(vol / 1e6).toFixed(1)} 万手</div>
      <div class="s">${amt != null ? "额 " + fmtMv(amt) : ""}</div></div>
  `;

  // ---- 分时图（价格 + VWAP / 分钟量）----
  const times = r.time, close = r.close, vwap = r.vwap;
  const volBars = r.volume.map((v, i) => {
    const up = close[i] != null && (close[i - 1] != null ? close[i] >= close[i - 1]
      : r.open[i] != null && close[i] >= r.open[i]);
    return { value: v, itemStyle: { color: up ? "rgba(239,75,87,.7)" : "rgba(46,189,133,.7)" } };
  });
  const c = mkChart("chart-minute");
  c.setOption({
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross" },
      backgroundColor: "rgba(22,27,36,.95)", borderColor: "#2a3342",
      textStyle: { color: "#dce3ee", fontSize: 12 },
      formatter: (items) => {
        const i = items[0].dataIndex;
        const p = close[i], w = vwap[i];
        return `<b>${times[i]}</b><br>
          价 <b>${p != null ? p.toFixed(2) : "—"}</b>　均价 <b>${w != null ? w.toFixed(2) : "—"}</b><br>
          量 <b>${r.volume[i] != null ? (r.volume[i] / 100).toFixed(0) + " 手" : "—"}</b>`;
      },
    },
    legend: { top: 0, right: 0, data: ["价格", "均价(VWAP)"],
              textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 60, right: 20, top: 30, height: "58%" },
      { left: 60, right: 20, top: "74%", height: "17%" },
    ],
    xAxis: [
      { type: "category", data: times, ...AXIS, gridIndex: 0, axisLabel: { show: false } },
      { type: "category", data: times, ...AXIS, gridIndex: 1,
        axisLabel: { ...AXIS.axisLabel, interval: (i, v) => /:(00|30)$/.test(v) } },
    ],
    yAxis: [
      { type: "value", scale: true, ...AXIS, gridIndex: 0 },
      { type: "value", ...AXIS, gridIndex: 1, splitLine: { show: false },
        axisLabel: { ...AXIS.axisLabel, formatter: (v) => (v >= 1e6 ? (v / 1e6).toFixed(0) + "万" : v) } },
    ],
    series: [
      { name: "价格", type: "line", data: close, showSymbol: false, z: 5,
        lineStyle: { width: 1.6, color: ACCENT }, itemStyle: { color: ACCENT },
        areaStyle: { color: "rgba(79,142,247,.07)" },
        markLine: r.prev_close != null ? {
          silent: true, symbol: "none",
          lineStyle: { type: "dashed", color: "rgba(220,227,238,.45)" },
          label: { formatter: "昨收 " + r.prev_close.toFixed(2), color: "#8b98ab", fontSize: 10 },
          data: [{ yAxis: r.prev_close }],
        } : undefined },
      { name: "均价(VWAP)", type: "line", data: vwap, showSymbol: false,
        lineStyle: { width: 1.2, color: "#f0a03c" }, itemStyle: { color: "#f0a03c" } },
      { name: "分钟量", type: "bar", data: volBars, xAxisIndex: 1, yAxisIndex: 1 },
    ],
  });
  $("#minute-hint").textContent =
    `数据: 本地 1 分钟K线（新浪源 · 真实价）· 本地覆盖 ${r.days.length} 个交易日`
    + (r.days.length ? `（${r.days[0]} ~ ${r.days[r.days.length - 1]}）` : "");
}

function closeMinuteModal() {
  $("#minute-modal").classList.remove("open");
}

function bindMinuteModal() {
  $("#minute-close").addEventListener("click", closeMinuteModal);
  $("#minute-mask").addEventListener("click", closeMinuteModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMinuteModal();
  });
  $("#minute-sync").addEventListener("click", async () => {
    const btn = $("#minute-sync");
    btn.disabled = true;
    btn.textContent = "同步中…";
    try {
      await apiPost("/api/minute", { code: state.stockCode });
      toast("已提交分钟线同步（新浪源，约 3~6 秒后自动刷新）");
      const code = state.stockCode, date = state.minuteDate;
      setTimeout(() => {
        if ($("#minute-modal").classList.contains("open")
            && state.stockCode === code && state.minuteDate === date) {
          openMinuteModal(date);   // 同步完成后自动重载
        }
      }, 5000);
    } catch (err) {
      toast("同步失败: " + err.message);
      btn.disabled = false;
      btn.textContent = "⟳ 立即拉取";
    }
  });
}

/* ============================== 买卖记录（自选股交易台账） ============================== */

/** 交易日期 -> K 线下标；非交易日吸附到之前最近的交易日，早于首日返回 -1。 */
function tradeDateIdx(date) {
  const d = state.stockData;
  if (!d) return -1;
  const dates = d.dates;
  let lo = 0, hi = dates.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (dates[mid] <= date) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

/** 实际成交价（真实元） -> 图表价格单位（前复权时即真实价）。 */
function tradeChartPrice(p) {
  const d = state.stockData;
  if (!d) return null;
  if (d.price_unit === "qfq") return p;
  if (d.real_price && d.hfq_close) return p * d.hfq_close / d.real_price;
  return null;  // 无真实价快照且图为后复权，无法换算
}

async function loadTrades(code) {
  if (!code) code = state.stockCode;
  if (!code) return;
  try {
    const d = await api("/api/trades?code=" + code);
    if (state.stockCode !== code) return;  // 已切到其他股票
    state.stockTrades = d.trades || [];
  } catch (err) {
    state.stockTrades = [];
    toast("交易记录加载失败: " + err.message);
  }
  renderTradeMarkers();
  renderTradeTable();
  updateTradeSummary();
}

/** 交易日期 -> 当前 K 线视图的下标（周/月聚合到所属K线；时K线不展示返回 -1）。 */
function tradeBarIdx(date) {
  const view = state.klineView;
  const d = state.stockData;
  if (!view || !d || view.freq === "60") return -1;
  const t = tradeDateIdx(date);   // 日线下标（非交易日吸附到之前最近交易日）
  if (t < 0) return -1;
  // endIdx[i] 为第 i 根K线覆盖的最后一个日线下标（递增），二分找第一根 endIdx >= t
  const ends = view.endIdx;
  let lo = 0, hi = ends.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ends[mid] >= t) { ans = mid; hi = mid - 1; } else lo = mid + 1;
  }
  return ans;
}

function renderTradeMarkers() {
  const c = charts["chart-kline"];
  const view = state.klineView;
  if (!c || !view) return;
  const buys = [], sells = [];
  const byIdx = {};
  for (const t of state.stockTrades) {
    const i = tradeBarIdx(t.date);
    if (i < 0) continue;
    (byIdx[i] = byIdx[i] || []).push(t);
    const price = tradeChartPrice(t.price);
    if (price == null) continue;
    const pt = { value: [view.labels[i], +price.toFixed(4)], trade: t };
    (t.side === "buy" ? buys : sells).push(pt);
  }
  state.tradeByIdx = byIdx;
  c.setOption({
    series: [
      { id: "trade-buy", data: buys },
      { id: "trade-sell", data: sells },
    ],
  });
}

function renderTradeTable() {
  const wrap = $("#trade-table-wrap");
  const tbody = $("#trade-table tbody");
  if (!wrap || !tbody) return;
  updateTradeDetailToggle();
  const ts = state.stockTrades;
  if (!ts.length) { wrap.style.display = "none"; tbody.innerHTML = ""; return; }
  wrap.style.display = state.tradeDetailOpen ? "block" : "none";
  tbody.innerHTML = ts
    .slice()
    .sort((a, b) => (a.date + a.created < b.date + b.created ? 1 : -1))
    .map((t) => `
      <tr>
        <td>${t.date}</td>
        <td class="${t.side === "buy" ? "up" : "down"}" style="font-weight:600">${t.side === "buy" ? "▲ 买入" : "▼ 卖出"}</td>
        <td class="num">${fmtNum(t.price)}</td>
        <td class="num">${t.shares.toLocaleString()}</td>
        <td class="num">${fmtNum(t.price * t.shares)}</td>
        <td style="color:var(--text-dim)">${escHtml(t.note || "")}</td>
      </tr>`).join("");
}

/** K 线标题栏「交易明细」开关按钮：有交易才显示，随展开状态换箭头。 */
function updateTradeDetailToggle() {
  const btn = $("#trade-detail-toggle");
  if (!btn) return;
  btn.style.display = state.stockTrades.length ? "" : "none";
  btn.textContent = state.tradeDetailOpen ? "▾ 交易明细" : "▸ 交易明细";
}

function updateTradeSummary() {
  const el = $("#trade-summary");
  if (!el) return;
  const d = state.stockData;
  if (!d || !state.stockTrades.length) { el.textContent = ""; el.title = ""; return; }
  const lastReal = d.price_unit === "qfq" ? d.close[d.dates.length - 1] : d.real_price;
  const pos = computePosition(state.stockTrades, lastReal);
  el.innerHTML = `${pos.n_trades} 笔 · `
    + (pos.held
      ? `持仓 <b>${pos.held.toLocaleString()}</b> 股 @${fmtNum(pos.avg_cost)}`
      : "已清仓")
    + ` · 已实现 <b class="${pctCls(pos.realized)}">${fmtSignedNum(pos.realized)}</b>`
    + (pos.held ? ` · 浮动 <b class="${pctCls(pos.unrealized)}">${fmtSignedNum(pos.unrealized)}</b>` : "");
  el.title = "按移动加权平均成本计算；浮动盈亏基于最新收盘价";
}

/* ---------- 交易记录页（CRUD） ---------- */

function fmtSignedYuan(v) {
  if (v == null || !isFinite(v)) return "—";
  return (v >= 0 ? "+" : "") + fmtYuan(v);
}

/** 千分位数字（不做万/亿压缩，用于交易台账等需精确对账的场景）。 */
function fmtNum(v, d = 2) {
  if (v == null || !isFinite(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtSignedNum(v, d = 2) {
  if (v == null || !isFinite(v)) return "—";
  return (v >= 0 ? "+" : "") + fmtNum(v, d);
}

/** 费用估算：买入=佣金+过户费；卖出=佣金+印花税+过户费（与 A 股标准一致）。 */
function estimateFee(side, amount) {
  const c = state.feeCfg;
  const comm = Math.max(Number(c.min) || 0, amount * (Number(c.comm) || 0) / 10000);
  const transfer = amount * (Number(c.transfer) || 0) / 100;
  const stamp = side === "sell" ? amount * (Number(c.stamp) || 0) / 100 : 0;
  return comm + transfer + stamp;
}

/** 单笔费用：有实际值用实际值，否则按费率估算。 */
function tradeFee(t) {
  if (t.fee != null && isFinite(t.fee)) return Number(t.fee);
  return estimateFee(t.side, t.price * t.shares);
}

/** 从 "600519" / "sh600519" / "600519.SH" / "茅台 600519" 提取并规范代码。 */
function normCodeInput(s) {
  const m = String(s || "").trim().match(/\b(\d{6})(?:\.(SH|SZ|BJ))?/i);
  if (!m) return null;
  const num = m[1];
  let suf;
  if (m[2]) suf = m[2].toUpperCase();
  else if (num[0] === "4" || num[0] === "8" || num.startsWith("920")) suf = "BJ";
  else if (num.startsWith("60") || num.startsWith("68") || num[0] === "9") suf = "SH";
  else suf = "SZ";
  return num + "." + suf;
}

async function loadTradesPage() {
  try {
    const d = await api("/api/trades");
    state.tradesPage.trades = d.trades || [];
    state.tradesPage.meta = d.meta || {};
  } catch (err) {
    toast("交易记录加载失败: " + err.message);
    return;
  }
  renderTradesPage();
}

function renderTradesPage() {
  const p = state.tradesPage;
  const wlCodes = new Set(state.watchlist.map((w) => w.code));
  const q = p.q.trim().toUpperCase();
  const match = (code, name) =>
    !q || code.toUpperCase().includes(q) || (name || "").toUpperCase().includes(q);

  // 流水（按过滤，时间倒序）
  const rows = p.trades
    .filter((t) => {
      const m = p.meta[t.code] || {};
      if (p.scope === "watchlist" && !wlCodes.has(t.code)) return false;
      return match(t.code, m.name);
    })
    .slice()
    .sort((a, b) => (a.date + a.created < b.date + b.created ? 1 : -1))
    .map((t) => ({ t, m: p.meta[t.code] || {} }));

  // 持仓汇总（按全部交易算成本，再按过滤展示股票）
  const byCode = {};
  for (const t of p.trades) (byCode[t.code] = byCode[t.code] || []).push(t);
  const posRows = Object.keys(byCode)
    .filter((code) => {
      const m = p.meta[code] || {};
      if (p.scope === "watchlist" && !wlCodes.has(code)) return false;
      return match(code, m.name);
    })
    .map((code) => {
      const m = p.meta[code] || {};
      const lst = byCode[code];
      const pos = computePosition(lst, m.latest_price);
      let fees = 0, buyAmt = 0, buyFee = 0, sellAmt = 0, sellFee = 0;
      for (const t of lst) {
        const amt = t.price * t.shares;
        const f = tradeFee(t);
        fees += f;
        if (t.side === "buy") { buyAmt += amt; buyFee += f; }
        else { sellAmt += amt; sellFee += f; }
      }
      // 摊薄成本（含费用）= (买入金额+买入费用-卖出金额+卖出费用) / 持仓 —— 股票APP常用口径
      const diluted = pos.held ? (buyAmt + buyFee - sellAmt + sellFee) / pos.held : null;
      return { code, name: m.name || "—", latest: m.latest_price, pos, fees, diluted };
    })
    .sort((a, b) => (a.code < b.code ? -1 : 1));

  const ptb = $("#trades-pos-table tbody");
  if (ptb) {
    ptb.innerHTML = posRows.length
      ? posRows.map((r) => `
      <tr data-code="${r.code}" title="点击跳转个股">
        <td style="color:var(--text-dim)">${r.code}</td>
        <td>${escHtml(r.name)}</td>
        <td class="num">${r.latest != null ? fmtNum(r.latest) : "—"}</td>
        <td class="num">${r.pos.n_trades}</td>
        <td class="num">${r.pos.held ? r.pos.held.toLocaleString() + " 股" : "已清仓"}</td>
        <td class="num">${r.pos.avg_cost != null ? fmtNum(r.pos.avg_cost) : "—"}</td>
        <td class="num" title="摊薄成本（含费用）：多数股票APP的口径">${r.diluted != null ? fmtNum(r.diluted) : "—"}</td>
        <td class="num" style="color:var(--text-dim)">${fmtNum(r.fees)}</td>
        <td class="num ${pctCls(r.pos.realized)}">${fmtSignedNum(r.pos.realized)}</td>
        <td class="num ${pctCls(r.pos.unrealized)}">${r.pos.held ? fmtSignedNum(r.pos.unrealized) : "—"}</td>
      </tr>`).join("")
      : `<tr><td colspan="10" class="hint" style="padding:12px">暂无交易记录</td></tr>`;
    ptb.querySelectorAll("tr[data-code]").forEach((tr) =>
      tr.addEventListener("click", () => { switchTab("stock"); pickStock(tr.dataset.code); })
    );
  }

  // 记账式渲染：按股票分组 + 借方(买入)/贷方(卖出)分列 + 逐笔持仓余额 + 组小计/总合计
  const balAfter = {};   // tid -> 该笔成交后此股持股
  const balByCode = {};  // code -> 该股最终持股
  for (const t of p.trades) { // p.trades 已按 (date, created) 升序
    balByCode[t.code] = (balByCode[t.code] || 0) + (t.side === "buy" ? t.shares : -t.shares);
    balAfter[t.id] = balByCode[t.code];
  }

  // 按股票分组（rows 已按时间倒序 → 组按最近一笔交易先后排列）
  const groupMap = {};
  const groups = [];
  for (const r of rows) {
    let g = groupMap[r.t.code];
    if (!g) { g = { code: r.t.code, m: r.m, items: [] }; groupMap[r.t.code] = g; groups.push(g); }
    g.items.push(r);
  }
  const sumGroup = (items) => {
    let buy = 0, sell = 0, fee = 0, net = 0;
    for (const { t } of items) {
      const amt = t.price * t.shares;
      if (t.side === "buy") buy += amt; else sell += amt;
      fee += tradeFee(t);
      net += t.side === "buy" ? t.shares : -t.shares;
    }
    return { buy, sell, fee, net };
  };
  const grand = sumGroup(rows);
  const heldTotal = groups.reduce((s, g) => s + (balByCode[g.code] || 0), 0);
  const subCells = (n, s, bal, label) => `
        <td colspan="2" style="text-align:left;font-weight:600">${label} · ${n} 笔</td>
        <td class="num" title="净股数 = 买入 − 卖出">${s.net >= 0 ? "+" : ""}${s.net.toLocaleString()}</td>
        <td class="num"></td>
        <td class="num" title="借方小计：买入成交额">${fmtNum(s.buy)}</td>
        <td class="num" title="贷方小计：卖出成交额">${fmtNum(s.sell)}</td>
        <td class="num">${fmtNum(s.fee)}</td>
        <td class="num" title="期末持仓">${bal.toLocaleString()} 股</td>
        <td class="num" title="净额 = 买入金额 − 卖出金额（不含费用）">${fmtSignedNum(s.buy - s.sell)}</td>
        <td></td>`;

  const tb = $("#trades-table tbody");
  const tf = $("#trades-table tfoot");
  if (tb) {
    tb.innerHTML = groups.length
      ? groups.map((g) => {
          const s = sumGroup(g.items);
          const bal = balByCode[g.code] || 0;
          const head = `
      <tr class="group-head"><td colspan="10">
        <span style="color:var(--text-dim)">${g.code}</span>
        <b>${escHtml(g.m.name || "—")}</b>${wlCodes.has(g.code) ? ' <span class="tag-wl" title="自选股">★</span>' : ""}
        <span class="hint"> · ${g.items.length} 笔 · 期末持仓 ${bal.toLocaleString()} 股${bal === 0 ? "（已清仓）" : ""}</span>
      </td></tr>`;
          const body = g.items.map(({ t }) => {
            const amt = t.price * t.shares;
            const b = balAfter[t.id] || 0;
            return `
      <tr>
        <td>${t.date}</td>
        <td class="${t.side === "buy" ? "up" : "down"}" style="font-weight:600">${t.side === "buy" ? "▲ 买入" : "▼ 卖出"}</td>
        <td class="num">${t.shares.toLocaleString()}</td>
        <td class="num">${fmtNum(t.price)}</td>
        <td class="num">${t.side === "buy" ? fmtNum(amt) : '<span class="hint">—</span>'}</td>
        <td class="num">${t.side === "sell" ? fmtNum(amt) : '<span class="hint">—</span>'}</td>
        <td class="num"${t.fee != null ? ' title="实际费用"' : ` title="按费率估算（佣金 ${state.feeCfg.comm}‱）"`}>${t.fee != null ? fmtNum(t.fee) : "≈" + fmtNum(tradeFee(t))}</td>
        <td class="num"${b < 0 ? ' style="color:var(--down)" title="卖出超过此前买入（缺买入记录）"' : ""}>${b.toLocaleString()}</td>
        <td style="color:var(--text-dim)">${escHtml(t.note || "")}</td>
        <td class="num">
          <button class="mini-btn" data-edit="${t.id}" data-editcode="${t.code}" title="编辑">✎</button>
          <button class="mini-btn" data-del="${t.id}" data-delcode="${t.code}" title="删除">✕</button>
        </td>
      </tr>`;
          }).join("");
          const sub = `<tr class="group-sub">${subCells(g.items.length, s, bal, "小计")}</tr>`;
          return head + body + sub;
        }).join("")
      : `<tr><td colspan="10" class="hint" style="padding:12px">暂无匹配的交易记录</td></tr>`;
    if (tf) tf.innerHTML = rows.length
      ? `<tr>${subCells(rows.length, grand, heldTotal, "总计")}</tr>`
      : "";
    tb.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); openTradesForm("edit", b.dataset.editcode, b.dataset.edit); })
    );
    tb.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); delTradePageRow(b.dataset.delcode, b.dataset.del); })
    );
  }

  const cnt = $("#trades-count");
  if (cnt) {
    if (!p.trades.length) cnt.textContent = "";
    else {
      let totFee = 0;
      for (const t of p.trades) totFee += tradeFee(t);
      cnt.textContent = `共 ${p.trades.length} 笔 · ${Object.keys(byCode).length} 只股票 · 费用 ≈${fmtNum(totFee)}`;
    }
  }
}

function setTradesFormSide(side) {
  state.tradesPage.formSide = side;
  $("#ntf-side").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.side === side)
  );
}

function openTradesForm(mode, code, tid) {
  const p = state.tradesPage;
  const box = $("#trades-form-box");
  if (!box) return;
  p.editing = mode === "edit" ? tid : null;
  const t = p.editing ? (p.trades.find((x) => x.id === tid && x.code === code) || null) : null;
  if (t) {
    p.editingCode = t.code;
    const m = p.meta[t.code] || {};
    $("#trades-form-title").textContent = `编辑交易记录 · ${t.code} ${m.name || ""}`.trim();
    const stock = $("#ntf-stock");
    stock.value = `${t.code} ${m.name || ""}`.trim();
    stock.disabled = true;
    $("#ntf-date").value = t.date;
    setTradesFormSide(t.side);
    $("#ntf-price").value = t.price;
    $("#ntf-shares").value = t.shares;
    $("#ntf-fee").value = t.fee != null ? t.fee : "";
    $("#ntf-note").value = t.note;
    $("#ntf-save").textContent = "保存修改";
  } else {
    p.editingCode = null;
    $("#trades-form-title").textContent = code ? `新增交易记录 · ${code}` : "新增交易记录";
    const stock = $("#ntf-stock");
    stock.disabled = false;
    stock.value = code || "";
    $("#ntf-date").value = META ? META.last_trade_date : "";
    $("#ntf-price").value = "";
    $("#ntf-shares").value = 100;
    $("#ntf-fee").value = "";
    $("#ntf-note").value = "";
    setTradesFormSide("buy");
    $("#ntf-save").textContent = "保存";
    if (!code) stock.focus();
  }
  box.style.display = "block";
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function hideTradesForm() {
  const box = $("#trades-form-box");
  if (box) box.style.display = "none";
  state.tradesPage.editing = null;
  state.tradesPage.editingCode = null;
}

async function saveTradesForm() {
  const p = state.tradesPage;
  const code = p.editing ? p.editingCode : normCodeInput($("#ntf-stock").value);
  if (!code) return toast("请填写股票代码（可输入代码或名称联想选择）");
  const date = $("#ntf-date").value;
  const price = parseFloat($("#ntf-price").value);
  const shares = parseInt($("#ntf-shares").value, 10);
  const note = $("#ntf-note").value.trim();
  const side = p.formSide;
  const feeRaw = $("#ntf-fee").value.trim();
  const fee = feeRaw === "" ? null : parseFloat(feeRaw);
  if (!date) return toast("请选择交易日期");
  if (!(price > 0)) return toast("请填写正确的成交价格");
  if (!(shares > 0)) return toast("请填写正确的股数");
  if (fee != null && !(fee >= 0)) return toast("费用应为非负数（留空按费率估算）");
  const btn = $("#ntf-save");
  btn.disabled = true;
  try {
    if (p.editing) {
      await apiPost("/api/trades", { action: "update", code, id: p.editing, date, side, price, shares, note, fee });
      toast(`已更新交易: ${code} ${date}`);
    } else {
      await apiPost("/api/trades", { action: "add", code, date, side, price, shares, note, fee });
      toast(`已记录${side === "buy" ? "买入" : "卖出"}: ${code} ${date} ${price.toFixed(2)} × ${shares} 股`);
    }
    hideTradesForm();
    await loadTradesPage();
    syncTradesElsewhere(code);
  } catch (err) {
    toast("保存失败: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

async function delTradePageRow(code, tid) {
  const t = state.tradesPage.trades.find((x) => x.id === tid && x.code === code);
  const desc = t ? `${t.date} ${t.side === "buy" ? "买入" : "卖出"} ${t.price.toFixed(2)} × ${t.shares} 股` : "";
  if (!confirm(`删除 ${code} 的这条记录（${desc}）？`)) return;
  try {
    await apiPost("/api/trades", { action: "remove", code, id: tid });
    toast("已删除交易记录");
    if (state.tradesPage.editing === tid) hideTradesForm();
    await loadTradesPage();
    syncTradesElsewhere(code);
  } catch (err) {
    toast("删除失败: " + err.message);
  }
}

/** 交易变更后：刷新自选持仓列 + 若个股页正展示该股票则同步 K 线标记。 */
function syncTradesElsewhere(code) {
  loadTradePositions();
  if (state.stockCode === code) loadTrades(code);
}

/** ===== 批量录入 ===== */
const BATCH_SIDE_MAP = {
  "买": "buy", "买入": "buy", "b": "buy", "buy": "buy",
  "卖": "sell", "卖出": "sell", "s": "sell", "sell": "sell",
};

/** 解析批量文本；返回行对象数组（含逐行 error，空行/注释/表头已剔除）。 */
function parseBatchText(text) {
  const rows = [];
  const lines = String(text || "").split(/\r?\n/);
  let headerDone = false;
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i].trim();
    if (!raw) continue;
    if (raw.startsWith("#") || raw.startsWith("//")) continue;
    const parts = raw.split(/[\s,，;；\t]+/).filter(Boolean);
    if (!headerDone && parts.some((p) => /^(日期|方向|价格|股数|费用|备注|成交日期|买卖)$/.test(p))) {
      headerDone = true; // 表头行
      continue;
    }
    const row = { line: i + 1, raw };
    if (parts.length < 4) { row.error = "至少需要 4 列：日期 方向 价格 股数"; rows.push(row); continue; }
    // 日期
    const dm = parts[0].match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
    if (!dm) { row.error = `日期无法识别：${parts[0]}（支持 YYYY-MM-DD / YYYY/M/D / YYYY.MM.DD）`; rows.push(row); continue; }
    row.date = `${dm[1]}-${dm[2].padStart(2, "0")}-${dm[3].padStart(2, "0")}`;
    const dt = new Date(row.date + "T00:00:00");
    if (isNaN(dt.getTime()) || dt.getFullYear() !== Number(dm[1])) { row.error = `日期无效：${parts[0]}`; rows.push(row); continue; }
    // 方向
    const side = BATCH_SIDE_MAP[parts[1].toLowerCase()];
    if (!side) { row.error = `方向无法识别：${parts[1]}`; rows.push(row); continue; }
    row.side = side;
    // 价格
    const price = parseFloat(parts[2]);
    if (!isFinite(price) || price <= 0) { row.error = `价格无效：${parts[2]}`; rows.push(row); continue; }
    row.price = price;
    // 股数
    const shares = Number(parts[3]);
    if (!Number.isInteger(shares) || shares <= 0) { row.error = `股数应为正整数：${parts[3]}`; rows.push(row); continue; }
    row.shares = shares;
    // 费用（可选，数字才算）+ 备注（其余）
    let rest = parts.slice(4);
    if (rest.length) {
      const f = Number(rest[0]);
      if (isFinite(f) && /^\d+(\.\d+)?$/.test(rest[0])) {
        if (f < 0) { row.error = `费用应为非负数：${rest[0]}`; rows.push(row); continue; }
        row.fee = f;
        rest = rest.slice(1);
      }
    }
    row.note = rest.join(" ").slice(0, 100);
    rows.push(row);
  }
  return rows;
}

function openTradesBatch() {
  const box = $("#trades-batch-box");
  if (!box) return;
  box.style.display = "block";
  const stock = $("#tbf-stock");
  if (!state.tradesPage.batchRows.length) {
    stock.value = state.stockCode ? state.stockCode : "";
    $("#tbf-text").value = "";
    $("#tbf-preview tbody").innerHTML = `<tr><td colspan="8" class="hint" style="padding:10px">填写上方文本后点「解析」</td></tr>`;
    $("#tbf-import").disabled = true;
    $("#tbf-import").textContent = "导入有效 0 条";
    $("#tbf-summary").textContent = "";
  }
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  stock.focus();
}

function hideTradesBatch() {
  const box = $("#trades-batch-box");
  if (box) box.style.display = "none";
}

function parseBatchPreview() {
  const code = normCodeInput($("#tbf-stock").value);
  if (!code) return toast("请先填写股票代码（可输入代码或名称联想选择）");
  const rows = parseBatchText($("#tbf-text").value);
  state.tradesPage.batchRows = rows;
  state.tradesPage.batchCode = code;
  const okRows = rows.filter((r) => !r.error);
  const tb = $("#tbf-preview tbody");
  if (tb) {
    tb.innerHTML = rows.length
      ? rows.map((r) => r.error
          ? `<tr class="batch-bad">
              <td class="num">${r.line}</td>
              <td colspan="7" style="color:var(--down)">✗ ${escHtml(r.raw)} —— ${escHtml(r.error)}</td>
            </tr>`
          : `<tr>
              <td class="num" style="color:var(--text-dim)">${r.line}</td>
              <td>${r.date}</td>
              <td class="${r.side === "buy" ? "up" : "down"}">${r.side === "buy" ? "▲ 买" : "▼ 卖"}</td>
              <td class="num">${fmtNum(r.price)}</td>
              <td class="num">${r.shares.toLocaleString()}</td>
              <td class="num">${r.fee != null ? fmtNum(r.fee) : '<span class="hint" title="留空按费率估算">—</span>'}</td>
              <td style="color:var(--text-dim)">${escHtml(r.note || "")}</td>
              <td class="up" style="font-weight:600">✓</td>
            </tr>`).join("")
      : `<tr><td colspan="8" class="hint" style="padding:10px">未解析到任何行（空行与 # 注释已忽略）</td></tr>`;
  }
  const imp = $("#tbf-import");
  imp.disabled = !okRows.length;
  imp.textContent = `导入有效 ${okRows.length} 条`;
  const sum = $("#tbf-summary");
  if (sum) sum.textContent = rows.length
    ? `共 ${rows.length} 行 · 有效 ${okRows.length} · 错误 ${rows.length - okRows.length}`
    : "";
}

async function importBatch() {
  const code = state.tradesPage.batchCode || normCodeInput($("#tbf-stock").value);
  if (!code) return toast("请先填写股票代码");
  const okRows = (state.tradesPage.batchRows || []).filter((r) => !r.error);
  if (!okRows.length) return toast("没有可导入的有效行");
  if (!confirm(`导入 ${okRows.length} 条记录到 ${code}？\n（不会修改已有记录，导入后请核对）`)) return;
  try {
    const d = await apiPost("/api/trades", {
      action: "add_batch", code,
      trades: okRows.map((r) => ({
        date: r.date, side: r.side, price: r.price, shares: r.shares,
        fee: r.fee != null ? r.fee : null, note: r.note || "",
      })),
    });
    const errs = d.errors || [];
    if (d.n_added) toast(`已导入 ${d.n_added} 条${errs.length ? `，${errs.length} 条失败` : ""}`);
    if (errs.length) {
      const detail = errs.slice(0, 5).map((e) => `第 ${e.index + 1} 条：${e.error}（${e.raw}）`).join("\n");
      alert(`以下 ${errs.length} 条未导入：\n` + detail + (errs.length > 5 ? "\n…" : ""));
    }
    state.tradesPage.batchRows = [];
    state.tradesPage.batchCode = null;
    hideTradesBatch();
    await loadTradesPage();
    syncTradesElsewhere(code);
  } catch (err) {
    toast("导入失败: " + err.message);
  }
}

/** 从个股页「管理交易」跳转：切到本页并预填该股。 */
function goToTradesPage(code) {
  switchTab("trades");
  if (code) {
    $("#trades-search").value = code;
    state.tradesPage.q = code;
    renderTradesPage();
    openTradesForm("add", code);
  }
}

function bindStockSuggest(inputId, suggestId) {
  const input = $(inputId);
  const box = $(suggestId);
  if (!input || !box) return;
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q || input.disabled) { box.style.display = "none"; return; }
    timer = setTimeout(async () => {
      try {
        const rows = (await api("/api/stocks?q=" + encodeURIComponent(q) + "&limit=8")) || [];
        box.innerHTML = rows.map((r) => `
          <div class="item" data-code="${r.code}">
            <span class="nm">${escHtml(r.name || r.code)}</span><span class="code">${r.code}</span>
          </div>`).join("") || `<div class="item" style="cursor:default;color:var(--text-dim)">无匹配</div>`;
        box.style.display = rows.length ? "block" : "none";
        box.querySelectorAll(".item[data-code]").forEach((el) =>
          el.addEventListener("click", () => {
            const r = rows.find((x) => x.code === el.dataset.code) || {};
            input.value = `${r.code || ""} ${r.name || ""}`.trim();
            box.style.display = "none";
          })
        );
      } catch (_) { box.style.display = "none"; }
    }, 250);
  });
  input.addEventListener("blur", () => setTimeout(() => { box.style.display = "none"; }, 150));
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") box.style.display = "none"; });
}
function bindTradesFormStock() { bindStockSuggest("#ntf-stock", "#ntf-suggest"); }

async function loadCompare() {
  const d = state.stockData;
  if (!d) return;
  const code = $("#cmp-index").value;
  try {
    const idx = await api("/api/index_kline?code=" + code);
    const map = new Map(idx.dates.map((dt, i) => [dt, idx.close[i]]));
    const idxSeries = d.dates.map((dt) => map.get(dt) ?? null);
    const c = mkChart("chart-cmp");
    c.setOption({
      tooltip: { ...TOOLTIP, valueFormatter: (v) => (v == null ? "—" : v.toFixed(2)) },
      legend: { top: 0, data: [d.name, idx.name], textStyle: { color: "#8b98ab", fontSize: 11 } },
      grid: { left: 56, right: 18, top: 34, bottom: 58 },
      xAxis: { type: "category", data: d.dates, ...AXIS },
      yAxis: { type: "value", scale: true, ...AXIS },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 12, borderColor: "#2a3342", backgroundColor: "#161b24", fillerColor: "rgba(79,142,247,.2)", textStyle: { color: "#8b98ab" } }],
      series: [
        { name: d.name, type: "line", data: normalize(d.close), showSymbol: false, lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT } },
        { name: idx.name, type: "line", data: normalize(idxSeries), showSymbol: false, lineStyle: { width: 1.4, type: "dashed", color: "#f0a03c" }, itemStyle: { color: "#f0a03c" } },
      ],
    });
    registerChartDates("chart-cmp", d.dates);   // 区间同步（同 K 线）
  } catch (err) { toast(err.message); }
}

/* ============================== 多因子复合突破 ============================== */

function mfGate(pass, label, valueTxt) {
  return `<span class="mf-gate ${pass ? "pass" : "fail"}" title="${pass ? "通过" : "未通过"}">` +
    `<span class="mf-gate-mark">${pass ? "✓" : "✗"}</span> ${label}` +
    (valueTxt ? ` <b>${valueTxt}</b>` : "") + `</span>`;
}

async function loadMultiFactor() {
  const cards = $("#mf-cards"), gates = $("#mf-gates"), banner = $("#mf-banner"),
        hint = $("#mf-hint");
  banner.style.display = "none";
  gates.innerHTML = "";
  hint.textContent = "";
  cards.innerHTML = `<div class="card"><div class="k">多因子复合突破</div>` +
    `<div class="v">…</div><div class="s">计算中（约 1 秒）</div></div>`;
  try {
    const r = await api(`/api/analysis?plugin=multi_factor_breakout&code=${state.stockCode}`);
    if (!r.ok) throw new Error(r.error || "计算失败");
    renderMultiFactor(r);
  } catch (err) {
    cards.innerHTML = "";
    if (charts["chart-mf"]) { charts["chart-mf"].dispose(); delete charts["chart-mf"]; }
    $("#chart-mf").innerHTML = "";
    hint.textContent = "多因子复合突破不可用: " + err.message;
  }
}

function renderMultiFactor(r) {
  const m = r.metrics || {};
  const cards = $("#mf-cards"), gates = $("#mf-gates"), banner = $("#mf-banner"),
        hint = $("#mf-hint");
  // 数据不足等降级场景：无核心指标，仅显示插件说明
  if (m.score_z == null || m.close == null) {
    cards.innerHTML = "";
    gates.innerHTML = "";
    setSubDot("mf", 0);
    if (charts["chart-mf"]) { charts["chart-mf"].dispose(); delete charts["chart-mf"]; }
    $("#chart-mf").innerHTML = "";
    hint.textContent = r.summary || "该股数据不足以计算多因子模型";
    return;
  }
  // 价格口径: 插件输出后复权价，换算成与 K 线图一致的"真实价等价"口径
  const d = state.stockData;
  const k = d && d.real_price && d.hfq_close ? d.real_price / d.hfq_close : 1;
  setSubDot("mf", m.signal_today ? 1 : 0);

  // ---- 指标卡 ----
  const reson = m.score_z != null && m.score_z >= 1.0;
  cards.innerHTML = `
    <div class="card"><div class="k">模型评分</div>
      <div class="v ${r.score >= 60 ? "up" : r.score < 40 ? "down" : ""}">${r.score ?? "—"}</div>
      <div class="s">${r.verdict || ""}</div></div>
    <div class="card"><div class="k">综合 z 分（四因子加权）</div>
      <div class="v ${pctCls(m.score_z)}">${m.score_z != null ? (m.score_z > 0 ? "+" : "") + m.score_z.toFixed(2) : "—"}</div>
      <div class="s">${reson ? "多因子共振" : "未共振（阈值 1.0）"}</div></div>
    <div class="card"><div class="k">量比（当日 / 前20日均量）</div>
      <div class="v ${m.gate_volume ? "up" : ""}">${m.vol_ratio != null ? m.vol_ratio.toFixed(2) + "×" : "—"}</div>
      <div class="s">硬门 ≥ 1.8×</div></div>
    <div class="card"><div class="k">ATR% 分位（近250日）</div>
      <div class="v ${m.gate_squeeze ? "up" : ""}">${m.squeeze_q != null ? (m.squeeze_q * 100).toFixed(0) + "%" : "—"}</div>
      <div class="s">${m.gate_squeeze ? "波动低位压缩" : "硬门 ≤ 30%"}</div></div>
    <div class="card"><div class="k">回测 5 日胜率</div>
      <div class="v ${m.bt_win5 != null ? pctCls(m.bt_win5 - 0.5) : ""}">${m.bt_win5 != null ? (m.bt_win5 * 100).toFixed(0) + "%" : "—"}</div>
      <div class="s">${m.bt_count != null ? `历史信号 ${m.bt_count} 个` : ""}</div></div>
    <div class="card"><div class="k">VAM · VPD</div>
      <div class="v" style="font-size:14px">${m.vam != null ? m.vam.toFixed(2) : "—"} · ${m.vpd != null ? m.vpd.toFixed(2) : "—"}</div>
      <div class="s">波动率调整动量 · 量价背离</div></div>
  `;

  // ---- 今日信号横幅 ----
  if (m.signal_today) {
    banner.textContent = `★ 今日触发多因子复合突破信号 —— 突破前20日最高 ${m.prior_high?.toFixed(2)} · 量比 ${m.vol_ratio?.toFixed(2)}× · ATR%分位 ${(m.squeeze_q * 100).toFixed(0)}% · 综合 z ${m.score_z?.toFixed(2)}`;
    banner.style.display = "block";
  }

  // ---- 三道硬门状态 ----
  gates.innerHTML = [
    mfGate(m.gate_breakout, "突破20日最高",
           `${(m.close * k).toFixed(2)} / ${(m.prior_high * k).toFixed(2)}`),
    mfGate(m.gate_volume, "放量 ≥1.8×", m.vol_ratio != null ? `${m.vol_ratio.toFixed(2)}×` : ""),
    mfGate(m.gate_squeeze, "波动低位压缩", m.squeeze_q != null ? `分位 ${(m.squeeze_q * 100).toFixed(0)}%` : ""),
    mfGate(reson, "多因子共振", `z ${m.score_z.toFixed(2)}`),
  ].join("");

  // ---- 复合图表（价格+突破线 / 量比 / 综合z+ATR%）----
  const S = r.series || {};
  const dates = (S.close || {}).dates || [];
  const close = (S.close || {}).values || [];
  const priorHigh = (S.prior_high || {}).values || [];
  const volRatio = (S.vol_ratio || {}).values || [];
  const atrPct = (S.atr_pct || {}).values || [];
  const scoreZ = (S.score_z || {}).values || [];
  const sig = (S.signal || {}).values || [];
  const sigPts = dates
    .map((dt, i) => (sig[i] === 1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null))
    .filter(Boolean);

  const c = mkChart("chart-mf");
  c.setOption({
    tooltip: { ...TOOLTIP },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 60, right: 16, top: 30, height: "36%" },
      { left: 60, right: 16, top: "48%", height: "17%" },
      { left: 60, right: 56, top: "70%", height: "20%" },
    ],
    xAxis: [0, 1, 2].map((gi) => ({
      type: "category", gridIndex: gi, data: dates, ...AXIS,
      axisLabel: gi === 2 ? AXIS.axisLabel : { show: false },
    })),
    yAxis: [
      { type: "value", gridIndex: 0, scale: true, ...AXIS },
      { type: "value", gridIndex: 1, ...AXIS },
      { type: "value", gridIndex: 2, ...AXIS },
      { type: "value", gridIndex: 2, position: "right", ...AXIS,
        axisLabel: { ...AXIS.axisLabel, formatter: (v) => (v * 100).toFixed(0) + "%" } },
    ],
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "收盘价", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: close.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT } },
      { name: "前20日最高", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: priorHigh.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.2, type: "dashed", color: "#8b98ab" },
        itemStyle: { color: "#8b98ab" } },
      { name: "信号触发", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: sigPts,
        symbol: "star", symbolSize: 13, itemStyle: { color: "#ef4b57" },
        tooltip: { formatter: (p) => `${p.value[0]}<br>★ 复合突破信号` } },
      { name: "量比", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volRatio,
        itemStyle: { color: (p) => (p.value >= 1.8 ? "rgba(239,75,87,.8)" : "rgba(79,142,247,.45)") },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "#f0a03c", type: "dashed" },
          label: { color: "#f0a03c", formatter: "1.8×", fontSize: 10 },
          data: [{ yAxis: 1.8 }] } },
      { name: "综合 z 分", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: scoreZ,
        showSymbol: false, lineStyle: { width: 1.6, color: "#b07cf7" }, itemStyle: { color: "#b07cf7" },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "#f0a03c", type: "dashed" },
          label: { color: "#f0a03c", formatter: "共振 1.0", fontSize: 10 },
          data: [{ yAxis: 1.0 }] } },
      { name: "ATR%", type: "line", xAxisIndex: 2, yAxisIndex: 3, data: atrPct,
        showSymbol: false, lineStyle: { width: 1.2, color: "#3cc7dd" }, itemStyle: { color: "#3cc7dd" } },
    ],
  });
  registerChartDates("chart-mf", dates);   // 区间同步（模型图数据窗口固定为最近 250 日）

  // ---- 历史信号明细表 ----
  const rows = (r.tables || {}).signals || [];
  const tb = $("#mf-table tbody");
  const cell = (v) => (v == null ? "—" : `<span class="${pctCls(v)}">${fmtPct(v)}</span>`);
  tb.innerHTML = rows.map((row) => `<tr>
      <td>${row.index}</td>
      <td class="num">${row.close != null ? (row.close * k).toFixed(2) : "—"}</td>
      <td class="num">${row.vol_ratio != null ? row.vol_ratio.toFixed(2) + "×" : "—"}</td>
      <td class="num">${row.squeeze_q != null ? (row.squeeze_q * 100).toFixed(0) + "%" : "—"}</td>
      <td class="num">${row.score_z != null ? (row.score_z > 0 ? "+" : "") + row.score_z.toFixed(2) : "—"}</td>
      <td class="num">${cell(row.fwd5)}</td>
      <td class="num">${cell(row.fwd10)}</td>
      <td class="num">${cell(row.fwd20)}</td>
    </tr>`).join("");
  $("#mf-table-wrap").style.display = rows.length ? "block" : "none";

  hint.textContent = (r.summary || "") +
    " · 因子与门判定只用当日及之前数据（无未来函数），前瞻收益仅用于回测评估";
}

/* ============================== GARCH 波动均值回归 ============================== */

function gmGate(pass, label, valueTxt) {
  return `<span class="mf-gate ${pass ? "pass" : "fail"}" title="${pass ? "通过" : "未通过"}">` +
    `<span class="mf-gate-mark">${pass ? "✓" : "✗"}</span> ${label}` +
    (valueTxt ? ` <b>${valueTxt}</b>` : "") + `</span>`;
}

async function loadGarch() {
  const cards = $("#gm-cards"), gates = $("#gm-gates"), banner = $("#gm-banner"),
        hint = $("#gm-hint");
  banner.style.display = "none";
  gates.innerHTML = "";
  hint.textContent = "";
  cards.innerHTML = `<div class="card"><div class="k">GARCH 波动均值回归</div>` +
    `<div class="v">…</div><div class="s">拟合中（ARMA+GARCH 约半秒）</div></div>`;
  try {
    const r = await api(`/api/analysis?plugin=garch_mean_reversion&code=${state.stockCode}`);
    if (!r.ok) throw new Error(r.error || "计算失败");
    renderGarch(r);
  } catch (err) {
    cards.innerHTML = "";
    if (charts["chart-gm"]) { charts["chart-gm"].dispose(); delete charts["chart-gm"]; }
    $("#chart-gm").innerHTML = "";
    hint.textContent = "GARCH 均值回归不可用: " + err.message;
  }
}

function renderGarch(r) {
  const m = r.metrics || {};
  const cards = $("#gm-cards"), gates = $("#gm-gates"), banner = $("#gm-banner"),
        hint = $("#gm-hint");
  // 数据不足等降级场景：无核心指标，仅显示插件说明
  if (m.dev_z == null || m.close == null) {
    cards.innerHTML = "";
    gates.innerHTML = "";
    setSubDot("gm", 0);
    if (charts["chart-gm"]) { charts["chart-gm"].dispose(); delete charts["chart-gm"]; }
    $("#chart-gm").innerHTML = "";
    hint.textContent = r.summary || "该股数据不足以计算 GARCH 模型（需约 300 个交易日）";
    return;
  }
  // 价格口径: 插件输出后复权价，换算成与 K 线图一致的"真实价等价"口径
  const d = state.stockData;
  const k = d && d.real_price && d.hfq_close ? d.real_price / d.hfq_close : 1;
  const fired = m.signal_today || 0;
  setSubDot("gm", fired);
  const REGIME_TXT = { range: "震荡市", trending: "强趋势", black_swan: "黑天鹅" };
  const zEntry = 2.0;

  // ---- 指标卡 ----
  cards.innerHTML = `
    <div class="card"><div class="k">模型评分</div>
      <div class="v ${r.score >= 60 ? "up" : r.score < 40 ? "down" : ""}">${r.score ?? "—"}</div>
      <div class="s">${r.verdict || ""}</div></div>
    <div class="card"><div class="k">偏离指标 dev_z</div>
      <div class="v ${fired > 0 ? "up" : fired < 0 ? "down" : ""}">${m.dev_z != null ? (m.dev_z > 0 ? "+" : "") + m.dev_z.toFixed(2) : "—"}</div>
      <div class="s">${fired > 0 ? "超卖（预测向上回归）" : fired < 0 ? "超买（预测向下回归）" : `触发阈值 ±${zEntry}`}</div></div>
    <div class="card"><div class="k">市场状态 regime</div>
      <div class="v" style="font-size:16px">${REGIME_TXT[m.regime] || m.regime || "—"}</div>
      <div class="s">${m.trend_score != null ? `20日位移 ${m.trend_score.toFixed(1)}σ · 单日 ${m.shock != null ? m.shock.toFixed(1) + "σ" : "—"}` : ""}</div></div>
    <div class="card"><div class="k">次日波动预测 σ̂</div>
      <div class="v">${m.sigma1 != null ? (m.sigma1 * 100).toFixed(2) + "%" : "—"}</div>
      <div class="s">GARCH α ${m.garch_alpha?.toFixed(2)} β ${m.garch_beta?.toFixed(2)} · 持续性 ${m.persistence?.toFixed(2)}</div></div>
    <div class="card"><div class="k">次日区间预测（高低点）</div>
      <div class="v" style="font-size:16px">${m.range_lo != null ? (m.range_lo * k).toFixed(2) + " ~ " + (m.range_hi * k).toFixed(2) : "—"}</div>
      <div class="s">历史命中率 ${m.band_hit != null ? (m.band_hit * 100).toFixed(1) + "%" : "—"}</div></div>
    <div class="card"><div class="k">回测 5 日方向胜率</div>
      <div class="v ${m.bt_win5 != null ? pctCls(m.bt_win5 - 0.5) : ""}">${m.bt_win5 != null ? (m.bt_win5 * 100).toFixed(0) + "%" : "—"}</div>
      <div class="s">${m.bt_count != null ? `历史信号 ${m.bt_count} 个 · 20日 ${m.bt_win20 != null ? (m.bt_win20 * 100).toFixed(0) + "%" : "—"}` : ""}</div></div>
    <div class="card"><div class="k">偏离回归半衰期</div>
      <div class="v">${m.half_life != null ? m.half_life.toFixed(0) + " 日" : "—"}</div>
      <div class="s">${m.rev_frac != null ? `5日预期回归 ${(m.rev_frac * 100).toFixed(0)}%（1-ρ⁵）` : ""}</div></div>
  `;

  // ---- 今日信号横幅 ----
  if (fired !== 0) {
    const dir = fired > 0 ? "超卖 → 预测向上回归" : "超买 → 预测向下回归";
    banner.textContent = `★ 今日触发均值回归信号（${dir}）—— dev_z ${m.dev_z.toFixed(2)} · ` +
      `预测 5 日内回归 ${(m.exp_rev * 100).toFixed(1)}% 至均值 ${(m.ma * k).toFixed(2)} · ` +
      `regime ${REGIME_TXT[m.regime]} · 半衰期 ${m.half_life?.toFixed(0)} 日`;
    banner.style.display = "block";
  }

  // ---- 四道状态门（模型逻辑透明化）----
  gates.innerHTML = [
    gmGate(Math.abs(m.dev_z) >= zEntry, `偏离达阈值 ±${zEntry}`,
           m.dev_z != null ? `dev_z ${m.dev_z > 0 ? "+" : ""}${m.dev_z.toFixed(2)}` : ""),
    gmGate(m.regime === "range", "震荡市（非趋势/黑天鹅）",
           `位移 ${m.trend_score?.toFixed(1)}σ / 冲击 ${m.shock?.toFixed(1)}σ`),
    gmGate(m.shock != null && m.shock < 6, "非黑天鹅（单日 < 6σ）",
           m.shock != null ? `${m.shock.toFixed(1)}σ` : ""),
    gmGate(m.rev_frac != null && m.rev_frac >= 0.05, "回归幅度足够（≥5%）",
           m.rev_frac != null ? `半衰期 ${m.half_life?.toFixed(0)} 日` : ""),
  ].join("");

  // ---- 复合图表（价格+区间包络+信号 / dev_z / σ̂%）----
  const S = r.series || {};
  const dates = (S.close || {}).dates || [];
  const close = (S.close || {}).values || [];
  const ma = (S.ma || {}).values || [];
  const bandLo = (S.band_lo || {}).values || [];
  const bandHi = (S.band_hi || {}).values || [];
  const devZ = (S.dev_z || {}).values || [];
  const sigma = (S.sigma || {}).values || [];
  const sig = (S.signal || {}).values || [];
  const sigUp = dates.map((dt, i) => (sig[i] === 1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);
  const sigDn = dates.map((dt, i) => (sig[i] === -1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);

  const c = mkChart("chart-gm");
  c.setOption({
    tooltip: { ...TOOLTIP },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    legend: { top: 0, data: ["收盘价", "MA60 均衡线", "区间下沿", "区间上沿"],
              textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    grid: [
      { left: 60, right: 16, top: 40, height: "34%" },
      { left: 60, right: 16, top: "50%", height: "17%" },
      { left: 60, right: 16, top: "72%", height: "18%" },
    ],
    xAxis: [0, 1, 2].map((gi) => ({
      type: "category", gridIndex: gi, data: dates, ...AXIS,
      axisLabel: gi === 2 ? AXIS.axisLabel : { show: false },
    })),
    yAxis: [
      { type: "value", gridIndex: 0, scale: true, ...AXIS },
      { type: "value", gridIndex: 1, ...AXIS },
      { type: "value", gridIndex: 2, ...AXIS },
    ],
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "收盘价", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: close.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT }, z: 5 },
      { name: "MA60 均衡线", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: ma.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.4, color: "#f0a03c" }, itemStyle: { color: "#f0a03c" } },
      { name: "区间上沿", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: bandHi.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(139,152,171,.7)" },
        itemStyle: { color: "#8b98ab" } },
      { name: "区间下沿", type: "line", xAxisIndex: 0, yAxisIndex: 0,
        data: bandLo.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(139,152,171,.7)" },
        itemStyle: { color: "#8b98ab" },
        areaStyle: { color: "rgba(79,142,247,.06)" } },
      { name: "超卖信号", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: sigUp,
        symbol: "star", symbolSize: 13, itemStyle: { color: UP },
        tooltip: { formatter: (p) => `${p.value[0]}<br>★ 超卖回归信号` } },
      { name: "超买信号", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: sigDn,
        symbol: "triangle", symbolSize: 10, itemStyle: { color: DOWN },
        tooltip: { formatter: (p) => `${p.value[0]}<br>▼ 超买回归信号` } },
      { name: "dev_z", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: devZ,
        showSymbol: false, lineStyle: { width: 1.6, color: "#b07cf7" }, itemStyle: { color: "#b07cf7" },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "#f0a03c", type: "dashed" },
          label: { color: "#f0a03c", fontSize: 10 },
          data: [{ yAxis: zEntry, label: { formatter: `+${zEntry}` } },
                 { yAxis: -zEntry, label: { formatter: `-${zEntry}` } }] } },
      { name: "次日σ̂%", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: sigma,
        showSymbol: false, lineStyle: { width: 1.2, color: "#3cc7dd" }, itemStyle: { color: "#3cc7dd" } },
    ],
  });
  registerChartDates("chart-gm", dates);   // 区间同步（模型图数据窗口固定为最近 250 日）

  // ---- 历史信号明细表 ----
  const rows = (r.tables || {}).signals || [];
  const tb = $("#gm-table tbody");
  const cell = (v) => (v == null ? "—" : `<span class="${pctCls(v)}">${fmtPct(v)}</span>`);
  tb.innerHTML = rows.map((row) => `<tr>
      <td>${row.index}</td>
      <td>${row.dir > 0 ? '<span class="up">超卖 ↗</span>' : '<span class="down">超买 ↘</span>'}</td>
      <td class="num">${row.close != null ? (row.close * k).toFixed(2) : "—"}</td>
      <td class="num">${row.dev_z != null ? (row.dev_z > 0 ? "+" : "") + row.dev_z.toFixed(2) : "—"}</td>
      <td class="num">${cell(row.fwd5)}</td>
      <td class="num">${cell(row.fwd10)}</td>
      <td class="num">${cell(row.fwd20)}</td>
    </tr>`).join("");
  $("#gm-table-wrap").style.display = rows.length ? "block" : "none";

  hint.textContent = (r.summary || "") +
    " · 信号与 regime 判定只用当日及之前数据（无未来函数）；区间包络由历史高/低偏移" +
    "97.5 分位标定（联合 95% 覆盖），前瞻收益与命中率仅用于回测评估";
}

/* ============================== 筹码分布与成本均线（插件） ============================== */

function cdGate(pass, label, valueTxt) {
  return `<span class="mf-gate ${pass ? "pass" : "fail"}" title="${pass ? "通过" : "未通过"}">` +
    `<span class="mf-gate-mark">${pass ? "✓" : "✗"}</span> ${label}` +
    (valueTxt ? ` <b>${valueTxt}</b>` : "") + `</span>`;
}

async function loadChip() {
  const cards = $("#cd-cards"), gates = $("#cd-gates"), banner = $("#cd-banner"),
        hint = $("#cd-hint");
  banner.style.display = "none";
  gates.innerHTML = "";
  hint.textContent = "";
  cards.innerHTML = `<div class="card"><div class="k">筹码分布与成本均线</div>` +
    `<div class="v">…</div><div class="s">构建筹码（换手衰减法 · 800 日）</div></div>`;
  try {
    const r = await api(`/api/analysis?plugin=chip_distribution&code=${state.stockCode}`);
    if (!r.ok) throw new Error(r.error || "计算失败");
    renderChipModel(r);
  } catch (err) {
    cards.innerHTML = "";
    ["chart-cd-1", "chart-cd-2", "chart-cd-3"].forEach((id) => {
      if (charts[id]) { charts[id].dispose(); delete charts[id]; }
      $("#" + id).innerHTML = "";
    });
    hint.textContent = "筹码模型不可用: " + err.message;
  }
}

function renderChipModel(r) {
  const m = r.metrics || {};
  const cards = $("#cd-cards"), gates = $("#cd-gates"), banner = $("#cd-banner"),
        hint = $("#cd-hint");
  if (m.profit_ratio == null || m.close == null) {
    cards.innerHTML = "";
    gates.innerHTML = "";
    setSubDot("cd", 0);
    ["chart-cd-1", "chart-cd-2", "chart-cd-3"].forEach((id) => {
      if (charts[id]) { charts[id].dispose(); delete charts[id]; }
      $("#" + id).innerHTML = "";
    });
    hint.textContent = r.summary || "该股数据不足以计算筹码模型（需约 250 个交易日）";
    return;
  }
  // 价格口径: 插件输出后复权价，换算成与 K 线图一致的"真实价等价"口径
  const d = state.stockData;
  const k = d && d.real_price && d.hfq_close ? d.real_price / d.hfq_close : 1;
  const fired = m.signal_today || 0;
  setSubDot("cd", fired);
  const REGIME_TXT = { oversold: "深度套牢区", overbought: "普遍获利区",
    accumulating: "集中建仓区", shock: "超高换手冲击", warmup: "预热期",
    normal: "常规状态" };
  const prLow = 0.10, prHigh = 0.90, shockTo = 0.10;
  const diff = m.avg_cost != null ? m.close / m.avg_cost - 1 : null;

  // ---- 指标卡 ----
  const win10ev = m.bt_win10_ev, win10 = m.bt_win10;
  const rtH = m.resist_touch_hold, rtB = m.resist_touch_hold_base;
  const stH = m.support_touch_hold, stB = m.support_touch_hold_base;
  const qTxt = (a, b) => a == null ? "—" :
    (b == null ? (a * 100).toFixed(1) + "%" : (a * 100).toFixed(1) + "% / 基线 " + (b * 100).toFixed(1) + "%");
  cards.innerHTML = `
    <div class="card"><div class="k">模型评分</div>
      <div class="v ${r.score != null && r.score >= 60 ? "up" : r.score != null && r.score < 40 ? "down" : ""}">${r.score ?? "—"}</div>
      <div class="s">${r.verdict || ""}</div></div>
    <div class="card"><div class="k">获利比例</div>
      <div class="v ${m.profit_ratio >= 0.5 ? "up" : "down"}">${(m.profit_ratio * 100).toFixed(1)}%</div>
      <div class="s ${diff != null ? pctCls(diff) : ""}">现价 vs 平均成本 ${diff != null ? fmtPct(diff) : "—"}</div></div>
    <div class="card"><div class="k">成本均线（平均成本）</div>
      <div class="v">${m.avg_cost != null ? (m.avg_cost * k).toFixed(2) : "—"}</div>
      <div class="s">中位成本 ${m.median_cost != null ? (m.median_cost * k).toFixed(2) : "—"}</div></div>
    <div class="card"><div class="k">70% 筹码集中度</div>
      <div class="v" style="font-size:16px">${m.conc70 != null ? (m.conc70 * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">${m.conc_improve != null ? `20日改善 ${(m.conc_improve * 100).toFixed(0)}%` : ""}</div></div>
    <div class="card"><div class="k">支撑 / 阻力（P15 / P85）</div>
      <div class="v" style="font-size:16px">${m.support != null && m.resistance != null ? (m.support * k).toFixed(2) + " / " + (m.resistance * k).toFixed(2) : "—"}</div>
      <div class="s">90% 区间 ${m.range90_lo != null && m.range90_hi != null ? (m.range90_lo * k).toFixed(2) + " ~ " + (m.range90_hi * k).toFixed(2) : "—"}</div></div>
    <div class="card"><div class="k">换手率</div>
      <div class="v">${m.turnover != null ? (m.turnover * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">20日均值 ${m.turnover_avg20 != null ? (m.turnover_avg20 * 100).toFixed(1) + "%" : "—"}${m.turnover_estimated ? "（估算）" : ""}</div></div>
    <div class="card"><div class="k">回测胜率（10日·事件）</div>
      <div class="v ${win10ev != null ? pctCls(win10ev - 0.5) : ""}">${win10ev != null ? (win10ev * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">按日口径 ${win10 != null ? (win10 * 100).toFixed(1) + "%" : "—"} · 事件 ${m.bt_count_ev ?? "—"} 个</div></div>
    <div class="card"><div class="k">支撑/阻力质量（触及收回率）</div>
      <div class="v" style="font-size:14px">阻 ${qTxt(rtH, rtB)}</div>
      <div class="s">支 ${qTxt(stH, stB)}（高=位更硬）</div></div>
  `;

  // ---- 今日信号横幅 ----
  if (fired !== 0) {
    const dir = fired > 0
      ? "超跌支撑（获利比例过低，卖压衰竭，看反弹）"
      : "获利抛压（获利比例过高，兑现压力大）";
    banner.textContent = `★ 今日触发筹码信号（${dir}）—— 获利比例 ${(m.profit_ratio * 100).toFixed(1)}% · ` +
      `成本均线 ${(m.avg_cost * k).toFixed(2)} · ${m.conc70 != null ? "集中度 " + (m.conc70 * 100).toFixed(1) + "%" : ""} · regime ${REGIME_TXT[m.regime] || m.regime}`;
    banner.style.display = "block";
  }

  // ---- 信号条件透明化 ----
  gates.innerHTML = [
    cdGate(m.profit_ratio <= prLow, `S1 获利比例 ≤${(prLow * 100).toFixed(0)}%（超跌支撑）`,
           `${(m.profit_ratio * 100).toFixed(1)}%`),
    cdGate(m.profit_ratio >= prHigh, `S2 获利比例 ≥${(prHigh * 100).toFixed(0)}%（获利抛压）`,
           `${(m.profit_ratio * 100).toFixed(1)}%`),
    cdGate(m.conc_improve != null && m.conc_improve >= 0.15 && m.avg_cost != null && m.close > m.avg_cost,
           "S3 集中度20日改善≥15% 且 价>成本（建仓）",
           m.conc_improve != null ? `${(m.conc_improve * 100).toFixed(0)}%` : ""),
    cdGate(m.turnover == null || m.turnover < shockTo, `非超高换手（单日 <${(shockTo * 100).toFixed(0)}%）`,
           m.turnover != null ? `${(m.turnover * 100).toFixed(1)}%` : ""),
  ].join("");

  // ---- 三张图 ----
  const S = r.series || {};
  const dates = (S.close || {}).dates || [];
  const close = (S.close || {}).values || [];
  const avgCost = (S.avg_cost || {}).values || [];
  const support = (S.support || {}).values || [];
  const resistance = (S.resistance || {}).values || [];
  const prS = (S.profit_ratio || {}).values || [];
  const c70 = (S.conc70 || {}).values || [];
  const sig = (S.signal || {}).values || [];
  const sigUp = dates.map((dt, i) => (sig[i] === 1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);
  const sigDn = dates.map((dt, i) => (sig[i] === -1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);

  const c1 = mkChart("chart-cd-1");
  c1.setOption({
    title: { text: "价格 × 成本均线 × 筹码支撑/阻力带（P15/P85）", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" } },
    legend: { top: 0, right: 0, data: ["收盘价", "成本均线", "支撑 P15", "阻力 P85"],
              textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", scale: true, ...AXIS },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "收盘价", type: "line", data: close.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT }, z: 5 },
      { name: "成本均线", type: "line", data: avgCost.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.5, color: "#f0a03c" }, itemStyle: { color: "#f0a03c" } },
      { name: "阻力 P85", type: "line", data: resistance.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(239,75,87,.65)" },
        itemStyle: { color: UP } },
      { name: "支撑 P15", type: "line", data: support.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(46,189,133,.65)" },
        itemStyle: { color: DOWN } },
      { name: "超跌支撑信号", type: "scatter", data: sigUp, symbol: "star", symbolSize: 13,
        itemStyle: { color: UP }, tooltip: { formatter: (p) => `${p.value[0]}<br>★ S1 超跌支撑` } },
      { name: "获利抛压信号", type: "scatter", data: sigDn, symbol: "triangle", symbolSize: 10,
        itemStyle: { color: DOWN }, tooltip: { formatter: (p) => `${p.value[0]}<br>▼ S2 获利抛压` } },
    ],
  });

  const c2 = mkChart("chart-cd-2");
  c2.setOption({
    title: { text: "获利比例（成本低于现价的筹码占比）", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" },
               valueFormatter: (v) => (v == null ? "—" : v.toFixed(1) + "%") },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", min: 0, max: 100, ...AXIS,
             axisLabel: { ...AXIS.axisLabel, formatter: "{value}%" } },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "获利比例", type: "line", data: prS, showSymbol: false,
        lineStyle: { width: 1.6, color: "#b07cf7" }, itemStyle: { color: "#b07cf7" },
        areaStyle: { color: "rgba(176,124,247,.08)" },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed" },
          label: { color: "#8b98ab", fontSize: 10 },
          data: [
            { yAxis: prLow * 100, lineStyle: { color: DOWN }, label: { formatter: `S1 ${prLow * 100}%` } },
            { yAxis: prHigh * 100, lineStyle: { color: UP }, label: { formatter: `S2 ${prHigh * 100}%` } },
          ] } },
    ],
  });

  const c3 = mkChart("chart-cd-3");
  c3.setOption({
    title: { text: "70% 筹码集中度（P85-P15 归一化，越低越集中）", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" },
               valueFormatter: (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%") },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", ...AXIS,
             axisLabel: { ...AXIS.axisLabel, formatter: (v) => (v * 100).toFixed(0) + "%" } },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "集中度", type: "line", data: c70, showSymbol: false,
        lineStyle: { width: 1.4, color: "#3cc7dd" }, itemStyle: { color: "#3cc7dd" },
        areaStyle: { color: "rgba(60,199,221,.08)" } },
    ],
  });
  ["chart-cd-1", "chart-cd-2", "chart-cd-3"].forEach((id) => registerChartDates(id, dates));

  // ---- 历史信号明细表（独立事件）----
  const TYPE_TXT = { support: '<span class="up">超跌支撑 ↗</span>',
                    resist: '<span class="down">获利抛压 ↘</span>',
                    conc: '<span style="color:#f0a03c">集中建仓 ◆</span>' };
  const rows = (r.tables || {}).signals || [];
  const tb = $("#cd-table tbody");
  const cell = (v) => (v == null ? "—" : `<span class="${pctCls(v)}">${fmtPct(v)}</span>`);
  tb.innerHTML = rows.map((row) => `<tr>
      <td>${row.index}</td>
      <td>${TYPE_TXT[row.type] || row.type}</td>
      <td class="num">${row.close != null ? (row.close * k).toFixed(2) : "—"}</td>
      <td class="num">${row.profit_ratio != null ? (row.profit_ratio * 100).toFixed(1) + "%" : "—"}</td>
      <td class="num">${cell(row.fwd5)}</td>
      <td class="num">${cell(row.fwd10)}</td>
      <td class="num">${cell(row.fwd20)}</td>
    </tr>`).join("");
  $("#cd-table-wrap").style.display = rows.length ? "block" : "none";

  hint.textContent = (r.summary || "") +
    " · 换手衰减法：每日筹码按换手率衰减后按日内三角分布（峰=均价）累积；" +
    "信号与 regime 判定只用当日及之前数据（无未来函数），前瞻收益与胜率仅用于回测评估";
}

/* ============================== 布林带通道与相对位置（插件） ============================== */

function bbGate(pass, label, valueTxt) {
  return `<span class="mf-gate ${pass ? "pass" : "fail"}" title="${pass ? "通过" : "未通过"}">` +
    `<span class="mf-gate-mark">${pass ? "✓" : "✗"}</span> ${label}` +
    (valueTxt ? ` <b>${valueTxt}</b>` : "") + `</span>`;
}

async function loadBB() {
  const cards = $("#bb-cards"), gates = $("#bb-gates"), banner = $("#bb-banner"),
        hint = $("#bb-hint");
  banner.style.display = "none";
  gates.innerHTML = "";
  hint.textContent = "";
  cards.innerHTML = `<div class="card"><div class="k">布林带通道</div>` +
    `<div class="v">…</div><div class="s">计算通道（MA±2σ · 挤压分位）</div></div>`;
  try {
    const r = await api(`/api/analysis?plugin=bollinger_channel&code=${state.stockCode}`);
    if (!r.ok) throw new Error(r.error || "计算失败");
    renderBBModel(r);
  } catch (err) {
    cards.innerHTML = "";
    ["chart-bb-1", "chart-bb-2", "chart-bb-3"].forEach((id) => {
      if (charts[id]) { charts[id].dispose(); delete charts[id]; }
      $("#" + id).innerHTML = "";
    });
    hint.textContent = "布林通道不可用: " + err.message;
  }
}

function renderBBModel(r) {
  const m = r.metrics || {};
  const cards = $("#bb-cards"), gates = $("#bb-gates"), banner = $("#bb-banner"),
        hint = $("#bb-hint");
  if (m.pctb == null || m.close == null) {
    cards.innerHTML = "";
    gates.innerHTML = "";
    setSubDot("bb", 0);
    ["chart-bb-1", "chart-bb-2", "chart-bb-3"].forEach((id) => {
      if (charts[id]) { charts[id].dispose(); delete charts[id]; }
      $("#" + id).innerHTML = "";
    });
    hint.textContent = r.summary || "该股数据不足以计算布林通道（需约 250 个交易日）";
    return;
  }
  // 价格口径: 插件输出后复权价，换算成与 K 线图一致的"真实价等价"口径
  const d = state.stockData;
  const k = d && d.real_price && d.hfq_close ? d.real_price / d.hfq_close : 1;
  const fired = m.signal_today || 0;
  setSubDot("bb", fired);
  const sigType = m.signal_type_today || "";
  const TYPE_ZH = { bottom: "区间下沿（%b≤0，震荡买点）",
                    top: "区间上沿（%b≥1，震荡卖点）",
                    squeeze_up: "挤压突破向上", squeeze_dn: "挤压突破向下" };
  const REGIME_TXT = { ride_up: "贴轨上行", ride_dn: "贴轨下行",
    trending: "强单边趋势", normal: "常规状态", warmup: "预热期" };
  const pctb = m.pctb;

  // ---- 指标卡 ----
  const win10ev = m.bt_win10_ev, win10 = m.bt_win10;
  const magRatio = (m.squeeze_mag10 != null && m.mag10_base)
    ? (m.squeeze_mag10 / m.mag10_base) : null;
  cards.innerHTML = `
    <div class="card"><div class="k">模型评分</div>
      <div class="v ${r.score != null && r.score >= 60 ? "up" : r.score != null && r.score < 40 ? "down" : ""}">${r.score ?? "—"}</div>
      <div class="s">${r.verdict || ""}</div></div>
    <div class="card"><div class="k">%b 相对位置</div>
      <div class="v ${pctb >= 1 ? "up" : pctb < 0 ? "down" : ""}">${pctb.toFixed(2)}</div>
      <div class="s">0=下轨 0.5=中轨 1=上轨${pctb < 0 ? "（下轨外）" : pctb > 1 ? "（上轨外）" : ""}</div></div>
    <div class="card"><div class="k">中轨 MA20</div>
      <div class="v">${m.mid != null ? (m.mid * k).toFixed(2) : "—"}</div>
      <div class="s ${m.mid != null ? pctCls(m.close / m.mid - 1) : ""}">现价 ${m.mid != null ? fmtPct(m.close / m.mid - 1) : "—"}</div></div>
    <div class="card"><div class="k">上轨 / 下轨（±2σ）</div>
      <div class="v" style="font-size:16px">${m.upper != null && m.lower != null ? (m.upper * k).toFixed(2) + " / " + (m.lower * k).toFixed(2) : "—"}</div>
      <div class="s">带宽 ${m.bandw != null ? (m.bandw * 100).toFixed(1) + "%" : "—"}</div></div>
    <div class="card"><div class="k">带宽 120 日分位</div>
      <div class="v" style="font-size:16px">${m.bw_percentile != null ? (m.bw_percentile * 100).toFixed(0) + "%" : "—"}</div>
      <div class="s" ${m.squeeze ? 'style="color:#b07cf7"' : ""}>${m.squeeze ? "挤压态（波动压缩）" : "非挤压态"}</div></div>
    <div class="card"><div class="k">通道校准（次日命中率）</div>
      <div class="v ${m.hit1 != null && m.hit1 >= 0.95 ? "up" : m.hit1 != null && m.hit1 < 0.85 ? "down" : ""}">${m.hit1 != null ? (m.hit1 * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">10日包络 ${m.hit10 != null ? (m.hit10 * 100).toFixed(1) + "%" : "—"} / 基线 ${m.hit10_base != null ? (m.hit10_base * 100).toFixed(1) + "%" : "—"}</div></div>
    <div class="card"><div class="k">回测胜率（10日·事件）</div>
      <div class="v ${win10ev != null ? pctCls(win10ev - 0.5) : ""}">${win10ev != null ? (win10ev * 100).toFixed(1) + "%" : "—"}</div>
      <div class="s">按日口径 ${win10 != null ? (win10 * 100).toFixed(1) + "%" : "—"} · 事件 ${m.bt_count_ev ?? "—"} 个</div></div>
    <div class="card"><div class="k">挤压扩张检验</div>
      <div class="v ${magRatio != null ? pctCls(magRatio - 1) : ""}">${magRatio != null ? magRatio.toFixed(2) + "×" : "—"}</div>
      <div class="s">S3后10日|收益| ${m.squeeze_mag10 != null ? (m.squeeze_mag10 * 100).toFixed(1) + "%" : "—"} vs 全样本 ${m.mag10_base != null ? (m.mag10_base * 100).toFixed(1) + "%" : "—"}</div></div>
  `;

  // ---- 今日信号横幅 ----
  if (fired !== 0) {
    const dir = TYPE_ZH[sigType] ||
      (fired > 0 ? "看多信号" : "看空信号");
    banner.textContent = `★ 今日触发布林信号（${dir}）—— %b ${pctb.toFixed(2)} · ` +
      `中轨 ${(m.mid * k).toFixed(2)} · 带宽 ${m.bandw != null ? (m.bandw * 100).toFixed(1) + "%" : "—"} · ` +
      `regime ${REGIME_TXT[m.regime] || m.regime}`;
    banner.style.display = "block";
  }

  // ---- 信号条件透明化 ----
  gates.innerHTML = [
    bbGate(pctb <= 0, "S1 %b ≤ 0（下轨外，区间买点）", pctb.toFixed(2)),
    bbGate(pctb >= 1, "S2 %b ≥ 1（上轨外，区间卖点）", pctb.toFixed(2)),
    bbGate((m.squeeze === 1 && (pctb >= 1 || pctb <= 0)) || sigType.startsWith("squeeze"),
           "S3 挤压 + 破轨（突破形态，近10日挤压态）",
           m.bw_percentile != null ? `带宽 ${m.bw_percentile * 100}% 分位` : ""),
    bbGate(m.regime !== "ride_up" && m.regime !== "ride_dn",
           "非贴轨运行（防强趋势提前离场）",
           REGIME_TXT[m.regime] || ""),
  ].join("");

  // ---- 三张图 ----
  const S = r.series || {};
  const dates = (S.close || {}).dates || [];
  const close = (S.close || {}).values || [];
  const mid = (S.mid || {}).values || [];
  const upper = (S.upper || {}).values || [];
  const lower = (S.lower || {}).values || [];
  const pctbS = (S.pctb || {}).values || [];
  const bandwS = (S.bandw || {}).values || [];
  const squeezeS = (S.squeeze || {}).values || [];
  const sig = (S.signal || {}).values || [];
  // 信号按类型拆分（需明细表类型对齐: 用 series.signal 的方向 + pctb 判挤压不精确，
  // 这里简化为按方向: +1 下沿/挤压向上, -1 上沿/挤压向下）
  const sigUp = dates.map((dt, i) => (sig[i] === 1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);
  const sigDn = dates.map((dt, i) => (sig[i] === -1 && close[i] != null ? [dt, +(close[i] * k).toFixed(2)] : null)).filter(Boolean);
  const sqPts = dates.map((dt, i) => (squeezeS[i] === 1 && bandwS[i] != null ? [dt, bandwS[i]] : null)).filter(Boolean);

  const c1 = mkChart("chart-bb-1");
  c1.setOption({
    title: { text: "价格 × 布林通道（MA20 ± 2σ）× 信号", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" } },
    legend: { top: 0, right: 0, data: ["收盘价", "中轨", "上轨", "下轨"],
              textStyle: { color: "#8b98ab", fontSize: 11 }, itemWidth: 14 },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", scale: true, ...AXIS },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "收盘价", type: "line", data: close.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.8, color: ACCENT }, itemStyle: { color: ACCENT }, z: 5 },
      { name: "中轨", type: "line", data: mid.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1.2, color: "#f0a03c" }, itemStyle: { color: "#f0a03c" } },
      { name: "上轨", type: "line", data: upper.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(239,75,87,.6)" },
        itemStyle: { color: UP } },
      { name: "下轨", type: "line", data: lower.map((v) => (v == null ? null : +(v * k).toFixed(2))),
        showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "rgba(46,189,133,.6)" },
        itemStyle: { color: DOWN },
        areaStyle: { color: "rgba(79,142,247,.05)" } },
      { name: "看多信号", type: "scatter", data: sigUp, symbol: "star", symbolSize: 13,
        itemStyle: { color: UP }, tooltip: { formatter: (p) => `${p.value[0]}<br>★ 下沿反弹 / 挤压向上` } },
      { name: "看空信号", type: "scatter", data: sigDn, symbol: "triangle", symbolSize: 10,
        itemStyle: { color: DOWN }, tooltip: { formatter: (p) => `${p.value[0]}<br>▼ 上沿回落 / 挤压向下` } },
    ],
  });

  const c2 = mkChart("chart-bb-2");
  c2.setOption({
    title: { text: "%b 相对位置（0=下轨 1=上轨，越界=突破）", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" },
               valueFormatter: (v) => (v == null ? "—" : (+v).toFixed(2)) },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", ...AXIS },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "%b", type: "line", data: pctbS, showSymbol: false,
        lineStyle: { width: 1.6, color: "#b07cf7" }, itemStyle: { color: "#b07cf7" },
        areaStyle: { color: "rgba(176,124,247,.08)" },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed" },
          label: { color: "#8b98ab", fontSize: 10 },
          data: [
            { yAxis: 1, lineStyle: { color: UP }, label: { formatter: "上轨 1.0" } },
            { yAxis: 0.5, lineStyle: { color: "rgba(139,152,171,.5)" }, label: { formatter: "中轨 0.5" } },
            { yAxis: 0, lineStyle: { color: DOWN }, label: { formatter: "下轨 0.0" } },
          ] } },
    ],
  });

  const c3 = mkChart("chart-bb-3");
  c3.setOption({
    title: { text: "带宽 %（波动率，低=挤压态）", left: 0, top: 0,
             textStyle: { color: "#8b98ab", fontSize: 12, fontWeight: "normal" } },
    tooltip: { ...TOOLTIP, axisPointer: { type: "cross" },
               valueFormatter: (v) => (v == null ? "—" : (+v).toFixed(2) + "%") },
    grid: { left: 60, right: 16, top: 34, bottom: 46 },
    xAxis: { type: "category", data: dates, ...AXIS },
    yAxis: { type: "value", ...AXIS,
             axisLabel: { ...AXIS.axisLabel, formatter: "{value}%" } },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "带宽", type: "line", data: bandwS, showSymbol: false,
        lineStyle: { width: 1.4, color: "#3cc7dd" }, itemStyle: { color: "#3cc7dd" },
        areaStyle: { color: "rgba(60,199,221,.08)" } },
      { name: "挤压态", type: "scatter", data: sqPts, symbolSize: 5,
        itemStyle: { color: "#b07cf7" },
        tooltip: { formatter: (p) => `${p.value[0]}<br>● 挤压态（带宽处于 120 日最低 10% 分位）` } },
    ],
  });
  ["chart-bb-1", "chart-bb-2", "chart-bb-3"].forEach((id) => registerChartDates(id, dates));

  // ---- 历史信号明细表（独立事件）----
  const TYPE_TXT = { bottom: '<span class="up">下沿反弹 ↗</span>',
                    top: '<span class="down">上沿回落 ↘</span>',
                    squeeze_up: '<span class="up">挤压向上 ◆</span>',
                    squeeze_dn: '<span class="down">挤压向下 ◆</span>' };
  const rows = (r.tables || {}).signals || [];
  const tb = $("#bb-table tbody");
  const cell = (v) => (v == null ? "—" : `<span class="${pctCls(v)}">${fmtPct(v)}</span>`);
  tb.innerHTML = rows.map((row) => `<tr>
      <td>${row.index}</td>
      <td>${TYPE_TXT[row.type] || row.type}</td>
      <td class="num">${row.close != null ? (row.close * k).toFixed(2) : "—"}</td>
      <td class="num">${row.pctb != null ? row.pctb.toFixed(2) : "—"}</td>
      <td class="num">${cell(row.fwd5)}</td>
      <td class="num">${cell(row.fwd10)}</td>
      <td class="num">${cell(row.fwd20)}</td>
    </tr>`).join("");
  $("#bb-table-wrap").style.display = rows.length ? "block" : "none";

  hint.textContent = (r.summary || "") +
    " · 通道 = MA20 ± 2σ（滚动标准差 ddof=0）；信号与 regime 判定只用当日及之前数据" +
    "（无未来函数），前瞻收益与胜率仅用于回测评估；" +
    "高波动板块（如创业板）可将 K 上调至 2.2~2.5 减少宽幅震荡虚假触发";
}

/* ============================== 筹码分布 ============================== */

async function loadChips() {
  const box = $("#chip-cards"), hint = $("#chip-hint");
  try {
    const d = await api("/api/chips?code=" + state.stockCode);
    const st = d.stats;
    const diff = st ? (d.last_close / st.avg_cost - 1) : null;
    box.innerHTML = st ? `
      <div class="card"><div class="k">获利盘比例</div>
        <div class="v ${st.profit_ratio >= 0.5 ? "up" : "down"}">${(st.profit_ratio * 100).toFixed(1)}%</div>
        <div class="s">成本低于现价 ${d.last_close.toFixed(2)} 的筹码</div></div>
      <div class="card"><div class="k">平均成本</div>
        <div class="v">${st.avg_cost.toFixed(2)}</div>
        <div class="s ${pctCls(diff)}">现价 ${fmtPct(diff)}</div></div>
      <div class="card"><div class="k">90% 成本区间</div>
        <div class="v" style="font-size:14px">${st.range90[0].toFixed(2)} ~ ${st.range90[1].toFixed(2)}</div>
        <div class="s">集中度 ${(st.conc90 * 100).toFixed(1)}%（中位 ${st.median_cost.toFixed(2)}）</div></div>
      <div class="card"><div class="k">70% 成本区间</div>
        <div class="v" style="font-size:14px">${st.range70[0].toFixed(2)} ~ ${st.range70[1].toFixed(2)}</div>
        <div class="s">${d.n_days} 个交易日</div></div>
    ` : "";
    renderChipChart(d);
    hint.textContent = (d.turnover_estimated
      ? "换手率缺失，已按 成交量/当前流通股本 估算（历史近似）；"
      : "红色 = 获利筹码（成本低于现价），蓝色 = 套牢筹码；覆盖全部可用历史；")
      + (d.price_unit === "qfq" ? "价格单位：前复权（最新价=真实价）" : "价格单位：后复权");
  } catch (err) {
    box.innerHTML = "";
    hint.textContent = "筹码分布不可用: " + err.message;
  }
}

function renderChipChart(d) {
  const c = mkChart("chart-chips");
  const n = d.bins.length;
  // 价格从下到上递增 → category 轴反转
  const cats = d.bins.map((b) => +b.toFixed(3));
  const rev = [...cats].reverse();
  const data = [...d.pct].reverse().map((p, k) => {
    const price = d.bins[n - 1 - k];
    return {
      value: p,
      itemStyle: { color: price <= d.last_close ? "rgba(239,75,87,.85)" : "rgba(79,142,247,.85)" },
    };
  });
  let markIdx = 0;
  for (let i = n - 1; i >= 0; i--) if (Math.abs(cats[i] - d.last_close) <= Math.abs(cats[markIdx] - d.last_close)) markIdx = i;
  c.setOption({
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      backgroundColor: "rgba(22,27,36,.95)", borderColor: "#2a3342",
      textStyle: { color: "#dce3ee", fontSize: 12 },
      formatter: (items) => {
        const i = items[0].dataIndex;
        const price = d.bins[n - 1 - i];
        return `价格 <b>${price.toFixed(2)}</b><br>筹码占比 <b>${items[0].value.toFixed(2)}%</b>`;
      },
    },
    grid: { left: 64, right: 24, top: 26, bottom: 44 },
    xAxis: { type: "value", ...AXIS, axisLabel: { ...AXIS.axisLabel, formatter: "{value}%" }, name: "筹码占比", nameTextStyle: { color: "#8b98ab" } },
    yAxis: {
      type: "category", data: rev, ...AXIS,
      axisLabel: { ...AXIS.axisLabel, interval: Math.max(1, Math.floor(n / 16)) - 1, formatter: (v) => (+v).toFixed(2) },
    },
    series: [{
      type: "bar", data, barWidth: "92%",
      markLine: {
        silent: true, symbol: "none",
        lineStyle: { color: "#e8e8e8", type: "dashed", width: 1.2 },
        label: { color: "#e8e8e8", formatter: `现价 ${d.last_close.toFixed(2)}`, position: "insideEndTop" },
        // 数值型 category 轴会把数值当作类目索引，必须使用倒序后的索引。
        data: [{ yAxis: n - 1 - markIdx }],
      },
    }],
  });
}

/* ============================== 季度 / 年报 ============================== */

function periodLabel(dateStr, mode) {
  const y = dateStr.slice(0, 4), m = dateStr.slice(5, 7);
  if (mode === "year" || m === "12") return y;
  const q = { "03": 1, "06": 2, "09": 3 }[m] || 1;
  return y.slice(2) + "Q" + q;
}

function fundRows(mode) {
  const d = state.fund;
  if (!d) return [];
  let rows = d.periods.map((p, i) => i);
  if (mode === "year") rows = rows.filter((i) => d.periods[i].slice(5, 7) === "12");
  const keep = mode === "year" ? 10 : 8;
  return rows.slice(-keep);
}

function renderFund(mode) {
  const d = state.fund;
  const hint = $("#fund-hint");
  const tbody = $("#fund-table tbody");
  if (!d) {
    tbody.innerHTML = "";
    return;
  }
  const idxs = fundRows(mode);
  const label = (i) => periodLabel(d.periods[i], mode);
  const yi = (v) => (v == null ? null : v >= 1e12 ? v / 1e12 : v / 1e8);
  const unit = (v) => (v == null ? "—" : v >= 1e12 ? (v / 1e12).toFixed(2) + " 万亿" : (v / 1e8).toFixed(1) + " 亿");
  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const pctCell = (v) => `<td class="num ${pctCls(v)}">${pct(v)}</td>`;

  // 图表：营收/净利润柱 + 净利同比线
  const c = mkChart("chart-fund");
  c.setOption({
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(22,27,36,.95)", borderColor: "#2a3342",
      textStyle: { color: "#dce3ee", fontSize: 12 },
      formatter: (items) => {
        let s = `<b>${items[0].axisValue}</b>`;
        for (const it of items) {
          const v = it.value;
          const txt = v == null ? "—" : it.seriesName === "净利同比" ? (v * 100).toFixed(1) + "%" : v.toFixed(2) + " 亿";
          s += `<br>${it.marker}${it.seriesName} <b>${txt}</b>`;
        }
        return s;
      },
    },
    legend: { top: 0, data: ["营业总收入", "净利润", "净利同比"], textStyle: { color: "#8b98ab", fontSize: 11 } },
    grid: { left: 64, right: 56, top: 34, bottom: 34 },
    xAxis: { type: "category", data: idxs.map(label), ...AXIS },
    yAxis: [
      { type: "value", ...AXIS, name: "亿元", nameTextStyle: { color: "#8b98ab" } },
      { type: "value", ...AXIS, splitLine: { show: false }, axisLabel: { ...AXIS.axisLabel, formatter: (v) => (v * 100).toFixed(0) + "%" } },
    ],
    series: [
      { name: "营业总收入", type: "bar", data: idxs.map((i) => yi(d.rev[i])), barWidth: "34%", itemStyle: { color: "rgba(79,142,247,.85)" } },
      { name: "净利润", type: "bar", data: idxs.map((i) => yi(d.net_profit[i])), barWidth: "34%", itemStyle: { color: "rgba(240,160,60,.9)" } },
      { name: "净利同比", type: "line", yAxisIndex: 1, data: idxs.map((i) => d.net_profit_yoy[i]), symbol: "circle", symbolSize: 5, lineStyle: { width: 1.6, color: "#2ebd85" }, itemStyle: { color: "#2ebd85" } },
    ],
  }, true);

  // 表格
  tbody.innerHTML = idxs.map((i) => `
    <tr>
      <td>${periodLabel(d.periods[i], mode === "year" ? "year" : "full")}</td>
      <td class="num">${unit(d.rev[i])}</td>
      ${pctCell(d.rev_yoy[i])}
      <td class="num">${unit(d.net_profit[i])}</td>
      ${pctCell(d.net_profit_yoy[i])}
      <td class="num">${d.eps[i] != null ? d.eps[i].toFixed(2) : "—"}</td>
      <td class="num">${pct(d.roe[i])}</td>
      <td class="num">${pct(d.gross_margin[i])}</td>
      <td class="num">${pct(d.net_margin[i])}</td>
      <td class="num">${pct(d.debt_ratio[i])}</td>
    </tr>`).reverse().join("");
  hint.textContent = `${d.n} 个报告期（${d.periods[0]} ~ ${d.periods[d.periods.length - 1]}），金额单位随数值自适应`;
}

async function loadFund() {
  const hint = $("#fund-hint"), tbody = $("#fund-table tbody");
  state.fund = null;
  tbody.innerHTML = "";
  const c = mkChart("chart-fund");
  c.clear();
  c.showLoading("default", { text: "首次打开在线抓取（约 1~2 秒）…", color: "#4f8ef7", textColor: "#8b98ab", maskColor: "rgba(14,17,23,.6)" });
  try {
    state.fund = await api("/api/fundamental?code=" + state.stockCode);
    renderFund(state.fundMode || "quarter");
  } catch (err) {
    hint.textContent = "财报数据不可用: " + err.message + "（可稍后重试，或运行 python -m analyzer fundamental --codes " + state.stockCode + " 手动抓取）";
  } finally {
    c.hideLoading();
  }
}

/* ============================== 个股分析·AI 综合分析 ============================== */

const AI_HINT_DEFAULT = "点击「生成 AI 分析」调用云端大模型（推理约需 3~8 分钟，生成期间可切换股票稍后回来查看）；报告缓存 6 小时，模型数据或新闻变化后自动重新生成";

function resetAIPanel() {
  const code = state.stockCode;
  state.ai = null;
  $("#ai-report").innerHTML = "";
  $("#ai-meta").textContent = "";
  $("#ai-context").textContent = "";
  $("#ai-status").textContent = "";
  $("#ai-hint").textContent = AI_HINT_DEFAULT;
  $("#ai-generate").style.display = "";
  $("#ai-refresh").style.display = "none";
  setAIButtons(false);   // 修复：上一只股票生成中切走时，按钮残留禁用导致新股票无法生成
  // 前端已有该股报告（本次会话内生成过）：直接展示，不重新请求
  const cached = state.aiByCode[code];
  if (cached) { renderAIFor(code, cached); return; }
  // 后端可能有该股的在途/已完成任务（如刷新页面后）：查询恢复
  if (code) syncAIFromServer(code);
}

// 轻量 Markdown 渲染：先整体 HTML 转义，再处理标题/加粗/行内代码/列表/分隔线（防 XSS）
function mdToHtml(md) {
  const inline = (s) => escHtml(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(偏多|看涨|超卖)/g, '<span class="ai-up">$1</span>')
    .replace(/(偏空|看跌|超买)/g, '<span class="ai-down">$1</span>')
    .replace(/(中性)/g, '<span class="ai-flat">$1</span>');
  let html = "", inList = false;
  let para = [];
  const flushPara = () => {
    if (para.length) { html += `<p>${para.map(inline).join("<br>")}</p>`; para = []; }
  };
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const raw of String(md).split("\n")) {
    const line = raw.trim();
    if (!line) { flushPara(); closeList(); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      html += `<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`;
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) { flushPara(); closeList(); html += "<hr>"; continue; }
    const li = line.match(/^[-*]\s+(.*)$/) || line.match(/^\d+[.、]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(li[1])}</li>`;
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara(); closeList();
  return html;
}

function renderAI(d) {
  $("#ai-report").innerHTML = mdToHtml(d.report || "");
  $("#ai-generate").style.display = "none";
  $("#ai-refresh").style.display = "";
  const tokens = d.usage && d.usage.total_tokens ? ` · tokens ${d.usage.total_tokens}` : "";
  $("#ai-meta").textContent =
    `${d.model || ""} · 生成于 ${d.created || ""}${d.cached ? "（缓存）" : ""}${tokens}`;
  $("#ai-hint").textContent = "";
  const cm = d.context_meta || {};
  const parts = [];
  if (cm.as_of) parts.push(`数据截至 ${cm.as_of}`);
  if (cm.models_ok != null) parts.push(`输入模型 ${cm.models_ok} 个`);
  if (cm.models_failed) parts.push(`失败 ${cm.models_failed} 个`);
  if (cm.news_count != null) parts.push(`新闻 ${cm.news_count} 条`);
  const warns = (d.warnings || []).map(escHtml).join("；");
  $("#ai-context").innerHTML = escHtml(parts.join(" · ")) + (warns ? `<br>数据警告：${warns}` : "");
}

/* ---------- 异步任务轮询（POST /api/ai_analysis -> GET /api/ai_analysis/status） ---------- */

const AI_POLL_MS = 3000;        // 轮询间隔（与后端下发的 poll_ms 一致）
const AI_POLL_MAX_FAILS = 5;    // 状态查询连续失败上限（约 15s），超过则放弃轮询

/** 生成/重新生成按钮的禁用态（属于当前展示的股票；切股时由 resetAIPanel 重置）。 */
function setAIButtons(disabled) {
  $("#ai-generate").disabled = disabled;
  $("#ai-refresh").disabled = disabled;
}

/** 渲染某股报告：仅当用户仍停在该股（切走了就只留 state.aiByCode 缓存，切回时展示）。 */
function renderAIFor(code, d) {
  if (state.stockCode !== code) return;
  state.ai = d;
  renderAI(d);
}

async function loadAI(refresh) {
  const code = state.stockCode;
  if (!code || state.aiPolling[code]) return;   // 该股已有在途请求/任务，不重复提交
  state.aiPolling[code] = "submitting";         // POST 返回前先占位防连点
  setAIButtons(true);
  $("#ai-status").textContent = "";
  $("#ai-hint").textContent = refresh ? "正在重新生成（忽略本地缓存）…" : "正在提交 AI 分析请求…";
  try {
    const d = await apiPost("/api/ai_analysis", { code, refresh: !!refresh });
    if (d && d.job_id) {
      // 异步任务：轮询直至完成（等待期间可切换股票，结果不丢）
      state.aiPolling[code] = d.job_id;
      pollAIJob(code, d.job_id);
    } else {
      // 缓存命中：同步返回完整报告（后端秒回路径）
      delete state.aiPolling[code];
      state.aiByCode[code] = d;
      if (state.stockCode === code) setAIButtons(false);   // 已切走则不动新股票的按钮态
      renderAIFor(code, d);
    }
  } catch (err) {
    delete state.aiPolling[code];
    if (state.stockCode === code) {
      setAIButtons(false);
      $("#ai-hint").textContent = "AI 分析不可用: " + err.message;
    }
    toast(err.message);
  }
}

/** 轮询任务直至 done/failed。等待期间切走不丢结果：完成后写入前端缓存并按需渲染。 */
async function pollAIJob(code, jobId) {
  const startedAt = Date.now();
  let fails = 0;
  while (state.aiPolling[code] === jobId) {
    await sleep(AI_POLL_MS);
    if (state.aiPolling[code] !== jobId) return;   // 已被中止/覆盖
    let st = null;
    try {
      st = await api(`/api/ai_analysis/status?job_id=${encodeURIComponent(jobId)}`);
      fails = 0;
    } catch (err) {
      if (++fails >= AI_POLL_MAX_FAILS) {
        delete state.aiPolling[code];
        if (state.stockCode === code) {
          setAIButtons(false);
          $("#ai-hint").textContent = "AI 分析状态查询失败: " + err.message + "（可重新生成）";
        }
        return;
      }
      continue;   // 瞬时网络抖动：下一轮继续
    }
    if (st.status === "done" && st.report) {
      delete state.aiPolling[code];
      state.aiByCode[code] = st.report;
      if (state.stockCode === code) setAIButtons(false);
      else toast(`${code} 的 AI 分析已生成，切回即可查看`);
      renderAIFor(code, st.report);
      return;
    }
    if (st.status === "failed") {
      delete state.aiPolling[code];
      if (state.stockCode === code) {
        setAIButtons(false);
        $("#ai-hint").textContent = "AI 分析生成失败: " + (st.error || "未知错误") + "（可重新生成）";
      }
      return;
    }
    // queued / running：更新等待时长提示（仅当前股）
    if (state.stockCode === code) {
      const sec = Math.floor((Date.now() - startedAt) / 1000);
      const label = st.status === "queued" ? "排队中" : "大模型推理中";
      $("#ai-hint").textContent =
        `AI 分析生成中（${label}，约需 3~8 分钟，可切换股票稍后回来查看）· 已等待 ${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
    }
  }
}

/** 进入某股时向后端对账：任务在途则恢复轮询、已完成则直接展示（页面刷新场景）。 */
async function syncAIFromServer(code) {
  try {
    const st = await api(`/api/ai_analysis/status?code=${encodeURIComponent(code)}`);
    if (state.stockCode !== code || !st) return;   // 查询期间又切走了
    if (st.status === "done" && st.report) {
      state.aiByCode[code] = st.report;
      renderAIFor(code, st.report);
    } else if (st.status === "failed") {
      $("#ai-hint").textContent = "上次 AI 分析生成失败: " + (st.error || "未知错误") + "（可重新生成）";
    } else if (st.status === "queued" || st.status === "running") {
      setAIButtons(true);
      if (!state.aiPolling[code]) {   // 本地无轮询（如刷新页面后）才拉起，避免重复轮询
        state.aiPolling[code] = st.job_id;
        pollAIJob(code, st.job_id);
      }
    }
    // status === "none"：保持默认提示；用户点击按钮时 POST 会命中缓存或新建任务
  } catch (_) { /* 对账失败静默：面板保持默认提示 */ }
}

/* ============================== 排行榜 ============================== */

function loadRank() {
  if (state.rankView === "board") loadBoardRank();
  else loadRankStocks();
}

const RANK_N_OPTS = {
  stock: `<option value="20">20 只</option><option value="50" selected>50 只</option><option value="100">100 只</option>`,
  board: `<option value="10">10 个</option><option value="30" selected>30 个</option><option value="80">80 个</option><option value="9999">全部</option>`,
};

function setRankView(view) {
  if (view === state.rankView) return;
  state.rankView = view;
  $("#rank-view").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  const stock = view === "stock";
  $("#rank-stock-wrap").style.display = stock ? "" : "none";
  $("#rank-board-wrap").style.display = stock ? "none" : "";
  // 个股视图: 市场 + 板块筛选；板块视图: 板块类型（行业/概念）
  $("#rank-market-label").style.display = stock ? "" : "none";
  $("#rank-market").style.display = stock ? "" : "none";
  $("#rank-board-label").style.display = stock ? "" : "none";
  $("#rank-board").style.display = stock ? "" : "none";
  $("#board-type-label").style.display = stock ? "none" : "";
  $("#board-type").style.display = stock ? "none" : "";
  const sel = $("#rank-n");
  const prev = sel.value;
  sel.innerHTML = RANK_N_OPTS[view];
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  loadRank();
}

function setBoardType(t) {
  if (t === state.boardType) return;
  state.boardType = t;
  $("#board-type").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.btype === t));
  loadRank();
}

async function loadRankStocks() {
  const start = periodStart(state.marketDays);
  const metric = $("#rank-metric").value;
  const asc = $("#rank-dir").value;
  const n = $("#rank-n").value;
  const tbody = $("#rank-table tbody");
  tbody.innerHTML = `<tr><td colspan="9" class="hint" style="padding:24px">加载中…</td></tr>`;
  try {
    const board = state.rankBoard ? `&board=${encodeURIComponent(state.rankBoard)}` : "";
    const market = state.rankMarket ? `&market=${state.rankMarket}` : "";
    const r = await api(`/api/rank?start=${start}&end=${META.last_trade_date || META.date_max}&metric=${metric}&n=${n}&asc=${asc}${board}${market}`);
    tbody.innerHTML = r.rows
      .map((row, i) => {
        const fmt = {
          ret: `<td class="num ${pctCls(row.ret)} col-metric">${fmtPct(row.ret)}</td>`,
          vol: `<td class="num up col-metric">${row.vol != null ? (row.vol * 100).toFixed(1) + "%" : "—"}</td>`,
          maxdd: `<td class="num down col-metric">${row.maxdd != null ? (row.maxdd * 100).toFixed(1) + "%" : "—"}</td>`,
          turnover: `<td class="num col-metric">${row.turnover != null ? row.turnover.toFixed(2) + "%" : "—"}</td>`,
        }[metric];
        const boardCell = row.board_name
          ? `<td><span class="board-tag" data-board="${row.board_code}" title="点击按 ${row.board_name} 筛选">${row.board_name}</span></td>`
          : `<td style="color:var(--text-dim)">—</td>`;
        return `<tr data-code="${row.code}">
          <td>${i + 1}</td><td style="color:var(--text-dim)">${row.code}</td><td>${row.name}</td>${boardCell}
          <td class="num ${pctCls(row.ret)}">${fmtPct(row.ret)}</td>
          <td class="num">${row.vol != null ? (row.vol * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${row.maxdd != null ? (row.maxdd * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${row.turnover != null ? row.turnover.toFixed(2) + "%" : "—"}</td>
          <td class="num" style="color:var(--text-dim)">${fmtMv(row.total_mv)}</td>
        </tr>`;
      })
      .join("");
    tbody.querySelectorAll("tr[data-code]").forEach((tr) =>
      tr.addEventListener("click", () => {
        switchTab("stock");
        pickStock(tr.dataset.code);
      })
    );
    // 「板块」列：点击只筛选，不跳转个股
    tbody.querySelectorAll(".board-tag").forEach((sp) =>
      sp.addEventListener("click", (e) => {
        e.stopPropagation();
        state.rankBoard = sp.dataset.board;
        const sel = $("#rank-board");
        if ([...sel.options].some((o) => o.value === state.rankBoard)) sel.value = state.rankBoard;
        loadRankStocks();
      })
    );
    if (!r.rows.length) {
      const filt = [state.rankMarket && "该市场", state.rankBoard && "该板块"].filter(Boolean).join(" + ");
      tbody.innerHTML = `<tr><td colspan="9" class="hint" style="padding:24px">该区间无满足样本量的股票${filt ? `（${filt}筛选下）` : ""}</td></tr>`;
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="hint" style="padding:24px">${err.message}</td></tr>`;
  }
}

async function loadBoards() {
  if (state.boardsLoaded) return;
  state.boardsLoaded = true;
  const sel = $("#rank-board");
  try {
    const r = await api("/api/boards");
    state.conceptAvailable = !!r.concept_available;
    if (!r.industry_available) {
      sel.innerHTML = `<option value="">无板块数据</option>`;
      sel.disabled = true;
      sel.title = "未找到 data/board_members.parquet，请先运行: python -m downloader board";
      return;
    }
    const opts = (bs) => bs
      .map((b) => `<option value="${b.board_code}">${b.board_name}（${b.n_stocks}）</option>`)
      .join("");
    let html = `<option value="">全部板块</option>`;
    html += `<optgroup label="行业板块">${opts(r.boards)}</optgroup>`;
    if (state.conceptAvailable) {
      const rc = await api("/api/boards?type=concept");
      html += `<optgroup label="概念板块">${opts(rc.boards)}</optgroup>`;
    }
    sel.innerHTML = html;
    sel.value = state.rankBoard && [...sel.options].some((o) => o.value === state.rankBoard)
      ? state.rankBoard : "";
  } catch (err) {
    sel.innerHTML = `<option value="">板块数据加载失败</option>`;
    sel.disabled = true;
  }
  // 板块视图的行业/概念切换按钮：无概念数据时隐藏「概念」
  $("#board-type").querySelectorAll("button").forEach((b) =>
    b.style.display = (b.dataset.btype === "concept" && !state.conceptAvailable) ? "none" : "");
}

async function loadBoardRank() {
  const start = periodStart(state.marketDays);
  const metric = $("#rank-metric").value;
  const asc = $("#rank-dir").value;
  const n = $("#rank-n").value;
  const tbody = $("#board-table tbody");
  tbody.innerHTML = `<tr><td colspan="10" class="hint" style="padding:24px">加载中…</td></tr>`;
  try {
    const r = await api(`/api/board_rank?start=${start}&end=${META.last_trade_date || META.date_max}&metric=${metric}&n=${n}&asc=${asc}&type=${state.boardType}`);
    tbody.innerHTML = r.rows
      .map((row, i) => {
        const fmt = {
          ret: `<td class="num ${pctCls(row.ret)} col-metric">${fmtPct(row.ret)}</td>`,
          vol: `<td class="num up col-metric">${row.vol != null ? (row.vol * 100).toFixed(1) + "%" : "—"}</td>`,
          maxdd: `<td class="num down col-metric">${row.maxdd != null ? (row.maxdd * 100).toFixed(1) + "%" : "—"}</td>`,
          turnover: `<td class="num col-metric">${row.turnover != null ? row.turnover.toFixed(2) + "%" : "—"}</td>`,
        }[metric];
        return `<tr data-board="${row.board_code}">
          <td>${i + 1}</td><td>${row.board_name}</td>
          <td class="num ${pctCls(row.ret)}">${fmtPct(row.ret)}</td>
          <td class="num">${row.vol != null ? (row.vol * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${row.maxdd != null ? (row.maxdd * 100).toFixed(1) + "%" : "—"}</td>
          <td class="num">${row.turnover != null ? row.turnover.toFixed(2) + "%" : "—"}</td>
          <td class="num up">${row.adv}</td>
          <td class="num down">${row.dec}</td>
          <td class="num">${row.n_stocks}</td>
          <td class="num" style="color:var(--text-dim)">${fmtMv(row.total_mv)}</td>
        </tr>`;
      })
      .join("");
    // 点击板块行 -> 切回个股视图并按该板块筛选
    tbody.querySelectorAll("tr[data-board]").forEach((tr) =>
      tr.addEventListener("click", () => {
        state.rankBoard = tr.dataset.board;
        loadBoards().then(() => {
          const sel = $("#rank-board");
          if ([...sel.options].some((o) => o.value === state.rankBoard)) sel.value = state.rankBoard;
        });
        setRankView("stock");
      })
    );
    if (!r.rows.length) tbody.innerHTML = `<tr><td colspan="10" class="hint" style="padding:24px">该区间无满足样本量的板块</td></tr>`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10" class="hint" style="padding:24px">${err.message}</td></tr>`;
  }
}

/* ============================== 个股分析·资金流向与公告 ============================== */

function renderFlow(d) {
  const items = d.items || [];
  const hint = $("#flow-hint"), status = $("#flow-status");
  if (!items.length) { hint.textContent = "无资金流数据"; return; }
  if (d.stale && status) status.textContent = "（网络异常，显示旧缓存）";
  const c = mkChart("chart-flow");
  const days = items.map((x) => x.day);
  const main = items.map((x) => (x.main_net != null ? x.main_net / 1e8 : null));
  const big = items.map((x) => (x.big_net != null ? x.big_net / 1e8 : null));
  c.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", axisPointer: { type: "cross" },
      valueFormatter: (v) => (v == null ? "-" : v.toFixed(2) + " 亿") },
    legend: { data: ["主力净流入(亿)", "超大单(亿)", "收盘价"], textStyle: { color: "#8b98ab" }, top: 0 },
    grid: { left: 56, right: 56, top: 30, bottom: 24 },
    xAxis: { type: "category", data: days, axisLabel: { color: "#8b98ab" } },
    yAxis: [
      { type: "value", name: "亿元", axisLabel: { color: "#8b98ab" }, splitLine: { lineStyle: { color: "rgba(139,152,171,.12)" } } },
      { type: "value", name: "价", axisLabel: { color: "#8b98ab" }, splitLine: { show: false } },
    ],
    series: [
      { name: "主力净流入(亿)", type: "bar", data: main,
        itemStyle: { color: (p) => (p.value >= 0 ? UP : DOWN) } },
      { name: "超大单(亿)", type: "bar", data: big, barGap: "-100%", opacity: .45,
        itemStyle: { color: (p) => (p.value >= 0 ? UP : DOWN) } },
      { name: "收盘价", type: "line", yAxisIndex: 1, data: items.map((x) => x.close),
        showSymbol: false, lineStyle: { color: ACCENT, width: 1.2 } },
    ],
  });
  hint.textContent = `近 ${items.length} 日：最近一日主力净流入 ${
    main[main.length - 1] != null ? main[main.length - 1].toFixed(2) + " 亿" : "—"}`;
}

async function loadFlow() {
  if (!state.stockCode) return;
  const code = state.stockCode;
  try {
    const d = await api("/api/flow?code=" + code);
    if (state.stockCode === code) renderFlow(d);
  } catch (err) {
    const hint = $("#flow-hint");
    if (hint) hint.textContent = "资金流数据不可用: " + err.message;
  }
}

function renderAnnounce(d) {
  const tbody = $("#ann-table tbody");
  const status = $("#ann-status");
  tbody.innerHTML = "";
  const items = d.items || [];
  if (d.stale && status) status.textContent = "（网络异常，显示旧缓存）";
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="hint" style="padding:18px">近 180 日无公告记录</td></tr>`;
    return;
  }
  for (const it of items.slice(0, 50)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="white-space:nowrap">${escHtml(it.day)}</td>
      <td>${escHtml(it.title)}</td>
      <td>${it.url ? `<a href="${escHtml(it.url)}" target="_blank" rel="noopener">原文 ↗</a>` : "—"}</td>`;
    tbody.appendChild(tr);
  }
}

async function loadAnnounce() {
  if (!state.stockCode) return;
  const code = state.stockCode;
  try {
    const d = await api("/api/announce?code=" + code);
    if (state.stockCode === code) renderAnnounce(d);
  } catch (err) {
    const tbody = $("#ann-table tbody");
    if (tbody) tbody.innerHTML =
      `<tr><td colspan="3" class="hint" style="padding:18px">公告数据不可用: ${escHtml(err.message)}</td></tr>`;
  }
}

/* ============================== 交易日记（记忆与复盘） ============================== */

const JOURNAL = { code: "", loaded: false };

function journalKindBadge(kind) {
  const map = { trade: "交易", model: "信号", system: "系统" };
  return `<span class="tag-hfq">${map[kind] || kind}</span>`;
}

async function loadJournal(code) {
  JOURNAL.code = code || "";
  const q = code ? "?code=" + encodeURIComponent(code) : "";
  try {
    const d = await api("/api/journal" + q);
    renderJournalEvents(d.events || []);
    renderJournalNotes(d.notes || []);
    renderJournalReviews(d.reviews || []);
    $("#journal-count").textContent =
      `事件 ${d.events.length} · 笔记 ${d.notes.length} · 复盘 ${d.reviews.length}`;
  } catch (err) {
    toast("日记加载失败: " + err.message);
  }
}

function renderJournalEvents(evs) {
  const tbody = $("#journal-events-table tbody");
  tbody.innerHTML = "";
  if (!evs.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="hint" style="padding:18px">暂无事件——登记交易或等待盘后扫描自动写入</td></tr>`;
    return;
  }
  for (const e of evs.slice(0, 100)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="white-space:nowrap">${escHtml(e.day || "")}</td>
      <td style="white-space:nowrap;color:var(--text-dim)">${escHtml((e.time || "").slice(11))}</td>
      <td>${journalKindBadge(e.kind)}</td>
      <td style="white-space:nowrap">${e.code ? `<a href="#stock" data-code="${escHtml(e.code)}" class="j-code">${escHtml(e.code)}</a>` : "—"}</td>
      <td>${escHtml(e.text || "")}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("a.j-code").forEach((a) =>
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      loadStock(a.dataset.code, { history: true });
    }));
}

function renderJournalNotes(notes) {
  const tbody = $("#journal-notes-table tbody");
  tbody.innerHTML = "";
  if (!notes.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="hint" style="padding:18px">还没有笔记——把观察/计划/教训记下来，LLM 复盘会引用它们</td></tr>`;
    return;
  }
  for (const n of notes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="white-space:nowrap">${escHtml(n.day || "")}</td>
      <td style="white-space:nowrap">${escHtml(n.code || "—")}</td>
      <td>${escHtml(n.text || "")}${n.edited ? ' <span class="hint">（已编辑）</span>' : ""}</td>
      <td><button class="mini-btn j-note-del" data-id="${escHtml(n.id)}">删除</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".j-note-del").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await apiPost("/api/journal", { action: "note_delete", id: b.dataset.id });
        loadJournal(JOURNAL.code);
      } catch (err) { toast(err.message); }
    }));
}

function renderJournalReviews(reviews) {
  const box = $("#journal-reviews");
  box.innerHTML = "";
  if (!reviews.length) {
    box.innerHTML = `<div class="hint" style="padding:14px">暂无复盘——盘后更新会为每只自选股自动生成规则版复盘，也可在上方手动生成</div>`;
    return;
  }
  for (const r of reviews.slice(0, 20)) {
    const div = document.createElement("div");
    div.className = "chart-box";
    const lines = (r.lines || []).map((l) =>
      `<li>[${escHtml(l.type)}] ${escHtml(l.text)}</li>`).join("");
    const isLlm = r.version === "llm";
    div.innerHTML = `
      <div class="chart-title">
        ${escHtml(r.day)} · ${escHtml(r.code)} · ${isLlm ? "LLM 复盘" : "规则复盘"}
        <span class="hint">${escHtml(r.time || "")}</span>
      </div>
      ${isLlm ? mdToHtml(r.summary || "") : `<p>${escHtml(r.summary || "")}</p><ul class="j-lines">${lines}</ul>`}`;
    box.appendChild(div);
  }
}

async function journalReviewAction(action) {
  const code = ($("#journal-review-code").value || "").trim().toUpperCase();
  if (!code) { toast("请输入股票代码，如 300687.SZ"); return; }
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(code)) { toast("代码格式应为 600519.SH"); return; }
  const btn = $(action === "review_generate" ? "#journal-review-gen" : "#journal-review-llm");
  btn.disabled = true;
  try {
    const d = await apiPost("/api/journal", { action, code });
    if (d.ok) { toast("复盘已生成"); loadJournal(JOURNAL.code); }
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

function bindJournal() {
  $("#journal-refresh").addEventListener("click", () => loadJournal(JOURNAL.code));
  $("#journal-note-add").addEventListener("click", async () => {
    const code = ($("#journal-note-code").value || "").trim().toUpperCase() || null;
    const text = ($("#journal-note-text").value || "").trim();
    if (!text) { toast("笔记内容不能为空"); return; }
    if (code && !/^\d{6}\.(SH|SZ|BJ)$/.test(code)) { toast("代码格式应为 600519.SH"); return; }
    try {
      await apiPost("/api/journal", { action: "note_add", code, text });
      $("#journal-note-text").value = "";
      loadJournal(JOURNAL.code);
    } catch (err) { toast(err.message); }
  });
  $("#journal-review-gen").addEventListener("click", () => journalReviewAction("review_generate"));
  $("#journal-review-llm").addEventListener("click", () => journalReviewAction("review_llm"));
  $("#journal-search").addEventListener("change", () => {
    const v = ($("#journal-search").value || "").trim().toUpperCase();
    loadJournal(v && /^\d{6}/.test(v) ? v : "");
  });
}

/* ============================== 框架 ============================== */

function switchTab(name, opts = {}) {
  // 「排行榜」已并入「市场」页：#rank 深链接映射到 market 并滚动到排行榜区
  if (name === "rank") {
    const anchor = !!opts.fromHash;
    name = "market";
    if (anchor) setTimeout(() => {
      const el = $("#rank-anchor");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 60);
  }
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  Object.values(charts).forEach((c) => c.resize());
  if (name === "market") { loadBoards(); loadRank(); }
  if (name === "stock") loadWatchlist();
  if (name === "trades") loadTradesPage();
  if (name === "journal") loadJournal(JOURNAL.code);
  if (name === "inbox") loadInbox();
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
}

function bindTabs() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
}

/* ---------- 个股分析·模型子标签 ---------- */

// 每个子标签页包含的图表 id（切换时 resize，修复隐藏容器 0×0 初始化）
const SUB_CHARTS = {
  cmp: ["chart-cmp"],
  mf: ["chart-mf"],
  gm: ["chart-gm"],
  cd: ["chart-cd-1", "chart-cd-2", "chart-cd-3", "chart-chips"],
  bb: ["chart-bb-1", "chart-bb-2", "chart-bb-3"],
  fund: ["chart-fund"],
};

function switchSub(name) {
  $$(".subtab").forEach((t) => t.classList.toggle("active", t.dataset.sub === name));
  $$(".subpanel").forEach((p) => p.classList.toggle("active", p.dataset.subpanel === name));
  state.subTab = name;
  (SUB_CHARTS[name] || []).forEach((id) => {
    if (charts[id]) charts[id].resize();
  });
}

function bindSubTabs() {
  $$(".subtab").forEach((t) => t.addEventListener("click", () => switchSub(t.dataset.sub)));
}

// 模型子标签的"今日信号"角标: dir>0 红点(看多) / dir<0 绿点(看空) / 0 清除
function setSubDot(sub, dir) {
  const btn = document.querySelector(`.subtab[data-sub="${sub}"]`);
  if (!btn) return;
  let dot = btn.querySelector(".dot");
  if (!dot) {
    dot = document.createElement("span");
    dot.className = "dot";
    btn.appendChild(dot);
  }
  dot.className = "dot" + (dir > 0 ? " up" : dir < 0 ? " down" : "");
}

function bindPeriodGroup(id, key, cb) {
  const g = document.getElementById(id);
  g.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      g.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state[key] = parseInt(b.dataset.days, 10);
      cb(state[key]);
    })
  );
}

window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));

/* ============================== 盘后更新 & 消息中心 ============================== */

const MSG = {
  timer: null,
  since: null,          // 游标：仅拉取比它新的消息（用于弹新提醒）
  firstLoad: true,
  unread: 0,
  drawerOpen: false,
};

// 更新状态 chip：idle/running/done/error
function renderUpdateChip(st) {
  const dot = $("#update-dot"), txt = $("#update-text");
  if (!dot || !txt) return;
  if (!st) { dot.className = "update-dot idle"; txt.textContent = "盘后更新"; return; }
  if (st.running) {
    dot.className = "update-dot running";
    txt.textContent = st.stage_label ? `更新中·${st.stage_label}` : "更新中…";
  } else if (st.errors && st.errors.length) {
    dot.className = "update-dot error";
    txt.textContent = `更新有 ${st.errors.length} 个错误`;
  } else if (st.last_ok) {
    dot.className = "update-dot done";
    txt.textContent = st.day ? `已更新 ${st.day}` : "已更新";
  } else {
    dot.className = "update-dot idle";
    txt.textContent = `每日 ${st.trigger || "15:30"}`;
  }
}

function renderUpdateProgress(st) {
  const box = $("#msg-progress");
  if (!box) return;
  if (!st || !st.running) { box.style.display = "none"; return; }
  box.style.display = "block";
  const idx = st.stage_index || 0, n = st.n_stages || 9;
  $("#msg-prog-stage").textContent = st.stage_label
    ? `第 ${idx}/${n} 步：${st.stage_label}` : "准备中…";
  const pct = Math.round((idx / n) * 100);
  $("#msg-prog-pct").textContent = `${pct}%`;
  $("#msg-bar-fill").style.width = `${pct}%`;
}

function msgLevelLabel(lv) {
  return { alert: "⚡ 触发", warn: "⚠ 提醒", ok: "✓ 完成", info: "· 进度" }[lv] || "·";
}

const SRC_LABEL = { model: "模型信号", trade: "交易提醒", system: "系统" };
const SRC_ICON = { model: "⚡", trade: "🔔", system: "🛠" };
function srcLabel(s) { return SRC_LABEL[s] || "系统"; }

function renderMsgList(messages) {
  const box = $("#msg-list");
  if (!box) return;
  if (!messages.length) { box.innerHTML = `<div class="msg-empty">暂无消息</div>`; return; }
  box.innerHTML = messages.map((m) => {
    const cls = `msg-item ${m.level || "info"}${m.read ? "" : " unread"}`;
    const t = (m.time || "").replace("T", " ").slice(5, 16);
    const src = m.source && m.source !== "system"
      ? `<span class="inbox-src ${m.source}">${escHtml(srcLabel(m.source))}</span> ` : "";
    return `<div class="${cls}">
      <div>${src}<span class="msg-lvl">${msgLevelLabel(m.level)}</span> ${escHtml(m.text)}</div>
      <div class="msg-time">${t}${m.code ? " · " + escHtml(m.code) : ""}</div>
    </div>`;
  }).join("");
}

function setUnreadBadge(n) {
  MSG.unread = n;
  const b = $("#msg-badge");
  if (b) {
    if (n > 0) { b.textContent = n > 99 ? "99+" : String(n); b.style.display = "block"; }
    else b.style.display = "none";
  }
  const tb = $("#inbox-tab-badge");
  if (tb) {
    if (n > 0) { tb.textContent = n > 99 ? "99+" : String(n); tb.style.display = "inline-block"; }
    else tb.style.display = "none";
  }
}

async function pollStatus() {
  try {
    const st = await api("/api/update/status");
    renderUpdateChip(st);
    if (MSG.drawerOpen) renderUpdateProgress(st);
  } catch (_) { renderUpdateChip(null); }
}

async function pollMessages() {
  // 首次全量（含已读态）；后续用游标只拉新消息（游标缺失时跳过增量轮询）
  if (MSG.firstLoad) {
    let data;
    try { data = await api("/api/notifications?limit=100"); } catch (_) { return; }
    MSG.firstLoad = false;
    MSG.since = data.cursor || null;
    renderMsgList(data.messages || []);
    setUnreadBadge(data.unread || 0);
    if (MSG.drawerOpen) pollStatus();
    return;
  }
  if (!MSG.since) return;
  let data;
  try { data = await api(`/api/notifications?since=${encodeURIComponent(MSG.since)}`); }
  catch (_) { return; }
  const fresh = data.messages || [];
  if (!fresh.length) return;
  MSG.since = data.cursor || MSG.since;
  if (MSG.drawerOpen) {
    // 抽屉打开：整列刷新（read 态以服务端为准）
    try {
      const all = await api("/api/notifications?limit=100");
      renderMsgList(all.messages || []);
      setUnreadBadge(all.unread || 0);
    } catch (_) { setUnreadBadge(MSG.unread + fresh.length); }
  } else {
    setUnreadBadge(MSG.unread + fresh.length);
  }
  const top = fresh.find((m) => m.level === "alert") || fresh.find((m) => m.level === "warn");
  if (top) toast(top.text);
  pollStatus();   // 新消息通常伴随状态变化，刷新 chip
}

function startMsgCenter() {
  pollStatus();
  pollMessages();
  MSG.timer = setInterval(() => { pollMessages(); if (MSG.drawerOpen) pollStatus(); }, 15000);
  $("#msg-btn")?.addEventListener("click", () => toggleDrawer(true));
  $("#msg-close")?.addEventListener("click", () => toggleDrawer(false));
  $("#msg-read")?.addEventListener("click", markAllRead);
  $("#msg-open-inbox")?.addEventListener("click", () => { toggleDrawer(false); switchTab("inbox"); });
  $("#update-chip")?.addEventListener("click", manualUpdate);
  bindInbox();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && MSG.drawerOpen) toggleDrawer(false);
  });
}

function toggleDrawer(open) {
  MSG.drawerOpen = open;
  const d = $("#msg-drawer");
  if (!d) return;
  d.classList.toggle("open", open);
  if (open) {
    api("/api/notifications?limit=100").then((data) => {
      renderMsgList(data.messages || []);
      setUnreadBadge(data.unread || 0);
      MSG.since = data.cursor || MSG.since;
    }).catch(() => {});
    pollStatus();
  }
}

async function markAllRead() {
  try {
    await apiPost("/api/notifications", { action: "read", all: true });
    setUnreadBadge(0);
    const data = await api("/api/notifications?limit=100");
    renderMsgList(data.messages || []);
  } catch (err) { toast(err.message); }
}

let updating = false;
async function manualUpdate() {
  if (updating) return;
  if (!confirm("立即触发一轮盘后数据更新？\n（自选股优先刷新，随后全市场日线/指数/板块/数据集，耗时数分钟）")) return;
  updating = true;
  try {
    const r = await apiPost("/api/update/run", {});
    if (r.ok) { toast("已启动盘后更新，进度见消息中心"); toggleDrawer(true); }
    else toast(r.error || "未能启动更新");
  } catch (err) { toast(err.message); }
  finally { updating = false; }
}

/* ============================== 站内信 · 整页收件箱 ============================== */

const INBOX = { source: null, level: null, unreadOnly: false, loaded: false, bound: false };

function inboxQuery(extra) {
  const q = new URLSearchParams(extra || {});
  q.set("limit", "200");
  if (INBOX.source) q.set("source", INBOX.source);
  if (INBOX.level) q.set("level", INBOX.level);
  if (INBOX.unreadOnly) q.set("unread", "1");
  return q.toString();
}

async function loadInbox() {
  const box = $("#inbox-list");
  if (!box) return;
  let data;
  try { data = await api("/api/notifications?" + inboxQuery()); }
  catch (err) { box.innerHTML = `<div class="msg-empty">加载失败：${escHtml(err.message)}</div>`; return; }
  MSG.since = data.cursor || MSG.since;
  renderInboxRail(data.counts);
  renderInboxList(data.messages || []);
  const c = data.counts || {};
  const sub = $("#inbox-sub");
  if (sub) sub.textContent = `${data.messages.length} 条 · 未读 ${c.unread || 0} / 共 ${c.total || 0}`;
  setUnreadBadge(c.unread || 0);
  INBOX.loaded = true;
}

function renderInboxRail(counts) {
  const rail = $("#inbox-rail");
  if (!rail) return;
  const bs = (counts && counts.by_source) || {};
  const item = (key, label, node, icon) => {
    const active = (INBOX.source || null) === key;
    const total = node ? node.total : (counts ? counts.total : 0);
    const unread = node ? node.unread : (counts ? counts.unread : 0);
    const dataAttr = key ? `data-src="${key}"` : `data-src=""`;
    return `<div class="inbox-cat${active ? " active" : ""}" ${dataAttr}>
      <span class="inbox-cat-ic">${icon}</span>
      <span class="inbox-cat-name">${escHtml(label)}</span>
      <span class="inbox-cat-n">${total || 0}</span>
      ${unread ? `<span class="inbox-cat-u">${unread}</span>` : ""}
    </div>`;
  };
  rail.innerHTML = `<div class="inbox-rail-title">来源</div>`
    + item(null, "全部收件箱", null, "📬")
    + `<div class="inbox-sep"></div>`
    + ["model", "trade", "system"].map((s) => {
        const n = bs[s] || { total: 0, unread: 0, label: srcLabel(s) };
        return item(s, n.label || srcLabel(s), n, SRC_ICON[s]);
      }).join("");
  rail.querySelectorAll("[data-src]").forEach((el) => el.addEventListener("click", () => {
    INBOX.source = el.dataset.src || null;
    loadInbox();
  }));
}

function renderInboxList(messages) {
  const box = $("#inbox-list");
  if (!box) return;
  if (!messages.length) { box.innerHTML = `<div class="msg-empty">该分类暂无消息</div>`; return; }
  box.innerHTML = messages.map((m) => {
    const t = (m.time || "").replace("T", " ").slice(0, 16);
    const src = `<span class="inbox-src ${m.source || "system"}">${escHtml(srcLabel(m.source))}</span>`;
    const jump = m.code
      ? `<span class="inbox-jump" data-code="${escHtml(m.code)}">${escHtml(m.code)} ↗ 看个股</span>` : "";
    const plugin = m.plugin ? `<span>· ${escHtml(m.plugin)}</span>` : "";
    return `<div class="inbox-item ${m.level || "info"}${m.read ? "" : " unread"}" data-id="${m.id}">
      <div class="inbox-item-top">${src}<span class="msg-lvl">${msgLevelLabel(m.level)}</span>
        <span class="inbox-item-text">${escHtml(m.text)}</span></div>
      <div class="inbox-item-meta"><span>${t}</span>${plugin}${jump}</div>
    </div>`;
  }).join("");
  // 点击股票跳转
  box.querySelectorAll(".inbox-jump[data-code]").forEach((el) => el.addEventListener("click", (e) => {
    e.stopPropagation();
    const it = el.closest(".inbox-item");
    if (it && it.classList.contains("unread")) markInboxRead([it.dataset.id], true);
    const code = el.dataset.code;
    switchTab("stock"); pickStock(code);
  }));
  // 点击整条 = 标记已读
  box.querySelectorAll(".inbox-item.unread").forEach((el) => el.addEventListener("click", () => {
    markInboxRead([el.dataset.id], true);
    el.classList.remove("unread");
  }));
}

async function markInboxRead(ids, quiet) {
  try { await apiPost("/api/notifications", { action: "read", ids }); }
  catch (_) { return; }
  if (!quiet) loadInbox();
  pollMessages();
}

function bindInbox() {
  if (INBOX.bound) return;
  INBOX.bound = true;
  $("#inbox-unread")?.addEventListener("change", (e) => { INBOX.unreadOnly = e.target.checked; loadInbox(); });
  $("#inbox-level")?.addEventListener("change", (e) => { INBOX.level = e.target.value || null; loadInbox(); });
  $("#inbox-refresh")?.addEventListener("click", loadInbox);
  $("#inbox-read")?.addEventListener("click", async () => {
    try {
      const body = { action: "read", all: true };
      if (INBOX.source) body.source = INBOX.source;
      await apiPost("/api/notifications", body);
      loadInbox(); pollMessages();
    } catch (err) { toast(err.message); }
  });
  $("#inbox-clear")?.addEventListener("click", async () => {
    const scope = INBOX.source ? `「${srcLabel(INBOX.source)}」` : "全部";
    if (!confirm(`清空${scope}的历史站内信？此操作不可撤销。`)) return;
    try {
      const body = { action: "clear" };
      if (INBOX.source) body.source = INBOX.source;
      const r = await apiPost("/api/notifications", body);
      toast(`已清空 ${r.removed || 0} 条`);
      loadInbox(); pollMessages();
    } catch (err) { toast(err.message); }
  });
}

/* ============================== 启动 ============================== */

// 启动入口：先查登录状态（多用户数据隔离）——未登录停在登录浮层；
// 登录/注册成功后整页刷新回到这里，已登录则直接 bootstrap()。
(async function init() {
  try {
    const st = await api("/api/auth/status");
    if (st.auth) {
      AUTH.enabled = true;
      bindAuth();   // 绑定登录浮层与退出按钮（每页加载一次；已登录也需退出按钮）
      if (!st.user) {
        $("#auth-hint").style.display = st.n_users === 0 ? "block" : "none";
        showAuth();
        return;
      }
      setUser(st.user);
    }
  } catch (_) { /* status 查询失败按已登录处理，后续 API 会自行拦截 401 */ }
  await bootstrap();
})().catch((err) => {
  document.body.innerHTML = `<div style="padding:40px;color:#ef4b57">加载失败：${err.message}</div>`;
});

async function bootstrap() {
  try {
    META = await api("/api/meta");
  } catch (err) {
    document.body.innerHTML = `<div style="padding:40px;color:#ef4b57">加载失败：${err.message}</div>`;
    return;
  }
  $("#meta-line").textContent =
    `${META.n_stocks} 只股票 · ${META.date_min} ~ ${META.date_max} · 数据截至 ${META.last_trade_date}` +
    (META.date_max !== META.last_trade_date
      ? `（${META.date_max} 部分更新：${META.n_stocks_last_day} 只）` : "");
  $("#foot-range").textContent = `${META.date_min} ~ ${META.date_max}`;

  bindTabs();
  bindSubTabs();
  bindMinuteModal();
  renderChips();
  bindSearch();
  startMsgCenter();   // 盘后更新状态 + 消息中心轮询提醒
  // 市场页：区间（图表与排行榜共用）→ 同时刷新图表与排行榜
  bindPeriodGroup("market-period", "marketDays", () => { loadMarket(); loadRank(); });
  bindPeriodGroup("stock-period", "stockDays", () => { updateStockCards(); syncChartPeriods(); });
  $("#rank-metric").addEventListener("change", loadRank);
  $("#rank-dir").addEventListener("change", loadRank);
  $("#rank-n").addEventListener("change", loadRank);
  $("#rank-view").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => setRankView(b.dataset.view)));
  $("#board-type").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => setBoardType(b.dataset.btype)));
  $("#rank-board").addEventListener("change", () => {
    state.rankBoard = $("#rank-board").value;
    loadRankStocks();
  });
  // 市场板块筛选（个股视图）：选项来自 meta.markets
  (function initMarketFilter() {
    const sel = $("#rank-market");
    const ms = META.markets || [];
    if (!ms.length) {
      sel.style.display = "none";
      $("#rank-market-label").style.display = "none";
      return;
    }
    sel.innerHTML = `<option value="">全部市场</option>` + ms
      .map((m) => `<option value="${m.key}">${m.name}（${m.n_stocks}）</option>`).join("");
    sel.addEventListener("change", () => {
      state.rankMarket = sel.value;
      loadRankStocks();
    });
  })();
  $("#cmp-index").addEventListener("change", loadCompare);
  $("#ai-generate").addEventListener("click", () => loadAI(false));
  $("#ai-refresh").addEventListener("click", () => loadAI(true));
  resetAIPanel();
  bindJournal();

  // K线周期切换（时/日/周/月）：初始按钮态来自上次选择（localStorage）
  $$("#kline-freq button").forEach((b) =>
    b.classList.toggle("active", b.dataset.freq === state.klineFreq));
  $("#kline-freq").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => setKlineFreq(b.dataset.freq)));
  // K 线下方交易明细表：点击展开/隐藏（记住选择）
  $("#trade-detail-toggle").addEventListener("click", () => {
    state.tradeDetailOpen = !state.tradeDetailOpen;
    lsSet("tradeDetailOpen", state.tradeDetailOpen);
    renderTradeTable();
  });
  // 自选股列表折叠（记住选择）
  applyWlCollapsed();
  $("#wl-collapse").addEventListener("click", () => {
    state.wlCollapsed = !state.wlCollapsed;
    lsSet("wlCollapsed", state.wlCollapsed);
    applyWlCollapsed();
  });

  $("#fund-mode").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      $("#fund-mode").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.fundMode = b.dataset.mode;
      if (state.fund) renderFund(state.fundMode);
    })
  );
  $("#wl-clear").addEventListener("click", async () => {
    if (!state.watchlist.length || !confirm(`确认清空全部 ${state.watchlist.length} 只自选股？`)) return;
    try {
      const d = await apiPost("/api/watchlist", { action: "clear" });
      state.watchlist = [];
      renderWatchlist();
      const sb = $("#star-btn");
      if (sb) { sb.classList.remove("on"); sb.innerHTML = "☆ 加自选"; }
      toast("自选已清空");
    } catch (err) { toast(err.message); }
  });
  $("#wl-news-refresh").addEventListener("click", () => {
    if (state.wlNewsCode) loadWlNews(state.wlNewsCode, true);
  });

  // 买卖记录：K 线面板「管理交易」跳转 + 交易记录页 CRUD
  $("#trade-btn").addEventListener("click", () => goToTradesPage(state.stockCode));
  $("#trades-add-btn").addEventListener("click", () => openTradesForm("add"));
  $("#trades-batch-btn").addEventListener("click", openTradesBatch);
  $("#tbf-close").addEventListener("click", hideTradesBatch);
  $("#tbf-parse").addEventListener("click", parseBatchPreview);
  $("#tbf-import").addEventListener("click", importBatch);
  bindStockSuggest("#tbf-stock", "#tbf-suggest");
  $("#tbf-text").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideTradesBatch();
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") parseBatchPreview();
  });
  $("#ntf-cancel").addEventListener("click", hideTradesForm);
  $("#ntf-close").addEventListener("click", hideTradesForm);
  $("#ntf-save").addEventListener("click", saveTradesForm);
  $("#ntf-side").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", (e) => { e.preventDefault(); setTradesFormSide(b.dataset.side); })
  );
  $("#trades-scope").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      $("#trades-scope").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.tradesPage.scope = b.dataset.scope;
      renderTradesPage();
    })
  );
  let tradesSearchTimer = null;
  $("#trades-search").addEventListener("input", (e) => {
    clearTimeout(tradesSearchTimer);
    tradesSearchTimer = setTimeout(() => {
      state.tradesPage.q = e.target.value;
      renderTradesPage();
    }, 200);
  });
  // 费率估算设置：改动即时重算并持久化到 localStorage
  $("#fee-comm").value = state.feeCfg.comm;
  $("#fee-min").value = state.feeCfg.min;
  const onFeeCfg = () => {
    state.feeCfg.comm = Math.max(0, parseFloat($("#fee-comm").value) || 0);
    state.feeCfg.min = Math.max(0, parseFloat($("#fee-min").value) || 0);
    try { localStorage.setItem("feeCfg", JSON.stringify(state.feeCfg)); } catch (_) {}
    renderTradesPage();
  };
  $("#fee-comm").addEventListener("input", onFeeCfg);
  $("#fee-min").addEventListener("input", onFeeCfg);
  bindTradesFormStock();
  $("#trades-form-box").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideTradesForm();
  });

  // 市场概览与自选列表并行拉取（两者互不依赖）：刷新首屏串行 RTT 减半
  const wlReady = loadWatchlist();   // 自选列表决定个股分析页默认展示的股票
  await loadMarket();
  loadBoards();
  loadRank();   // 市场页（含排行榜）为默认页，启动即加载
  await wlReady;
  // 个股分析页默认显示第一只自选股；无自选或加载失败时回退到预设第一只
  const firstCode = state.watchlist.length ? state.watchlist[0].code : null;
  if (!firstCode || !(await pickStock(firstCode))) {
    pickStock(PRESET_STOCKS[0][0]).catch(() => {});
  }

  // 深链接：#stock / #market / #rank / #trades / #inbox 直达（#rank 滚动到排行榜区）
  const initial = location.hash.replace("#", "");
  if (["stock", "market", "rank", "trades", "inbox"].includes(initial))
    switchTab(initial, { fromHash: initial === "rank" });
}
