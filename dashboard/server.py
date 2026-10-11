#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""learn-coach 学习系统 · 本地服务器

两个角色：
1. 仪表盘（只读）：解析 workspaces\\ 状态文件，渲染总览/图谱/复习/资料页
2. AI 教练（读写）：网页端聊天学习——立项、资料上传(RAG-lite 检索)、教学测验、
   通过 action 协议把进度/错题/卡片写回工作区文件（与教练窗口共用同一套文件协议）

- 仅绑定 127.0.0.1；API Key 保存在本机 learn-coach\\config.json（exe 旁）
- 需要 Python 3.8+；PDF 解析可选依赖 pdfminer.six / pypdf
- 删除主题 = 移入 workspaces\\_archives\\（可恢复），彻底删除需在归档中二次确认
"""
import base64
import datetime as _dt
import hashlib
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
ARCHIVE_ROOT = WS_ROOT / "_archives"
NO_BROWSER = "--no-browser" in sys.argv

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7"
MAX_UPLOAD = 60 * 1024 * 1024
MAX_UPLOAD_MB = MAX_UPLOAD // (1024 * 1024)
TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
DOCX_SUFFIX = ".docx"
PDF_SUFFIX = ".pdf"
CARD_INTERVALS = [1, 3, 7, 14, 30, 60]   # 间隔重复序列（天）

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


def _load_json(path: Path, default):
    try:
        return json.loads(read_text(path))
    except Exception:
        return default


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


def _edge_pairs(clean: str):
    """把一段边清单文本解析成 (from, to) 组。

    兼容两种写法：
    - 顺序/链式：  K1→K3 / K1→K3→K5 / K1,K2→K3（逗号=组，前后各乘）
    - 逗号并列：   K1→K3, K1→K4（每对独立成边）
    判别：编号组数 == 箭头数+1 → 顺序配对；组数 == 箭头数*2 → 两两配对。
    """
    tokens = re.findall(r"%s(?:\s*/\s*%s)*|→|↔" % (NODE_ID, NODE_ID), clean)
    groups, ops = [], []
    for t in tokens:
        if t in ("→", "↔"):
            ops.append(t)
        else:
            groups.append(re.findall(r"K\d+|M\d+-\d+", t))
    pairs = []
    if not ops or not groups:
        return pairs, groups
    if len(groups) == len(ops) + 1:
        seq = range(len(ops))
        for i in seq:
            for a in groups[i]:
                for b in groups[i + 1]:
                    pairs.append((a, b, ops[i]))
    elif len(groups) == 2 * len(ops):
        for i in range(len(ops)):
            g1, g2 = groups[2 * i], groups[2 * i + 1]
            for a in g1:
                for b in g2:
                    pairs.append((a, b, ops[i]))
    return pairs, groups


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
                pairs, groups = _edge_pairs(clean)
                for a, b, op in pairs:
                    if op == "↔":
                        edges.append({"from": a, "to": b, "type": etype})
                        edges.append({"from": b, "to": a, "type": etype})
                    else:
                        edges.append({"from": a, "to": b, "type": etype})
                # 无有效边且只含一个编号 → 内部易混标记（如 "K08 内部·响应↔恢复"）
                if not pairs and len(groups) == 1 and len(groups[0]) == 1:
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


def _plan_meta(txt: str) -> dict:
    """计划元数据：按行匹配（跳过表格行），兼容 v1/v2 与教练实际产出格式。

    - goal：目标等级：X / 目标：X / 考试目标：X
    - deadline：截止：2026-11-06 / 周期：2026-10-09 → 2026-11-06（取结束日期）
    - daily_hours：每日 2h / 每日投入 2h
    - total_hours：总预算 56h / 共 56h
    """
    meta = {"goal": "", "deadline": "", "daily_hours": "", "total_hours": ""}
    for line in txt.splitlines():
        if line.lstrip().startswith("|"):
            continue
        s = line.strip()
        if not meta["goal"]:
            m = re.match(r"^[-*>]?\s*(?:目标等级|考试目标|总目标|学习目标等级|目标)\s*[：:]\s*(.+)$", s)
            if m and "|" not in m.group(1):
                meta["goal"] = m.group(1).strip()
        if not meta["deadline"]:
            m = re.match(r"^[-*>]?\s*(?:截止时间|截止日期|截止|deadline)\s*[：:]?\s*(\d{4}-\d{2}-\d{2})", s, re.I)
            if m:
                meta["deadline"] = m.group(1)
            elif "周期" in s or "日程" in s:
                m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:→|->|—|至|到)\s*(\d{4}-\d{2}-\d{2})", s)
                if m:
                    meta["deadline"] = m.group(2)
        if not meta["daily_hours"]:
            m = re.search(r"每日(?:投入)?\s*[：:]?\s*([\d.]+)\s*h", s)
            if m:
                meta["daily_hours"] = m.group(1)
        if not meta["total_hours"]:
            m = re.search(r"(?:总预算|总时长|合计|总共|共)\s*[：:]?\s*([\d.]+)\s*h", s)
            if m:
                meta["total_hours"] = m.group(1)
    return meta


def parse_plan(d: Path):
    txt = read_text(d / "plan.md")
    meta = _plan_meta(txt)
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


_SCAN_CACHE = {}   # path -> (fingerprint, result)：状态文件未变时直接复用解析结果


def _ws_fingerprint(d: Path):
    fps = []
    for f in ("progress.json", "knowledge-map.md", "plan.md", "mistakes.md",
              "flashcards.md", "profile.md"):
        p = d / f
        try:
            st = p.stat()
            fps.append((f, st.st_mtime_ns, st.st_size))
        except OSError:
            fps.append((f, 0, 0))
    return tuple(fps)


def scan_workspace(d: Path):
    fp = _ws_fingerprint(d)
    hit = _SCAN_CACHE.get(str(d))
    if hit and hit[0] == fp:
        return hit[1]
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
            card.setdefault("back", md.get("back", ""))
            card.setdefault("due", md.get("due", ""))
            cards.append(card)
    known = {c.get("id") for c in cards}
    for cid, md in fc_md.items():
        if cid not in known:
            cards.append(dict(md, id=cid))
    result = {"name": d.name, "progress": progress, "kmap": parse_kmap(d),
              "mistakes": parse_mistakes(d), "flashcards": cards,
              "plan": plan, "profile": parse_profile(d)}
    _SCAN_CACHE[str(d)] = (fp, result)
    return result


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
    if not safe or safe in (".", "..") or ".." in safe or safe.startswith("_"):
        raise CoachError("非法的工作区名称")
    return WS_ROOT / safe


GEN_NOTE = "> 本文件由网页立项生成，后续由 AI 教练维护。"


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
               "- %s 网页立项：工作区创建，等待 AI 教练完成资料协商与计划。" % today(), "", GEN_NOTE]
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
    write_text(d / "plan.md", "# 学习计划 — %s\n\n> 状态：**待规划**（AI 教练将在资料就绪后生成）\n\n%s\n" % (d.name, GEN_NOTE))
    write_text(d / "material-index.md",
               "# 资料索引 — %s\n\n| 编号 | 名称 | 类型 | 权威等级 | 说明 |\n| --- | --- | --- | --- | --- |\n\n%s\n" % (d.name, GEN_NOTE))
    write_text(d / "mistakes.md",
               "# 错题本 — %s\n\n| # | 日期 | 模块 | 关联知识点 | 题目摘要 | 我的答案 | 正确答案 | 错因 | 变式练习状态 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n" % d.name)
    write_text(d / "flashcards.md",
               "# 间隔重复卡片 — %s\n\n| 卡片ID | 正面（问题） | 背面（答案） | 模块 | 当前间隔(天) | 到期日 | 状态 |\n| --- | --- | --- | --- | --- | --- | --- |\n" % d.name)
    write_text(d / "knowledge-map.md", "# 知识图谱 — %s\n\n> 待消化管道生成。\n\n%s\n" % (d.name, GEN_NOTE))
    return d.name


# ================= 资料上传 + RAG-lite =================

def pdf_pages_text(data: bytes):
    """逐页提取 PDF 文字：返回 [(页码(1起), 文本), ...]，供分页索引与溯源定位。"""
    pages, scan_hint = [], False
    try:  # 首选 pdfminer 单次遍历
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
        for i, layout in enumerate(extract_pages(io.BytesIO(data)), start=1):
            parts = [el.get_text() for el in layout if isinstance(el, LTTextContainer)]
            pages.append((i, "".join(parts)))
        if any(t.strip() for _p, t in pages):
            return pages
        scan_hint = True
    except ImportError:
        pass
    except CoachError:
        raise
    except Exception:
        pages = []
    try:  # 兜底 pypdf 逐页
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for i, pg in enumerate(reader.pages, start=1):
            try:
                pages.append((i, pg.extract_text() or ""))
            except Exception:
                pages.append((i, ""))
        if any(t.strip() for _p, t in pages):
            return pages
    except ImportError:
        if not pages and not scan_hint:
            raise CoachError("缺少 PDF 解析库（pdfminer.six / pypdf），请先 pip install pypdf")
    except Exception as exc:
        if not pages:
            raise CoachError("PDF 解析失败：%s" % str(exc)[:120])
    if not any(t.strip() for _p, t in pages):
        raise CoachError("PDF 中几乎提取不到文字——可能是扫描件/图片型 PDF（无文字层），请提供文字版或转存为 txt 后上传")
    return pages


def material_pages(path: Path):
    """返回 [(页码或None, 文本), ...]：PDF 分页，其他格式整篇一页。"""
    if path.suffix.lower() == PDF_SUFFIX:
        return pdf_pages_text(path.read_bytes())
    t = material_text(path)
    return [(None, t)] if t.strip() else []


DOCX_LAYOUT_JUNK = re.compile(
    r"\b(?:left|right|top|bottom|center)(?:top|bottom|left|right|center)*\b", re.I)


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 60 * 1024 * 1024:
            raise CoachError("docx 内文过大（解压后 >60MB），已拒绝读取")
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = _html.unescape(text)
    # 过滤文本框排版坐标标记（lefttop / rightbottom 之类），常见于排版导出的 docx 垃圾
    cleaned = DOCX_LAYOUT_JUNK.sub(" ", text)
    if len(re.sub(r"\s", "", cleaned)) < max(30, len(re.sub(r"\s", "", text)) * 0.3):
        raise CoachError("该 docx 提取不到有效正文（可能是扫描/特殊排版导出），请另存为 txt/md 后上传")
    return cleaned


def save_upload(ws: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_UPLOAD:
        raise CoachError("文件过大（>%dMB）：%s" % (MAX_UPLOAD_MB, filename))
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
    return read_text(path)


DOCX_HEAD_RE = re.compile(r"<w:pStyle w:val=\"(?:Heading|heading)([1-6])\"")


def docx_html(data: bytes) -> str:
    """docx → 简单 HTML（段落 + 标题层级），供资料页在线阅读。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 60 * 1024 * 1024:
            raise CoachError("docx 内文过大（解压后 >60MB），已拒绝读取")
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    # 按段落切分，识别标题样式
    parts = re.split(r"(<w:p[ >])", xml)
    paragraphs, buf = [], ""
    for seg in parts:
        if seg == "<w:p " or seg == "<w:p>":
            if buf:
                paragraphs.append(buf)
            buf = seg
        elif buf:
            buf += seg
    if buf:
        paragraphs.append(buf)
    body, seen_text = [], 0
    for p in paragraphs:
        style = DOCX_HEAD_RE.search(p[:600])
        text = _html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        text = DOCX_LAYOUT_JUNK.sub(" ", text).strip()
        if not text:
            continue
        seen_text += len(text)
        if style:
            lv = min(int(style.group(1)) + 1, 5)
            body.append("<h%d>%s</h%d>" % (lv, _html.escape(text), lv))
        else:
            body.append("<p>%s</p>" % _html.escape(text))
    if seen_text < 30:
        raise CoachError("该 docx 提取不到有效正文（可能是扫描/特殊排版导出）")
    return ("<!doctype html><meta charset=\"utf-8\">"
            "<style>body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;font-size:16px;"
            "line-height:1.9;color:#26231e;max-width:820px;margin:0 auto;padding:28px 34px;"
            "background:#fff}h2,h3,h4,h5{line-height:1.4;margin:1.2em 0 .5em}"
            "p{margin:.45em 0}</style>" + "".join(body))


