# learn-coach · AI 学习教练

把任意学习资料变成一个带你学习的 AI 教练系统：**资料进、证书出**。

## 它做什么

```
上传资料（.md/.txt/.docx/.pdf）或让 AI 联网找
  → 自动分析流水线：逐段消化 → 生成知识图谱 → 生成学习计划 → 填充模块
  → 每日学习循环：清到期卡片 → 教学（讲解+提问+费曼复述）→ 模块测验
  → 错题本 + 间隔重复（1/3/7/14/30 天）
  → 客观终考（盲考官出题阅卷分离）→ 学习报告
```

## 特性

- **任意领域**：考证、教材、技能、兴趣（中医/法律/金融/编程…），小学到成人可用（学段自适应）
- **本地优先**：数据全部存在本机文件夹，断网可开，删除即卸载
- **客观考核**：评分 rubric 先行、逐题引用资料出处、及格线考前锁定不放水
- **简洁界面**：六个页面（总览 / AI 教练 / 知识图谱 / 复习 / 资料 / 设置），左侧导航直达
- **复习自助闭环**：卡片翻面自测后直接判定「记住了 / 没记住」，间隔 1→3→7→14→30→60 天自动排期，无需经过教练
- **图谱路径高亮**：点击知识点，高亮它的全部前置学习路径（金色流动边），右侧显示详情（卡片/错题/笔记/原文定位）
- **安全删除**：归档式删除（workspaces\_archives，可随时恢复），彻底删除需二次确认
- **零依赖分发**：PyInstaller 打包单文件 exe，收件人免装 Python

## 目录结构

```
learn-coach/
├── SKILL.md              # 引擎行为规则（教学/考核/安全边界）
├── WORKSPACE-SPEC.md     # 工作区文件规范
├── dashboard/
│   ├── server.py         # 本地服务器 + AI 教练（智谱 API + RAG + action 写盘协议）
│   ├── index.html / app.js / style.css   # 前端（零依赖原生 JS）
│   ├── make_icon.py      # 应用图标生成（纯标准库）
│   └── manifest.webmanifest / assets/
├── domains/              # 领域适配包（通用/信息安全…）
├── templates/            # 计划/测验/终考/报告/家长周报 模板
└── 启动仪表盘.bat         # 调试启动（正式入口为打包后的 exe）
```

## 快速开始（开发者）

```bash
# 1. 直接运行（需 Python 3.8+，无第三方依赖）
python dashboard/server.py
# 浏览器打开 http://127.0.0.1:8765/ → 设置页填智谱 API Key

# 2. 打包桌面应用（可选，需 pip install pyinstaller）
python -m PyInstaller --onefile --noconsole --name learn-coach ^
  --icon "dashboard/assets/app.ico" ^
  --add-data "<绝对路径>/dashboard;dashboard" ^
  dashboard/server.py
```

## 使用

1. 双击打包后的 exe（或 `python dashboard/server.py`；调试可用环境变量 `LC_PORT` 指定端口）
2. 「设置」页填入智谱 API Key（[open.bigmodel.cn](https://open.bigmodel.cn)；Coding Plan 套餐 Key 接口地址用 `https://open.bigmodel.cn/api/coding/paas/v4`）
3. 「＋ 新主题」立项：填目标/周期/每日投入 → 上传资料 → 自动分析 → 开始学习
4. 日常：总览页点「开始学习」；说「继续学习 / 出题考我 / 没懂再讲一遍」；到「复习」页清到期卡片

## 注意

- 学习数据与 Key 保存在本机（`workspaces\`、`config.json`，均已 gitignore）
- 中医/法律/金融内容仅供学习参考，不构成诊疗/法律/投资建议
- 教学评分由 AI 生成，重要考试请以官方为准

## License

MIT
