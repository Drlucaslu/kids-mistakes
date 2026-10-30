# 错题本（cuotiben）

家庭错题本 + 任务跟踪。家里的 tutor（语文 / 英语 / 数学）拍照录入孩子做错的题，系统按间隔复习把题"翻出来"复测，
直到孩子掌握；tutor 布置任务，孩子打勾，tutor 确认；家长看周报。部署在自家 Olares One 上。

## 角色

| 角色 | 能做什么 |
|---|---|
| 家长 | 全部；管理账号、错因、知识点、AI 模型、备份。从 Olares 桌面打开时自动以家长身份登录 |
| 老师 | 只看/改自己负责的科目：录错题、复测 ✓/✗、审核变式题、布置和确认任务、看周报 |
| 孩子 | 看"今天"：要复习的题（做完才能看答案）、勾任务；看自己的错题本。不能自己把题标为掌握 |

界面语言：English（默认）/ 中文，家长在"设置 → 语言"里切换，对所有人生效；AI 写的讲解和知识点名称也跟着用这个语言。
文字都写成中文原文并经过 `_()`，英文在 `cuoti/i18n.py` 的 `EN` 里；`tests/test_i18n.py` 会检查没有漏翻的字符串。

登录用 PIN：孩子至少 4 位，老师/家长至少 6 位；连续输错 5 次锁 10 分钟。

## 复习规则

录入后第 1、3、7、14、30 天复测（`cuoti/schedule.py`）。老师复测答对进下一档，答错回到第 1 天；
连续答对 3 次 = 已掌握，30 天后再抽查一次。孩子自己点"我做完了"只记录练习、解锁答案，不推进排期。

## AI（可选）

- 拍照保存后自动识别题目、孩子答案、正确答案，并建议知识点和错因 —— 只填空着的栏目，不覆盖老师写的。
- "让 AI 出 2 道变式题"：同知识点、同难度；老师审核（可改字）后才进入孩子的复习。
- 默认 Claude（`claude-opus-5-5`），也可在"设置"里切到 OpenAI 兼容接口（本地 VLM）。没有 Key 时 AI 按钮置灰，其余照常。
- 发给模型的只有照片和题目文字，不含孩子姓名。
- 用 OpenAI：设置 → AI 模型里选"OpenAI 兼容接口"，Base URL 填 `https://api.openai.com/v1`，模型名填一个能看图的模型；
  Key 放在 Olares 环境变量 `MODEL_API_KEY`（不是 `ANTHROPIC_API_KEY`）。

## 本地开发

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
DATA_DIR=~/.cache/cuoti/dev INIT_PARENT_PIN=123456 MODEL_PROVIDER=mock PORT=8765 \
  sh -c '.venv/bin/python scripts/seed_demo.py && .venv/bin/python -m cuoti'
```

演示账号 PIN：哥哥 1111、妹妹 2222、老师 333333 / 444444 / 555555、家长 123456。

## 部署到 Olares

与 kinnet-ai-coo 相同的免镜像仓库方式：运行镜像是官方 `python:3.12-slim`，代码 + wheels 打包后用
`olares-cli files upload` 传到 `drive/Data/cuotiben/bundle/`，init 容器离线安装。没有公开镜像。

```bash
scripts/deploy-olares.sh install          # 首次；读取 ~/.cache/cuotiben/olares-deploy.env（600）
scripts/deploy-olares.sh upgrade          # 之后每次：自动 +1 patch 版本并升级
```

- 访问地址：`https://<随机前缀>.<你的 Olares ID>.olares.com`；想要好记的地址可设三级域名，
  如 `olares-cli settings apps domain set cuotiben cuotiben --third-level class` → `class.<你的 Olares ID>.olares.com`。
- 数据：`/data/app`（Olares 文件 → Data → cuotiben → app）里是 `cuoti.db`、`photos/`、`backups/`（每天一份，留 30 份）。
- 改环境变量（如 API Key）：Olares 设置 → 应用 → 错题本 → 环境变量，或 `olares-cli settings apps env set cuotiben --var ANTHROPIC_API_KEY=...`。

## 代码结构

- `cuoti/web.py` — 全部页面与表单处理（FastAPI + Jinja2 服务端渲染）
- `cuoti/schedule.py` — 复习排期；`cuoti/report.py` — 周报统计；`cuoti/ai.py` — 识题与变式题
- `cuoti/db.py` — SQLite 表结构；`cuoti/auth.py` — PIN 与会话；`cuoti/backup.py` — 每日备份
- `deploy/olares/cuotiben` — Olares chart；`scripts/deploy-olares.sh` — 部署脚本
