/* learn-coach 学习系统 · 前端（零依赖：仪表盘 + AI教练聊天） */
"use strict";

let DATA = null;   // /api/state 结果（含 config）
let WS = null;     // 当前工作区
let TAB = "overview";
let CARD_FILTER = "due";   // due | all
let SEL_NODE = null;

const MOD_STATUS = {
  "passed": ["⭐ 已过关", "green"],
  "learning": ["学习中", "amber"],
  "quiz-failed": ["再试一次", "red"],
  "exempt-pending": ["免修待验", "blue"],
  "not-started": ["未开始", "gray"],
};
const NODE_LABEL = { mastered: "已掌握", suspected: "疑似掌握", unlearned: "未学" };
const NODE_COLOR = { mastered: "#16a34a", suspected: "#d97706", unlearned: "#94a3b8" };
const EDGE_LABEL = { pre: "前置（→ 先学）", rel: "关联（原理相通）", conf: "易混（干扰项原料）" };
const ACT_LABEL = {
  update_progress: "更新进度", add_flashcards: "新增卡片", add_mistake: "错题入账",
  write_note: "写教学笔记", append_profile: "档案记录",
  write_kmap: "生成知识图谱", write_plan: "生成学习计划", advance_digest: "推进消化进度",
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const today = () => new Date().toISOString().slice(0, 10);
const badge = (text, color) => `<span class="badge ${color || "gray"}">${esc(text)}</span>`;

/* 文件读取为 base64（分块，避免大文件栈溢出） */
function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

function readFilesB64(files) {
  return Promise.all([...files].map(f => new Promise((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok({ name: f.name, b64: bufToB64(r.result) });
    r.onerror = no;
    r.readAsArrayBuffer(f);
  })));
}

/* ---------- 迷你 Markdown 渲染（转义优先，防注入） ---------- */

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*]+)\*/g, "<i>$1</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function md(src) {
  const lines = String(src || "").split(/\r?\n/);
  let out = "", i = 0;
  const flushTable = () => {
    const rows = [];
    while (i < lines.length && /^\s*\|/.test(lines[i])) {
      const cells = lines[i].trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
      if (!cells.every(c => /^-{2,}$/.test(c) || c === "---" || c === "")) rows.push(cells);
      i++;
    }
    if (!rows.length) return "";
    let t = "<table><thead><tr>" + rows[0].map(c => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
    for (let r = 1; r < rows.length; r++) {
      t += "<tr>" + rows[r].map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
    }
    return t + "</tbody></table>";
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      i++; let code = "";
      while (i < lines.length && !/^\s*```/.test(lines[i])) code += lines[i++] + "\n";
      i++;
      out += `<pre><code>${esc(code)}</code></pre>`;
      continue;
    }
    if (/^\s*\|/.test(line)) { out += flushTable(); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out += `<h${h[1].length + 0}>${inline(h[2])}</h${h[1].length}>`; i++; continue; }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.、]\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      let items = "";
      while (i < lines.length && (/^\s*[-*]\s+/.test(lines[i]) || /^\s*\d+[.、]\s+/.test(lines[i]))) {
        items += "<li>" + inline(lines[i].replace(/^\s*(?:[-*]|\d+[.、])\s+/, "")) + "</li>";
        i++;
      }
      out += ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      let q = "";
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { q += inline(lines[i].replace(/^\s*>\s?/, "")) + " "; i++; }
      out += `<blockquote>${q}</blockquote>`;
      continue;
    }
    if (!line.trim()) { i++; continue; }
    let para = "";
    while (i < lines.length && lines[i].trim() && !/^\s*(?:#{1,4}\s|[-*]\s|\d+[.、]\s|>|\||```)/.test(lines[i])) {
      para += inline(lines[i]) + " "; i++;
    }
    out += `<p>${para}</p>`;
  }
  return out;
}

/* ---------- 数据加载 ---------- */

async function load(keepName) {
  const view = $("#view");
  try {
    const res = await fetch("/api/state?ts=" + Date.now(), { cache: "no-store" });
    DATA = await res.json();
  } catch (e) {
    view.innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "，请确认 server.py 正在运行。</div>";
    return;
  }
  const sel = $("#ws-select");
  sel.innerHTML = DATA.workspaces.map(w => `<option>${esc(w.name)}</option>`).join("");
  if (!DATA.workspaces.length) {
    WS = null;
    view.innerHTML = `<div class="empty">还没有学习工作区。<br>
      <button id="empty-new" class="ctl on" style="margin-top:12px">＋ 新主题</button>
      <div class="hint" style="margin-top:8px">新建主题时可选「从文件夹导入」恢复之前的备份</div></div>`;
    $("#empty-new").addEventListener("click", () => setTab("new"));
    return;
  }
  const name = keepName && DATA.workspaces.some(w => w.name === keepName) ? keepName : DATA.workspaces[0].name;
  sel.value = name;
  WS = DATA.workspaces.find(w => w.name === name);
  $("#updated").textContent = "数据更新于 " + new Date().toLocaleString("zh-CN");
  render();
}

async function refreshStateQuiet() {
  try {
    const res = await fetch("/api/state?ts=" + Date.now(), { cache: "no-store" });
    DATA = await res.json();
    const cur = DATA.workspaces.find(w => w.name === (WS && WS.name));
    if (cur) WS = cur;
    $("#ws-select").innerHTML = DATA.workspaces.map(w => `<option>${esc(w.name)}</option>`).join("");
    $("#ws-select").value = WS.name;
    $("#updated").textContent = "数据更新于 " + new Date().toLocaleString("zh-CN");
    if (TAB === "coach") renderRail();
  } catch (e) { /* 静默 */ }
}

/* ---------- 通用 ---------- */

function modBadge(st) {
  const [label, color] = MOD_STATUS[st] || [st || "未知", "gray"];
  return badge(label, color);
}

