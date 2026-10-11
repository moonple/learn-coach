/* learn-coach 学习系统 · 前端（零依赖）
   视图：总览 / AI教练 / 知识图谱 / 复习(卡片+错题) / 资料 / 设置 */
"use strict";

let DATA = null;        // /api/state 结果（含 config、rev）
let WS = null;          // 当前工作区
let TAB = "home";
let REV_SEEN = "";      // 已见的数据版本（轻量自动刷新）

const MOD_STATUS = {
  "passed": ["⭐ 已过关", "green"],
  "learning": ["学习中", "amber"],
  "quiz-failed": ["再试一次", "red"],
  "exempt-pending": ["免修待验", "blue"],
  "not-started": ["未开始", "gray"],
};
const NODE_LABEL = { mastered: "已掌握", suspected: "疑似掌握", unlearned: "未学" };
const NODE_COLOR = { mastered: "#16a34a", suspected: "#d97706", unlearned: "#a8a29b" };
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

/* ---------- API 封装 ---------- */

async function api(path, opts) {
  const res = await fetch(path, opts);
  let d = {};
  try { d = await res.json(); } catch (e) { /* ignore */ }
  if (d.error) throw new Error(d.error);
  return d;
}
const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

/* ---------- 文件读取为 base64（分块，避免大文件栈溢出） ---------- */

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
    if (h) { const lv = Math.min(4, h[1].length); out += `<h${lv}>${inline(h[2])}</h${lv}>`; i++; continue; }
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
    DATA = await api("/api/state?ts=" + Date.now());
    REV_SEEN = DATA.rev || "";
  } catch (e) {
    view.innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "，请确认服务正在运行。</div>";
    return;
  }
  const sel = $("#ws-select");
  sel.innerHTML = DATA.workspaces.map(w => `<option>${esc(w.name)}</option>`).join("");
  if (!DATA.workspaces.length) {
    WS = null;
    view.innerHTML = `<div class="empty" style="padding:60px 26px">
      <div style="font-size:17px;font-weight:700;margin-bottom:8px">开始你的第一个学习主题</div>
      <div class="hint" style="margin-bottom:16px">上传学习资料（或让 AI 帮你找），系统会自动消化成课程、带你学习、出题验收。</div>
      <button id="empty-new" class="btn primary big">＋ 新建学习主题</button></div>`;
    $("#empty-new").addEventListener("click", openNewTopicModal);
    return;
  }
  const name = keepName && DATA.workspaces.some(w => w.name === keepName) ? keepName : DATA.workspaces[0].name;
  sel.value = name;
  WS = DATA.workspaces.find(w => w.name === name);
  $("#updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  render();
}

async function refreshStateQuiet() {
  try {
    const d = await api("/api/state?ts=" + Date.now());
    DATA = d;
    const cur = DATA.workspaces.find(w => w.name === (WS && WS.name));
    if (cur) WS = cur;
    const sel = $("#ws-select");
    sel.innerHTML = DATA.workspaces.map(w => `<option>${esc(w.name)}</option>`).join("");
    if (WS) sel.value = WS.name;
  } catch (e) { /* 静默 */ }
}

/* 数据版本自动刷新：文件变化（教练写盘/流水线）时静默更新当前视图 */
setInterval(async () => {
  if (!WS || document.hidden) return;
  if (TAB === "coach" && CHAT_BUSY) return;           // 教练回复中不打扰
  if (TAB === "review" && REVIEW.active) return;      // 复习进行中不打扰
  try {
    const d = await api("/api/state?ts=" + Date.now());
    if (d.rev && d.rev !== REV_SEEN) {
      const cur = d.workspaces.find(w => w.name === WS.name);
      DATA = d; REV_SEEN = d.rev;
      if (cur) WS = cur;
      render();
    }
  } catch (e) { /* 静默 */ }
}, 15000);

/* ---------- 通用 ---------- */

function modBadge(st) {
  const [label, color] = MOD_STATUS[st] || [st || "未知", "gray"];
  return badge(label, color);
}

function daysTo(deadline) {
  if (!deadline) return null;
  return Math.ceil((new Date(deadline + "T23:59:59") - new Date()) / 86400000);
}

const TAB_TITLE = { home: "总览", coach: "AI 教练", graph: "知识图谱", review: "复习", library: "资料", settings: "设置" };

