# AI Code Review + GitLab/GitHub（Python）

## 目标

- 接收 GitLab Merge Request Webhook
- 接收 GitHub Pull Request Webhook
- 拉取 MR changes/diff，构建上下文
- LLM 做 Risk Planning（单次、不 loop）
- 文件级 Focused Review（可选受控 ReAct + 工具）
- 回写 GitLab MR note / GitHub PR review（先全局评论；行内评论接口预留）

## 运行

准备环境变量（可参考 `env.example`），然后：

```bash
cd /Users/chenweimin/mySpaces/ai-code-review
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查：`GET /health`

Webhook：`POST /gitlab/webhook`
Webhook：`POST /github/webhook`

## 本地测试（无需真实 GitLab/LLM）

如果你暂时没有 GitLab 实例或 OpenAI-compatible LLM 网关，也可以用本项目内置的 mock server 在本机跑通闭环。

详细步骤见：`tmp/local-testing.md`

## 环境变量

- **GITLAB_BASE_URL**: 例如 `https://gitlab.example.com`（可选；配置了才启用 GitLab webhook）
- **GITLAB_TOKEN**: 用于调用 GitLab API 的 Token
- **GITLAB_WEBHOOK_SECRET**: Webhook 里配置的 Secret Token（对应 Header `X-Gitlab-Token`）
- **GITHUB_API_BASE_URL**: 例如 `https://api.github.com`；GitHub Enterprise 常见为 `https://github.example.com/api/v3`（可选；配置了才启用 GitHub webhook）
- **GITHUB_TOKEN**: 用于调用 GitHub API 的 Token（建议机器人账号 / fine-grained token）
- **GITHUB_WEBHOOK_SECRET**: GitHub Webhook 的 Secret（用于校验 `X-Hub-Signature-256`）
- **LLM_BASE_URL**: 你后续提供（OpenAI-compatible）
- **LLM_API_KEY**: 你后续提供
- **LLM_MODEL**: 例如 `claude-sonnet-4`（按你的网关定义）

## GitLab Webhook 建议

- 监听：`merge_request`（open/update）
- 用 `MR IID + head SHA` 做幂等（本项目预留 key，但默认不做持久化）