function daysTo(deadline) {
  if (!deadline) return null;
  return Math.ceil((new Date(deadline + "T23:59:59") - new Date()) / 86400000);
}

function setTab(name) {
  TAB = name;
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  render();
}

/* ---------- 总览 ---------- */

function renderOverview() {
  const p = WS.progress || {};
  const plan = WS.plan || { meta: {}, modules: [] };
  const mods = p.modules || [];
  const passed = mods.filter(m => m.status === "passed").length;
  const exempt = mods.filter(m => m.status === "exempt-pending").length;
  const dueCards = (WS.flashcards || []).filter(c => c.due && c.due <= today()).length;
  const dl = daysTo(plan.meta.deadline);
  const diag = p.diagnostic && p.diagnostic.score != null ? Math.round(p.diagnostic.score * 100) + "%" : "—";

  const strip = mods.map(m => {
    const pm = (plan.modules || []).find(x => x.id === m.id) || {};
    return `<div class="mod-chip s-${esc(m.status)}" title="${esc(m.name)} · 预计 ${pm.hours || "?"} h">
      <div class="mid">${esc(m.id)} ${modBadge(m.status)}</div>
      <div class="mname">${esc(m.name)}</div></div>`;
  }).join("");

  const prof = (WS.profile && WS.profile.table) || {};
  const info = ["学习主题", "学段 / 领域 / 发起人", "目标等级", "截止时间", "每日投入"]
    .filter(k => prof[k]).map(k => `<tr><th>${esc(k)}</th><td>${esc(prof[k])}</td></tr>`).join("");

  $("#view").innerHTML = `
  <div class="card hero-cta" style="margin-bottom:14px">
    <div class="cta-text">
      <div style="font-size:17px;font-weight:700">📌 今日任务</div>
      <div class="what">${esc(p.next_action || "对教练说「继续学习」即可开始")}</div>
    </div>
    <button class="go" id="go-coach">开始学习 →</button>
  </div>
  <div class="hero">
    <div class="card stat"><h3>当前阶段</h3>
      <div class="value">阶段 ${esc(p.phase ?? "—")}</div>
      <div class="sub">${esc(p.phase_label || "")}</div></div>
    <div class="card stat"><h3>模块进度</h3>
      <div class="value">${passed} / ${mods.length}</div>
      <div class="sub">已过关；另有 ${exempt} 个免修待验</div>
      <div class="progress-track" style="margin-top:8px">
        <div class="progress-fill" style="width:${mods.length ? (passed / mods.length * 100) : 0}%"></div>
      </div></div>
    <div class="card stat${dueCards ? " warn" : ""}"><h3>今日到期卡片</h3>
      <div class="value">${dueCards} 张</div>
      <div class="sub">共 ${(WS.flashcards || []).length} 张在途</div></div>
    <div class="card stat${dl != null && dl <= 7 ? " warn" : ""}"><h3>距截止</h3>
      <div class="value">${dl == null ? "—" : dl + " 天"}</div>
      <div class="sub">${esc(plan.meta.deadline || "计划未设截止")}</div></div>
  </div>
  <div class="grid cols-2">
    <div class="card next-action"><h3>下一步（恢复断点用）</h3>
      <div class="act">${esc(p.next_action || "—")}</div>
      <div class="hint" style="margin-top:8px">对教练说「继续学习」即从此处继续；也可直接到「AI教练」页学习。</div></div>
    <div class="card"><h3>摸底诊断</h3>
      <div style="font-size:20px;font-weight:700">${diag}</div>
      <div class="hint">起点分数；错题 ${(WS.mistakes || []).length} 条已入账</div></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>模块状态条</h3>
    <div class="module-strip">${strip || "暂无模块数据"}</div></div>
  <div class="card" style="margin-top:14px"><h3>学习档案摘要</h3>
    <table class="tbl">${info || "<tr><td>暂无 profile 数据</td></tr>"}</table></div>`;
  const goBtn = $("#go-coach");
  if (goBtn) goBtn.onclick = () => setTab("coach");
}

/* ---------- 模块进度 ---------- */

function renderModules() {
  const p = WS.progress || {};
  const plan = WS.plan || { modules: [] };
  const mods = (p.modules || []).map(m => {
    const pm = plan.modules.find(x => x.id === m.id) || {};
    const qh = (m.quiz_history || []).map(h =>
      `${esc(h.date)}：<b>${Math.round((h.score ?? 0) * 100)}%</b>`).join("、") || "—";
    return `<tr>
      <td><b>${esc(m.id)}</b></td>
      <td>${esc(m.name)}${pm.goals ? `<div class="hint">目标：${esc(pm.goals)}</div>` : ""}</td>
      <td>${pm.hours ? pm.hours + " h" : "—"}</td>
      <td>${(pm.deps || []).join("、") || "—"}</td>
      <td>${modBadge(m.status)}</td>
      <td>${qh}</td>
      <td>${esc(pm.pass || "测验 ≥70%")}</td>
      <td>${m.evidence ? esc(m.evidence) : "—"}</td>
    </tr>`;
  }).join("");
  $("#view").innerHTML = `
  <div class="note">模块状态与测验成绩来自 progress.json，时长/依赖/目标来自 plan.md；过关线锁定，考后不放水。</div>
  <table class="tbl" style="margin-top:12px">
    <thead><tr><th>编号</th><th>模块</th><th>时长</th><th>依赖</th><th>状态</th><th>测验历史</th><th>过关标准</th><th>证据</th></tr></thead>
    <tbody>${mods || '<tr><td colspan="8">暂无数据</td></tr>'}</tbody>
  </table>`;
}

/* ---------- 知识图谱 ---------- */

