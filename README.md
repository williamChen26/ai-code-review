# AI Code Review

基于 LLM 的自动化代码审查服务，支持 **GitLab**（含自托管）和 **GitHub**。

接收 Webhook → 拉取变更 diff → AST 索引 + 向量检索构建上下文 → LLM 风险规划 → 文件级聚焦审查 → 回写评论。

## 架构概览

```
Webhook (GitLab MR / GitHub PR)
  │
  ▼
┌─────────────────────────────────────────────────────┐
│  FastAPI 服务                                        │
│                                                      │
│  1. 校验签名 / Token                                 │
│  2. 拉取 MR changes / PR files                      │
│  3. git clone/pull → AST 解析 → pgvector 索引       │
│  4. diff → changed symbols → related symbols        │
│  5. LLM Risk Planning（JSON 输出）                   │
│  6. LLM 文件级 Focused Review（JSON 输出）           │
│  7. 确定性合成 Markdown → 回写评论                   │
└─────────────────────────────────────────────────────┘
         │                         │
    GitLab API               GitHub API
  (MR note)              (PR review)
```

## 项目结构

```
app/
├── main.py                 # FastAPI 入口，路由注册
├── config.py               # 环境变量加载与校验
├── debug_utils.py          # 结构化日志，步骤追踪
├── github/
│   ├── webhook.py          # Webhook 路由，HMAC SHA256 签名校验
│   ├── client.py           # GitHub API 客户端（list PR files / create review）
│   ├── adapter.py          # GitHub PR files → 平台无关 ReviewContext
│   └── schemas.py          # Pydantic schemas（webhook event / API response）
├── gitlab/
│   ├── webhook.py          # Webhook 路由，X-Gitlab-Token 校验
│   ├── client.py           # GitLab API 客户端（get MR changes / post note）
│   ├── adapter.py          # GitLab MR changes → 平台无关 ReviewContext
│   └── schemas.py          # Pydantic schemas（webhook event / API response）
├── review/
│   ├── orchestrator.py     # 核心流程编排（webhook → review → post）
│   ├── planner.py          # LLM 风险规划（单次 JSON 输出）
│   ├── reviewer.py         # LLM 文件级审查（逐文件 JSON 输出）
│   ├── synthesis.py        # 确定性 Markdown 合成
│   ├── models.py           # 领域模型（FileChange / ReviewContext / RiskPlan 等）
│   ├── context.py          # 语言推断
│   ├── context_retrieval.py# Symbol 检索，构建 FileReviewContext
│   └── diff_parser.py      # diff → 变更行号提取
├── indexing/
│   ├── indexer.py          # 全量/增量索引
│   ├── repo_sync.py        # git clone/pull
│   ├── parser.py           # AST 解析（Python / TS / JS）
│   └── file_scanner.py     # 可索引文件扫描
├── llm/
│   ├── client.py           # LiteLLM chat completions
│   └── embedding.py        # Embedding API
├── storage/
│   ├── pg.py               # Postgres + pgvector 客户端
│   └── models.py           # DB 模型（FileRecord / SymbolRecord 等）
├── infra/
│   ├── cache.py
│   └── rate_limit.py
└── dev/
    ├── mock_gitlab_server.py   # Mock GitLab API（本地测试用）
    └── mock_openai_server.py   # Mock LLM（本地测试用）
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. 配置环境变量

复制 `env.example` 为 `.env` 并填入实际值（详见下方「环境变量」章节）。

### 3. 启动服务

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查（k8s / LB 探活） |
| `POST` | `/gitlab/webhook` | GitLab MR Webhook（需配置 GitLab 环境变量） |
| `POST` | `/github/webhook` | GitHub PR Webhook（需配置 GitHub 环境变量） |
| `POST` | `/index/full` | 手动触发全量索引（调试 / 初始化用） |

## 环境变量

### 必填

| 变量 | 说明 | 示例 |
|------|------|------|
| `LLM_BASE_URL` | OpenAI-compatible LLM API 地址 | `https://your-llm-proxy.com/v1` |
| `OPENAI_API_KEY` | LLM API Key（由 LiteLLM 自动读取） | `sk-...` |
| `INDEX_PG_DSN` | Postgres + pgvector 连接串 | `postgresql://user:pass@localhost:5432/codeindex` |
| `INDEX_REPO_BASE_DIR` | 仓库 clone 存放目录 | `/data/repos` |
| `INDEX_GIT_BIN` | git 可执行文件路径 | `/usr/bin/git` |

### GitLab 集成（可选，三个必须同时配置）

| 变量 | 说明 | 示例 |
|------|------|------|
| `GITLAB_BASE_URL` | GitLab 实例地址（支持自托管） | `https://gitlab.company.com` |
| `GITLAB_TOKEN` | Personal Access Token（需要 `api` scope） | `glpat-xxxx` |
| `GITLAB_WEBHOOK_SECRET` | Webhook Secret Token | `your-webhook-secret` |

### GitHub 集成（可选，三个必须同时配置）

| 变量 | 说明 | 示例 |
|------|------|------|
| `GITHUB_API_BASE_URL` | GitHub API 地址 | `https://api.github.com` |
| `GITHUB_TOKEN` | GitHub Token（建议 fine-grained PAT） | `ghp_xxxx` |
| `GITHUB_WEBHOOK_SECRET` | Webhook Secret（用于 HMAC SHA256 校验） | `your-webhook-secret` |

> **注意**：GitLab 和 GitHub 至少需要配置一组，否则服务启动会报错。

## GitLab Webhook 配置

在 GitLab 项目 → Settings → Webhooks 中添加：

| 配置项 | 值 |
|--------|-----|
| URL | `https://your-service.com/gitlab/webhook` |
| Secret token | 与 `GITLAB_WEBHOOK_SECRET` 一致 |
| Trigger | 勾选 **Merge request events** |
| SSL verification | 根据你的部署环境选择 |

服务会处理以下 MR action：
- `open` / `reopen` / `update` → 触发 AI Review
- `merge` → 触发增量索引（目标分支为 main 时）

### 自托管 GitLab 说明

本项目完全兼容 GitLab 自托管实例：
- `GITLAB_BASE_URL` 指向你的自托管地址即可
- API 路径遵循标准 GitLab v4 API（`/api/v4/...`）
- Git clone 使用 `oauth2:TOKEN` 格式注入鉴权信息到 HTTPS URL
- 自动启用 `access_raw_diffs=true` 参数，绕过 diff 大小限制

## GitHub Webhook 配置

在 GitHub 仓库 → Settings → Webhooks 中添加：

| 配置项 | 值 |
|--------|-----|
| Payload URL | `https://your-service.com/github/webhook` |
| Content type | `application/json` |
| Secret | 与 `GITHUB_WEBHOOK_SECRET` 一致 |
| Events | 选择 **Pull requests** |

服务会处理以下 PR action：
- `opened` / `reopened` / `synchronize` → 触发 AI Review
- `closed`（且已 merged）→ 触发增量索引（目标分支为 main 时）

## 本地测试

内置 Mock 服务器可在没有真实 GitLab / GitHub / LLM 的情况下验证服务流程。

详见：[`tmp/local-testing.md`](tmp/local-testing.md)

## 开发

```bash
# 运行测试
.venv/bin/python -m pytest tests/ -v

# 代码检查
.venv/bin/ruff check app/ tests/
```