function setTab(name) {
  stopPipePoll();
  TAB = name;
  document.querySelectorAll("#nav button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  $("#top-title").textContent = TAB_TITLE[name] || name;
  render();
}

/* ================= 总览 ================= */

function renderHome() {
  const p = WS.progress || {};
  const plan = WS.plan || { meta: {}, modules: [] };
  const mods = p.modules || [];
  const passed = mods.filter(m => m.status === "passed").length;
  const exempt = mods.filter(m => m.status === "exempt-pending").length;
  const cards = WS.flashcards || [];
  const dueCards = cards.filter(c => c.due && c.due <= today());
  const dl = daysTo(plan.meta.deadline);
  const diag = p.diagnostic && p.diagnostic.score != null ? Math.round(p.diagnostic.score * 100) + "%" : "—";

  const modRows = mods.map(m => {
    const pm = (plan.modules || []).find(x => x.id === m.id) || {};
    const qh = (m.quiz_history || []).map(h =>
      `${esc(h.date).slice(5)}：<b>${Math.round((h.score ?? 0) * 100)}%</b>`).join("、") || "—";
    return `<tr>
      <td><b>${esc(m.id)}</b></td>
      <td>${esc(m.name)}${pm.goals ? `<div class="hint">${esc(pm.goals)}</div>` : ""}</td>
      <td>${pm.hours ? pm.hours + "h" : "—"}</td>
      <td>${modBadge(m.status)}</td>
      <td>${qh}</td>
    </tr>`;
  }).join("");

  const t = today();
  const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
  const ms = WS.mistakes || [];
  const weekM = ms.filter(m => m.date >= weekAgo);
  const kstat = { mastered: 0, suspected: 0, unlearned: 0 };
  ((WS.kmap || {}).nodes || []).forEach(n => { if (kstat[n.status] !== undefined) kstat[n.status]++; });
  const tl = ((WS.profile || {}).timeline || []).slice().reverse()
    .map(e => `<li><span class="date">${esc(e.date)}</span> — ${esc(e.text)}</li>`).join("");
  const prof = (WS.profile || {}).table || {};

  $("#view").innerHTML = `
  <div class="page">
  <div class="card hero">
    <div class="hero-text">
      <div class="hero-title">继续学习 · ${esc(WS.name)}
        ${badge("阶段 " + (p.phase ?? "—"), "blue")}</div>
      <div class="hero-next">${esc(p.next_action || "对教练说「继续学习」即可开始")}</div>
      ${p.phase_label ? `<div class="hint" style="margin-top:4px">${esc(p.phase_label)}</div>` : ""}
    </div>
    <div class="hero-actions">
      <button class="btn primary big" id="go-coach">开始学习 →</button>
      ${dueCards.length ? `<button class="btn big" id="go-review">先复习 ${dueCards.length} 张卡</button>` : ""}
    </div>
  </div>

  <div class="grid cols-4">
    <div class="card stat"><h3>模块进度</h3>
      <div class="value">${passed} / ${mods.length}</div>
      <div class="sub">已过关${exempt ? `；${exempt} 个免修待验` : ""}</div>
      <div class="progress-track" style="margin-top:8px">
        <div class="progress-fill green" style="width:${mods.length ? (passed / mods.length * 100) : 0}%"></div>
      </div></div>
    <div class="card stat${dueCards.length ? " warn" : ""}"><h3>今日到期卡片</h3>
      <div class="value">${dueCards.length} 张</div>
      <div class="sub">在途共 ${cards.length} 张 · <a href="#" id="lnk-review">去复习</a></div></div>
    <div class="card stat${dl != null && dl <= 7 ? " warn" : ""}"><h3>距截止</h3>
      <div class="value">${dl == null ? "—" : dl + " 天"}</div>
      <div class="sub">${esc(plan.meta.deadline || "计划未设截止")}${plan.meta.total_hours ? " · 总量 " + esc(plan.meta.total_hours) + "h" : ""}</div></div>
    <div class="card stat"><h3>摸底诊断</h3>
      <div class="value">${diag}</div>
      <div class="sub">错题 ${ms.length} 条已入账</div></div>
  </div>

  ${mods.length ? `
  <div class="card" style="margin-top:14px"><h3>学习模块</h3>
    <table class="tbl">
      <thead><tr><th>编号</th><th>模块</th><th>时长</th><th>状态</th><th>测验历史</th></tr></thead>
      <tbody>${modRows}</tbody>
    </table></div>` : ""}

  <details class="card" style="margin-top:14px">
    <summary style="cursor:pointer;font-weight:600">学习周报（打印 / 导出 PDF）</summary>
    <div style="padding-top:12px">
      <div class="no-print" style="text-align:right;margin-bottom:8px">
        <button class="btn ghost-sm" onclick="window.print()">🖨 打印 / 导出 PDF</button>
      </div>
      <table class="tbl">
        <tr><th style="width:110px">主题 / 学段</th><td>${esc(prof["学习主题"] || WS.name)} · ${esc(prof["学段 / 领域 / 发起人"] || "—")}</td></tr>
        <tr><th>目标 / 截止</th><td>${esc(plan.meta.goal || prof["目标等级"] || "—")} · ${esc(plan.meta.deadline || "—")}${dl != null ? "（剩 " + dl + " 天）" : ""}</td></tr>
        <tr><th>当前进度</th><td>模块 ${passed}/${mods.length} 过关，${exempt} 个免修待验；阶段 ${esc(p.phase ?? "—")} ${esc(p.phase_label || "")}</td></tr>
        <tr><th>知识掌握</th><td>已掌握 ${kstat.mastered} · 疑似 ${kstat.suspected} · 未学 ${kstat.unlearned}（图谱 ${((WS.kmap || {}).nodes || []).length} 节点）</td></tr>
        <tr><th>本周错题</th><td>新增 ${weekM.length} 条（累计 ${ms.length}）</td></tr>
        <tr><th>卡片复习</th><td>今日到期 ${dueCards.length} 张 / 在途 ${cards.length} 张</td></tr>
        <tr><th>下一步</th><td>${esc(p.next_action || "—")}</td></tr>
      </table>
      ${tl ? `<h3 style="margin:14px 0 8px;font-size:13px;color:var(--muted)">学习历程</h3>
        <ul class="timeline">${tl.slice(0, 12)}</ul>` : ""}
    </div>
  </details>
  </div>`;

  $("#go-coach").onclick = () => setTab("coach");
  const gr = $("#go-review"); if (gr) gr.onclick = () => setTab("review");
  const lr = $("#lnk-review"); if (lr) lr.onclick = (ev) => { ev.preventDefault(); setTab("review"); };
}

/* ================= AI 教练 ================= */

let CHAT_BUSY = false;
let CHAT_SEEN = 0;

function actChips(actions) {
  if (!actions || !actions.length) return "";
  return actions.map(a =>
    `<span class="act-chip${a.ok ? "" : " fail"}">${a.ok ? "✅" : "⚠"} ${esc(ACT_LABEL[a.name] || a.name)}：${esc(a.result || "")}</span>`).join("");
}

function bindLocSpans(root) {
  // 就地绑定原文跳转：.loc（聊天标记，data-file）与 [data-openfile]（面板/片段按钮）
  root.querySelectorAll(".loc, [data-openfile]").forEach(el => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const file = String(el.dataset.file || el.dataset.openfile || "")
        .replace(/^《|》$/g, "");
      if (file) openReader(file, el.dataset.openpage || el.dataset.page);
    };
  });
  root.querySelectorAll("[data-kid]").forEach(el => {
    el.onclick = (ev) => { ev.stopPropagation(); showNode(el.dataset.kid); };
  });
}

function appendBubble(role, content, actions) {
  const log = $("#chat-log");
  if (!log) return null;
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const who = role === "user" ? "我" : role === "assistant" ? "教练" : "系统";
  let html = md(content);
  html = html.replace(/📍\s*[《"「]?([^》"「）)\s]+)[》"」]?\s*[pP]\s*(\d+)/g,
    '<span class="loc" data-file="$1" data-page="$2">📍 $1 p$2 ↗</span>');
  div.innerHTML = `<div class="who">${who}</div><div class="md">${html}</div>${actChips(actions)}`;
  bindLocSpans(div);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