VIEW_WRAP = """<!doctype html><meta charset="utf-8">
<style>body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;font-size:16px;line-height:1.9;
color:#26231e;max-width:820px;margin:0 auto;padding:28px 34px;background:#fff;white-space:pre-wrap}
p{margin:.45em 0}</style><p>%s</p>"""


def material_view(path: Path) -> bytes:
    """资料在线阅读视图：docx/txt/md → HTML 字节。"""
    if path.suffix.lower() == DOCX_SUFFIX:
        return docx_html(path.read_bytes()).encode("utf-8")
    return (VIEW_WRAP % _html.escape(read_text(path))).encode("utf-8")


_PAGE_TEXT_CACHE = {}   # (path, mtime) -> pages


def material_page_text(ws: str, name: str, page: int) -> dict:
    """提取资料指定页（或全文开头）的文字，供「原文提问」直接引用，无 OCR 误差。"""
    p = material_path(ws, name)
    if p.suffix.lower() == PDF_SUFFIX:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        key = str(p)
        cached = _PAGE_TEXT_CACHE.get(key)
        if not cached or cached[0] != mtime:
            cached = (mtime, pdf_pages_text(p.read_bytes()))
            _PAGE_TEXT_CACHE[key] = cached
        pages = cached[1]
        n = max(1, min(int(page or 1), len(pages)))
        texts = [t for _i, t in pages[n - 1:n + 1] if t.strip()]  # 当前页 + 下一页
        return {"file": p.name, "page": n, "total_pages": len(pages),
                "text": "\n".join(texts)[:3000]}
    t = material_text(p)
    return {"file": p.name, "page": None, "total_pages": None, "text": t.strip()[:3000]}


