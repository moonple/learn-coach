#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建 learn-coach 桌面快捷方式（pywin32，Unicode 安全）。
目标显式指向 wscript.exe，绕过 .vbs 文件关联被改/被拦的情况。
运行：python dashboard/make_shortcut.py
"""
import os
import sys

try:
    import win32com.client
except ImportError:
    print("ERROR: pywin32 未安装（pip install pywin32）")
    sys.exit(1)

ROOT = r"C:\代码文件夹\learn-coach"
EXE = os.path.join(ROOT, "learn-coach.exe")
ICON = os.path.join(ROOT, "dashboard", "assets", "app.ico")
DESKTOP = os.path.join(os.environ["USERPROFILE"], "Desktop")
LNK = os.path.join(DESKTOP, "learn-coach 学习系统.lnk")

if not os.path.exists(EXE):
    print("ERROR: 缺文件", EXE)
    sys.exit(1)

# 清理任何旧的/乱码 learn-coach 快捷方式
for name in os.listdir(DESKTOP):
    if name.lower().startswith("learn-coach") and name.lower().endswith(".lnk"):
        try:
            os.remove(os.path.join(DESKTOP, name))
            print("removed old:", name)
        except OSError:
            pass

ws = win32com.client.Dispatch("WScript.Shell")
lnk = ws.CreateShortcut(LNK)
lnk.TargetPath = EXE
lnk.WorkingDirectory = ROOT
lnk.IconLocation = ICON
lnk.Description = "AI 学习教练：资料进、证书出"
lnk.Save()

# 回读校验
check = ws.CreateShortcut(LNK)
print("created:", LNK)
print("target:", check.TargetPath)
assert check.TargetPath.lower().endswith("learn-coach.exe")
print("VERIFY-OK")