async function renderCoach() {
  const hasKey = DATA.config && DATA.config.has_key;
  $("#view").innerHTML = `
  ${hasKey ? "" : '<div class="warnbar">⚠ 尚未配置 API Key——请到「设置」页填写智谱 API Key（保存在本机 learn-coach\\config.json），配置后即可与教练对话。</div>'}
  <div class="coach-wrap">
    <section class="card chat-panel">
      <div class="pipebar" id="pipebar">
        <span id="pipebar-text">自动分析中…</span>
        <div class="progress-track"><div class="progress-fill" id="pipebar-fill" style="width:0%"></div></div>
        <button class="btn ghost-sm" id="pipe-stop">停止</button>
      </div>
      <div class="chat-log" id="chat-log"></div>
      <div class="quick-chips" id="quick-chips">
        <button data-msg="继续学习">▶ 继续学习</button>
        <button data-msg="出题考我">✏️ 出题考我</button>
        <button data-msg="继续消化资料">📖 继续消化资料</button>
        <button data-msg="资料已全部消化，请构建完整知识图谱和学习计划">🗂 生成图谱与计划</button>
        <button data-msg="我没看懂，请换一种更简单的方式再讲一遍">❓ 没懂，再讲一遍</button>
        <button id="shot-btn" title="截取当前屏幕并识别文字，向你读不懂的原文提问">📸 截图提问</button>
      </div>
      <div class="chat-input">
        <textarea id="chat-box" rows="2" placeholder="回答教练的问题，或说：继续学习 / 出题考我 / 帮我找资料…（Enter 发送，Shift+Enter 换行）"></textarea>
        <button id="chat-send" class="btn primary">发送</button>
      </div>
    </section>
    <aside class="coach-rail">
      <div class="card"><h3>当前状态</h3><div id="rail-state" class="hint">读取中…</div></div>
      <div class="card"><h3>资料库</h3>
        <div id="rail-materials" class="hint">读取中…</div>
        <input type="file" id="coach-files" multiple accept=".md,.txt,.markdown,.docx,.pdf" style="display:none">
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button id="coach-upload" class="btn">⬆ 上传资料</button>
          <button id="pipe-start" class="btn" title="逐段消化全部资料，自动生成图谱与计划">▶ 开始分析</button>
        </div>
        <span id="upload-status" class="hint"></span>
        <div class="hint" style="margin-top:6px">支持 .md / .txt / .docx / .pdf（扫描版 PDF 无法提取文字）。上传后自动开始全书分析：逐段消化 → 生成图谱与计划 → 填充模块。</div>
      </div>
      <div class="card"><h3>本次写盘动作</h3><div id="rail-actions" class="hint">暂无</div></div>
    </aside>
  </div>`;
  renderRail();
  startPipePoll();

  $("#pipe-stop").onclick = async () => {
    $("#pipe-stop").disabled = true;
    try { await post("/api/pipeline/stop", { ws: WS.name }); } catch (e) { /* ignore */ }
    $("#pipe-stop").disabled = false;
  };
  $("#pipe-start").onclick = async () => {
    try {
      await post("/api/pipeline/start", { ws: WS.name });
      appendBubble("sys", "⚙ 自动分析已开始，进度见上方进度条；完成后自动生成知识图谱、学习计划并填充模块。");
    } catch (e) {
      $("#upload-status").textContent = "⚠ " + e.message;
    }
  };

  const upBtn = $("#coach-upload"), upInput = $("#coach-files"), upStatus = $("#upload-status");
  upBtn.onclick = () => upInput.click();
  upInput.onchange = async () => {
    const files = [...upInput.files];
    if (!files.length) return;
    upStatus.textContent = "上传中…";
    try {
      const d = await post("/api/upload", { ws: WS.name, files: await readFilesB64(files) });
      const errs = (d.index.errors || []).map(e => `${e.file}：${e.error}`).join("；");
      upStatus.textContent = "已上传 " + d.saved.length + " 份，索引 " + d.index.chunks + " 段" + (errs ? "；⚠ " + errs : "");
      upInput.value = "";
      renderRail();
      await startPipeline();
    } catch (e) {
      upStatus.textContent = "⚠ " + e.message;
    }
  };

  // 载入历史（自动分析消息不进聊天流）
  try {
    const d = await api("/api/chat/history?ws=" + encodeURIComponent(WS.name));
    const rows = (d.messages || []).filter(m => m.kind !== "pipeline");
    rows.forEach(m => appendBubble(m.role, m.content, m.actions));
    CHAT_SEEN = (d.messages || []).length;
    if (!rows.length) {
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
    typing.textContent = "教练思考中…（复杂任务可能需要 1–2 分钟）";
    log.appendChild(typing);
    log.scrollTop = log.scrollHeight;
    try {
      const data = await post("/api/chat", { ws: WS.name, message: text });
      typing.remove();
      appendBubble("assistant", data.reply, data.actions);
      CHAT_SEEN += 2;
      if (data.actions && data.actions.length) {
        $("#rail-actions").innerHTML = actChips(data.actions);
        await refreshStateQuiet();
        renderRail();
      }
    } catch (e) {
      typing.remove();
      appendBubble("sys", "⚠ " + e.message);
    }
    CHAT_BUSY = false;
    send.disabled = false;
    box.focus();
  };
  send.onclick = doSend;

  /* OCR 文本清理：去行首尾空白、压缩连续空行、限长，减少界面噪声 */
function cleanOcr(t) {
  return String(t || "").split(/\r?\n/).map(l => l.trim())
    .filter((l, i, a) => l !== "" || (i > 0 && a[i - 1] !== ""))
    .join("\n").replace(/\n{3,}/g, "\n\n").trim().slice(0, 1500);
}

const shotBtn = $("#shot-btn");
  shotBtn.onclick = async () => {
    if (CHAT_BUSY) return;
    const tip = appendBubble("sys", "📸 已截取当前屏幕，正在识别文字…（截图包含全部屏幕内容，稍后可删改识别结果）");
    try {
      const d = await post("/api/screen/capture", {});
      tip.remove();
      if (d.error) { appendBubble("sys", "⚠ " + d.error); return; }
      box.value = `【截图提问】\n> 识别原文：${cleanOcr(d.text)}\n\n我的问题：`;
      box.focus();
      box.selectionStart = box.selectionEnd = box.value.length;
    } catch (e) { tip.remove(); appendBubble("sys", "⚠ " + e.message); }
  };
  box.addEventListener("paste", async (ev) => {
    const items = [...((ev.clipboardData && ev.clipboardData.items) || [])];
    const imgItem = items.find(i => i.type.startsWith("image/"));
    if (!imgItem) return;
    ev.preventDefault();
    const file = imgItem.getAsFile();
    if (!file) return;
    const tip = appendBubble("sys", "📸 正在识别粘贴的图片…");
    try {
      const b64 = await new Promise((ok, no) => {
        const r = new FileReader();
        r.onload = () => ok(bufToB64(r.result));
        r.onerror = no;
        r.readAsArrayBuffer(file);
      });
      const d = await post("/api/ocr", { b64 });
      tip.remove();
      box.value = `【截图提问】\n> 识别原文：${cleanOcr(d.text)}\n\n我的问题：`;
      box.focus();
      box.selectionStart = box.selectionEnd = box.value.length;
    } catch (e) { tip.remove(); appendBubble("sys", "⚠ " + e.message); }
  });
  document.querySelectorAll("#quick-chips button[data-msg]").forEach(b =>
    b.addEventListener("click", () => {
      if (CHAT_BUSY) return;
      box.value = b.dataset.msg;
      doSend();
    }));
  box.addEventListener("keydown", (ev) => {
    // isComposing/229：中文输入法选词回车不发送，避免消息被截断
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
      ev.preventDefault();
      doSend();
    }
  });
  // 资料页「原文提问」带来的待填内容
  if (PENDING_ASK) {
    box.value = PENDING_ASK.head + PENDING_ASK.text + "\n\n我的问题：";
    PENDING_ASK = null;
    box.focus();
    box.selectionStart = box.selectionEnd = box.value.length;
  }
}

async function startPipeline() {
  try {
    await post("/api/pipeline/start", { ws: WS.name });
    appendBubble("sys", "⚙ 自动分析已开始，进度见上方进度条；完成后自动生成知识图谱、学习计划并填充模块。");
    return true;
  } catch (e) {
    const el = $("#upload-status");
    if (el) el.textContent = "⚠ " + e.message;
    return false;
  }
}

function renderRail() {
  const p = (WS && WS.progress) || {};
  const el = $("#rail-state");
  if (el) el.innerHTML = `
    <div><b>阶段 ${esc(p.phase ?? "—")}</b> ${esc(p.phase_label || "")}</div>
    <div class="hint" style="margin-top:6px"><b>下一步：</b>${esc(p.next_action || "—")}</div>`;
  const mat = $("#rail-materials");
  if (mat && WS) {
    api("/api/materials?ws=" + encodeURIComponent(WS.name)).then(d => {
      mat.innerHTML = d.files && d.files.length
        ? d.files.map(f => `<div class="mat-file">📄 ${esc(f)}</div>`).join("") +
          `<div class="hint" style="margin-top:6px">索引 ${d.indexed} 段，提问时自动检索</div>`
        : '<div class="hint">暂无资料。点「上传资料」添加，或对教练说"帮我找资料"。</div>';
    }).catch(() => {});
  }
}

/* 流水线轮询：只在教练页运行，离开即停 */
let PIPE_TIMER = null;
let PIPE_WAS_RUNNING = false;

function stopPipePoll() {
  if (PIPE_TIMER) { clearInterval(PIPE_TIMER); PIPE_TIMER = null; }
}

function startPipePoll() {
  stopPipePoll();
  const update = async () => {
    if (!WS || !$("#pipebar")) { stopPipePoll(); return; }
    try {
      const d = await api("/api/pipeline?ws=" + encodeURIComponent(WS.name));
      const bar = $("#pipebar"), txt = $("#pipebar-text"), fill = $("#pipebar-fill");
      if (!bar || !txt) { stopPipePoll(); return; }
      if (d.running) {
        PIPE_WAS_RUNNING = true;
        bar.classList.add("show");
        const pct = d.total_chunks ? Math.round(d.digested / d.total_chunks * 100) : 0;
        fill.style.width = pct + "%";
        txt.textContent = d.stage === "build"
          ? "正在生成知识图谱与学习计划…"
          : `消化中：第 ${d.segment_no ?? "?"}/${d.segments_total ?? "?"} 段（${d.digested}/${d.total_chunks} 块）`;
      } else if (d.stage === "done") {
        if (PIPE_WAS_RUNNING) {
          PIPE_WAS_RUNNING = false;
          bar.classList.remove("show");
          appendBubble("sys", "✅ 自动分析完成：图谱、计划与模块已生成，可到「总览」「知识图谱」查看。");
          await refreshStateQuiet();
          renderRail();
        } else {
          bar.classList.remove("show");
        }
      } else if (d.stage === "error") {
        bar.classList.add("show");
        txt.textContent = "⚠ 自动分析失败：" + (d.error || "未知错误");
        fill.style.width = "0%";
        PIPE_WAS_RUNNING = false;
      } else if (d.stage === "stopped") {
        bar.classList.remove("show");
        if (PIPE_WAS_RUNNING) {
          PIPE_WAS_RUNNING = false;
          appendBubble("sys", "自动分析已停止（进度保留，可点「开始分析」继续）。");
        }
      } else {
        bar.classList.remove("show");
      }
    } catch (err) { /* 静默 */ }
  };
  update();
  PIPE_TIMER = setInterval(update, 4000);
}