def build_rag_index(ws: str) -> dict:
    """分页索引：每个资料块带 page 字段（PDF 页码，其他格式为 null），供溯源定位。"""
    d = ws_dir(ws)
    files_ok, errors, chunks = [], [], []
    for p in sorted((d / "materials").glob("*")):
        if not (p.is_file() and p.suffix.lower() in TEXT_SUFFIXES | {DOCX_SUFFIX, PDF_SUFFIX}):
            continue
        try:
            pages = material_pages(p)
        except CoachError as exc:
            errors.append({"file": p.name, "error": str(exc)})
            continue
        if not pages:
            continue
        files_ok.append(p.name)
        for page_no, text in pages:
            paras, buf = [], ""
            for line in text.splitlines():
                buf += line + "\n"
                if len(buf) >= 500:
                    paras.append(buf)
                    buf = ""
            if buf.strip():
                paras.append(buf)
            for i, para in enumerate(paras):
                head = para.strip().splitlines()[0][:40] if para.strip() else ""
                chunks.append({"file": p.name, "seq": i, "page": page_no,
                               "head": head, "text": para.strip()[:800]})
    write_text(d / "rag-index.json", json.dumps(
        {"built": today(), "files": files_ok, "errors": errors, "chunks": chunks}, ensure_ascii=False))
    return {"files": files_ok, "chunks": len(chunks), "errors": errors}


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
             "page": c.get("page"), "score": s} for s, c in scored[:top]]


