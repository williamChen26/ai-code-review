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


