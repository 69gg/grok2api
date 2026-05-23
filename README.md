# Grok2API

Python/uv 版 Grok2API。项目只支持 uv 工作流，不提供 Docker、npm、wrangler 或 `python main.py` 启动方式。

## 快速开始

```bash
uv sync
cp config.toml.example config.toml
uv run granian --interface asgi grok2api.main:app --host 0.0.0.0 --port 8000
```

## 配置

- `config.toml`：本地运行配置，已加入 `.gitignore`。
- `config.toml.example`：完整示例配置，提交到仓库。
- 环境变量可覆盖少量路径和日志配置：`DATA_DIR`、`LOG_DIR`、`LOG_LEVEL`。

## Cloudflare Clearance

`proxy.enabled = true` 时，服务启动后会通过本地 Turnstile solver 立即获取一次 `cf_clearance`，之后按 `proxy.refresh_interval` 自动续期，并写回 `config.toml` 的 `proxy.cf_clearance` / `proxy.cf_cookies` / `proxy.user_agent`。

solver 运行依赖已放入默认依赖，仍然只使用 uv 安装和启动。首次使用 Chromium 回退路线时，Playwright 会按官方方式执行 `python -m playwright install chromium` 安装浏览器二进制。

## API

保留旧 Python 版 API 能力，包括 OpenAI 兼容聊天、Responses、Anthropic Messages、图片、视频、上传、文件访问、管理 API、function API 和自动注册 API。

## 模型池规则

- `ssoBasic`：支持 `grok-4.3-fast`、`grok-imagine-1.0`、`grok-imagine-1.0-edit`，以及全部 **Console Chat Playground** 模型（见下）。
- `ssoSuper`：保持旧项目模型能力。
- 图片生成/编辑内部使用 `grok-4.3`。

## Console Chat Playground 模型

通过 `console.x.ai` SSO 逆向的 Playground 免费高级模型。在 `GET /v1/models` 中 `owned_by` 为 `xai-console`。

| model_id | 说明 |
|---|---|
| `grok-4.3` / `grok-4.3-search` | grok-4.3；`-search` 自动启用 web/x 搜索 |
| `grok-build-0.1` / `grok-build-0.1-search` | build 模型 |
| `grok-4.20-0309-non-reasoning` / `...-search` | 支持 frequency/presence penalty |
| `grok-4.20-0309-reasoning` / `...-search` | 内置 reasoning + encrypted CoT |
| `grok-4.20-multi-agent-0309` / `...-search` | multi-agent |

### 与 grok.com 通道的区别

- Console 模型走 **xAI Responses API 原生 tool call**，不使用 grok prompt 的 `<call>` 协议。
- Token 用量来自上游 `response.completed.usage`，不是本地估算。

### 客户端多轮对话约定

1. Playground 上游默认 `store=false`，**不要依赖** `previous_response_id`；每次请求应携带 **完整对话 history**。
2. Reasoning 模型会返回 `reasoning_content`（Chat）、`thinking` block（Anthropic）或 Responses `reasoning` item；其中 encrypted blob **不可解密**，但应 **原样保存并在下一轮原样回传**，以保留内部推理上下文。
3. `grok-4.3` 默认返回 **明文 reasoning summary**（非 encrypted）；其余 reasoning 模型为 encrypted 透传。
4. `-search` 变体会自动注入 `web_search_preview` 与 `x_search`，无需客户端手动添加搜索 tools。

配置见 `config.toml.example` 的 `[console]` 段。

## 检查

```bash
uv run ruff check
uv run mypy
uv run pytest
```