/* ================= 知识图谱 ================= */

let SEL_NODE = null;

function upstreamOf(edges, id) {
  const parents = {};
  edges.forEach(e => {
    if (e.type === "pre") (parents[e.to] = parents[e.to] || []).push(e.from);
  });
  const seen = new Set([id]);
  const hiEdges = new Set();
  const queue = [id];
  while (queue.length) {
    const cur = queue.shift();
    (parents[cur] || []).forEach(p => {
      hiEdges.add(p + "→" + cur);
      if (!seen.has(p)) { seen.add(p); queue.push(p); }
    });
  }
  return { nodes: seen, edges: hiEdges };
}

function renderGraph() {
  const kmap = WS.kmap || { nodes: [], edges: [], internal: [] };
  if (!kmap.nodes.length) {
    $("#view").innerHTML = '<div class="empty">该主题暂无知识图谱。<br>资料就绪后让 AI 教练运行自动分析（生成 knowledge-map.md）。</div>';
    return;
  }
  const stat = { mastered: 0, suspected: 0, unlearned: 0 };
  kmap.nodes.forEach(n => { if (stat[n.status] !== undefined) stat[n.status]++; });
  const pct = Math.round(stat.mastered / kmap.nodes.length * 100);

  const modOrder = ((WS.progress || {}).modules || []).map(m => m.id);
  kmap.nodes.forEach(n => { if (!modOrder.includes(n.module)) modOrder.push(n.module); });
  const modName = Object.fromEntries((((WS.progress || {}).modules) || []).map(m => [m.id, m.name]));

  const colW = 150, padL = 56, padT = 70, gapY = 64;
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
    return d ? `<path class="edge ${e.type}" data-ekey="${esc(e.from + "→" + e.to)}" d="${d}" ${e.type === "pre" ? 'marker-end="url(#arr)"' : ""}>
      <title>${esc(e.from + " → " + e.to + " · " + (EDGE_LABEL[e.type] || e.type))}</title></path>` : "";
  }).join("");

  const nodes = kmap.nodes.map(n => {
    const p = pos[n.id];
    return `<g class="node" data-id="${esc(n.id)}" transform="translate(${p.x},${p.y})">
      <circle r="16" fill="${NODE_COLOR[n.status] || NODE_COLOR.unlearned}" stroke="#26231e22">
        <title>${esc(n.id + " · " + n.name)}&#10;${esc(NODE_LABEL[n.status] || n.status)}${n.note ? "（" + esc(n.note) + "）" : ""}</title>
      </circle>
      <text text-anchor="middle" dy="3.5">${esc(n.id.replace("K", ""))}</text>
    </g>`;
  }).join("");

  const colLabels = modOrder.map((mid, i) =>
    `<text class="col-label" x="${padL + i * colW + colW / 2}" y="34" text-anchor="middle">${esc(mid + " " + (modName[mid] || "").slice(0, 9))}</text>`).join("");

  $("#view").innerHTML = `
  <div class="page">
  <div class="graph-statbar">
    <span><i class="dot" style="background:${NODE_COLOR.mastered}"></i>已掌握 <b>${stat.mastered}</b></span>
    <span><i class="dot" style="background:${NODE_COLOR.suspected}"></i>疑似 <b>${stat.suspected}</b></span>
    <span><i class="dot" style="background:${NODE_COLOR.unlearned}"></i>未学 <b>${stat.unlearned}</b></span>
    <span>掌握率 <b>${pct}%</b></span>
    <span>共 ${kmap.nodes.length} 节点 / ${kmap.edges.length} 边</span>
    <span style="margin-left:auto">—— 前置边 · <span style="color:#3b82f6">- - 关联</span> · <span style="color:#ef4444">··· 易混</span></span>
    <span class="hint">点击节点查看详情并高亮学习路径</span>
  </div>
  <div class="graph-layout">
    <div>
      <div class="graph-toolbar no-print">
        <button class="btn ghost-sm" id="gz-out" title="缩小">－</button>
        <span id="gz-val" style="min-width:44px;text-align:center;font-size:12.5px;color:var(--muted)"></span>
        <button class="btn ghost-sm" id="gz-in" title="放大">＋</button>
        <button class="btn ghost-sm" id="gz-fit" title="缩放到整图可见">⊡ 适应</button>
        <button class="btn ghost-sm" id="gz-100" title="恢复原始大小">1:1</button>
        <span class="hint">滚轮缩放 · 按住拖拽平移</span>
      </div>
      <div id="graph-wrap"><div id="graph-inner">
        <svg class="kmap" viewBox="0 0 ${W} ${H}">
          <defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
            <path d="M0,0 L6,3 L0,6 z" fill="#8a8273"/></marker></defs>
          ${edges}${colLabels}${nodes}
        </svg>
      </div></div>
    </div>
    <div class="card node-panel" id="node-panel">
      <h3>节点详情</h3><div class="hint">点击图谱中的节点：高亮它的全部前置学习路径，并在此显示详情。</div>
    </div>
  </div>
  </div>`;
  bindGraphZoom(W, H);
  // 节点点击直接绑定（不依赖全局委托）
  document.querySelectorAll("svg.kmap .node").forEach(g => {
    g.onclick = () => showNode(g.dataset.id);
  });
}

/* 图谱缩放与平移：滚轮以光标为锚缩放，拖拽平移，适应/1:1 一键复位 */
const GRAPH = { scale: 1, w: 0, h: 0, suppressUntil: 0 };

function applyGraphScale() {
  const inner = $("#graph-inner");
  if (!inner) return;
  inner.style.width = (GRAPH.w * GRAPH.scale) + "px";
  inner.style.height = (GRAPH.h * GRAPH.scale) + "px";
  const val = $("#gz-val");
  if (val) val.textContent = Math.round(GRAPH.scale * 100) + "%";
}

function graphZoomAt(factor, cx, cy) {
  const wrap = $("#graph-wrap");
  if (!wrap) return;
  const old = GRAPH.scale;
  const ns = Math.min(3, Math.max(0.2, old * factor));
  if (ns === old) return;
  const px = cx + wrap.scrollLeft, py = cy + wrap.scrollTop;
  GRAPH.scale = ns;
  applyGraphScale();
  wrap.scrollLeft = px * ns / old - cx;
  wrap.scrollTop = py * ns / old - cy;
}

function bindGraphZoom(W, H) {
  GRAPH.w = W; GRAPH.h = H;
  const wrap = $("#graph-wrap");
  if (!wrap) return;
  if (!GRAPH.scale || !isFinite(GRAPH.scale)) GRAPH.scale = 1;
  applyGraphScale();
  // 初始未缩放过 → 适应宽度
  if (!GRAPH.inited) {
    GRAPH.scale = Math.min(1, (wrap.clientWidth - 24) / W);
    applyGraphScale();
    GRAPH.inited = true;
  }
  const zin = $("#gz-in"), zout = $("#gz-out"), zfit = $("#gz-fit"), z100 = $("#gz-100");
  if (zin) zin.onclick = () => graphZoomAt(1.25, wrap.clientWidth / 2, wrap.clientHeight / 2);
  if (zout) zout.onclick = () => graphZoomAt(0.8, wrap.clientWidth / 2, wrap.clientHeight / 2);
  if (zfit) zfit.onclick = () => {
    GRAPH.scale = Math.min((wrap.clientWidth - 24) / GRAPH.w, (wrap.clientHeight - 24) / GRAPH.h, 1.4);
    applyGraphScale();
    wrap.scrollLeft = 0; wrap.scrollTop = 0;
  };
  if (z100) z100.onclick = () => { GRAPH.scale = 1; applyGraphScale(); };
  wrap.onwheel = (ev) => {
    ev.preventDefault();
    const r = wrap.getBoundingClientRect();
    graphZoomAt(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - r.left, ev.clientY - r.top);
  };
  let drag = null;
  wrap.onmousedown = (ev) => {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, sl: wrap.scrollLeft, st: wrap.scrollTop, moved: false };
    ev.preventDefault();
  };
  wrap.onmousemove = (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) {
      drag.moved = true;
      // 只在短时间内抑制图谱区的节点点击，避免拖拽尾触发选中；面板按钮不受影响
      GRAPH.suppressUntil = Date.now() + 150;
      wrap.classList.add("grabbing");
      wrap.scrollLeft = drag.sl - dx;
      wrap.scrollTop = drag.st - dy;
    }
  };
  const endDrag = () => {
    drag = null;
    wrap.classList.remove("grabbing");
  };
  wrap.onmouseup = endDrag;
  wrap.onmouseleave = endDrag;
}