# ================= action 协议（AI 写回工作区） =================

ACTION_RE = re.compile(r"<action\s+name=\"([\w_]+)\">\s*(.*?)\s*</action>", re.DOTALL)


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
        return "plan.md 已生成（总览页可见）"
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


# ================= 卡片自助复习（间隔重复闭环） =================

def review_card(ws: str, card_id: str, remembered: bool) -> dict:
    """复习页自助判定：更新 progress.json 与 flashcards.md 的间隔/到期日。

    间隔序列 1→3→7→14→30→60；答错重置为 1 天。
    """
    d = ws_dir(ws)
    prog = _load_json(d / "progress.json", {})
    cards = prog.get("flashcards") if isinstance(prog, dict) else None
    if not isinstance(cards, list):
        cards, prog["flashcards"] = [], []
    card = next((c for c in cards if isinstance(c, dict) and c.get("id") == card_id), None)
    if card is None:
        raise CoachError("卡片不存在：%s" % card_id)
    try:
        cur = int(float(card.get("interval_days") or 1))
    except (TypeError, ValueError):
        cur = 1
    nxt = next((i for i in CARD_INTERVALS if i > cur), CARD_INTERVALS[-1]) if remembered else 1
    due = (_dt.date.today() + _dt.timedelta(days=nxt)).isoformat()
    card["interval_days"] = nxt
    card["due"] = due
    card["status"] = "在途" if remembered else "重置"
    card["last_review"] = today()
    log = prog.setdefault("review_log", [])
    log.append({"id": card_id, "date": today(), "remembered": bool(remembered),
                "interval": nxt, "ts": _dt.datetime.now().isoformat(timespec="seconds")})
    prog["updated_at"] = today()
    write_text(d / "progress.json", json.dumps(prog, ensure_ascii=False, indent=2))
    # 同步 flashcards.md 对应行（7 列：卡片ID|正面|背面|模块|间隔|到期|状态）
    fc_path = d / "flashcards.md"
    lines = read_text(fc_path).splitlines()
    for i, l in enumerate(lines):
        if re.match(r"^\|\s*%s\s*\|" % re.escape(card_id), l):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            while len(cells) < 7:
                cells.append("")
            cells[4] = str(nxt)
            cells[5] = due
            cells[6] = card["status"]
            lines[i] = "| " + " | ".join(cells) + " |"
            break
    write_text(fc_path, "\n".join(lines) + "\n")
    return {"id": card_id, "interval_days": nxt, "due": due,
            "remembered": bool(remembered)}


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


def _is_pipeline_msg(row: dict) -> bool:
    return row.get("kind") == "pipeline" or (
        row.get("role") == "user" and str(row.get("content", "")).startswith("【自动分析】"))


def migrate_chat(ws: str):
    """一次性迁移：给历史里的自动分析消息补 kind=pipeline 标记（幂等）。"""
    p = chat_path(ws)
    if not p.exists():
        return
    rows, changed = [], False
    for line in read_text(p).splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    pipe = False
    for r in rows:
        if r.get("kind"):
            pipe = (r["kind"] == "pipeline")
            continue
        if r.get("role") == "user" and str(r.get("content", "")).startswith("【自动分析】"):
            r["kind"] = "pipeline"
            pipe = True
            changed = True
        elif r.get("role") == "assistant" and pipe:
            r["kind"] = "pipeline"
            changed = True
        else:
            pipe = False
    if changed:
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


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


def append_history(ws: str, role: str, content: str, actions=None, kind="chat"):
    p = chat_path(ws)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": role, "content": content, "actions": actions or [],
                            "kind": kind,
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
6. **分段消化协议**：当本轮注入标明【资料分段 N/M】时——(a) 只消化注入的段落，给出知识点/术语/与前文联系的简明摘要，可用 write_note 追加笔记；(b) 回复末尾必须输出 <action name="advance_digest">{{"to": 提示的游标值}}</action>（服务器校验，防跳段防重复）；(c) 告知进度（第 N/M 段）并提示说「继续消化资料」进入下一段；(d) **M 段全部完成后进入建库流水线**：① write_kmap 生成完整知识图谱（知识点表：编号/名称/类型/Bloom/模块/状态 + 前置/关联/易混三类边清单，基于全部已消化段落，可分多轮输出）；② write_plan 生成学习计划（模块表：学习目标/资料映射/时长/依赖/过关线 70%，并写入学员档案中的周期与每日投入）；③ update_progress 写入 modules 数组、phase=2、next_action=摸底诊断；④ 提示学员到「总览」「知识图谱」页查看并确认。禁止假装读过未注入的段落。
7. 严格但友好：先准确后友好；指出错误直接明确并附"怎么改"。
8. 回复长度适度：教学讲解可用标题/列表/表格；聊天式追问保持简短。
9. 学员也可能在「复习」页自助完成卡片间隔判定（不经过你），这不影响你的教学判断。

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
- write_kmap 的知识点表固定 7 列表头：| 编号 | 名称 | 类型 | 出处 | Bloom | 模块 | 状态 |；编号用 K1、K2…；模块列填 M1/M2…；文件后半写 "## 边清单" 及 前置边/关联边/易混边 三个小节，边用 K1→K2 或 K1↔K2 列出（也可用逗号并列：K1→K3, K1→K4）。
- write_plan 的模块表固定 7 列：| 模块 | 名称 | 学习目标 | 资料映射 | 时长 | 依赖 | 过关线 |；模块列只填编号（M1），名称放"名称"列，时长写"16h"形式；计划头部写明「目标等级：X」「截止：YYYY-MM-DD」「每日 Xh」「共 Xh」。