function renderGraph() {
  const kmap = WS.kmap || { nodes: [], edges: [], internal: [] };
  if (!kmap.nodes.length) {
    $("#view").innerHTML = '<div class="empty">该工作区暂无知识图谱。<br>资料就绪后让 AI 教练运行消化管道（生成 knowledge-map.md）。</div>';
    return;
  }
  const modOrder = ((WS.progress || {}).modules || []).map(m => m.id);
  kmap.nodes.forEach(n => { if (!modOrder.includes(n.module)) modOrder.push(n.module); });
  const modName = Object.fromEntries((((WS.progress || {}).modules) || []).map(m => [m.id, m.name]));

  const colW = 150, padL = 60, padT = 70, gapY = 66;
  const cols = {};
  kmap.nodes.forEach(n => { (cols[n.module] = cols[n.module] || []).push(n); });
  const W = padL + modOrder.length * colW + 30;
  const H = Math.max(...Object.values(cols).map(a => a.length)) * gapY + padT + 30;

  const pos = {};
  modOrder.forEach((mid, ci) => (cols[mid] || []).forEach((n, ri) => {
    pos[n.id] = { x: padL + ci * colW + colW / 2, y: padT + ri * gapY };
  }));

  function edgePath(a, b) {
    const p1 = pos[a], p2 = pos[b];
    if (!p1 || !p2) return null;
    const dx = p2.x - p1.x, dy = p2.y - p1.y, L = Math.hypot(dx, dy) || 1;
    const ux = dx / L, uy = dy / L, r = 20;
    const x1 = p1.x + ux * r, y1 = p1.y + uy * r;
    const x2 = p2.x - ux * r, y2 = p2.y - uy * r;
    const mx = (x1 + x2) / 2 - uy * 16, my = (y1 + y2) / 2 + ux * 16;
    return `M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`;
  }

  const edges = kmap.edges.map(e => {
    const d = edgePath(e.from, e.to);
    return d ? `<path class="edge ${e.type}" d="${d}" ${e.type === "pre" ? 'marker-end="url(#arr)"' : ""}>
      <title>${esc(e.from + " → " + e.to + " · " + (EDGE_LABEL[e.type] || e.type))}</title></path>` : "";
  }).join("");

  const nodes = kmap.nodes.map(n => {
    const p = pos[n.id];
    return `<g class="node" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
      <circle r="16" fill="${NODE_COLOR[n.status] || NODE_COLOR.unlearned}" stroke="#0f172a22">
        <title>${esc(n.id + " · " + n.name)}&#10;${esc(NODE_LABEL[n.status] || n.status)}${n.note ? "（" + esc(n.note) + "）" : ""}</title>
      </circle>
      <text text-anchor="middle" dy="3.5">${esc(n.id.replace("K", ""))}</text>
    </g>`;
  }).join("");

  const colLabels = modOrder.map((mid, i) =>
    `<text class="col-label" x="${padL + i * colW + colW / 2}" y="34" text-anchor="middle">${esc(mid + " " + (modName[mid] || "").slice(0, 9))}</text>`).join("");

  const stat = { mastered: 0, suspected: 0, unlearned: 0 };
  kmap.nodes.forEach(n => stat[n.status] !== undefined && stat[n.status]++);

  $("#view").innerHTML = `
  <div class="legend">
    <span><i class="dot" style="background:${NODE_COLOR.mastered}"></i>已掌握 ${stat.mastered}</span>
    <span><i class="dot" style="background:${NODE_COLOR.suspected}"></i>疑似掌握 ${stat.suspected}</span>
    <span><i class="dot" style="background:${NODE_COLOR.unlearned}"></i>未学 ${stat.unlearned}</span>
    <span>—— 前置边</span><span style="color:#3b82f6">- - 关联边</span><span style="color:#ef4444">··· 易混边</span>
    <span>共 ${kmap.nodes.length} 节点 / ${kmap.edges.length} 边</span>
  </div>
  <div id="graph-wrap">
    <svg class="kmap" viewBox="0 0 ${W} ${H}">
      <defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L6,3 L0,6 z" fill="#64748b"/></marker></defs>
      ${edges}${colLabels}${nodes}
    </svg>
  </div>
  <div class="card node-panel" id="node-panel">
    <h3>节点详情</h3><div class="hint">点击图谱中的节点查看详情；悬停可看速览。</div>
  </div>`;
}

const kChips = (arr) => (arr && arr.length)
  ? arr.map(n => `<span class="kchip" data-kid="${esc(n.id)}" title="${esc(n.name)}">${esc(n.id)} ${esc(n.name.length > 14 ? n.name.slice(0, 14) + "…" : n.name)}</span>`).join(" ")
  : '<span class="hint">无</span>';