const kChips = (arr) => (arr && arr.length)
  ? arr.map(n => `<span class="kchip" data-kid="${esc(n.id)}" title="${esc(n.name)}">${esc(n.id)} ${esc(n.name.length > 14 ? n.name.slice(0, 14) + "…" : n.name)}</span>`).join(" ")
  : '<span class="hint">无</span>';

function parsePageFromSource(src) {
  const m = String(src || "").match(/(?:p|P|第)\s*(\d+)\s*(?:页)?/) || String(src || "").match(/(\d+)\s*页/);
  return m ? parseInt(m[1], 10) : null;
}

function highlightPath(id) {
  const kmap = (WS && WS.kmap) || { edges: [] };
  const svg = document.querySelector("svg.kmap");
  if (!svg) return;
  const up = upstreamOf(kmap.edges, id);
  svg.querySelectorAll(".node").forEach(g => {
    g.classList.toggle("sel", g.dataset.id === id);
    g.classList.toggle("on-path", id !== g.dataset.id && up.nodes.has(g.dataset.id));
    g.classList.toggle("dim", !up.nodes.has(g.dataset.id));
  });
  svg.querySelectorAll(".edge").forEach(p => {
    p.classList.toggle("on-path", up.edges.has(p.dataset.ekey));
  });
}

async function showNode(id) {
  const panel = $("#node-panel");
  if (!panel) return;
  SEL_NODE = id;
  highlightPath(id);
  panel.innerHTML = `<h3>${esc(id)}</h3><div class="hint">加载知识点内容…</div>`;
  try {
    const d = await api("/api/knode?ws=" + encodeURIComponent(WS.name) + "&id=" + encodeURIComponent(id));
    const stColor = d.status === "mastered" ? "green" : d.status === "suspected" ? "amber" : "gray";
    const srcPage = parsePageFromSource(d.source);
    const locBtns = (d.loc || []).map(l =>
      `<button class="btn ghost-sm" data-openfile="${esc(l.file)}" data-openpage="${l.page || 1}"
        title="${esc(l.match)}">📍 ${esc(l.file)}${l.page ? " p" + l.page : ""}</button>`).join("");
    panel.innerHTML = `
      <h3>${esc(d.id)} · ${esc(d.name)}</h3>
      <table class="tbl">
        <tr><th style="width:88px">模块</th><td>${esc(d.module)}</td></tr>
        <tr><th>类型/Bloom</th><td>${esc(d.type)} / ${esc(d.bloom)}</td></tr>
        <tr><th>状态</th><td>${badge(NODE_LABEL[d.status] || d.status, stColor)} ${d.note ? '<span class="hint">' + esc(d.note) + "</span>" : ""}</td></tr>
        <tr><th>出处</th><td>${esc(d.source || "—")}${srcPage ? ` <button class="btn ghost-sm" data-openfile="${esc(d.source || "")}" data-openpage="${srcPage}">📍 原文 p${srcPage}</button>` : ""}</td></tr>
        <tr><th>前置（先学）</th><td>${kChips(d.pre_in)}</td></tr>
        <tr><th>后续（支撑）</th><td>${kChips(d.pre_out)}</td></tr>
        <tr><th>关联（相通）</th><td>${kChips(d.related)}</td></tr>
        <tr><th>易混（对照记）</th><td>${kChips(d.confusable)}</td></tr>
      </table>
      ${locBtns ? `<h3 style="margin-top:12px">📍 在原文中定位</h3>
        <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-start">${locBtns}
        <span class="hint">点击直接跳到资料页对应位置</span></div>` : ""}
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
        d.rag.map(c => `<div class="note" style="margin-bottom:8px"><b>${esc(c.file)}</b>${c.page ? ` · 第 ${esc(String(c.page))} 页 <button class="btn ghost-sm" data-openfile="${esc(c.file)}" data-openpage="${c.page}">📍 查看</button>` : ""}
          <div class="hint">${esc((c.text || "").slice(0, 200))}</div></div>`).join("") : ""}`;
    bindLocSpans(panel);
  } catch (e) {
    panel.innerHTML = `<h3>${esc(id)}</h3><div class="note">⚠ ${esc(e.message)}</div>`;
  }
}

/* ================= 复习（卡片 + 错题） ================= */

let REVIEW_TAB = "cards";     // cards | mistakes
const REVIEW = { mode: "due", active: false, queue: [], idx: 0, hit: 0, miss: 0, flipped: false, browsed: false };

function renderReview() {
  const head = `
  <div class="subtabs no-print">
    <button class="${REVIEW_TAB === "cards" ? "on" : ""}" data-rt="cards">🃏 卡片复习${dueCount() ? `（${dueCount()} 张到期）` : ""}</button>
    <button class="${REVIEW_TAB === "mistakes" ? "on" : ""}" data-rt="mistakes">📝 错题本${(WS.mistakes || []).length ? `（${WS.mistakes.length}）` : ""}</button>
  </div>`;
  $("#view").innerHTML = `<div class="page">${head}<div id="review-body"></div></div>`;
  document.querySelectorAll("[data-rt]").forEach(b =>
    b.onclick = () => { REVIEW_TAB = b.dataset.rt; renderReview(); });
  if (REVIEW_TAB === "cards") renderCardsPanel();
  else renderMistakes();
}

function dueCount() {
  const t = today();
  return (WS.flashcards || []).filter(c => c.due && c.due <= t).length;
}

function renderCardsPanel() {
  const cards = (WS.flashcards || []).slice().sort((a, b) => (a.due || "9").localeCompare(b.due || "9"));
  const t = today();
  const due = cards.filter(c => c.due && c.due <= t);
  const body = $("#review-body");

  if (REVIEW.active) { renderReviewStage(); return; }

  if (!cards.length) {
    body.innerHTML = `<div class="empty">还没有复习卡片。<br>教练在测验后会自动生成间隔重复卡片（1 → 3 → 7 → 14 → 30 → 60 天）。</div>`;
    return;
  }
  const nextDue = cards.find(c => c.due && c.due > t);
  body.innerHTML = `
  <div class="card" style="text-align:center;padding:36px 24px">
    <div style="font-size:20px;font-weight:700">${due.length ? `今天有 ${due.length} 张卡片到期` : "今天没有到期卡片 🎉"}</div>
    <div class="hint" style="margin:8px 0 18px">
      ${due.length ? "先作答再翻面对照，然后自己判定记没记住——系统会据此安排下次复习时间。" :
        (nextDue ? `最近一张 ${esc(nextDue.due)} 到期。` : "")}在途共 ${cards.length} 张。
    </div>
    <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
      ${due.length ? `<button class="btn primary big" id="rv-start">开始复习</button>` : ""}
      <button class="btn big" id="rv-browse">浏览全部卡片</button>
    </div>
  </div>
  <div id="rv-wall"></div>`;
  const s = $("#rv-start");
  if (s) s.onclick = () => {
    REVIEW.active = true; REVIEW.mode = "due";
    REVIEW.queue = due.slice(); REVIEW.idx = 0; REVIEW.hit = 0; REVIEW.miss = 0; REVIEW.flipped = false;
    renderCardsPanel();
  };
  $("#rv-browse").onclick = () => renderWall(cards, t);
}

