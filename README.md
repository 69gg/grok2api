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

- `ssoBasic`：仅支持 `grok-4.3-fast`、`grok-imagine-1.0`、`grok-imagine-1.0-edit`。
- `ssoSuper`：保持旧项目模型能力。
- 图片生成/编辑内部使用 `grok-4.3`。

## 检查

```bash
uv run ruff check
uv run mypy
uv run pytest
```
