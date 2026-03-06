## 本地测试指南（不依赖真实 GitLab/LLM）

### 目标

在本机跑通最小闭环：

- `POST /gitlab/webhook`
- 服务调用 GitLab API 拉 MR diff（这里用 mock GitLab）
- 服务调用 OpenAI-compatible LLM（这里用 mock LLM）
- 服务回写 MR note（写回 mock GitLab）

### 0) 安装依赖

```bash
cd /Users/williamchen/Desktop/project/ai-code-review
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 1) 启动 Mock LLM（端口 9001）

```bash
cd /Users/williamchen/Desktop/project/ai-code-review
source .venv/bin/activate
python -m app.dev.mock_openai_server
```

### 2) 启动 Mock GitLab API（端口 9002）

```bash
cd /Users/williamchen/Desktop/project/ai-code-review
source .venv/bin/activate
python -m app.dev.mock_gitlab_server
```

### 3) 启动 AI Code Review 服务（端口 8000）

> 注意：本项目会严格校验环境变量，缺失就会启动失败（这是期望行为）。

```bash
cd /Users/williamchen/Desktop/project/ai-code-review
source .venv/bin/activate

export GITLAB_BASE_URL="http://127.0.0.1:9002"
export GITLAB_TOKEN="dummy-token"
export GITLAB_WEBHOOK_SECRET="local-secret"

export LLM_BASE_URL="http://127.0.0.1:9001"
export LLM_API_KEY="dummy-key"
export LLM_MODEL="mock-model"

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 4) 手动触发一次 Webhook

```bash
curl -sS -X POST "http://127.0.0.1:8000/gitlab/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: local-secret" \
  -d '{
    "object_kind": "merge_request",
    "user": { "username": "local" },
    "project": { "id": 123, "web_url": "http://example.local/project" },
    "object_attributes": {
      "iid": 1,
      "action": "update",
      "last_commit": { "id": "1111111111111111111111111111111111111111" }
    }
  }'
```

预期返回：

```json
{"status":"ok"}
```

### 5) 验证是否“回写评论”成功

```bash
curl -sS "http://127.0.0.1:9002/__debug__/notes" | python -m json.tool
```

你会看到 `notes[0].body` 里出现合成的 review 评论。

---

## 调试指南

### 调试架构

项目已集成结构化日志系统 (`app/debug_utils.py`)，提供以下功能：

1. **Request ID 追踪**：每个请求有唯一标识，便于追踪完整链路
2. **步骤计数器**：清晰显示当前执行到第几步
3. **耗时统计**：每个步骤的执行时间
4. **分层日志**：DEBUG/INFO/WARNING/ERROR 级别控制

### 日志输出格式

```
时间 | 级别 | [请求ID] Step 序号 | 模块名 | 消息
```

示例：
```
2026-03-05 14:30:01 | INFO  | [a1b2c3d4] Step 01 | tracker.gitlab_webhook | [开始] → 解析 Webhook Event
2026-03-05 14:30:01 | INFO  | [a1b2c3d4] Step 02 | tracker.gitlab_webhook | [+0.05s] → 获取 MR 变更列表
2026-03-05 14:30:02 | INFO  | [a1b2c3d4] Step 03 | tracker.gitlab_webhook | [+1.23s] → 同步仓库
```

### PyCharm 调试配置

1. **创建 Run Configuration**
   - Run → Edit Configurations → + → Python
   - Script path: 选择 `uvicorn`（在虚拟环境 bin 目录）
   - Parameters: `app.main:app --host 127.0.0.1 --port 8000 --reload`
   - Working directory: 项目根目录
   - Environment variables: 配置必需的环境变量（见上方）

2. **设置断点**
   - 在 `app/review/orchestrator.py` 的 `run_review` 函数入口
   - 在 `app/review/planner.py` 的 `plan_risk` 函数
   - 在 `app/review/reviewer.py` 的 `review_high_risk_files` 函数

3. **使用 Evaluate Expression**
   - 暂停时可以查看 `context.changes`、`plan.highRiskFiles` 等变量

### 日志级别控制

在 `app/main.py` 中修改：

```python
# 查看所有细节（包括变量值）
setup_logging(level=logging.DEBUG)

# 只看主要步骤
setup_logging(level=logging.INFO)

# 只看警告和错误
setup_logging(level=logging.WARNING)
```

### 核心流程步骤说明

**启动阶段：**
1. Step 1: 加载配置（环境变量校验）
2. Step 2: 创建 HTTP Client
3. Step 3: 初始化 LLM Client
4. Step 4: 构建 Review Orchestrator（含数据库连接）
5. Step 5: 创建 FastAPI 应用
6. Step 6: 注册 Webhook 路由

**Webhook 处理阶段（以 GitLab 为例）：**
1. Step 01: 解析 Webhook Event
2. Step 02: 获取 MR 变更列表
3. Step 03: 同步仓库（git clone/pull）
4. Step 04: 确保初始索引存在
5. Step 05: 构建 Review Context
6. Step 06: 执行 AI Review
7. Step 07: 发送评论到 GitLab MR

**AI Review 阶段：**
1. Step 01: 开始 Review（列出变更文件）
2. Step 02: Risk Planning（LLM 分析风险）
3. Step 03: 构建上下文包（检索相关代码）
4. Step 04: 文件级 Review（逐个审查高风险文件）
5. Step 05: 合成最终评论

### 常见问题排查

| 问题 | 检查点 |
|------|--------|
| 程序卡在启动 | 查看 Step 4，可能是数据库连接问题 |
| Review 很慢 | 查看 Step 02-04 的耗时，定位瓶颈 |
| LLM 报错 | 查看 DEBUG 日志中的请求/响应内容 |
| 索引失败 | 查看 `ensure_initial_index` 相关日志 |

### 手动触发调试

```bash
# 带详细日志的 curl 请求
curl -v -X POST "http://127.0.0.1:8000/gitlab/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: local-secret" \
  -d @- << 'EOF'
{
  "object_kind": "merge_request",
  "user": { "username": "local" },
  "project": { "id": 123, "web_url": "http://example.local/project" },
  "object_attributes": {
    "iid": 1,
    "action": "update",
    "target_branch": "main",
    "last_commit": { "id": "1111111111111111111111111111111111111111" }
  }
}
EOF
```

### 使用 debug_utils 的示例代码

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
        # ...
```
