#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""learn-coach 学习系统 · 本地服务器

两个角色：
1. 仪表盘（只读）：解析 workspaces\\ 状态文件，渲染进度/图谱/卡片/错题/周报
2. AI 教练（读写）：网页端聊天学习——立项、资料上传(RAG-lite 检索)、教学测验、
   通过 action 协议把进度/错题/卡片写回工作区文件（与教练窗口共用同一套文件协议）

- 仅绑定 127.0.0.1；API Key 保存在本地 dashboard\\config.json
- 需要 Python 3.8+，零第三方依赖
"""
import base64
import datetime as _dt
import html as _html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if getattr(sys, "frozen", False):
    # PyInstaller 打包形态：静态资源在临时解包目录，数据（workspaces/config）放 exe 旁边
    DASH_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "dashboard"
    ROOT = Path(sys.executable).resolve().parent
else:
    DASH_DIR = Path(__file__).resolve().parent
    ROOT = DASH_DIR.parent            # learn-coach\
CONFIG_PATH = ROOT / "config.json"
WS_ROOT = ROOT / "workspaces"
NO_BROWSER = "--no-browser" in sys.argv

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7"
MAX_UPLOAD = 60 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
DOCX_SUFFIX = ".docx"
PDF_SUFFIX = ".pdf"

today = lambda: _dt.date.today().isoformat()


class CoachError(Exception):
    pass


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ================= 工作区文件解析（仪表盘用） =================

def parse_progress(d: Path):
    p = d / "progress.json"
    if not p.exists():
        return None
    try:
        return json.loads(read_text(p))
    except Exception as exc:
        return {"_error": "progress.json 解析失败: %s" % exc}


def _paren(s: str) -> str:
    m = re.search(r"[（(]([^）)]*)[）)]", s)
    return m.group(1).strip() if m else ""


def _norm_node_status(cell: str):
    s = cell.strip()
    if s.startswith("已掌握"):
        return "mastered"
    if s.startswith("疑似"):
        return "suspected"
    return "unlearned"


NODE_ID = r"(?:K\d+|M\d+-\d+)"   # 知识点编号：K1 或 M1-01 两种写法


def parse_kmap(d: Path):
    txt = read_text(d / "knowledge-map.md")
    nodes, edges, internal = [], [], []
    mode, etype = "nodes", "pre"
    header = None   # 表头列名 → 列索引（自适应任意列数/列序）
    for line in txt.splitlines():
        h = re.match(r"^#{2,4}\s*(.+?)\s*$", line)
        if h:
            head = h.group(1)
            if "边清单" in head or "边" == head.strip():
                mode = "edges"
            elif mode == "edges":
                if "前置" in head:
                    etype = "pre"
                elif "关联" in head:
                    etype = "rel"
                elif "易混" in head:
                    etype = "conf"
            continue
        if mode == "nodes":
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                if "编号" in line and "名称" in line:
                    header = {}
                    for i, c in enumerate(cells):
                        cl = c.lower()
                        if "编号" in c:
                            header["id"] = i
                        elif "名称" in c:
                            header["name"] = i
                        elif "类型" in c:
                            header["type"] = i
                        elif "出处" in c or "来源" in c:
                            header["source"] = i
                        elif "bloom" in cl:
                            header["bloom"] = i
                        elif "模块" in c:
                            header["module"] = i
                        elif "状态" in c:
                            header["status"] = i
                continue
            if "id" not in header or header["id"] >= len(cells):
                continue
            if not re.match(r"^%s$" % NODE_ID, cells[header["id"]]):
                continue

            def cell(key):
                i = header.get(key)
                return cells[i] if i is not None and i < len(cells) else ""

            kid = cells[header["id"]]
            mod = cell("module")
            if not re.match(r"^M\d+$", mod):
                mm = re.match(r"(M\d+)", kid)
                mod = mm.group(1) if mm else ""
            nodes.append({"id": kid, "name": cell("name"), "type": cell("type"),
                          "source": cell("source"), "bloom": cell("bloom"),
                          "module": mod,
                          "status": _norm_node_status(cell("status")),
                          "note": _paren(cell("status"))})
        else:
            if not re.search(r"K\d|M\d", line) or line.lstrip().startswith(("|", ">")):
                continue
            for seg in re.split(r"[；;]", line):
                clean = re.sub(r"[（(][^）)]*[）)]", "", seg)
                tokens = re.findall(r"%s(?:\s*/\s*%s)*|→|↔" % (NODE_ID, NODE_ID), clean)
                groups, ops = [], []
                for t in tokens:
                    if t in ("→", "↔"):
                        ops.append(t)
                    else:
                        groups.append(re.findall(r"K\d+|M\d+-\d+", t))
                seg_edges = []
                for i, _op in enumerate(ops):
                    if i + 1 < len(groups) and groups[i] and groups[i + 1]:
                        for a in groups[i]:
                            for b in groups[i + 1]:
                                seg_edges.append({"from": a, "to": b, "type": etype})
                edges.extend(seg_edges)
                # 无有效边且只含一个编号 → 内部易混标记（如 "K08 内部·响应↔恢复"）
                if not seg_edges and len(groups) == 1 and len(groups[0]) == 1:
                    internal.append({"id": groups[0][0], "type": etype, "note": seg.strip()})
    seen, uniq = set(), []
    for e in edges:
        key = (e["from"], e["to"], e["type"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return {"nodes": nodes, "edges": uniq, "internal": internal}


def _norm_reason(cell: str) -> str:
    for key in ("概念不清", "混淆", "粗心", "超纲"):
        if key in cell:
            return key
    return "其他"


def parse_mistakes(d: Path):
    rows = []
    for line in read_text(d / "mistakes.md").splitlines():
        m = re.match(r"^\|\s*(E\d+)\s*\|(.+)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 8 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        rows.append({"id": m.group(1), "date": cells[0], "module": cells[1], "point": cells[2],
                     "question": cells[3], "my": cells[4], "correct": cells[5],
                     "reason": _norm_reason(cells[6]), "reason_raw": cells[6], "variant": cells[7]})
    return rows


def parse_flashcards_md(d: Path):
    out = {}
    for line in read_text(d / "flashcards.md").splitlines():
        m = re.match(r"^\|\s*(F\d+)\s*\|(.+)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) >= 6:
            out[m.group(1)] = {"front": cells[0], "back": cells[1], "module": cells[2],
                               "interval": cells[3], "due": cells[4], "status": cells[5]}
    return out


def parse_plan(d: Path):
    txt = read_text(d / "plan.md")
    meta = {}
    for key, pat in (
        ("goal", r"目标等级[：:]?\s*([^　\s（()）｜|，,。]+)"),
        ("deadline", r"截止[：:]?\s*(\d{4}-\d{2}-\d{2})"),
        ("daily_hours", r"每日[投入]?\s*[：:]?\s*([\d.]+)\s*h"),
        ("total_hours", r"总预算\s*[：:]?\s*([\d.]+)\s*h"),
    ):
        m = re.search(pat, txt)
        meta[key] = m.group(1) if m else ""
    modules, cur = [], None
    for line in txt.splitlines():
        m = re.match(r"^###\s+(M\d+)\s+(.+?)（([\d.]+)\s*h）", line)
        if m:
            cur = {"id": m.group(1), "name": m.group(2).strip(),
                   "hours": float(m.group(3)), "deps": [], "goals": "", "pass": ""}
            modules.append(cur)
            continue
        if cur is None:
            continue
        dm = re.match(r"^-\s*依赖：(.+)$", line)
        if dm:
            cur["deps"] = re.findall(r"M\d+", dm.group(1))
        gm = re.match(r"^-\s*学习目标：(.+)$", line)
        if gm:
            cur["goals"] = gm.group(1).strip()
        pm = re.match(r"^-\s*过关标准：(.+)$", line)
        if pm:
            cur["pass"] = pm.group(1).strip()
    if not modules:
        # 表格形式的模块安排（表头驱动，兼容 | M1 名称 | 目标 | 映射 | 时长 | 依赖 | 过关线 | 及变体）
        phead = None
        for line in txt.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if phead is None:
                if "模块" in line and ("时长" in line or "学时" in line):
                    phead = {}
                    for i, c in enumerate(cells):
                        if "模块" in c:
                            phead["id"] = i
                        elif "目标" in c:
                            phead["goals"] = i
                        elif "时长" in c or "学时" in c:
                            phead["hours"] = i
                        elif "依赖" in c or "先修" in c:
                            phead["deps"] = i
                        elif "过关" in c:
                            phead["pass"] = i
                        elif "名称" in c:
                            phead["name"] = i
                continue
            if "id" not in phead or phead["id"] >= len(cells):
                continue
            m0 = re.match(r"^(M\d+)[\s.、·\-—：:]*(.*)$", cells[phead["id"]])
            if not m0:
                continue

            def pc(key):
                i = phead.get(key)
                return cells[i] if i is not None and i < len(cells) else ""

            hm = re.search(r"([\d.]+)\s*h", pc("hours"))
            try:
                hours = float(hm.group(1)) if hm else 0.0
            except ValueError:
                hours = 0.0
            modules.append({"id": m0.group(1), "name": pc("name") or m0.group(2),
                            "hours": hours, "deps": re.findall(r"M\d+", pc("deps")),
                            "goals": pc("goals"), "pass": pc("pass")})
    return {"meta": meta, "modules": modules}


def parse_profile(d: Path):
    txt = read_text(d / "profile.md")
    table = {}
    for line in txt.splitlines():
        m = re.match(r"^\|\s*([^|#\-\s][^|]*)\|\s*([^|]*)\|", line)
        if m:
            key = m.group(1).strip()
            if key and key != "项":
                table[key] = m.group(2).strip()
    timeline = []
    for line in txt.splitlines():
        dm = re.match(r"^-\s+(\d{4}-\d{2}-\d{2})\s+(.+)$", line)
        if dm:
            timeline.append({"date": dm.group(1), "text": dm.group(2).strip()})
    return {"table": table, "timeline": timeline}


def scan_workspace(d: Path):
    progress = parse_progress(d)
    plan = parse_plan(d)
    # 模块归一化：title→name、planned/pending→not-started、缺失的 name/hours 从计划表回填
    if isinstance(progress, dict) and isinstance(progress.get("modules"), list):
        pm_by_id = {m["id"]: m for m in plan["modules"] if isinstance(m, dict) and m.get("id")}
        for m in progress["modules"]:
            if not m.get("name") and m.get("title"):
                m["name"] = m["title"]
            if not m.get("name") and m.get("id") in pm_by_id:
                m["name"] = pm_by_id[m["id"]].get("name", "")
                if not m.get("hours"):
                    m["hours"] = pm_by_id[m["id"]].get("hours")
            if m.get("status") in ("planned", "pending"):
                m["status"] = "not-started"
    fc_md = parse_flashcards_md(d)
    cards = []
    if isinstance(progress, dict) and isinstance(progress.get("flashcards"), list):
        for c in progress["flashcards"]:
            card = dict(c)
            md = fc_md.get(c.get("id"), {})
            card["back"] = md.get("back", "")
            card.setdefault("due", md.get("due", ""))
            cards.append(card)
    known = {c.get("id") for c in cards}
    for cid, md in fc_md.items():
        if cid not in known:
            cards.append(dict(md, id=cid))
    return {"name": d.name, "progress": progress, "kmap": parse_kmap(d),
            "mistakes": parse_mistakes(d), "flashcards": cards,
            "plan": plan, "profile": parse_profile(d)}


# ================= 配置（API Key 本地保存） =================

def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(read_text(CONFIG_PATH))
        except Exception:
            cfg = {}
    cfg.setdefault("base_url", DEFAULT_BASE_URL)
    cfg.setdefault("model", DEFAULT_MODEL)
    return cfg


def save_config(patch: dict):
    cfg = load_config()
    for k in ("api_key", "base_url", "model"):
        if patch.get(k):
            cfg[k] = str(patch[k]).strip()
    write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def masked_config():
    cfg = load_config()
    key = cfg.get("api_key", "")
    return {"has_key": bool(key), "key_hint": (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("已设置" if key else ""),
            "base_url": cfg.get("base_url", DEFAULT_BASE_URL),
            "model": cfg.get("model", DEFAULT_MODEL)}


# ================= 网页立项（新工作区脚手架） =================

def ws_dir(name: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    if not safe or safe in (".", "..") or ".." in safe:
        raise CoachError("非法的工作区名称")
    return WS_ROOT / safe


SAFFIX_NOTE = "> 本文件由网页立项生成，后续由 AI 教练维护。"


def create_workspace(form: dict) -> str:
    name = (form.get("topic") or "").strip()
    if not name:
        raise CoachError("请填写学习主题")
    d = ws_dir(name)
    if d.exists():
        raise CoachError("工作区已存在：%s" % d.name)
    goal = form.get("goal", "")
    grade = form.get("grade", "及格")
    days = form.get("period_days", "28")
    daily = form.get("daily_hours", "2")
    stage = form.get("stage", "成人")
    domain = form.get("domain", "")
    deadline = ""
    try:
        deadline = (_dt.date.today() + _dt.timedelta(days=int(days))).isoformat()
    except Exception:
        pass
    for sub in ("materials", "notes", "quiz", "exam", "assets"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    profile = ["# 学员档案 — %s" % d.name, "",
               "| 项 | 值 | 状态 |", "| --- | --- | --- |",
               "| 学习主题 | %s | 已确认 |" % (name + ("（目标：%s）" % goal if goal else "")),
               "| 学段 / 领域 / 发起人 | %s / %s / 本人学习 | 已确认 |" % (stage, domain or "待定"),
               "| 目标等级 | %s | 已确认 |" % grade,
               "| 截止时间 | %s（%s 天） | 已确认 |" % (deadline, days),
               "| 每日投入 | %s h | 已确认 |" % daily,
               "| 资料位置 | 网页上传 / AI 检索（待确认） | 待定 |", "",
               "## 确认记录", "",
               "- %s 网页立项：工作区创建，等待 AI 教练完成资料协商与计划。" % today(), SAFFIX_NOTE, ""]
    write_text(d / "profile.md", "\n".join(profile))
    progress = {
        "topic": d.name,
        "profile": {"stage": stage, "domain": domain, "initiator": "本人"},
        "engine_version": 2, "web_client": True,
        "status": "learning", "phase": 0,
        "phase_label": "0a-资料协商（网页立项，资料待就绪）",
        "next_action": "AI 教练完成资料确认 → 消化 → 诊断 → 计划",
        "current_module": None, "modules": [], "flashcards": [], "exams": [],
        "plan_locked": False, "updated_at": today(),
    }
    write_text(d / "progress.json", json.dumps(progress, ensure_ascii=False, indent=2))
    write_text(d / "plan.md", "# 学习计划 — %s\n\n> 状态：**待规划**（AI 教练将在资料就绪后生成）\n%s\n" % (d.name, SAFFIX_NOTE))
    write_text(d / "material-index.md",
               "# 资料索引 — %s\n\n| 编号 | 名称 | 类型 | 权威等级 | 说明 |\n| --- | --- | --- | --- | --- |\n%s\n" % (d.name, SAFFIX_NOTE))
    write_text(d / "mistakes.md",
               "# 错题本 — %s\n\n| # | 日期 | 模块 | 关联知识点 | 题目摘要 | 我的答案 | 正确答案 | 错因 | 变式练习状态 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n" % d.name)
    write_text(d / "flashcards.md",
               "# 间隔重复卡片 — %s\n\n| 卡片ID | 正面（问题） | 背面（答案） | 模块 | 当前间隔(天) | 到期日 | 状态 |\n| --- | --- | --- | --- | --- | --- | --- |\n" % d.name)
    write_text(d / "knowledge-map.md", "# 知识图谱 — %s\n\n> 待消化管道生成。\n%s\n" % (d.name, SAFFIX_NOTE))
    return d.name


# ================= 资料上传 + RAG-lite =================

def pdf_text(data: bytes) -> str:
    """提取 PDF 文字层：pdfminer.six 优先，pypdf 兜底。扫描件无文字层会明确报错。"""
    text, errs = "", []
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(io.BytesIO(data))
    except ImportError:
        errs.append("pdfminer未安装")
    except Exception as exc:
        errs.append("pdfminer:" + str(exc)[:80])
    if not text.strip():
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except ImportError:
            errs.append("pypdf未安装")
        except Exception as exc:
            errs.append("pypdf:" + str(exc)[:80])
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text.strip()) < 10:
        raise CoachError("PDF 中几乎提取不到文字（%s）——可能是扫描件/图片型 PDF（无文字层），请提供文字版或转存为 txt 后上传"
                         % "；".join(errs)[:120])
    return text


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 60 * 1024 * 1024:
            raise CoachError("docx 内文过大（解压后 >60MB），已拒绝读取")
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return _html.unescape(text)


def save_upload(ws: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_UPLOAD:
        raise CoachError("文件过大（>15MB）：%s" % filename)
    fname = re.sub(r'[\\/:*?"<>|]', "_", Path(filename).name)
    suffix = Path(fname).suffix.lower()
    if suffix not in TEXT_SUFFIXES | {DOCX_SUFFIX, PDF_SUFFIX}:
        raise CoachError("暂支持 .md / .txt / .docx / .pdf（扫描版 PDF 无法提取文字）；收到：%s" % suffix)
    d = ws_dir(ws)
    (d / "materials").mkdir(parents=True, exist_ok=True)
    (d / "materials" / fname).write_bytes(data)
    return {"file": fname, "bytes": len(data)}


def material_text(path: Path) -> str:
    if path.suffix.lower() == DOCX_SUFFIX:
        try:
            return docx_text(path.read_bytes())
        except CoachError:
            raise
        except Exception:
            return ""
    if path.suffix.lower() == PDF_SUFFIX:
        return pdf_text(path.read_bytes())
    return read_text(path)


def build_rag_index(ws: str) -> dict:
    d = ws_dir(ws)
    docs = []
    for p in sorted((d / "materials").glob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES | {DOCX_SUFFIX, PDF_SUFFIX}:
            text = material_text(p)
            if text.strip():
                docs.append({"file": p.name, "text": text})
    chunks = []
    for doc in docs:
        paras, buf = [], ""
        for line in doc["text"].splitlines():
            buf += line + "\n"
            if len(buf) >= 500:
                paras.append(buf)
                buf = ""
        if buf.strip():
            paras.append(buf)
        for i, para in enumerate(paras):
            head = para.strip().splitlines()[0][:40] if para.strip() else ""
            chunks.append({"file": doc["file"], "seq": i, "head": head, "text": para.strip()[:800]})
    write_text(d / "rag-index.json", json.dumps(
        {"built": today(), "files": [x["file"] for x in docs], "chunks": chunks}, ensure_ascii=False))
    return {"files": [x["file"] for x in docs], "chunks": len(chunks)}


def rag_overview(ws: str, budget: int = 12000) -> list:
    """资料概览：按文件顺序取开头段落，供教练整体消化分析。"""
    d = ws_dir(ws)
    idx_path = d / "rag-index.json"
    if not idx_path.exists():
        build_rag_index(ws)
    idx = _load_json(idx_path, {})
    by_file = {}
    for c in idx.get("chunks", []):
        by_file.setdefault(c["file"], []).append(c)
    out, used = [], 0
    for fname, chunks in by_file.items():
        for c in chunks[:8]:
            text = c["text"][:600]
            out.append({"file": fname, "seq": c.get("seq"), "text": text})
            used += len(text)
            if used >= budget:
                return out
    return out


# ================= 分段消化（全书逐段读入） =================

SEGMENT_SIZE = 15   # 每段注入的资料块数（约 9k 字）


def digest_state_path(ws: str) -> Path:
    return ws_dir(ws) / "digest-state.json"


def rag_segment(ws: str, size: int = SEGMENT_SIZE) -> dict:
    """取下一批资料段（按全书顺序），带进度元信息；不推进游标，推进由 advance_digest 动作完成。"""
    d = ws_dir(ws)
    idx_path = d / "rag-index.json"
    if not idx_path.exists():
        build_rag_index(ws)
    idx = _load_json(idx_path, {})
    chunks = idx.get("chunks", [])
    st = _load_json(digest_state_path(ws), {"cursor": 0})
    cursor = min(int(st.get("cursor", 0)), len(chunks))
    seg = chunks[cursor:cursor + size]
    return {
        "segment_no": cursor // size + 1,
        "segments_total": max(1, (len(chunks) + size - 1) // size),
        "chunk_cursor": cursor,
        "next_cursor": min(cursor + size, len(chunks)),
        "total_chunks": len(chunks),
        "done": cursor >= len(chunks),
        "chunks": [{"file": c["file"], "seq": c.get("seq"), "text": c["text"]} for c in seg],
    }


def _terms(q: str):
    words = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}", q)
    grams = [q[i:i + 2] for i in range(len(q) - 1) if not re.match(r"[\s，。？！；：、]", q[i:i + 2])]
    return set(words) | set(grams)


def rag_query(ws: str, question: str, top: int = 4) -> list:
    d = ws_dir(ws)
    idx_path = d / "rag-index.json"
    if not idx_path.exists():
        build_rag_index(ws)
    try:
        idx = json.loads(read_text(idx_path))
    except Exception:
        return []
    terms = _terms(question)
    if not terms:
        return []
    scored = []
    for c in idx.get("chunks", []):
        text = c["text"]
        score = sum(1 for t in terms if t in text)
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [{"file": c["file"], "head": c["head"], "text": c["text"],
             "score": s} for s, c in scored[:top]]


# ================= action 协议（AI 写回工作区） =================

ACTION_RE = re.compile(r"<action\s+name=\"([\w_]+)\">\s*(.*?)\s*</action>", re.DOTALL)


def _load_json(path: Path, default):
    try:
        return json.loads(read_text(path))
    except Exception:
        return default


def _deep_merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list) and v and isinstance(v[0], dict) and "id" in v[0]:
            by_id = {m.get("id"): m for m in dst[k]}
            for item in v:
                if item.get("id") in by_id:
                    _deep_merge(by_id[item["id"]], item)
                else:
                    dst[k].append(item)
        else:
            dst[k] = v


def apply_action(ws: str, name: str, payload: dict) -> str:
    d = ws_dir(ws)
    if name == "update_progress":
        prog = _load_json(d / "progress.json", {})
        _deep_merge(prog, payload.get("patch", {}))
        prog["updated_at"] = today()
        write_text(d / "progress.json", json.dumps(prog, ensure_ascii=False, indent=2))
        return "progress.json 已更新"
    if name == "add_flashcards":
        prog = _load_json(d / "progress.json", {})
        cards = payload.get("cards", [])
        exist = {c.get("id") for c in prog.get("flashcards", [])}
        lines = []
        for c in cards:
            if c.get("id") in exist:
                continue
            prog.setdefault("flashcards", []).append(c)
            lines.append("| %s | %s | %s | %s | %s | %s | 在途 |" % (
                c.get("id", ""), c.get("front", "").replace("|", "\\|"),
                c.get("back", "").replace("|", "\\|"), c.get("module", ""),
                c.get("interval_days", 1), c.get("due", "")))
        if lines:
            with open(d / "flashcards.md", "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        prog["updated_at"] = today()
        write_text(d / "progress.json", json.dumps(prog, ensure_ascii=False, indent=2))
        return "新增 %d 张卡片" % len(lines)
    if name == "add_mistake":
        r = payload.get("row", {})
        line = "| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("id", ""), r.get("date", today()), r.get("module", ""),
            r.get("point", "").replace("|", "\\|"), r.get("question", "").replace("|", "\\|"),
            r.get("my", "").replace("|", "\\|"), r.get("correct", "").replace("|", "\\|"),
            r.get("reason", ""), r.get("variant", "待出"))
        with open(d / "mistakes.md", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return "错题 %s 已入账" % r.get("id", "")
    if name == "write_note":
        mod = re.sub(r"[^\w-]", "", payload.get("module", "M0")) or "M0"
        note_path = d / "notes" / ("%s-教学笔记.md" % mod)
        head = "\n\n## %s 补充\n" % today()
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(head + payload.get("content", "").strip() + "\n")
        return "笔记 %s 已写入" % note_path.name
    if name == "write_kmap":
        content = str(payload.get("content", "")).strip()
        if not content:
            raise CoachError("write_kmap 内容为空")
        write_text(d / "knowledge-map.md", content)
        return "knowledge-map.md 已生成（图谱页可见）"
    if name == "write_plan":
        content = str(payload.get("content", "")).strip()
        if not content:
            raise CoachError("write_plan 内容为空")
        write_text(d / "plan.md", content)
        return "plan.md 已生成（学习进度页可见）"
    if name == "advance_digest":
        raw_to = payload.get("to", 0) if isinstance(payload, dict) else 0
        try:
            to = int(str(raw_to).strip().strip("\"'"))
        except (TypeError, ValueError):
            to = 0
        st = _load_json(digest_state_path(ws), {"cursor": 0})
        cur = int(st.get("cursor", 0))
        idx = _load_json(ws_dir(ws) / "rag-index.json", {})
        total = len(idx.get("chunks", []))
        to = max(cur, min(to, min(total, cur + SEGMENT_SIZE * 2)))
        st["cursor"] = to
        write_text(digest_state_path(ws), json.dumps(st, ensure_ascii=False))
        return "消化进度推进到 %d/%d 段" % (to, total)
    if name == "append_profile":
        text = payload.get("text", "").strip()
        if text and not text.startswith("- "):
            text = "- " + text
        with open(d / "profile.md", "a", encoding="utf-8") as f:
            f.write(text + "\n")
        return "档案已追加确认记录"
    raise CoachError("未知 action：%s" % name)


def extract_actions(reply: str):
    actions = []
    clean = ACTION_RE.sub("", reply).rstrip()
    for name, body in ACTION_RE.findall(reply):
        try:
            payload = json.loads(body)
        except Exception as exc:
            actions.append({"name": name, "ok": False, "result": "JSON 解析失败: %s" % exc})
            continue
        actions.append({"name": name, "payload": payload, "ok": None, "result": ""})
    return clean, actions


# ================= AI 教练（LLM 调用） =================

def state_digest(ws: str) -> str:
    info = scan_workspace(ws_dir(ws))
    prog = info["progress"] or {}
    mods = [{"id": m.get("id"), "name": m.get("name"), "status": m.get("status"),
             "quiz": m.get("quiz_history")} for m in prog.get("modules", [])]
    kstat = {"mastered": 0, "suspected": 0, "unlearned": 0}
    for n in info["kmap"]["nodes"]:
        if n["status"] in kstat:
            kstat[n["status"]] += 1
    t = today()
    due = [c.get("front", "")[:50] for c in info["flashcards"] if c.get("due") and c["due"] <= t]
    idx = _load_json(ws_dir(ws) / "rag-index.json", {})
    total_chunks = len(idx.get("chunks", []))
    dst = _load_json(ws_dir(ws) / "digest-state.json", {})
    cursor = min(int(dst.get("cursor", 0)), total_chunks) if total_chunks else 0
    prof = info["profile"]["table"]
    digest = {
        "阶段": prog.get("phase"), "阶段说明": prog.get("phase_label"),
        "下一步": prog.get("next_action"), "计划锁定": prog.get("plan_locked"),
        "当前模块": prog.get("current_module"), "模块": mods,
        "学员档案": {
            "学段领域": prof.get("学段 / 领域 / 发起人", ""),
            "目标等级": prof.get("目标等级", ""),
            "截止/周期": prof.get("截止时间", ""),
            "每日投入": prof.get("每日投入", ""),
            "指定考源": prof.get("指定考源", ""),
            "起点自评": info["profile"]["timeline"][-1]["text"][:120] if info["profile"]["timeline"] else "",
        },
        "摸底": (prog.get("diagnostic") or {}).get("score"),
        "图谱统计": kstat,
        "错题最近": [{"id": m["id"], "点": m["point"], "错因": m["reason"]} for m in info["mistakes"][-5:]],
        "今日到期卡片数": len(due), "到期卡片": due,
        "计划": info["plan"]["meta"],
        "资料库": {"files": idx.get("files", []), "chunks": total_chunks,
                   "已消化块数": cursor, "消化完成": bool(total_chunks) and cursor >= total_chunks},
    }
    return json.dumps(digest, ensure_ascii=False)


def chat_path(ws: str) -> Path:
    return ws_dir(ws) / "chat-web.jsonl"


def load_history(ws: str, limit: int = 40) -> list:
    p = chat_path(ws)
    if not p.exists():
        return []
    rows = []
    for line in read_text(p).splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows[-limit:]


def append_history(ws: str, role: str, content: str, actions=None):
    p = chat_path(ws)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "actions": actions or [],
                            "ts": _dt.datetime.now().isoformat(timespec="seconds")},
                           ensure_ascii=False) + "\n")


def call_llm(messages: list, use_search: bool = False) -> str:
    cfg = load_config()
    if not cfg.get("api_key"):
        raise CoachError("尚未配置 API Key：请到「设置」页填写智谱 API Key（本地保存）")

    def request_body() -> dict:
        # 交互场景默认关闭思考模式：大幅提速，且避免思考耗尽输出额度导致空回复
        body = {"model": cfg.get("model", DEFAULT_MODEL), "messages": messages,
                "temperature": 0.6, "max_tokens": 10000, "stream": False,
                "thinking": {"type": "disabled"}}
        if use_search:
            body["tools"] = [{"type": "web_search",
                              "web_search": {"search_result": True, "search_engine": "search_std"}}]
        return body

    def send(body: dict) -> dict:
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise CoachError("API 调用失败（HTTP %d）：%s — 请检查 Key/模型名（当前：%s）"
                             % (exc.code, detail, cfg.get("model")))
        except urllib.error.URLError as exc:
            raise CoachError("网络错误：%s" % exc.reason)
        except (TimeoutError, OSError) as exc:
            raise CoachError("AI 响应超时或网络异常（%s）。请稍等重试，或把要求拆成更短的指令。" % exc)

    data = send(request_body())
    content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    if not content:
        raise CoachError("模型返回为空，请重试一次；若持续出现请在设置页换用 glm-4.7。")
    return content


SYSTEM_TMPL = """你是「AI学习教练」，正在网页端直接辅导学员。全程简体中文。