function renderWall(cards, t) {
  const wall = $("#rv-wall");
  wall.innerHTML = `
  <div class="hint" style="margin:16px 0 8px">全部卡片（点击翻面自测；正式复习判定请用上方「开始复习」）</div>
  <div class="cardwall">${cards.map(c => `
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
    </div>`).join("")}</div>`;
  wall.querySelectorAll(".fcard").forEach(el =>
    el.addEventListener("click", () => el.classList.toggle("flipped")));
}

function renderReviewStage() {
  const body = $("#review-body");
  const total = REVIEW.queue.length;
  if (REVIEW.idx >= total) {
    const acc = REVIEW.hit + REVIEW.miss ? Math.round(REVIEW.hit / (REVIEW.hit + REVIEW.miss) * 100) : 0;
    body.innerHTML = `
    <div class="review-stage">
      <div class="card" style="padding:40px 24px">
        <div style="font-size:34px">${acc >= 80 ? "🎉" : acc >= 50 ? "👍" : "💪"}</div>
        <div style="font-size:20px;font-weight:700;margin:10px 0">本轮复习完成</div>
        <div>共 ${total} 张 · 记住 <b style="color:var(--green)">${REVIEW.hit}</b> · 没记住 <b style="color:var(--red)">${REVIEW.miss}</b> · 记住率 <b>${acc}%</b></div>
        <div class="hint" style="margin:8px 0 18px">记住的卡片已按 1 → 3 → 7 → 14 → 30 → 60 天排期；没记住的明天再来。</div>
        <div style="display:flex;gap:10px;justify-content:center">
          <button class="btn" id="rv-exit">返回</button>
        </div>
      </div>
    </div>`;
    $("#rv-exit").onclick = () => { REVIEW.active = false; renderReview(); };
    return;
  }
  const c = REVIEW.queue[REVIEW.idx];
  body.innerHTML = `
  <div class="review-stage">
    <div class="review-meta">
      <span>第 ${REVIEW.idx + 1} / ${total} 张</span>
      <span>✓ ${REVIEW.hit} · ✗ ${REVIEW.miss}</span>
    </div>
    <div class="rcard${REVIEW.flipped ? " flipped" : ""}" id="rv-card">
      <div class="inner">
        <div class="face front">
          <div class="q">${esc(c.front || c.id)}</div>
          <div class="meta"><span>${esc(c.id)} · ${esc(c.module || "")}</span><span>间隔 ${esc(String(c.interval_days ?? c.interval ?? "—"))} 天</span></div>
          <div class="tap-hint">先在心里作答，点击卡片看答案</div>
        </div>
        <div class="face back">
          <div class="a">${esc(c.back || "（背面见 flashcards.md）")}</div>
          <div class="meta"><span>${esc(c.id)}</span><span>点击翻回</span></div>
        </div>
      </div>
    </div>
    ${REVIEW.flipped ? `
    <div class="review-actions">
      <button class="btn bad" id="rv-miss">✗ 没记住</button>
      <button class="btn good" id="rv-hit">✓ 记住了</button>
    </div>
    <div class="hint" style="margin-top:10px">不确定就诚实选「没记住」——间隔重复靠真实的遗忘信号工作。</div>` : `
    <div class="review-actions">
      <button class="btn" id="rv-skip">⏭ 跳过</button>
    </div>`}
  </div>`;
  const cardEl = $("#rv-card");
  cardEl.onclick = () => { REVIEW.flipped = !REVIEW.flipped; renderReviewStage(); };
  const judge = async (remembered) => {
    try {
      const d = await post("/api/card/review", { ws: WS.name, id: c.id, remembered });
      if (remembered) REVIEW.hit++; else REVIEW.miss++;
      REVIEW.idx++; REVIEW.flipped = false;
      renderReviewStage();
      refreshStateQuiet();
    } catch (e) {
      alert("⚠ " + e.message);
    }
  };
  const hit = $("#rv-hit"); if (hit) hit.onclick = () => judge(true);
  const miss = $("#rv-miss"); if (miss) miss.onclick = () => judge(false);
  const skip = $("#rv-skip"); if (skip) skip.onclick = () => { REVIEW.idx++; REVIEW.flipped = false; renderReviewStage(); };
}