async function showNode(id) {
  const panel = $("#node-panel");
  if (!panel) return;
  SEL_NODE = id;
  document.querySelectorAll("svg.kmap .node").forEach(g =>
    g.classList.toggle("sel", g.dataset.id === id));
  panel.innerHTML = `<h3>${esc(id)}</h3><div class="hint">加载知识点内容…</div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const res = await fetch("/api/knode?ws=" + encodeURIComponent(WS.name) + "&id=" + encodeURIComponent(id));
    const d = await res.json();
    if (d.error) throw new Error(d.error);
    const stColor = d.status === "mastered" ? "green" : d.status === "suspected" ? "amber" : "gray";
    panel.innerHTML = `
      <h3>${esc(d.id)} · ${esc(d.name)}</h3>
      <table class="tbl">
        <tr><th style="width:90px">模块</th><td>${esc(d.module)}</td></tr>
        <tr><th>类型/Bloom</th><td>${esc(d.type)} / ${esc(d.bloom)}</td></tr>
        <tr><th>状态</th><td>${badge(NODE_LABEL[d.status] || d.status, stColor)} ${d.note ? '<span class="hint">' + esc(d.note) + "</span>" : ""}</td></tr>
        <tr><th>出处</th><td>${esc(d.source || "—")}</td></tr>
        <tr><th>前置（先学）</th><td>${kChips(d.pre_in)}</td></tr>
        <tr><th>后续（支撑）</th><td>${kChips(d.pre_out)}</td></tr>
        <tr><th>关联（相通）</th><td>${kChips(d.related)}</td></tr>
        <tr><th>易混（对照记）</th><td>${kChips(d.confusable)}</td></tr>
      </table>
      ${d.cards && d.cards.length ? `<h3 style="margin-top:12px">相关复习卡片（${d.cards.length}）</h3>` +
        d.cards.map(c => `<details class="mrow"><summary><b>${esc(c.id)}</b> ${esc(c.front)}</summary>
          <div class="mbody"><div class="line correct">${esc(c.back || "—")}</div></div></details>`).join("") : ""}
      ${d.mistakes && d.mistakes.length ? `<h3 style="margin-top:12px">相关错题（${d.mistakes.length}）</h3>` +
        d.mistakes.map(m => `<details class="mrow"><summary><b>${esc(m.id)}</b> ${esc(m.point)} ${badge(m.reason, "red")}</summary>
          <div class="mbody">
            <div class="line mine"><span class="k">我的答案：</span>${esc(m.my)}</div>
            <div class="line correct"><span class="k">正确答案：</span>${esc(m.correct)}</div>
          </div></details>`).join("") : ""}
      ${d.notes && d.notes.length ? `<h3 style="margin-top:12px">教学笔记摘录</h3>` +
        d.notes.map(n => `<div class="note" style="margin-bottom:8px"><b>${esc(n.file)}</b><div class="hint">${esc(n.excerpt)}</div></div>`).join("") : ""}
      ${d.rag && d.rag.length ? `<h3 style="margin-top:12px">资料片段</h3>` +
        d.rag.map(c => `<div class="note" style="margin-bottom:8px"><b>${esc(c.file)}</b><div class="hint">${esc(c.text.slice(0, 200))}</div></div>`).join("") : ""}
      <div class="hint" style="margin-top:8px">点击上方知识点标签可在图谱内跳转。</div>`;
  } catch (e) {
    panel.innerHTML = `<h3>${esc(id)}</h3><div class="note">⚠ ${esc(e.message)}</div>`;
  }
}

/* ---------- 卡片复习 ---------- */

function renderCards() {
  const cards = (WS.flashcards || []).slice().sort((a, b) => (a.due || "9").localeCompare(b.due || "9"));
  const t = today();
  const dueN = cards.filter(c => c.due && c.due <= t).length;
  if (CARD_FILTER === "due" && dueN === 0 && cards.length) CARD_FILTER = "all";
  const list = cards.filter(c => CARD_FILTER === "all" || (c.due && c.due <= t));
  const nextDue = cards.length ? cards[0].due : "";
  $("#view").innerHTML = `
  <div class="note">先作答，再点卡片翻面对照；答对/答错请在教练处确认，以更新复习间隔。${dueN ? "" : `今天没有到期卡片${nextDue ? "（最近到期 " + esc(nextDue) + "）" : ""}，以下为全部。`}</div>
  <div style="margin:12px 0" class="no-print">
    <button class="ctl ${CARD_FILTER === "due" ? "on" : ""}" id="f-due">今日到期（${dueN}）</button>
    <button class="ctl ${CARD_FILTER === "all" ? "on" : ""}" id="f-all">全部（${cards.length}）</button>
  </div>
  <div class="cardwall">${list.map(c => `
    <div class="fcard${c.due && c.due <= t ? " due" : ""}" data-id="${esc(c.id)}">
      <div class="inner">
        <div class="face front">
          <div class="q">${esc(c.front || c.id)}</div>
          <div class="meta"><span>${esc(c.id)} · ${esc(c.module || "")}</span><span>到期 ${esc(c.due || "—")}</span></div>
        </div>
        <div class="face back">
          <div class="a">${esc(c.back || "（背面见 flashcards.md）")}</div>
          <div class="meta"><span>间隔 ${esc(String(c.interval_days ?? c.interval ?? "—"))} 天</span><span>点击翻回</span></div>
        </div>
      </div>
    </div>`).join("") || '<div class="empty">没有符合条件的卡片</div>'}</div>`;
  $("#f-due").onclick = () => { CARD_FILTER = "due"; renderCards(); };
  $("#f-all").onclick = () => { CARD_FILTER = "all"; renderCards(); };
  document.querySelectorAll(".fcard").forEach(el =>
    el.addEventListener("click", () => el.classList.toggle("flipped")));
}

/* ---------- 错题分析 ---------- */

function renderMistakes() {
  const ms = WS.mistakes || [];
  if (!ms.length) { $("#view").innerHTML = '<div class="empty">暂无错题记录。</div>'; return; }
  const byReason = {}, byMod = {};
  ms.forEach(m => { byReason[m.reason] = (byReason[m.reason] || 0) + 1; byMod[m.module] = (byMod[m.module] || 0) + 1; });
  const bars = (obj, max) => Object.entries(obj).sort((a, b) => b[1] - a[1]).map(([k, v]) => `
    <div class="dist-row"><span class="label">${esc(k)}</span>
      <span class="bar"><i style="width:${v / max * 100}%"></i></span>
      <span class="num">${v}</span></div>`).join("");
  const rows = ms.map(m => `
    <details class="mrow"><summary>
      <b>${esc(m.id)}</b> ${badge(m.module, "gray")}
      <span>${esc(m.point)}</span> ${badge(m.reason, m.reason === "混淆" ? "amber" : "red")}
      <span class="hint">${esc(m.date)}</span></summary>
      <div class="mbody">
        <div class="line"><span class="k">题目：</span>${esc(m.question)}</div>
        <div class="line mine"><span class="k">我的答案：</span>${esc(m.my)}</div>
        <div class="line correct"><span class="k">正确答案：</span>${esc(m.correct)}</div>
        <div class="line"><span class="k">错因：</span>${esc(m.reason_raw)}</div>
        <div class="line"><span class="k">变式练习：</span>${esc(m.variant)}</div>
      </div></details>`).join("");
  $("#view").innerHTML = `
  <div class="grid cols-2">
    <div class="card"><h3>错因分布</h3>${bars(byReason, Math.max(...Object.values(byReason)))}</div>
    <div class="card"><h3>模块分布</h3>${bars(byMod, Math.max(...Object.values(byMod)))}</div>
  </div>
  <div style="margin-top:14px">${rows}</div>`;
}

/* ---------- 学习周报 ---------- */

function renderWeekly() {
  const p = WS.progress || {};
  const plan = WS.plan || { meta: {} };
  const t = today();
  const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
  const ms = WS.mistakes || [];
  const weekM = ms.filter(m => m.date >= weekAgo);
  const kstat = { mastered: 0, suspected: 0, unlearned: 0 };
  (WS.kmap.nodes || []).forEach(n => kstat[n.status] !== undefined && kstat[n.status]++);
  const mods = p.modules || [];
  const passed = mods.filter(m => m.status === "passed").length;
  const exempt = mods.filter(m => m.status === "exempt-pending").length;
  const dl = daysTo(plan.meta.deadline);
  const prof = (WS.profile || {}).table || {};
  const tl = ((WS.profile || {}).timeline || []).slice().reverse()
    .map(e => `<li><span class="date">${esc(e.date)}</span> — ${esc(e.text)}</li>`).join("");

  $("#view").innerHTML = `
  <div class="no-print" style="margin-bottom:12px;text-align:right">
    <button class="ctl on" onclick="window.print()">🖨 打印 / 导出 PDF（家长周报）</button>
  </div>
  <div class="card">
    <h3>学习周报 · ${esc(WS.name)} · 截至 ${t}</h3>
    <table class="tbl">
      <tr><th>主题 / 学段</th><td>${esc(prof["学习主题"] || WS.name)} · ${esc(prof["学段 / 领域 / 发起人"] || "—")}</td></tr>
      <tr><th>目标 / 截止</th><td>${esc(plan.meta.goal || prof["目标等级"] || "—")} · ${esc(plan.meta.deadline || "—")}${dl != null ? "（剩 " + dl + " 天）" : ""}</td></tr>
      <tr><th>当前进度</th><td>模块 ${passed}/${mods.length} 过关，${exempt} 个免修待验；阶段 ${esc(p.phase ?? "—")} ${esc(p.phase_label || "")}</td></tr>
      <tr><th>知识掌握</th><td>已掌握 ${kstat.mastered} · 疑似 ${kstat.suspected} · 未学 ${kstat.unlearned}（图谱 ${(WS.kmap.nodes || []).length} 节点）</td></tr>
      <tr><th>本周错题</th><td>新增 ${weekM.length} 条（累计 ${ms.length}）${weekM.length ? "：" + weekM.map(m => m.id).join("、") : ""}</td></tr>
      <tr><th>卡片复习</th><td>今日到期 ${(WS.flashcards || []).filter(c => c.due && c.due <= t).length} 张 / 在途 ${(WS.flashcards || []).length} 张</td></tr>
      <tr><th>下一步</th><td>${esc(p.next_action || "—")}</td></tr>
    </table>
  </div>
  <div class="card" style="margin-top:14px">
    <h3>学习历程（确认记录）</h3>
    <ul class="timeline">${tl || "<li>暂无记录</li>"}</ul>
  </div>`;
}

/* 自动分析流水线：进度轮询 + 聊天实时刷新 */
let PIPE_TIMER = null;
let PIPE_WAS_RUNNING = false;
let CHAT_SEEN = 0;

async function syncChatHistory() {
  try {
    const r = await fetch("/api/chat/history?ws=" + encodeURIComponent(WS.name), { cache: "no-store" });
    const d = await r.json();
    const rows = d.messages || [];
    if (rows.length > CHAT_SEEN) {
      const log = $("#chat-log");
      rows.slice(CHAT_SEEN).forEach(m => log && appendBubble(m.role, m.content, m.actions));
      CHAT_SEEN = rows.length;
      if (log) log.scrollTop = log.scrollHeight;
    }
  } catch (e) { /* 静默 */ }
}

function startPipePoll() {
  if (PIPE_TIMER) clearInterval(PIPE_TIMER);
  const update = async () => {
    const e = $("#pipe-status"), btn = $("#pipe-stop");
    if (!e || !WS) return;
    try {
      const r = await fetch("/api/pipeline?ws=" + encodeURIComponent(WS.name), { cache: "no-store" });
      const d = await r.json();
      if (d.running) {
        PIPE_WAS_RUNNING = true;
        e.textContent = d.stage === "build"
          ? "⚙ 自动分析：正在生成知识图谱与学习计划…"
          : `⚙ 自动分析：消化第 ${d.segment_no ?? "?"}/${d.segments_total ?? "?"} 段（${d.digested}/${d.total_chunks} 块）`;
        if (btn) btn.style.display = "";
        syncChatHistory();
      } else {
        if (btn) btn.style.display = "none";
        if (d.stage === "done") {
          e.textContent = "✅ 自动分析完成：图谱、计划与模块已生成";
          if (PIPE_WAS_RUNNING) {
            PIPE_WAS_RUNNING = false;
            await syncChatHistory();
            refreshStateQuiet();
            renderRail();
          }
        } else if (d.stage === "error") {
          e.textContent = "⚠ 自动分析失败：" + (d.error || "未知错误");
          PIPE_WAS_RUNNING = false;
          syncChatHistory();
        } else if (d.stage === "stopped") {
          e.textContent = "自动分析已停止（进度保留，可重新上传触发）";
          PIPE_WAS_RUNNING = false;
          syncChatHistory();
        } else {
          e.textContent = "";
        }
      }
    } catch (err) { /* 静默 */ }
  };
  update();
  PIPE_TIMER = setInterval(update, 4000);
}

async function startPipeline(wsName) {
  try {
    const res = await fetch("/api/pipeline/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ws: wsName }),
    });
    const d = await res.json();
    if (d.error) throw new Error(d.error);
    const e = $("#pipe-status");
    if (e) e.textContent = "⚙ 自动分析已启动…";
    appendBubble("sys", "⚙ 自动分析已开始：过程消息将实时显示在这里，无需手动操作；完成后自动生成知识图谱、学习计划并填充模块。");
    return true;
  } catch (e) {
    const el = $("#pipe-status");
    if (el) el.textContent = "⚠ " + e.message;
    return false;
  }
}

/* ---------- AI 教练（聊天学习） ---------- */

let CHAT_BUSY = false;

function actChips(actions) {
  if (!actions || !actions.length) return "";
  return actions.map(a =>
    `<span class="act-chip${a.ok ? "" : " fail"}">${a.ok ? "✅" : "⚠"} ${esc(ACT_LABEL[a.name] || a.name)}：${esc(a.result || "")}</span>`).join("");
}

function appendBubble(role, content, actions) {
  const log = $("#chat-log");
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const who = role === "user" ? "我" : role === "assistant" ? "教练" : "系统";
  div.innerHTML = `<div class="who">${who}</div><div class="md">${md(content)}</div>${actChips(actions)}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderRail() {
  const p = (WS && WS.progress) || {};
  const el = $("#rail-state");
  if (el) el.innerHTML = `
    <div><b>阶段 ${esc(p.phase ?? "—")}</b> ${esc(p.phase_label || "")}</div>
    <div class="hint" style="margin-top:6px"><b>下一步：</b>${esc(p.next_action || "—")}</div>`;
  const mat = $("#rail-materials");
  if (mat) {
    fetch("/api/materials?ws=" + encodeURIComponent(WS.name)).then(r => r.json()).then(d => {
      mat.innerHTML = d.files && d.files.length
        ? d.files.map(f => `<div>📄 ${esc(f)}</div>`).join("") +
          `<div class="hint">索引 ${d.indexed} 段，提问时自动检索</div>`
        : '<div class="hint">暂无资料。点下方「⬆ 上传资料」添加，或直接对教练说"帮我找资料"。</div>';
    }).catch(() => {});
  }
}

async function renderCoach() {
  const hasKey = DATA.config && DATA.config.has_key;
  $("#view").innerHTML = `
  ${hasKey ? "" : '<div class="warnbar">⚠ 尚未配置 API Key——请到「设置」页填写智谱 API Key（保存在本机 dashboard\\config.json），配置后即可与教练对话。</div>'}
  <div class="coach-wrap">
    <section class="card chat-panel">
      <div class="chat-log" id="chat-log"></div>
      <div class="quick-chips" id="quick-chips">
        <button data-msg="继续学习">▶️ 继续学习</button>
        <button data-msg="继续消化资料">📖 继续消化资料</button>
        <button data-msg="资料已消化完毕，请构建完整知识图谱和学习计划">🗂️ 生成图谱与计划</button>
        <button data-msg="出题考我">✏️ 出题考我</button>
        <button data-msg="我想先复习今天的卡片">🃏 先复习卡片</button>
        <button data-msg="我没看懂，请换一种更简单的方式再讲一遍">❓ 没懂，再讲一遍</button>
        <button data-msg="我想结束今天的学习了，帮我总结一下">🏁 结束并总结</button>
      </div>
      <div class="chat-input">
        <textarea id="chat-box" rows="2" placeholder="回答教练的问题，或说：继续学习 / 开始规划 / 我上传了资料，请分析消化 / 出题考我 / 调整计划…（Enter 发送，Shift+Enter 换行）"></textarea>
        <button id="chat-send" class="ctl on">发送</button>
      </div>
      <div class="hint" style="margin-top:6px">教练会通过写盘动作把测验结果、卡片、错题、计划调整写回工作区；请勿与教练窗口（ZCode）同时学习同一主题。</div>
    </section>
    <aside class="coach-rail">
      <div class="card"><h3>当前状态</h3><div id="rail-state" class="hint">读取中…</div></div>
      <div class="card"><h3>资料库</h3>
        <div id="rail-materials" class="hint">读取中…</div>
        <input type="file" id="coach-files" multiple accept=".md,.txt,.markdown,.docx,.pdf" style="display:none">
        <div style="margin-top:8px">
          <button id="coach-upload" class="ctl">⬆ 上传资料</button>
          <span id="upload-status" class="hint"></span>
        </div>
        <div class="hint" style="margin-top:4px">支持 .md / .txt / .docx / .pdf（扫描版 PDF 无法提取文字），<b>随时可传</b>。<b>上传后自动开始全书分析</b>：逐段消化 → 生成图谱与计划 → 填充模块，全程无需手动催促。</div>
        <div id="pipe-status" class="hint" style="margin-top:6px"></div>
        <div style="margin-top:6px"><button id="pipe-stop" class="ctl" style="display:none">■ 停止自动分析</button></div>
      </div>
      <div class="card"><h3>本次写盘动作</h3><div id="rail-actions" class="hint">暂无</div></div>
    </aside>
  </div>`;
  renderRail();
  startPipePoll();
  const stopBtn = $("#pipe-stop");
  if (stopBtn) stopBtn.onclick = async () => {
    stopBtn.disabled = true;
    try {
      await fetch("/api/pipeline/stop", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ws: WS.name }),
      });
    } catch (e) { /* ignore */ }
    stopBtn.disabled = false;
  };
  const upBtn = $("#coach-upload"), upInput = $("#coach-files"), upStatus = $("#upload-status");
  if (upBtn) {
    upBtn.onclick = () => upInput.click();
    upInput.onchange = async () => {
      const files = [...upInput.files];
      if (!files.length) return;
      upStatus.textContent = "上传中…";
      try {
        const res = await fetch("/api/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ws: WS.name, files: await readFilesB64(files) }),
        });
        const d = await res.json();
        if (d.error) throw new Error(d.error);
        upStatus.textContent = "已上传 " + d.saved.length + " 份，索引 " + d.index.chunks + " 段";
        upInput.value = "";
        renderRail();
        startPipeline(WS.name);
      } catch (e) {
        upStatus.textContent = "⚠ " + e.message;
      }
    };
  }
  // 载入历史
  try {
    const res = await fetch("/api/chat/history?ws=" + encodeURIComponent(WS.name));
    const d = await res.json();
    (d.messages || []).forEach(m => appendBubble(m.role, m.content, m.actions));
    CHAT_SEEN = (d.messages || []).length;
    if (!(d.messages || []).length) {
      appendBubble("sys", "聊天记录为空。常用开场：「继续学习」「我没有资料，帮我找资料并规划」或直接回答教练的到期卡片。");
    }
  } catch (e) { /* ignore */ }

  const box = $("#chat-box"), send = $("#chat-send");
  const doSend = async () => {
    const text = box.value.trim();
    if (!text || CHAT_BUSY) return;
    box.value = "";
    appendBubble("user", text);
    CHAT_BUSY = true;
    send.disabled = true;
    const log = $("#chat-log");
    const typing = document.createElement("div");
    typing.className = "typing";
    typing.id = "typing";
    typing.textContent = "教练思考中…（首次规划可能需要 1–2 分钟）";
    log.appendChild(typing);
    log.scrollTop = log.scrollHeight;
    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ws: WS.name, message: text }),
      });
      const data = await res.json();
      typing.remove();
      if (data.error) {
        appendBubble("sys", "⚠ " + data.error);
      } else {
        appendBubble("assistant", data.reply, data.actions);
        if (data.actions && data.actions.length) {
          $("#rail-actions").innerHTML = actChips(data.actions);
          refreshStateQuiet();
        }
      }
    } catch (e) {
      typing.remove();
      appendBubble("sys", "⚠ 网络错误：" + e.message);
    }
    CHAT_BUSY = false;
    send.disabled = false;
    box.focus();
  };
  send.onclick = doSend;
  document.querySelectorAll("#quick-chips button").forEach(b =>
    b.addEventListener("click", () => {
      if (CHAT_BUSY) return;
      box.value = b.dataset.msg;
      doSend();
    }));
  box.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); doSend(); }
  });
}