# 溯源与截图提问
10. **页码溯源**：注入的资料块带 page 字段（PDF 页码）。讲解知识点、出题、评分引用时，都标注来源（格式：📍《文件名》p50）。write_kmap 的"出处"列必须写"文件名 p页码"（如 "教材.pdf p50"），使前端可一键定位原文。
11. **截图/原文提问**：学员消息带【截图提问】标记时，其中"识别原文"来自 OCR（可能有错字）——先按语义在资料中检索对应段落，回答严格优先依据原书内容并标注页码。带【原文提问】标记时，"当前阅读内容"是从资料页直接提取的准确文字（无 OCR 误差）——直接就这段内容讲解/答疑，同样标注文件名与页码。原书没有的才标注〔补充〕并明确说明"书中未提及"。自主阅读场景下学员可能跳跃式提问，不受教学计划顺序约束，直接就所问内容作答。
<action name="append_profile">{{"text": "{today} 计划调整：…（理由）"}}</action>
说明：modules 按 id 合并，不会整表覆盖；不要输出协议之外的动作。"""


def coach_reply(ws: str, message: str, kind: str = "chat") -> dict:
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
        if h.get("role") in ("user", "assistant") and h.get("content") and not _is_pipeline_msg(h):
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
    append_history(ws, "user", message, kind=kind)
    append_history(ws, "assistant", clean, results, kind=kind)
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
        raise CoachError("所选文件夹里没有 progress.json——请选择具体主题的文件夹，而不是装着多个主题的外层文件夹")
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
            raise CoachError("单个文件过大（>%dMB）：%s" % (MAX_UPLOAD_MB, rel))
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    stat = build_rag_index(name) if (d / "materials").exists() else {"files": [], "chunks": 0}
    return {"ok": True, "ws": name, "files": count, "index": stat}


def material_delete_check(ws: str, name: str) -> dict:
    """删除资料前的影响评估：
    - blocked：它是主题仅剩的资料（先上传替代或归档主题）
    - important：图谱/计划依据的基础资料（警告 + 双重确认后才可删）
    """
    p = material_path(ws, name)
    d = ws_dir(ws)
    files = [q.name for q in sorted((d / "materials").glob("*")) if q.is_file()]
    if len(files) <= 1:
        return {"blocked": True, "important": False,
                "reason": "这是当前主题仅剩的资料，不能删除。想更换资料请先上传新文件再删旧的；想清空请归档整个主题（设置页）。"}
    idx = _load_json(d / "rag-index.json", {})
    chunks = idx.get("chunks", [])
    total = len(chunks) or 1
    share = sum(1 for c in chunks if c.get("file") == p.name) / total
    reasons = []
    kmap_txt = read_text(d / "knowledge-map.md")
    plan_txt = read_text(d / "plan.md")
    mindex_txt = read_text(d / "material-index.md")
    if p.name in kmap_txt:
        reasons.append("知识图谱（knowledge-map.md）引用了这份资料，删除后知识点的原文定位会失效")
    if p.name in plan_txt:
        reasons.append("学习计划（plan.md）引用了这份资料，删除后模块的资料映射会失效")
    if p.name in mindex_txt:
        reasons.append("资料索引（material-index.md）登记了这份资料")
    kmap_has_nodes = bool(re.search(r"^\|\s*(?:编号|K\d+)", kmap_txt, re.M))
    if share >= 0.5 and kmap_has_nodes:
        reasons.append("它贡献了知识库 %.0f%% 的资料块——图谱和计划主要依据它消化生成" % (share * 100))
    return {"blocked": False, "important": bool(reasons), "reasons": reasons}


def material_delete(ws: str, name: str, confirm: str) -> dict:
    check = material_delete_check(ws, name)
    if check["blocked"]:
        raise CoachError(check["reason"])
    if check["important"] and confirm != "__DELETE_BASE__":
        raise CoachError("该资料是学习库的基础资料，需要二次确认（重要删除确认）才能删除")
    p = material_path(ws, name)
    p.unlink()
    stat = build_rag_index(ws)
    return {"ok": True, "deleted": p.name, "index": {"files": stat["files"], "chunks": stat["chunks"]},
            "errors": stat["errors"]}


# ================= 工作区归档（可恢复的"删除"） =================

def archive_workspace(name: str) -> dict:
    d = ws_dir(name)
    if d.parent.resolve() != WS_ROOT.resolve() or not d.is_dir():
        raise CoachError("工作区不存在：%s" % name)
    if get_pipeline(name).get("running"):
        set_pipeline(name, stop=True)
        time.sleep(0.3)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE_ROOT / ("%s-%s" % (d.name, _dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.move(str(d), str(target))
    n_files = sum(1 for p in target.rglob("*") if p.is_file())
    return {"ok": True, "ws": d.name, "archived_as": target.name, "files": n_files}


def list_archives() -> list:
    if not ARCHIVE_ROOT.is_dir():
        return []
    out = []
    for p in sorted(ARCHIVE_ROOT.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        out.append({"name": p.name,
                    "files": sum(1 for q in p.rglob("*") if q.is_file()),
                    "archived_at": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def restore_archive(src: str) -> dict:
    p = ARCHIVE_ROOT / (src or "").strip()
    if not p.is_dir() or p.parent.resolve() != ARCHIVE_ROOT.resolve():
        raise CoachError("归档不存在：%s" % src)
    m = re.match(r"^(.*)-\d{8}-\d{6}$", p.name)
    base = m.group(1) if m else p.name
    target = WS_ROOT / base
    if target.exists():
        raise CoachError("已存在同名工作区「%s」，请先处理它再恢复" % base)
    shutil.move(str(p), str(target))
    migrate_chat(base)
    return {"ok": True, "ws": base}


def purge_archive(src: str) -> dict:
    p = ARCHIVE_ROOT / (src or "").strip()
    if not p.is_dir() or p.parent.resolve() != ARCHIVE_ROOT.resolve():
        raise CoachError("归档不存在：%s" % src)
    n_files = sum(1 for q in p.rglob("*") if q.is_file())
    shutil.rmtree(p)
    return {"ok": True, "ws": p.name, "removed_files": n_files}


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


def _kmap_built(ws: str) -> bool:
    return bool(re.search(r"^\|\s*(?:编号|K\d+)", read_text(ws_dir(ws) / "knowledge-map.md"), re.M))


def _plan_built(ws: str) -> bool:
    txt = read_text(ws_dir(ws) / "plan.md")
    return ("待规划" not in txt) and bool(re.search(r"^\|.*M\d+", txt, re.M))


def _run_pipeline(ws: str):
    """后台线程：逐段消化全部资料 → 生成图谱与计划 → 填充模块。

    消化过程消息写为 kind=pipeline，不混入学员聊天记录。
    """
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
                    coach_reply(ws, "【自动分析】继续消化资料：只消化本段，给出要点摘要并追加到全书笔记，末尾执行推进动作。",
                                kind="pipeline")
                    last_err = ""
                    break
                except CoachError as exc:
                    last_err = str(exc)
                    time.sleep(5)
            if last_err:
                set_pipeline(ws, running=False, stage="error", error=last_err[:200])
                return
        set_pipeline(ws, stage="build")
        # 消化末段时模型可能已执行建库（write_kmap/write_plan），避免重复消耗
        if not (_kmap_built(ws) and _plan_built(ws)):
            coach_reply(ws, "【自动分析】资料已全部消化，请构建完整知识图谱和学习计划，直接写入模块，并推进到可以开始学习的状态。",
                        kind="pipeline")
        set_pipeline(ws, running=False, stage="done")
    except CoachError as exc:
        set_pipeline(ws, running=False, stage="error", error=str(exc)[:200])
    except Exception as exc:
        set_pipeline(ws, running=False, stage="error", error="流水线异常：%s" % exc)


# ================= 原文定位 / 截图 OCR =================

def material_path(ws: str, name: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    if ".." in (name or "") or not safe:
        raise CoachError("非法文件名")
    p = ws_dir(ws) / "materials" / safe
    if not p.is_file():
        raise CoachError("资料不存在：%s" % safe)
    return p


def _browser_exes():
    """Edge / Chrome 候选路径（去重）。"""
    cands = []
    for env, sub in (("%ProgramFiles(x86)%", r"Microsoft\Edge\Application\msedge.exe"),
                     ("%ProgramFiles%", r"Google\Chrome\Application\chrome.exe"),
                     ("%ProgramFiles%", r"Microsoft\Edge\Application\msedge.exe")):
        p = Path(os.environ.get(env.strip("%"), "") or "C:\\") / sub
        if p.exists():
            cands.append(p)
    return list(dict.fromkeys(cands))


def open_material_external(ws: str, name: str, page) -> str:
    """用外部程序打开资料；PDF 优先走 Edge/Chrome 内置阅读器（支持 #page=N 定页）。"""
    p = material_path(ws, name)
    page_no = 0
    try:
        page_no = int(str(page or 0))
    except (TypeError, ValueError):
        page_no = 0
    if p.suffix.lower() == PDF_SUFFIX and page_no > 0:
        for exe in _browser_exes():
            url = "file:///" + str(p).replace("\\", "/") + "#page=%d" % page_no
            subprocess.Popen([str(exe), url])
            return "已在浏览器阅读器打开并定位到第 %d 页" % page_no
    try:
        os.startfile(str(p))  # noqa: S606 本机受信文件
        return "已用系统默认程序打开" + ("（默认阅读器可能不支持自动定页，请手动翻到第 %d 页）" % page_no if page_no else "")
    except Exception as exc:
        raise CoachError("打开失败：%s" % exc)


