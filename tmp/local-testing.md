# 本地测试指南

## 前置条件

| 依赖 | 说明 |
|------|------|
| Python 3.11+ | 建议 3.12 |
| PostgreSQL + pgvector | 索引存储，可用 Docker 启动 |
| git | 仓库 clone/pull |

## 0) 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 1) 启动 PostgreSQL（如果本地没有）

```bash
docker run -d --name pgvector \
  -e POSTGRES_USER=codeindex \
  -e POSTGRES_PASSWORD=codeindex \
  -e POSTGRES_DB=codeindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

## 2) 启动 Mock LLM（端口 9001）

```bash
source .venv/bin/activate
python -m app.dev.mock_openai_server
```

## 3) 启动 Mock GitLab API（端口 9002）

```bash
source .venv/bin/activate
python -m app.dev.mock_gitlab_server
```

## 4) 启动 AI Code Review 服务（端口 8000）

> 服务启动时会严格校验环境变量，缺失即报错（这是期望行为）。

```bash
source .venv/bin/activate

# --- 基础设施 ---
export LLM_BASE_URL="http://127.0.0.1:9001"
export OPENAI_API_KEY="dummy-key"

export INDEX_PG_DSN="postgresql://codeindex:codeindex@127.0.0.1:5432/codeindex"
export INDEX_REPO_BASE_DIR=".repos"
export INDEX_GIT_BIN="$(which git)"

# --- GitLab 集成（指向 Mock Server）---
export GITLAB_BASE_URL="http://127.0.0.1:9002"
export GITLAB_TOKEN="dummy-token"
export GITLAB_WEBHOOK_SECRET="local-secret"

# --- GitHub 集成（可选，如需同时测试）---
# export GITHUB_API_BASE_URL="https://api.github.com"
# export GITHUB_TOKEN="ghp_your_token"
# export GITHUB_WEBHOOK_SECRET="local-github-secret"

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 5) 测试 GitLab Webhook

### 发送 MR 事件

```bash
curl -sS -X POST "http://127.0.0.1:8000/gitlab/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: local-secret" \
  -d '{
    "object_kind": "merge_request",
    "user": { "username": "local-tester" },
    "project": {
      "id": 123,
      "web_url": "http://localhost:9002/group/project",
      "git_http_url": "https://github.com/psf/black.git"
    },
    "object_attributes": {
      "iid": 1,
      "action": "update",
      "target_branch": "main",
      "source_branch": "feat/test",
      "last_commit": { "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
    }
  }'
```

> **`git_http_url` 说明**：服务会用这个 URL 做 `git clone`。上面示例用了一个公开仓库。
> 如果你不需要索引功能（首次测试会触发 clone），可以先用 `/index/full` 手动建索引。
> 如果 clone 步骤报错，可暂时忽略，Webhook 解析和 Mock API 调用仍然能验证。

预期返回（如果整条链路跑通）：

```json
{"status":"ok"}
```

### 验证评论是否写回成功

```bash
curl -sS "http://127.0.0.1:9002/__debug__/notes" | python -m json.tool
```

你会看到 `notes[0].body` 里出现合成的 review 评论。

## 6) 测试 GitHub Webhook

GitHub Webhook 需要 HMAC SHA256 签名。以下脚本可自动计算签名并发送：

```bash
SECRET="local-github-secret"

PAYLOAD='{
  "action": "opened",
  "pull_request": {
    "number": 42,
    "head": { "sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "ref": "feat/test" },
    "base": { "ref": "main" },
    "merged": false
  },
  "repository": {
    "name": "my-project",
    "owner": { "login": "my-org" },
    "full_name": "my-org/my-project",
    "clone_url": "https://github.com/psf/black.git"
  }
}'

SIGNATURE="sha256=$(printf '%s' "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"

curl -sS -X POST "http://127.0.0.1:8000/github/webhook" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  -d "$PAYLOAD"
```

> **前提**：需要在启动服务时配置 GitHub 环境变量（取消步骤 4 中的注释）。
> GitHub 没有内置 Mock Server，需要真实的 GitHub Token 和可访问的 API。