/* ---------- 立项向导 ---------- */

function renderNew() {
  $("#view").innerHTML = `
  <div class="card form">
    <h3>新建学习主题</h3>
    <div class="field"><label>学习主题 *</label>
      <input id="n-topic" placeholder="如：中级会计实务 / Python 数据分析 / 小学五年级数学">
      <div class="hint">将创建独立工作区，与现有主题互不影响</div></div>
    <div class="field"><label>学习目标 / 考试名称</label>
      <input id="n-goal" placeholder="如：通过 11 月考试 / 能独立完成数据分析项目"></div>
    <div class="row">
      <div class="field"><label>目标等级</label>
        <select id="n-grade"><option>及格</option><option>良好</option><option>优秀</option><option>精通</option></select></div>
      <div class="field"><label>学段</label>
        <select id="n-stage"><option>成人</option><option>大学</option><option>高中</option><option>初中</option><option>小学</option></select></div>
    </div>
    <div class="row">
      <div class="field"><label>计划周期（天）</label><input id="n-days" type="number" value="28" min="3" max="365"></div>
      <div class="field"><label>每日投入（小时）</label><input id="n-daily" type="number" value="2" min="0.5" max="12" step="0.5"></div>
    </div>
    <div class="field"><label>领域（可选）</label>
      <input id="n-domain" placeholder="如：法律 / 金融 / 中医 / 信息安全"></div>
    <div class="field"><label>学习资料（可选，支持 .md / .txt / .docx / .pdf 多选）</label>
      <input id="n-files" type="file" multiple accept=".md,.txt,.markdown,.docx,.pdf">
      <div class="hint">不上传也行——创建后到「AI教练」页对教练说"帮我找资料"，它会联网检索候选清单供你确认。</div></div>
    <div class="field" style="border-top:1px dashed var(--line);padding-top:14px">
      <label>或：从文件夹导入已有主题（恢复备份）</label>
      <input id="imp-dir" type="file" webkitdirectory directory multiple style="display:none">
      <div>
        <button id="imp-btn" class="ctl">📂 选择主题文件夹…</button>
        <span id="imp-status" class="hint"></span>
      </div>
      <div class="hint">选择包含 progress.json 的主题文件夹（例如备份里的「NISP二级」），应用会完整恢复该主题的进度、错题、卡片与资料。</div>
    </div>
    <div class="actions">
      <button id="n-create" class="ctl on">创建并开始</button>
    </div>
    <result id="n-result"></result>
  </div>`;

  $("#n-create").onclick = async () => {
    const topic = $("#n-topic").value.trim();
    const result = $("#n-result");
    if (!topic) { result.textContent = "⚠ 请填写学习主题"; return; }
    result.textContent = "创建中…";
    try {
      let res = await fetch("/api/ws/create", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic, goal: $("#n-goal").value.trim(), grade: $("#n-grade").value,
          stage: $("#n-stage").value, domain: $("#n-domain").value.trim(),
          period_days: $("#n-days").value, daily_hours: $("#n-daily").value,
        }),
      });
      let data = await res.json();
      if (data.error) throw new Error(data.error);
      const wsName = data.ws;
      const files = [...$("#n-files").files];
      if (files.length) {
        result.textContent = "上传资料中…";
        const encoded = await readFilesB64(files);
        res = await fetch("/api/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ws: wsName, files: encoded }),
        });
        data = await res.json();
        if (data.error) throw new Error(data.error);
        result.textContent = "已上传 " + data.saved.length + " 个文件，索引 " + data.index.chunks + " 段。自动分析将在打开教练页后开始。";
      }
      await load(wsName);
      setTab("coach");
      startPipeline(wsName);
    } catch (e) {
      result.textContent = "⚠ " + e.message;
    }
  };

  // 从文件夹导入已有工作区
  const impBtn = $("#imp-btn"), impDir = $("#imp-dir"), impStatus = $("#imp-status");
  impBtn.onclick = () => impDir.click();
  impDir.onchange = async () => {
    const files = [...impDir.files];
    if (!files.length) return;
    const rootName = (files[0].webkitRelativePath || "").split("/")[0] || "导入主题";
    const relFiles = files
      .map(f => ({ path: (f.webkitRelativePath || f.name).split("/").slice(1).join("/"), file: f }))
      .filter(x => x.path);
    if (!relFiles.length) { impStatus.textContent = "⚠ 无法读取文件夹结构"; return; }
    impStatus.textContent = "读取 " + relFiles.length + " 个文件…";
    try {
      const encoded = [];
      for (const item of relFiles) {
        const b64 = await new Promise((ok, no) => {
          const r = new FileReader();
          r.onload = () => ok(bufToB64(r.result));
          r.onerror = no;
          r.readAsArrayBuffer(item.file);
        });
        encoded.push({ path: item.path, b64 });
      }
      impStatus.textContent = "导入中…";
      const res = await fetch("/api/ws/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: rootName, files: encoded }),
      });
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      impStatus.textContent = "✅ 已导入 " + d.files + " 个文件（索引 " + d.index.chunks + " 段）";
      impDir.value = "";
      await load(d.ws);
      setTab("overview");
    } catch (e) {
      impStatus.textContent = "⚠ " + e.message;
    }
  };
}