def _ps(script_name: str, args: list, timeout: int = 90):
    script = DASH_DIR / script_name
    if not script.exists():
        raise CoachError("缺少脚本 %s" % script_name)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)] + args,
        capture_output=True, timeout=timeout)
    err = (r.stderr or b"").decode("gbk", "replace").strip()
    if r.returncode == 2:
        raise CoachError("系统未安装中文/英文 OCR 语言包：设置 → 时间和语言 → 语言 → 添加语言（中文）后重试")
    if r.returncode != 0:
        raise CoachError("PowerShell 执行失败：%s" % err[:150])
    return r


def ocr_image(png_path: Path) -> str:
    out = png_path.with_suffix(".ocr.txt")
    try:
        _ps("ocr.ps1", ["-png", str(png_path), "-out", str(out)])
    except CoachError:
        # rc=1 时错误信息写在结果文件里，优先读它
        text = read_text(out).strip()
        if text.startswith("__ERROR__"):
            raise CoachError("OCR 失败：%s" % text[9:150])
        if text == "__NOLANG__":
            raise CoachError("系统未安装中文/英文 OCR 语言包：设置 → 时间和语言 → 语言 → 添加语言（中文）后重试")
        raise
    text = read_text(out).strip()
    if text.startswith("__ERROR__"):
        raise CoachError("OCR 失败：%s" % text[9:150])
    return text


