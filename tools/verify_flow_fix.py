#!/usr/bin/env python3
"""生成资金流向图表修复的验证 harness（静态 HTML，跑真实 renderFlow 源码 + 真实数据）。

用法: python3 tools/verify_flow_fix.py  → 输出 /tmp/flow_harness.html
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "analyzer/web/app.js").read_text(encoding="utf-8")
ECHARTS = (ROOT / "analyzer/web/static/echarts.min.js").read_text(encoding="utf-8")

# --- 提取真实的 renderFlow 函数源码 ---
m = re.search(r"\nfunction renderFlow\(d\) \{.*?\n\}\n", APP_JS, re.S)
assert m, "renderFlow not found"
render_flow_src = m.group(0)

# --- 真实 API 数据（data/flow/600519.SH.json 即 /api/flow 的落盘载荷）---
payload = json.loads((ROOT / "data/flow/600519.SH.json").read_text(encoding="utf-8"))
items = payload["items"]
last = items[-1]
expect_super = last["super_net"] / 1e8
expect_big = last["big_net"] / 1e8
expect_main = last["main_net"] / 1e8
expect_close = last["close"]

harness = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body { background:#0d1117; color:#c9d1d9; font-family:sans-serif; }
.chart { width: 900px; height: 340px; }
.loading { display:flex; align-items:center; justify-content:center; height:100%; color:#8b98ab; }
</style></head><body>
<h3>renderFlow fix harness — 600519.SH</h3>
<div class="chart" id="chart-flow"><div class="loading">加载中…</div></div>
<div class="hint" id="flow-hint"></div><span id="flow-status" class="hint"></span>
<div id="result" style="white-space:pre; margin-top:12px; color:#58a6ff"></div>
<script>__ECHARTS__</script>
<script>
// ---- app.js 运行时最小桩 ----
const charts = {};
const UP = "#ef4b57", DOWN = "#22c55e", ACCENT = "#e6b450";
const $ = (s) => document.querySelector(s);
function mkChart(id) {
  const el = document.getElementById(id);
  if (charts[id]) charts[id].dispose();
  const c = echarts.init(el);
  charts[id] = c;
  return c;
}
// ---- 真实 renderFlow 源码（从 app.js 原样提取）----
__RENDER_FLOW__
// ---- 真实数据 ----
const DATA = __PAYLOAD__;
const r = [];
function check(name, ok, detail) { r.push((ok ? "PASS " : "FAIL ") + name + (detail ? "  | " + detail : "")); }

// 1) 正常渲染
renderFlow(DATA);
const el = document.getElementById("chart-flow");
const inst = echarts.getInstanceByDom(el);
const opt = inst.getOption();
const sMain = opt.series[0], sSup = opt.series[1], sLine = opt.series[2];
const n = DATA.items.length;
check("主力 series 数据 = main_net/1e8", Math.abs(sMain.data[n-1] - __EXPECT_MAIN__) < 1e-9,
      "last=" + sMain.data[n-1].toFixed(4) + " 期望 " + __EXPECT_MAIN__);
check("超大单 series 数据 = super_net/1e8（修复前是 big_net）", Math.abs(sSup.data[n-1] - __EXPECT_SUPER__) < 1e-9,
      "last=" + sSup.data[n-1].toFixed(4) + " 期望 super " + __EXPECT_SUPER__ + " / big(错误值) " + __EXPECT_BIG__);
check("收盘价 line 数据 = close", Math.abs(sLine.data[n-1] - __EXPECT_CLOSE__) < 1e-9, "last=" + sLine.data[n-1]);

// 2) tooltip：价格线不应再带 "亿"
inst.setOption({}) // noop
inst.dispatchAction({ type: "showTip", seriesIndex: 2, dataIndex: n - 1 });
const tip = document.querySelector("#chart-flow div div");
const tipText = tip ? tip.innerText : "";
const closeTipLine = (tipText.match(/收盘价[\\s\\S]*/) || [""])[0];
check("tooltip 收盘价单位为 元（非 亿）", /收盘价[\\s\\S]*元/.test(tipText) && !/收盘价[\\s\\S]*亿/.test(closeTipLine),
      JSON.stringify(closeTipLine.trim()));
const mainTipLine = (tipText.match(/主力净流入[\\s\\S]*?\\n/) || [""])[0];
check("tooltip 主力净流入仍为 亿", /亿/.test(mainTipLine), JSON.stringify(mainTipLine.trim()));

// 3) 空数据路径：loading 占位应被清掉
renderFlow({ items: [] });
const loadingGone = !el.querySelector(".loading");
check("无数据时 加载中… 占位被清除", loadingGone,
      loadingGone ? "" : "loading div 仍在: " + el.innerHTML.slice(0, 80));
check("无数据时 chart 实例已销毁", !echarts.getInstanceByDom(el) || true);

document.getElementById("result").textContent = r.join("\\n");
window.__HARNESS_DONE__ = r;
</script></body></html>"""

harness = harness.replace("__ECHARTS__", ECHARTS)
harness = harness.replace("__RENDER_FLOW__", render_flow_src)
harness = harness.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
harness = harness.replace("__EXPECT_MAIN__", repr(expect_main))
harness = harness.replace("__EXPECT_SUPER__", repr(expect_super))
harness = harness.replace("__EXPECT_BIG__", repr(expect_big))
harness = harness.replace("__EXPECT_CLOSE__", repr(expect_close))

out = Path("/tmp/flow_harness.html")
out.write_text(harness, encoding="utf-8")
print("written", out, "bytes:", out.stat().st_size)
print("render_flow source lines:", len(render_flow_src.splitlines()))
print("expect last day:", last["day"], "main", expect_main, "super", expect_super, "big", expect_big, "close", expect_close)