/* ---------- 设置 ---------- */

function renderSettings() {
  const cfg = (DATA && DATA.config) || {};
  $("#view").innerHTML = `
  <div class="card form">
    <h3>设置 · 内置 AI（智谱开放平台）</h3>
    <div class="note">涉及账号密钥，建议由家长或管理员设置；配置一次即可长期使用。</div>
    <div class="field"><label>API Key</label>
      <input id="s-key" type="password" placeholder="${cfg.has_key ? "已配置（" + esc(cfg.key_hint) + "），留空则不修改" : "粘贴智谱 API Key（open.bigmodel.cn 控制台获取）"}">
      <div class="hint">仅保存在本机 dashboard\\config.json，不随工作区同步、不上传</div></div>
    <div class="field"><label>API 地址（OpenAI 兼容）</label>
      <input id="s-url" value="${esc(cfg.base_url || "")}" placeholder="https://open.bigmodel.cn/api/paas/v4"></div>
    <div class="field"><label>模型</label>
      <input id="s-model" value="${esc(cfg.model || "glm-4.7")}" placeholder="glm-4.7 / glm-4.6 …">
      <div class="hint">若报"模型不存在"，换成你账号可用的模型名</div></div>
    <div class="actions">
      <button id="s-save" class="ctl on">保存</button>
      <button id="s-test" class="ctl">测试连接</button>
    </div>
    <result id="s-result"></result>
  </div>`;
  $("#s-save").onclick = async () => {
    const body = { base_url: $("#s-url").value.trim(), model: $("#s-model").value.trim() };
    if ($("#s-key").value.trim()) body.api_key = $("#s-key").value.trim();
    const res = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json();
    $("#s-result").textContent = data.error ? "⚠ " + data.error :
      "已保存（Key：" + (data.has_key ? data.key_hint : "未设置") + "）";
    if (DATA) DATA.config = data;
  };
  $("#s-test").onclick = async () => {
    $("#s-result").textContent = "测试中…";
    try {
      const res = await fetch("/api/config/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const data = await res.json();
      $("#s-result").textContent = data.error ? "⚠ " + data.error : "✅ 连接正常，模型回复：" + data.echo;
    } catch (e) {
      $("#s-result").textContent = "⚠ " + e.message;
    }
  };
}

/* ---------- 渲染入口与事件 ---------- */

function render() {
  // 无工作区时仅「新主题」「设置」两个视图可用
  if (!WS && TAB !== "new" && TAB !== "settings") return;
  ({ overview: renderOverview, modules: renderModules, graph: renderGraph,
     cards: renderCards, mistakes: renderMistakes, weekly: renderWeekly,
     coach: renderCoach, new: renderNew, settings: renderSettings })[TAB]();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#tabs").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-tab]");
    if (btn) setTab(btn.dataset.tab);
  });
  $("#new-topic").addEventListener("click", () => {
    if (!DATA || !DATA.workspaces) return;
    setTab("new");
  });
  $("#refresh").addEventListener("click", () => load(WS && WS.name));
  $("#reload-page").addEventListener("click", () => location.reload());
  $("#del-topic").addEventListener("click", async () => {
    if (!WS) return;
    const name = WS.name;
    if (!confirm(`确定删除主题「${name}」吗？\n\n将删除：学习进度、错题、卡片、聊天记录、知识图谱、计划，以及上传的全部资料文件（含本地文件）。\n此操作不可恢复。`)) return;
    if (!confirm(`再次确认：真的删除「${name}」？`)) return;
    try {
      const res = await fetch("/api/ws/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ws: name }),
      });
      const d = await res.json();
      if (d.error) throw new Error(d.error);
      await load();
      alert(`已删除「${name}」，清理 ${d.removed_files} 个文件。`);
    } catch (e) {
      alert("删除失败：" + e.message);
    }
  });
  $("#ws-select").addEventListener("change", (ev) => {
    WS = DATA.workspaces.find(w => w.name === ev.target.value);
    render();
  });
  $("#view").addEventListener("click", (ev) => {
    const node = ev.target.closest("svg.kmap .node");
    if (node) { showNode(node.dataset.id); return; }
    const chip = ev.target.closest("[data-kid]");
    if (chip) showNode(chip.dataset.kid);
  });
  load();
});