def screen_capture_ocr() -> str:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "screen.png"
        _ps("capture.ps1", ["-out", str(png)], timeout=30)
        return ocr_image(png)


def _source_loc_candidates(ws: str, node: dict, idx: dict) -> list:
    """把知识点的"出处"解析成原文定位候选：优先"文件名 p页码"直解，
    否则用出处文字/节点名在 RAG 索引里反查（章名、节名通常出现在块首行）。"""
    chunks = idx.get("chunks", [])
    files = set(idx.get("files", [])) or {c.get("file") for c in chunks}
    out, seen = [], set()

    def push(file, page, why, score):
        if not file or (files and file not in files):
            return
        key = (file, page)
        if key in seen:
            return
        seen.add(key)
        out.append({"file": file, "page": page, "match": why, "score": score})

    src = node.get("source", "")
    # 形如 "xxx.pdf p50" / "《xxx》 p50"
    m = re.search(r"([^\s:：]+?\.(?:pdf|docx|txt|md))\s*[pP]\s*(\d+)", src, re.I)
    if m:
        push(m.group(1), int(m.group(2)), "出处标明页码", 100)
    # 出处是章/节名（如"第十八章"）：在块首行里找
    src_terms = [t for t in re.split(r"[、，,；;\s（）()]+", src) if len(t) >= 2][:4]
    for term in src_terms:
        for c in chunks:
            head = (c.get("head") or "") + (c.get("text") or "")[:150]
            if term in head:
                push(c["file"], c.get("page"), "出处「%s」所在位置" % term[:16], 80)
                break
    # 节点名反查：复用 RAG 打分（中文 2-gram），取前两个命中
    for hit in rag_query(ws, node.get("name", ""), 2):
        push(hit["file"], hit.get("page"), "知识点内容匹配", 50)
    out.sort(key=lambda x: -x["score"])
    return out[:3]


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

    # 卡片按"模块+名称命中"筛选，替代原来整组返回的粗匹配
    name_terms = [t for t in _terms(node["name"]) if len(t) >= 2][:12]
    cards = [c for c in info["flashcards"]
             if (c.get("module") == node["module"]
                 and (not name_terms or any(t in (c.get("front") or "") for t in name_terms)))]
    if not cards:  # 名称没命中时退回同模块
        cards = [c for c in info["flashcards"] if c.get("module") == node["module"]]
    refs = set(re.findall(r"E\d+", node.get("note", "") + " " + node.get("name", "")))
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
    idx = _load_json(ws_dir(ws) / "rag-index.json", {})
    return {
        "id": kid, "name": node["name"], "module": node["module"], "type": node["type"],
        "bloom": node["bloom"], "status": node["status"], "note": node["note"],
        "source": node["source"],
        "loc": _source_loc_candidates(ws, node, idx),
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

    def _bad_origin(self) -> bool:
        """CSRF 防护：浏览器跨站请求必带 Origin/Referer，其 host 必须是本机回环。

        无这两个头的请求（curl、本机脚本）放行，不影响调试。
        """
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            try:
                host = (urlparse(v).hostname or "").lower()
            except Exception:
                host = ""
            if host not in ("127.0.0.1", "localhost", "::1", ""):
                return True
        return False

    def do_GET(self):
        if self._bad_host() or self._bad_origin():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/state":
                spaces = [scan_workspace(p) for p in sorted(WS_ROOT.iterdir())
                          if p.is_dir() and not p.name.startswith("_")] \
                    if WS_ROOT.exists() else []
                rev = hashlib.md5(json.dumps(spaces, sort_keys=True, default=str)
                                  .encode("utf-8")).hexdigest()[:10]
                return self._json({"workspaces": spaces, "config": masked_config(), "rev": rev})
            if u.path == "/api/config":
                return self._json(masked_config())
            if u.path == "/api/archives":
                return self._json({"archives": list_archives()})
            if u.path == "/api/chat/history":
                ws = (q.get("ws") or [""])[0]
                migrate_chat(ws)
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
            if u.path == "/api/material/file":
                ws = (q.get("ws") or [""])[0]
                name = (q.get("name") or [""])[0]
                p = material_path(ws, name)
                ctype = {".pdf": "application/pdf",
                         ".txt": "text/plain; charset=utf-8",
                         ".md": "text/plain; charset=utf-8",
                         ".markdown": "text/plain; charset=utf-8",
                         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                         }.get(p.suffix.lower(), "application/octet-stream")
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/material/view":
                ws = (q.get("ws") or [""])[0]
                name = (q.get("name") or [""])[0]
                p = material_path(ws, name)
                body = material_view(p)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/material/page-text":
                ws = (q.get("ws") or [""])[0]
                name = (q.get("name") or [""])[0]
                page = (q.get("page") or ["1"])[0]
                return self._json(material_page_text(ws, name, page))
            if u.path == "/api/materials":
                ws = (q.get("ws") or [""])[0]
                d = ws_dir(ws)
                files = [p.name for p in sorted((d / "materials").glob("*")) if p.is_file()] \
                    if (d / "materials").exists() else []
                idx_path = d / "rag-index.json"
                if files and not idx_path.exists():
                    build_rag_index(ws)
                idx = _load_json(idx_path, {})
                return self._json({"files": files, "indexed": idx.get("chunks") and len(idx["chunks"]) or 0,
                                   "errors": idx.get("errors", [])})
            return super().do_GET()
        except CoachError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": "服务器错误: %s" % exc}, 500)

    def do_POST(self):
        if self._bad_host() or self._bad_origin():
            return self._json({"error": "forbidden"}, 403)
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
            if u.path == "/api/ws/archive":
                body = self._body()
                return self._json(archive_workspace((body.get("ws") or "").strip()))
            if u.path == "/api/ws/restore":
                body = self._body()
                return self._json(restore_archive((body.get("src") or "").strip()))
            if u.path == "/api/ws/purge":
                body = self._body()
                src = (body.get("src") or "").strip()
                if not ("__PURGE__" == body.get("confirm")):
                    raise CoachError("请传 confirm=__PURGE__ 以确认彻底删除（不可恢复）")
                return self._json(purge_archive(src))
            if u.path == "/api/card/review":
                body = self._body()
                ws = (body.get("ws") or "").strip()
                card_id = (body.get("id") or "").strip()
                remembered = bool(body.get("remembered"))
                if not card_id:
                    raise CoachError("缺少卡片 id")
                return self._json({"ok": True, **review_card(ws, card_id, remembered)})
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
            if u.path == "/api/material/delete-check":
                body = self._body()
                return self._json(material_delete_check(
                    (body.get("ws") or "").strip(), (body.get("name") or "").strip()))
            if u.path == "/api/material/delete":
                body = self._body()
                return self._json(material_delete(
                    (body.get("ws") or "").strip(), (body.get("name") or "").strip(),
                    body.get("confirm") or ""))
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
                    raise CoachError("自动分析进行中（%s，第 %s/%s 段），完成后即可对话；也可在资料卡点「停止」。" % (
                        st.get("stage"), st.get("segment_no", "?"), st.get("segments_total", "?")))
                return self._json(coach_reply(ws, message))
            if u.path == "/api/material/open":
                body = self._body()
                return self._json({"ok": True, "message":
                    open_material_external(body.get("ws", ""), body.get("name", ""), body.get("page"))})
            if u.path == "/api/ocr":
                import tempfile
                body = self._body()
                try:
                    data = base64.b64decode(body.get("b64", ""))
                except Exception:
                    raise CoachError("图片数据无效")
                if not data:
                    raise CoachError("图片为空")
                if len(data) > 20 * 1024 * 1024:
                    raise CoachError("图片过大（>20MB）")
                with tempfile.TemporaryDirectory() as td:
                    png = Path(td) / "pasted.png"
                    png.write_bytes(data)
                    text = ocr_image(png)
                if not text:
                    raise CoachError("未识别到文字（截图里要有清晰的文字内容）")
                return self._json({"ok": True, "text": text[:2000]})
            if u.path == "/api/screen/capture":
                return self._json({"ok": True, "text": screen_capture_ocr()[:2000]})
            if u.path == "/api/pipeline/start":
                body = self._body()
                ws = (body.get("ws") or "").strip()
                if not ws or not ws_dir(ws).is_dir():
                    raise CoachError("工作区不存在")
                if get_pipeline(ws).get("running"):
                    raise CoachError("自动分析已在进行中")
                idx = _load_json(ws_dir(ws) / "rag-index.json", {})
                if not idx.get("chunks"):
                    err_hint = ""
                    for e in idx.get("errors", []):
                        err_hint = "（%s：%s）" % (e.get("file", ""), e.get("error", ""))
                        break
                    raise CoachError("该主题还没有可索引的资料，请先上传%s" % err_hint)
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
    for exe in _browser_exes():
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
    # LC_PORT 指定端口（调试用）；单实例：已有实例在跑 → 只打开应用窗口后退出
    env_port = os.environ.get("LC_PORT", "").strip()
    if env_port:
        port = int(env_port)
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        url = "http://127.0.0.1:%d/" % port
        print("learn-coach 学习系统已启动: %s（LC_PORT 指定端口）" % url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
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