# 引擎规则（必须遵守）
{skill_md}

# 工作区当前状态（机器可读，每轮自动刷新）
{digest}

# 本轮资料上下文
{rag}

# 网页端行为约定
1. **学员档案里已有的信息（周期、每日投入、目标等级、学段、考源等）直接使用，禁止重复询问**；只有档案确实缺失且影响规划的信息才追问一句。
2. 每轮先判断该做什么：清到期卡片（先问学员卡片问题、学员作答后再讲评）→ 按计划教学 → 苏格拉底提问 → 模块测验 → 评分与计划调整。
3. 出题一次只出一题，等学员作答再继续；评分必须先给评分依据（资料出处），再给分数与讲评。
4. 测验结果、卡片、错题、阶段变化、计划调整，必须通过 action 块写盘，写盘才算数；普通讲解不需要 action。
5. 测验结束后必须综合（计划+进度+答题情况）分析并调整后续计划：需要时用 update_progress 修改 phase/next_action/模块安排，并用 append_profile 记录调整理由（以日期开头）。
6. 资料未覆盖的内容标注〔补充〕；不虚构资料；学员问资料外内容时说明出处级别。
6. **分段消化协议**：当本轮注入标明【资料分段 N/M】时——(a) 只消化注入的段落，给出知识点/术语/与前文联系的简明摘要，可用 write_note 追加笔记；(b) 回复末尾必须输出 <action name="advance_digest">{{"to": 提示的游标值}}</action>（服务器校验，防跳段防重复）；(c) 告知进度（第 N/M 段）并提示说「继续消化资料」进入下一段；(d) **M 段全部完成后进入建库流水线**：① write_kmap 生成完整知识图谱（知识点表：编号/名称/类型/Bloom/模块/状态 + 前置/关联/易混三类边清单，基于全部已消化段落，可分多轮输出）；② write_plan 生成学习计划（模块表：学习目标/资料映射/时长/依赖/过关线 70%，并写入学员档案中的周期与每日投入）；③ update_progress 写入 modules 数组、phase=2、next_action=摸底诊断；④ 提示学员到「学习进度」「知识图谱」页查看并确认。禁止假装读过未注入的段落。
7. 严格但友好：先准确后友好；指出错误直接明确并附"怎么改"。
8. 回复长度适度：教学讲解可用标题/列表/表格；聊天式追问保持简短。