## 7) 手动全量索引（可选）

如果你想先建好索引再测试 Webhook，可以单独触发：

```bash
curl -sS -X POST "http://127.0.0.1:8000/index/full" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "gitlab",
    "repo_key": "123",
    "clone_url": "https://github.com/psf/black.git",
    "branch": "main"
  }'
```

---

## 调试指南

### 日志格式

```
时间 | 级别 | [请求ID] Step 序号 | 模块名 | 消息
```

示例：

```
2026-03-05 14:30:01 | INFO  | [a1b2c3d4] Step 01 | tracker.gitlab_webhook | [开始] → 解析 Webhook Event
2026-03-05 14:30:01 | INFO  | [a1b2c3d4] Step 02 | tracker.gitlab_webhook | [+0.05s] → 获取 MR 变更列表
2026-03-05 14:30:02 | INFO  | [a1b2c3d4] Step 03 | tracker.gitlab_webhook | [+1.23s] → 同步仓库
```

### 日志级别

在 `app/main.py` 中修改：

```python
setup_logging(level=logging.DEBUG)    # 所有细节（含变量值）
setup_logging(level=logging.INFO)     # 主要步骤
setup_logging(level=logging.WARNING)  # 只看警告和错误
```

### 核心流程步骤

**启动阶段：**

1. 加载配置（环境变量校验）
2. 创建 HTTP Client
3. 初始化 LLM Client
4. 构建 Review Orchestrator（含数据库连接）
5. 创建 FastAPI 应用
6. 注册 Webhook 路由

**Webhook 处理（GitLab / GitHub 共用 pipeline）：**

1. 解析 Webhook Event
2. 获取 MR changes / PR files
3. 同步仓库（git clone/pull）
4. 确保初始索引存在
5. 构建 ReviewContext
6. 执行 AI Review
7. 回写评论

**AI Review 内部阶段：**

1. Risk Planning — LLM 分析变更文件风险等级
2. 构建上下文包 — 检索 changed/related symbols
3. 文件级 Review — 逐个审查高风险文件
4. 合成最终评论 — 确定性 Markdown 拼接

### 常见问题排查

| 问题 | 检查点 |
|------|--------|
| 服务启动失败 | 检查环境变量是否齐全，Postgres 是否可连接 |
| 卡在 Step 3（同步仓库） | `git_http_url` 是否可访问，Token 是否有 clone 权限 |
| Review 很慢 | 查看各 Step 耗时，通常瓶颈在 LLM 调用 |
| LLM 报错 | 检查 `LLM_BASE_URL` 和 `OPENAI_API_KEY` |
| GitLab API 报错 | 检查 `GITLAB_BASE_URL` 和 `GITLAB_TOKEN` |
| 索引失败 | 检查 `INDEX_PG_DSN` 连接、pgvector 扩展是否安装 |
| 收到 `overflow` 警告 | MR 变更文件过多，部分 diff 被截断，属于正常降级 |

### PyCharm 调试配置

1. **Run Configuration**
   - Run → Edit Configurations → + → Python
   - Script path: `.venv/bin/uvicorn`
   - Parameters: `app.main:app --host 127.0.0.1 --port 8000 --reload`
   - Working directory: 项目根目录
   - Environment variables: 配置步骤 4 中的环境变量

2. **推荐断点位置**
   - `app/review/orchestrator.py` → `run_review()` 入口
   - `app/review/planner.py` → `plan_risk()`
   - `app/review/reviewer.py` → `review_high_risk_files()`

3. **Evaluate Expression**
   - 暂停时查看 `context.changes`、`plan.highRiskFiles`、`comments` 等变量

### 使用 debug_utils

```python
from app.debug_utils import get_logger, step_tracker

logger = get_logger(__name__)

async def my_function():
    with step_tracker("my_operation") as tracker:
        tracker.step("第一步：准备数据")
        # ... 业务逻辑

        tracker.step("第二步：调用外部服务")
        # ... 业务逻辑

        tracker.substep("处理响应")  # 子步骤，不增加主步骤计数
```