function renderMistakes() {
  const ms = WS.mistakes || [];
  const body = $("#review-body");
  if (!ms.length) {
    body.innerHTML = '<div class="empty">暂无错题记录。<br>测验答错的题会由教练自动记入错题本。</div>';
    return;
  }
  const byReason = {}, byMod = {};
  ms.forEach(m => { byReason[m.reason] = (byReason[m.reason] || 0) + 1; byMod[m.module] = (byMod[m.module] || 0) + 1; });
  const bars = (obj, max) => Object.entries(obj).sort((a, b) => b[1] - a[1]).map(([k, v]) => `
    <div class="dist-row"><span class="label">${esc(k)}</span>
      <span class="bar"><i style="width:${v / max * 100}%"></i></span>
      <span class="num">${v}</span></div>`).join("");
  const rows = ms.slice().reverse().map(m => `
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
  body.innerHTML = `
  <div class="grid cols-2">
    <div class="card"><h3>错因分布</h3>${bars(byReason, Math.max(...Object.values(byReason)))}</div>
    <div class="card"><h3>模块分布</h3>${bars(byMod, Math.max(...Object.values(byMod)))}</div>
  </div>
  <div style="margin-top:14px">${rows}</div>`;
}

/* ================= 资料页 ================= */

let READER = { file: null, page: 1 };
let PENDING_ASK = null;   // { file, page, text } 由资料页生成，教练页渲染后填入输入框

function openReader(file, page) {
  READER = { file: file || null, page: Math.max(1, parseInt(page, 10) || 1) };
  setTab("library");
}

async function renderLibrary() {
  const view = $("#view");
  view.innerHTML = '<div class="empty">读取资料列表…</div>';
  let mats = [], indexed = 0, idxErrors = [];
  try {
    const d = await api("/api/materials?ws=" + encodeURIComponent(WS.name));
    mats = d.files || []; indexed = d.indexed || 0; idxErrors = d.errors || [];
  } catch (e) { /* ignore */ }
  if (!mats.length) {
    view.innerHTML = `<div class="empty">当前主题还没有资料。<br>到「AI 教练」页上传资料（或让教练联网找），这里可以阅读原文并按页定位。</div>`;
    return;
  }
  let file = READER.file;
  if (file && !mats.includes(file)) {
    const stem = (s) => s.replace(/\.[^.]+$/, "").replace(/\s*p\d+$/i, "");
    const hit = mats.find(m => stem(m).includes(stem(file)) || stem(file).includes(stem(m)));
    file = hit || null;
  }
  if (!file) file = mats.find(m => m.toLowerCase().endsWith(".pdf")) || mats[0];
  const isPdf = file.toLowerCase().endsWith(".pdf");
  const base = "/api/material/file?ws=" + encodeURIComponent(WS.name) + "&name=" + encodeURIComponent(file);
  const fileUrl = isPdf ? base : ("/api/material/view?ws=" + encodeURIComponent(WS.name) + "&name=" + encodeURIComponent(file));

  view.innerHTML = `
  <div class="page">
  <div class="library-layout">
    <aside class="card reader-list"><h3>资料文件（${mats.length}）</h3>
      ${mats.map(m => `<div class="rfile-row${m === file ? " on" : ""}">
        <div class="rfile grow" data-file="${esc(m)}">📄 ${esc(m)}</div>
        <button class="del-mat" data-del="${esc(m)}" title="删除这份资料">✕</button>
      </div>`).join("")}
      <div class="hint" style="margin-top:8px">已索引 ${indexed} 段${idxErrors.length ? `<br>⚠ ${esc(idxErrors.map(e => e.file).join("、"))} 解析失败` : ""}</div>
      <div style="margin-top:8px"><button class="btn ghost-sm" id="idx-rebuild">↻ 重建索引</button>
      <span id="idx-status" class="hint"></span></div>
    </aside>
    <section class="card reader-main">
      <div class="reader-bar">
        <b>${esc(file)}</b>
        ${isPdf ? `<span>页 <input id="r-page" type="number" min="1" value="${READER.page || 1}">
          <button id="r-go" class="btn ghost-sm">跳转</button></span>` : ""}
        <button id="r-ask" class="btn primary ghost-ask" title="把当前正在阅读的内容发给教练提问（直接提取文字，无 OCR 误差）">💬 原文提问</button>
        <button id="r-ext" class="btn ghost-sm">↗ 外部打开</button>
      </div>
      <iframe id="r-frame" src="${fileUrl}${isPdf ? "#page=" + (READER.page || 1) : ""}"></iframe>
    </section>
  </div>
  </div>`;
  document.querySelectorAll(".rfile").forEach(el =>
    el.addEventListener("click", () => { READER = { file: el.dataset.file, page: 1 }; renderLibrary(); }));
  document.querySelectorAll(".del-mat").forEach(el =>
    el.addEventListener("click", (ev) => { ev.stopPropagation(); deleteMaterial(el.dataset.del); }));
  const go = $("#r-go");
  if (go) go.onclick = () => {
    const p = Math.max(1, parseInt($("#r-page").value, 10) || 1);
    READER.page = p;
    // 缓存穿透参数必须在 # 之前：仅 fragment 变化时浏览器不会重载文档
    $("#r-frame").src = base + "?t=" + Date.now() + "#page=" + p;
  };
  $("#r-ext").onclick = async () => {
    try {
      const d = await post("/api/material/open", { ws: WS.name, name: file, page: READER.page || 1 });
      if (d.error) alert("⚠ " + d.error);
    } catch (e) { alert("⚠ " + e.message); }
  };
  const rb = $("#idx-rebuild");
  if (rb) rb.onclick = async () => {
    $("#idx-status").textContent = "重建中…";
    try {
      const d = await post("/api/rag/rebuild", { ws: WS.name });
      $("#idx-status").textContent = "已索引 " + d.index.chunks + " 段" +
        ((d.index.errors || []).length ? "；⚠ " + d.index.errors.map(e => esc(e.file)).join("、") + " 解析失败" : "");
    } catch (e) { $("#idx-status").textContent = "⚠ " + e.message; }
  };
  $("#r-ask").onclick = () => askCurrentPage(file, isPdf);
}

/* 删除资料：仅剩的锁死；基础资料红色警告+双重确认；普通资料单次确认 */
async function deleteMaterial(name) {
  let check;
  try {
    check = await post("/api/material/delete-check", { ws: WS.name, name });
  } catch (e) { alert("⚠ " + e.message); return; }
  if (check.blocked) { alert("⛔ " + check.reason); return; }
  let token = "";
  if (check.important) {
    const msg = `「${name}」是学习库的基础资料：\n\n` +
      (check.reasons || []).map(r => "· " + r).join("\n") +
      "\n\n删除后图谱与计划文件仍保留，但它们的原文定位、原文提问将失效。确定删除吗？";
    if (!confirm(msg)) return;
    if (!confirm(`再次确认：真的删除「${name}」？此操作不可恢复。`)) return;
    token = "__DELETE_BASE__";
  } else {
    if (!confirm(`删除资料「${name}」？删除后自动重建索引。`)) return;
  }
  try {
    const d = await post("/api/material/delete", { ws: WS.name, name, confirm: token });
    if (READER.file === name) READER = { file: null, page: 1 };
    await refreshStateQuiet();
    renderLibrary();
  } catch (e) { alert("⚠ " + e.message); }
}

/* 「原文提问」：提取当前阅读位置的文字 → 跳到教练页填入输入框 */
async function askCurrentPage(file, isPdf) {
  const btn = $("#r-ask");
  if (btn) { btn.disabled = true; btn.textContent = "提取中…"; }
  let text = "", page = null;
  try {
    if (isPdf) {
      const d = await api("/api/material/page-text?ws=" + encodeURIComponent(WS.name) +
        "&name=" + encodeURIComponent(file) + "&page=" + (READER.page || 1));
      text = d.text || "";
      page = d.page;
    } else {
      // 同源 HTML 视图：直接取 iframe 可视区的段落文字
      const frame = $("#r-frame");
      try {
        const doc = frame.contentDocument;
        const vw = frame.contentWindow;
        const els = [...doc.body.querySelectorAll("h2,h3,h4,h5,p")];
        const vis = els.filter(el => {
          const r = el.getBoundingClientRect();
          return r.bottom > 70 && r.top < vw.innerHeight - 40 && el.textContent.trim();
        });
        text = (vis.length ? vis : els.slice(0, 14)).map(el => el.textContent.trim()).join("\n");
      } catch (e) { /* 拿不到 iframe 内容时走服务端整篇提取 */ }
      if (!text.trim()) {
        const d = await api("/api/material/page-text?ws=" + encodeURIComponent(WS.name) +
          "&name=" + encodeURIComponent(file) + "&page=1");
        text = d.text || "";
      }
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "💬 原文提问"; }
    alert("⚠ 提取内容失败：" + e.message);
    return;
  }
  text = String(text || "").trim().slice(0, 1200);
  if (!text) {
    if (btn) { btn.disabled = false; btn.textContent = "💬 原文提问"; }
    alert("当前页面没有可提取的文字（可能是扫描版）。");
    return;
  }
  PENDING_ASK = {
    text,
    head: `【原文提问】\n> 当前阅读：《${file}》${page ? " p" + page : ""}\n> 内容摘录：\n`,
  };
  setTab("coach");
}

/* ================= 设置 ================= */

async function renderSettings() {
  const cfg = (DATA && DATA.config) || {};
  $("#view").innerHTML = `
  <div class="page">
  <div class="card form" style="max-width:640px;margin:0 auto 14px">
    <h3>内置 AI（智谱开放平台）</h3>
    <div class="field"><label>API Key</label>
      <input id="s-key" type="password" placeholder="${cfg.has_key ? "已配置（" + esc(cfg.key_hint) + "），留空则不修改" : "粘贴智谱 API Key（open.bigmodel.cn 控制台获取）"}">
      <div class="hint">仅保存在本机 learn-coach\\config.json，不上传、不随工作区同步</div></div>
    <div class="field"><label>API 地址（OpenAI 兼容）</label>
      <input id="s-url" value="${esc(cfg.base_url || "")}" placeholder="https://open.bigmodel.cn/api/paas/v4"></div>
    <div class="field"><label>模型</label>
      <input id="s-model" value="${esc(cfg.model || "glm-4.7")}" placeholder="glm-4.7 / glm-4.6 …">
      <div class="hint">若报「模型不存在」，换成你账号可用的模型名</div></div>
    <div class="actions">
      <button id="s-save" class="btn primary">保存</button>
      <button id="s-test" class="btn">测试连接</button>
    </div>
    <span class="result" id="s-result"></span>
  </div>

  <div class="card form" style="max-width:640px;margin:0 auto 14px">
    <h3>主题管理</h3>
    ${WS ? `<div class="field" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>当前主题：<b>${esc(WS.name)}</b></span>
      <button class="btn danger" id="t-archive">📦 归档当前主题</button>
      <div class="hint" style="width:100%">归档 = 移入 workspaces\\_archives，可随时恢复；彻底删除在归档列表中操作。</div>
    </div>` : ""}
    <div class="field">
      <label>从文件夹导入主题（恢复备份）</label>
      <input id="imp-dir" type="file" webkitdirectory directory multiple style="display:none">
      <div><button id="imp-btn" class="btn">📂 选择主题文件夹…</button> <span id="imp-status" class="hint"></span></div>
    </div>
    <div class="field"><label>已归档主题</label><div id="archive-list" class="hint">读取中…</div></div>
  </div>

  <div class="card form" style="max-width:640px;margin:0 auto">
    <h3>关于</h3>
    <div class="hint">learn-coach 学习系统 · 数据全部保存在本机：<br>
    学习数据 learn-coach\\workspaces\\ · API Key learn-coach\\config.json<br>
    引擎规则 SKILL.md · 工作区规范 WORKSPACE-SPEC.md</div>
  </div>
  </div>`;

  $("#s-save").onclick = async () => {
    const body = { base_url: $("#s-url").value.trim(), model: $("#s-model").value.trim() };
    if ($("#s-key").value.trim()) body.api_key = $("#s-key").value.trim();
    try {
      const data = await post("/api/config", body);
      $("#s-result").textContent = "已保存（Key：" + (data.has_key ? data.key_hint : "未设置") + "）";
      if (DATA) DATA.config = data;
    } catch (e) { $("#s-result").textContent = "⚠ " + e.message; }
  };
  $("#s-test").onclick = async () => {
    $("#s-result").textContent = "测试中…";
    try {
      const data = await post("/api/config/test", {});
      $("#s-result").textContent = "✅ 连接正常，模型回复：" + data.echo;
    } catch (e) { $("#s-result").textContent = "⚠ " + e.message; }
  };

  const ta = $("#t-archive");
  if (ta) ta.onclick = async () => {
    if (!WS) return;
    if (!confirm(`确定归档主题「${WS.name}」吗？\n\n学习数据将移入 workspaces\\_archives，可随时在设置页恢复；不影响已归档的其他主题。`)) return;
    try {
      const d = await post("/api/ws/archive", { ws: WS.name });
      await load();
      alert(`已归档「${d.ws}」（${d.files} 个文件）。可在设置页恢复。`);
    } catch (e) { alert("归档失败：" + e.message); }
  };

  renderArchives();

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
      const d = await post("/api/ws/import", { name: rootName, files: encoded });
      impStatus.textContent = "✅ 已导入 " + d.files + " 个文件（索引 " + d.index.chunks + " 段）";
      impDir.value = "";
      await load(d.ws);
      renderSettings();
    } catch (e) {
      impStatus.textContent = "⚠ " + e.message;
    }
  };
}

async function renderArchives() {
  const el = $("#archive-list");
  if (!el) return;
  try {
    const d = await api("/api/archives");
    const list = d.archives || [];
    el.innerHTML = list.length ? list.map(a => `
      <div style="display:flex;align-items:center;gap:8px;padding:6px 0;flex-wrap:wrap;border-bottom:1px dashed var(--line)">
        <span>📦 <b>${esc(a.name)}</b> <span class="hint">${a.files} 文件 · ${esc(a.archived_at)}</span></span>
        <span style="margin-left:auto;display:flex;gap:6px">
          <button class="btn ghost-sm" data-restore="${esc(a.name)}">恢复</button>
          <button class="btn ghost-sm" data-purge="${esc(a.name)}" style="color:var(--red)">彻底删除</button>
        </span>
      </div>`).join("") : "（无归档）";
    el.querySelectorAll("[data-restore]").forEach(b => b.onclick = async () => {
      try {
        const d = await post("/api/ws/restore", { src: b.dataset.restore });
        await load(d.ws);
        renderSettings();
      } catch (e) { alert("恢复失败：" + e.message); }
    });
    el.querySelectorAll("[data-purge]").forEach(b => b.onclick = async () => {
      if (!confirm(`彻底删除归档「${b.dataset.purge}」？\n\n所有学习数据与资料将被永久删除，不可恢复。`)) return;
      if (!confirm(`最后确认：真的永久删除「${b.dataset.purge}」？`)) return;
      try {
        await post("/api/ws/purge", { src: b.dataset.purge, confirm: "__PURGE__" });
        renderArchives();
      } catch (e) { alert("删除失败：" + e.message); }
    });
  } catch (e) {
    el.textContent = "⚠ " + e.message;
  }
}

/* ================= 新主题弹层 ================= */

function closeModal() {
  const root = $("#modal-root");
  root.innerHTML = "";
}

function openNewTopicModal() {
  const root = $("#modal-root");
  root.innerHTML = `
  <div class="modal-mask" id="modal-mask">
    <div class="modal">
      <button class="close-x" id="m-close">✕</button>
      <h2>新建学习主题</h2>
      <div class="form">
        <div class="field"><label>学习主题 *</label>
          <input id="n-topic" placeholder="如：黄帝内经 / 中级会计实务 / Python 数据分析">
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
          <div class="hint">不上传也行——创建后到「AI 教练」页说"帮我找资料"，教练会联网检索候选清单供你确认。</div></div>
        <div class="actions">
          <button id="n-create" class="btn primary">创建并开始</button>
        </div>
        <span class="result" id="n-result"></span>
      </div>
    </div>
  </div>`;
  $("#m-close").onclick = closeModal;
  $("#modal-mask").addEventListener("click", (ev) => {
    if (ev.target === $("#modal-mask")) closeModal();
  });
  $("#n-create").onclick = async () => {
    const topic = $("#n-topic").value.trim();
    const result = $("#n-result");
    if (!topic) { result.textContent = "⚠ 请填写学习主题"; return; }
    result.textContent = "创建中…";
    try {
      let d = await post("/api/ws/create", {
        topic, goal: $("#n-goal").value.trim(), grade: $("#n-grade").value,
        stage: $("#n-stage").value, domain: $("#n-domain").value.trim(),
        period_days: $("#n-days").value, daily_hours: $("#n-daily").value,
      });
      const wsName = d.ws;
      const files = [...$("#n-files").files];
      if (files.length) {
        result.textContent = "上传资料中…";
        d = await post("/api/upload", { ws: wsName, files: await readFilesB64(files) });
        const errs = (d.index.errors || []).map(e => `${e.file}：${e.error}`).join("；");
        result.textContent = "已上传 " + d.saved.length + " 份，索引 " + d.index.chunks + " 段" + (errs ? "；⚠ " + errs : "");
      }
      closeModal();
      await load(wsName);
      setTab("coach");
      if (files.length) startPipeline();
    } catch (e) {
      result.textContent = "⚠ " + e.message;
    }
  };
  $("#n-topic").focus();
}

/* ================= 渲染入口与全局事件 ================= */

function render() {
  if (!WS && TAB !== "settings") {
    if (TAB !== "home") TAB = "home";
  }
  ({ home: renderHome, coach: renderCoach, graph: renderGraph,
     review: renderReview, library: renderLibrary, settings: renderSettings })[TAB]();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#nav").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-tab]");
    if (btn) setTab(btn.dataset.tab);
  });
  $("#new-topic").addEventListener("click", openNewTopicModal);
  // 手动刷新：重新读取工作区数据并重绘当前页；保留教练页未发送的输入与复习进度尽量不丢
  $("#refresh").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.classList.add("spinning");
    const keepInput = TAB === "coach" ? ($("#chat-box")?.value || "") : "";
    const keepReview = TAB === "review" ? JSON.parse(JSON.stringify(REVIEW)) : null;
    try {
      await load(WS && WS.name);
      if (keepInput && TAB === "coach") {
        const box = $("#chat-box");
        if (box) { box.value = keepInput; }
      }
      if (keepReview && TAB === "review") {
        // 队列里的卡片可能已变化，只恢复浏览态，不复原过期的复习队列
        if (!keepReview.active) Object.assign(REVIEW, keepReview);
      }
    } finally {
      setTimeout(() => btn.classList.remove("spinning"), 400);
    }
  });
  $("#ws-select").addEventListener("change", (ev) => {
    WS = DATA.workspaces.find(w => w.name === ev.target.value);
    REVIEW.active = false;
    render();
  });
  $("#view").addEventListener("click", (ev) => {
    if (Date.now() < GRAPH.suppressUntil && ev.target.closest("svg.kmap")) return;
    const node = ev.target.closest("svg.kmap .node");
    if (node) { showNode(node.dataset.id); return; }
    const chip = ev.target.closest("[data-kid]");
    if (chip) { showNode(chip.dataset.kid); return; }
    const loc = ev.target.closest(".loc, [data-openfile]");
    if (loc && (loc.dataset.file || loc.dataset.openfile)) {
      openReader(String(loc.dataset.file || loc.dataset.openfile).replace(/^《|》$/g, ""),
        loc.dataset.openpage || loc.dataset.page);
    }
  });
  load();
});