# action 写盘协议（回复末尾输出，JSON 必须合法）
<action name="update_progress">{{"patch": {{"phase_label": "…", "next_action": "…", "modules": [{{"id": "M1", "status": "passed", "quiz_history": [{{"date": "{today}", "score": 0.75}}]}}]}}}}</action>
<action name="add_flashcards">{{"cards": [{{"id": "F13", "front": "问题", "back": "答案", "module": "M1", "interval_days": 1, "due": "{tomorrow}"}}]}}</action>
<action name="add_mistake">{{"row": {{"id": "E10", "date": "{today}", "module": "M1", "point": "知识点", "question": "题目", "my": "学员答案", "correct": "正确答案", "reason": "概念不清|混淆|粗心|超纲", "variant": "待出"}}}}</action>
<action name="write_note">{{"module": "M1", "content": "教学笔记内容（markdown）"}}</action>
<action name="write_kmap">{{"content": "完整 knowledge-map.md 内容（markdown，含知识点表与三类边清单）"}}</action>
<action name="write_plan">{{"content": "完整 plan.md 内容（markdown，含模块表与周期/投入）"}}</action>
<action name="advance_digest">{{"to": 15}}</action>

# 输出格式规范（服务器按固定列解析，不合规会导致图谱/计划页空白）
- update_progress 的 modules 每项必须含：id（"M1"）、**name（模块名，必填）**、status、hours（数字）。
- write_kmap 的知识点表固定 7 列表头：| 编号 | 名称 | 类型 | 出处 | Bloom | 模块 | 状态 |；编号用 K1、K2…；模块列填 M1/M2…；文件后半写 "## 边清单" 及 前置边/关联边/易混边 三个小节，边用 K1→K2 或 K1↔K2 列出。
- write_plan 的模块表固定 7 列：| 模块 | 名称 | 学习目标 | 资料映射 | 时长 | 依赖 | 过关线 |；模块列只填编号（M1），名称放"名称"列，时长写"16h"形式。
<action name="append_profile">{{"text": "{today} 计划调整：…（理由）"}}</action>
说明：modules 按 id 合并，不会整表覆盖；不要输出协议之外的动作。"""


def coach_reply(ws: str, message: str) -> dict:
    digest_intent = any(k in message for k in (
        "分析资料", "分析我上传", "我上传了资料", "上传了资料", "消化", "读一下资料",
        "看看资料", "资料总结", "总结资料", "开始规划", "规划学习", "资料规划",
        "下一段", "继续读", "继续分析", "继续检索", "继续消化", "分段"))
    idx = _load_json(ws_dir(ws) / "rag-index.json", {})
    has_materials = bool(idx.get("chunks"))
    # 联网搜索只在明确的"找资料"意图且与本地资料消化无关时启用
    use_search = (not digest_intent) and any(k in message for k in (
        "找资料", "联网找", "网上找", "搜索资料", "网上搜", "找书", "推荐书", "推荐资料", "官方教材", "考纲"))
    skill_md = read_text(ROOT / "SKILL.md") or "（未找到 SKILL.md，按通用教练规则执行）"
    build_intent = any(k in message for k in (
        "构建图谱", "生成图谱", "知识图谱", "学习计划", "生成计划", "建立计划",
        "构建计划", "确认模块", "填写模块", "正式计划"))
    if build_intent:
        notes_txt = ""
        notes_dir = ws_dir(ws) / "notes"
        if notes_dir.exists():
            for p in sorted(notes_dir.glob("*.md")):
                notes_txt += "【%s】\n%s\n---\n" % (p.name, read_text(p))
        rag_ctx = []
        rag_note = ("本轮注入【教学笔记全集】（消化期间沉淀的要点，共 %d 字）。"
                    "请结合笔记与对话历史中的分段消化摘要，执行建库流水线：write_kmap → write_plan → update_progress。" % len(notes_txt)
                    if notes_txt else "暂无笔记；请基于对话历史中的分段消化摘要执行建库流水线：write_kmap → write_plan → update_progress。")
        if notes_txt:
            rag_note += "\n" + notes_txt[:20000]
    elif digest_intent and has_materials:
        seg = rag_segment(ws)
        if seg["done"]:
            rag_note = "本地资料已全部消化完毕（%d 段）。不要再分段；学员提问时使用检索即可；建议推进到图谱/计划阶段。" % seg["total_chunks"]
            rag_ctx = []
        else:
            meta = {k: v for k, v in seg.items() if k != "chunks"}
            rag_note = ("本轮注入【资料分段 %d/%d】（全书 %d 个资料块，已消化到第 %d 块，本段为第 %d—%d 块）。\n"
                        "消化完本段后必须在回复末尾输出 <action name=\"advance_digest\">{\"to\": %d}</action> 推进进度。\n分段元信息：%s"
                        % (seg["segment_no"], seg["segments_total"], seg["total_chunks"],
                           seg["chunk_cursor"], seg["chunk_cursor"] + 1, seg["next_cursor"],
                           seg["next_cursor"], json.dumps(meta, ensure_ascii=False)))
            rag_ctx = seg["chunks"]
    else:
        rag_ctx = rag_query(ws, message)
        rag_note = "本轮相关资料片段（RAG 按学员问题检索，含出处文件名）"
    system = SYSTEM_TMPL.format(
        skill_md=skill_md[:6000], digest=state_digest(ws),
        rag=rag_note + "\n" + (json.dumps(rag_ctx, ensure_ascii=False) or "[]"),
        today=today(),
        tomorrow=(_dt.date.today() + _dt.timedelta(days=1)).isoformat())
    messages = [{"role": "system", "content": system}]
    for h in load_history(ws, 12):
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"][:4000]})
    messages.append({"role": "user", "content": message})
    raw = call_llm(messages, use_search=use_search)
    clean, actions = extract_actions(raw)
    if not clean.strip():
        if actions:
            clean = "（本轮已执行下方写盘动作。）"
        else:
            raise CoachError("模型返回为空，请重试一次；若持续出现请缩短问题。")
    results = []
    for a in actions:
        try:
            a["result"] = apply_action(ws, a["name"], a["payload"])
            a["ok"] = True
        except Exception as exc:
            a["result"] = "执行失败：%s" % exc
            a["ok"] = False
        results.append({"name": a["name"], "ok": a["ok"], "result": a["result"]})
    append_history(ws, "user", message)
    append_history(ws, "assistant", clean, results)
    return {"reply": clean, "actions": results, "search_used": use_search}


# ================= HTTP =================

def _sanitize_rel(p):
    """相对路径清洗：只允许工作区内部相对路径，拒绝 .. / 绝对路径 / 盘符 / 点开头文件。"""
    p = str(p).replace("\\", "/")
    parts = [seg for seg in p.split("/")
             if seg and seg not in (".", "..") and ":" not in seg and not seg.startswith(".")]
    return Path(*parts) if parts else None


def import_workspace(name: str, files: list) -> dict:
    name = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    if not name:
        raise CoachError("文件夹名为空")
    rels = [f.get("path", "") for f in files]
    if "progress.json" not in rels:
        raise CoachError("所选文件夹里没有 progress.json——请选择具体主题的文件夹（例如备份里的「NISP二级」），而不是装着多个主题的外层文件夹")
    d = WS_ROOT / name
    if d.exists():
        raise CoachError("同名工作区已存在：%s" % name)
    count = 0
    for f in files:
        rel = _sanitize_rel(f.get("path", ""))
        if rel is None:
            continue
        data = base64.b64decode(f.get("b64", ""))
        if len(data) > MAX_UPLOAD:
            raise CoachError("单个文件过大（>15MB）：%s" % rel)
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    stat = build_rag_index(name) if (d / "materials").exists() else {"files": [], "chunks": 0}
    return {"ok": True, "ws": name, "files": count, "index": stat}


# ================= 自动分析流水线（上传后全自动消化→建库） =================

PIPELINES = {}
PIPELINE_LOCK = threading.Lock()


def get_pipeline(ws: str) -> dict:
    with PIPELINE_LOCK:
        return dict(PIPELINES.get(ws) or {"running": False, "stage": "idle"})


def set_pipeline(ws: str, **kw):
    with PIPELINE_LOCK:
        PIPELINES.setdefault(ws, {"running": False, "stage": "idle", "stop": False})
        PIPELINES[ws].update(kw)


def _run_pipeline(ws: str):
    """后台线程：逐段消化全部资料 → 生成图谱与计划 → 填充模块。"""
    set_pipeline(ws, running=True, stage="digest", error="", stop=False, segment_no=0, segments_total=0)
    try:
        while True:
            if get_pipeline(ws).get("stop"):
                set_pipeline(ws, running=False, stage="stopped")
                return
            seg = rag_segment(ws)
            set_pipeline(ws, stage="digest", segment_no=seg["segment_no"],
                         segments_total=seg["segments_total"])
            if seg["done"]:
                break
            last_err = ""
            for attempt in (1, 2):
                try:
                    coach_reply(ws, "【自动分析】继续消化资料：只消化本段，给出要点摘要并追加到全书笔记，末尾执行推进动作。")
                    last_err = ""
                    break
                except CoachError as exc:
                    last_err = str(exc)
                    time.sleep(5)
            if last_err:
                set_pipeline(ws, running=False, stage="error", error=last_err[:200])
                return
        set_pipeline(ws, stage="build")
        coach_reply(ws, "【自动分析】资料已全部消化，请构建完整知识图谱和学习计划，直接写入模块，并推进到可以开始学习的状态。")
        set_pipeline(ws, running=False, stage="done")
    except CoachError as exc:
        set_pipeline(ws, running=False, stage="error", error=str(exc)[:200])
    except Exception as exc:
        set_pipeline(ws, running=False, stage="error", error="流水线异常：%s" % exc)


def knowledge_detail(ws: str, kid: str) -> dict:
    """聚合单个知识点的全部内容：元数据、关联节点、卡片、错题、笔记摘录、资料片段。"""
    d = ws_dir(ws)
    info = scan_workspace(d)
    nodes = {n["id"]: n for n in info["kmap"]["nodes"]}
    node = nodes.get(kid)
    if not node:
        raise CoachError("知识点不存在：%s" % kid)

    def with_names(ids):
        return [{"id": i, "name": nodes[i]["name"] if i in nodes else i} for i in ids]

    pre_out, pre_in, rel_ids, conf_ids = [], [], [], []
    for e in info["kmap"]["edges"]:
        if e["type"] == "pre":
            if e["from"] == kid:
                pre_out.append(e["to"])
            elif e["to"] == kid:
                pre_in.append(e["from"])
        elif e["from"] == kid or e["to"] == kid:
            (rel_ids if e["type"] == "rel" else conf_ids).append(
                e["to"] if e["from"] == kid else e["from"])

    cards = [c for c in info["flashcards"] if c.get("module") == node["module"]]
    refs = set(re.findall(r"E\d+", node.get("note", "") + " " + node.get("name", "")))
    name_terms = [t for t in _terms(node["name"]) if len(t) >= 2][:12]
    mistakes = []
    for m in info["mistakes"]:
        if m["id"] in refs or (name_terms and any(t in m["point"] for t in name_terms)):
            mistakes.append(m)

    keys = [kid] + [seg for seg in re.split(r"[：:/、，。（）()\[\]]", node["name"]) if len(seg) >= 3][:3]
    notes_hits = []
    notes_dir = d / "notes"
    if notes_dir.exists():
        for p in sorted(notes_dir.glob("*.md")):
            txt = read_text(p)
            for key in keys:
                idx = txt.find(key)
                if idx >= 0:
                    notes_hits.append({"file": p.name, "excerpt": txt[max(0, idx - 80): idx + 600].strip()})
                    break
            if len(notes_hits) >= 2:
                break
    rag = rag_query(ws, node["name"], 2)
    return {
        "id": kid, "name": node["name"], "module": node["module"], "type": node["type"],
        "bloom": node["bloom"], "status": node["status"], "note": node["note"],
        "source": node["source"],
        "pre_in": with_names(pre_in), "pre_out": with_names(pre_out),
        "related": with_names(rel_ids), "confusable": with_names(conf_ids),
        "cards": [{"id": c.get("id"), "front": c.get("front"), "back": c.get("back", "")} for c in cards],
        "mistakes": [{"id": m["id"], "point": m["point"], "reason": m["reason"],
                      "my": m["my"], "correct": m["correct"]} for m in mistakes],
        "notes": notes_hits, "rag": rag,
    }


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".webmanifest": "application/manifest+json",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASH_DIR), **kwargs)

    def end_headers(self):
        # 界面文件禁缓存：改版后 F5 即生效，避免旧页面误导
        if self.path.endswith((".js", ".css", ".html")) or urlparse(self.path).path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _bad_host(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return not (host.startswith("127.0.0.1") or host.startswith("localhost"))

    def do_GET(self):
        if self._bad_host():
            return self._json({"error": "forbidden host"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/state":
                spaces = [scan_workspace(p) for p in sorted(WS_ROOT.iterdir()) if p.is_dir()] \
                    if WS_ROOT.exists() else []
                return self._json({"workspaces": spaces, "config": masked_config()})
            if u.path == "/api/config":
                return self._json(masked_config())
            if u.path == "/api/chat/history":
                ws = (q.get("ws") or [""])[0]
                return self._json({"messages": load_history(ws)})
            if u.path == "/api/rag/query":
                ws = (q.get("ws") or [""])[0]
                question = (q.get("q") or [""])[0]
                return self._json({"chunks": rag_query(ws, question)})
            if u.path == "/api/pipeline":
                ws = (q.get("ws") or [""])[0]
                st = get_pipeline(ws)
                dst = _load_json(digest_state_path(ws), {"cursor": 0})
                idx = _load_json(ws_dir(ws) / "rag-index.json", {})
                st["digested"] = dst.get("cursor", 0)
                st["total_chunks"] = len(idx.get("chunks", []))
                return self._json(st)
            if u.path == "/api/knode":
                ws = (q.get("ws") or [""])[0]
                kid = (q.get("id") or [""])[0]
                return self._json(knowledge_detail(ws, kid))
            if u.path == "/api/materials":
                ws = (q.get("ws") or [""])[0]
                d = ws_dir(ws)
                files = [p.name for p in sorted((d / "materials").glob("*")) if p.is_file()] \
                    if (d / "materials").exists() else []
                idx_path = d / "rag-index.json"
                if files and not idx_path.exists():
                    build_rag_index(ws)
                idx = _load_json(idx_path, {})
                return self._json({"files": files, "indexed": idx.get("chunks") and len(idx["chunks"]) or 0})
            return super().do_GET()
        except CoachError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": "服务器错误: %s" % exc}, 500)

    def do_POST(self):
        if self._bad_host():
            return self._json({"error": "forbidden host"}, 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/config":
                cfg = save_config(self._body())
                return self._json(masked_config())
            if u.path == "/api/config/test":
                out = call_llm([{"role": "user", "content": "回复两个字：正常"}])
                return self._json({"ok": True, "echo": out[:50]})
            if u.path == "/api/ws/create":
                name = create_workspace(self._body())
                return self._json({"ok": True, "ws": name})
            if u.path == "/api/ws/import":
                body = self._body()
                return self._json(import_workspace(body.get("name", ""), body.get("files", [])))
            if u.path == "/api/ws/delete":
                body = self._body()
                name = (body.get("ws") or "").strip()
                d = ws_dir(name)
                if d.parent.resolve() != WS_ROOT.resolve() or not d.is_dir():
                    raise CoachError("工作区不存在：%s" % name)
                n_files = sum(1 for p in d.rglob("*") if p.is_file())
                shutil.rmtree(d)
                return self._json({"ok": True, "ws": d.name, "removed_files": n_files})
            if u.path == "/api/upload":
                body = self._body()
                ws = body.get("ws", "")
                saved = []
                for f in body.get("files", []):
                    data = base64.b64decode(f.get("b64", ""))
                    saved.append(save_upload(ws, f.get("name", "file"), data))
                stat = build_rag_index(ws)
                return self._json({"ok": True, "saved": saved, "index": stat})
            if u.path == "/api/rag/rebuild":
                body = self._body()
                return self._json({"ok": True, "index": build_rag_index(body.get("ws", ""))})
            if u.path == "/api/chat":
                body = self._body()
                ws = body.get("ws", "")
                message = (body.get("message") or "").strip()
                if not message:
                    raise CoachError("消息为空")
                if not ws_dir(ws).exists():
                    raise CoachError("工作区不存在：%s" % ws)
                st = get_pipeline(ws)
                if st.get("running"):
                    raise CoachError("自动分析进行中（%s，第 %s/%s 段），完成后即可对话；也可在资料库卡片点「停止」。" % (
                        st.get("stage"), st.get("segment_no", "?"), st.get("segments_total", "?")))
                return self._json(coach_reply(ws, message))
            if u.path == "/api/pipeline/start":
                body = self._body()
                ws = (body.get("ws") or "").strip()
                if not ws or not ws_dir(ws).is_dir():
                    raise CoachError("工作区不存在")
                if get_pipeline(ws).get("running"):
                    raise CoachError("自动分析已在进行中")
                idx = _load_json(ws_dir(ws) / "rag-index.json", {})
                if not idx.get("chunks"):
                    raise CoachError("该主题还没有资料，请先上传")
                threading.Thread(target=_run_pipeline, args=(ws,), daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/pipeline/stop":
                body = self._body()
                set_pipeline((body.get("ws") or "").strip(), stop=True)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except CoachError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": "服务器错误: %s" % exc}, 500)

    def log_message(self, fmt, *args):
        pass


def open_app_window(url: str):
    """优先用 Edge/Chrome 的 --app 模式开独立应用窗口，否则退回默认浏览器。"""
    cands = []
    for env, sub in (("%ProgramFiles(x86)%", r"Microsoft\Edge\Application\msedge.exe"),
                     ("%ProgramFiles%", r"Google\Chrome\Application\chrome.exe"),
                     ("%ProgramFiles%", r"Microsoft\Edge\Application\msedge.exe")):
        p = Path(os.environ.get(env.strip("%"), "") or "C:\\") / sub
        if p.exists():
            cands.append(p)
    for exe in dict.fromkeys(cands):
        try:
            subprocess.Popen([str(exe), "--app=" + url, "--window-size=1360,880"])
            return
        except Exception:
            continue
    webbrowser.open(url)


def _probe_ports():
    """探测 8765–8775 是否已有实例在运行（显式绕过系统代理）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in range(8765, 8776):
        try:
            with opener.open("http://127.0.0.1:%d/api/config" % port, timeout=0.6) as r:
                if r.status == 200:
                    return port
        except Exception:
            continue
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 单实例：已有实例在跑 → 只打开应用窗口后退出
    existing = _probe_ports()
    if existing:
        url = "http://127.0.0.1:%d/" % existing
        print("已在运行：%s，直接打开窗口" % url)
        open_app_window(url)
        return
    srv, port = None, 8765
    for cand in range(8765, 8776):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if srv is None:
        print("no free port in 8765-8775")
        sys.exit(1)
    url = "http://127.0.0.1:%d/" % port
    print("learn-coach 学习系统已启动: %s  （关闭本窗口或 Ctrl+C 退出）" % url)
    if not NO_BROWSER:
        threading.Timer(1.0, lambda: open_app_window(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
