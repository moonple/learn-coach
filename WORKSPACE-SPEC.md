# WORKSPACE-SPEC v2 — 学习工作区规范

learn-coach v2 的全部产出物落在本地工作区（`learn-coach\workspaces\<主题>\`）。一切状态实时写盘，用户说"继续学习"时从 progress.json 恢复断点。本规范与 SKILL.md v2 配套；v1 工作区按最低字段兼容读取，升级时只增不删。

## 1. 目录结构

```
learn-coach\
├── SKILL.md                      # 技能定义（v2）
├── WORKSPACE-SPEC.md             # 本规范
├── domains\                      # 领域适配包（<领域>.md）
│   └── 通用.md                   # 默认领域包
├── templates\                    # 产出物模板（v2 字段）
│   ├── plan-template.md
│   ├── quiz-template.md
│   ├── exam-template.md
│   ├── report-template.md
│   └── 家长周报-template.md
└── workspaces\<主题>\
    ├── profile.md                # 学员档案
    ├── material-index.md         # 资料索引（编号/权威等级/时效）
    ├── materials\                # 原始资料副本与解析产物
    ├── knowledge-map.md          # 知识图谱（节点表+三类边）
    ├── coverage-check.md         # 覆盖率检查表
    ├── plan.md                   # 学习计划（确认后锁定，改动留痕）
    ├── progress.json             # 机器可读状态（断点恢复）
    ├── notes\                    # 教学笔记（每模块）
    ├── quiz\                     # 模块测验（试卷+密封答案+评分）
    ├── mistakes.md               # 错题本
    ├── flashcards.md             # 间隔重复卡片（与 progress.json 同步）
    ├── exam\                     # 终考（考纲封存+试卷+答卷+评分）
    ├── report.md                 # 学习报告（阶段 6）
    └── assets\                   # 图文视频资源预留
```

## 2. profile.md 必含字段

学段（小学/初中/高中/大学/成人）、领域、发起人身份（本人/家长）、目标、计划时长、每日投入、考源偏好。

## 3. knowledge-map.md 格式

- 知识点表：编号、名称、类型（事实/概念/程序/原理/案例）、出处（M0x 第x章）、Bloom 层级、模块归属、状态（未学/疑似掌握/已掌握）。
- 三类边清单：前置边（先学 A 才能懂 B）、关联边（原理相通）、易混边（常被混淆，出干扰项的原料）。

## 4. coverage-check.md 格式

资料章节 → 模块 → 知识点数 → 状态[已覆盖/经确认排除+理由]。未覆盖项必须写明理由并经用户确认，不许静默丢弃。

## 5. progress.json 最低字段（兼容 v1）

```json
{
  "topic": "主题",
  "profile": {"stage": "成人", "domain": "法律", "initiator": "本人"},
  "status": "learning | paused | passed",
  "current_module": "M3",
  "modules": [
    {"id": "M1", "name": "…", "status": "not-started|learning|quiz-failed|passed|exempt-pending",
     "quiz_history": [{"date": "2026-10-07", "score": 0.75}],
     "evidence": "quiz\\M1-v1.md"}
  ],
  "flashcards": [
    {"id": "F1", "front": "…", "module": "M1", "interval_days": 1, "due": "2026-10-08"}
  ],
  "exams": [{"date": "…", "level": "L2|L3", "score": 0.86, "grade": "优秀", "evidence": "exam\\…"}]
}
```

- v1 文件缺 profile 等字段时按默认值补齐读取，不要求迁移。

## 6. 领域适配包（domains\<领域>.md）模板

知识特点 / 题型配比 / 教学风格 / 权威资料源清单 / 版本敏感性 / L3 考源清单 / 安全边界。首次遇到新领域时按此模板生成草稿，经用户确认后保存。领域包优先级高于 SKILL.md 通用规则。

## 7. templates\ 必备五个

plan、quiz、exam、report、家长周报，字段与本规范一致。
