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
- `proxy.console_proxy_url`：Console 与 **Free CLI（cli-chat-proxy）** 共用代理；为空时回退 `proxy.base_proxy_url`。
- `token.console_team_auto_init_enabled`：默认开启。服务会为缺少 `console_team_id` 的 `ssoBasic` 主动调用 Console `CreateTeam`，并把每号 team id 持久化到账号数据；请求命中缺字段账号时也会先补全再访问 Console。
- `[build]`：Free Grok 4.5 / Composer CLI 通道，**默认全开**（pull 后无需改配置即可用）。后台会像 Console team 一样**静默**为已有 `ssoBasic` 账号铸 OIDC 到 `oidcBuild` 池；请求只轮询**已有 auth** 的号，并按需 `refresh_token` 续期。代理走 `console_proxy_url`。详见 `docs/build_cli_protocol.md`。

## Cloudflare Clearance

`proxy.enabled = true` 时，服务启动后会通过本地 Turnstile solver 立即获取一次 `cf_clearance`，之后按 `proxy.refresh_interval` 自动续期，并写回 `config.toml` 的 `proxy.cf_clearance` / `proxy.cf_cookies` / `proxy.user_agent`。

solver 运行依赖已放入默认依赖，仍然只使用 uv 安装和启动。首次使用 Chromium 回退路线时，Playwright 会按官方方式执行 `python -m playwright install chromium` 安装浏览器二进制。

## API

保留旧 Python 版 API 能力，包括 OpenAI 兼容聊天、Responses、Anthropic Messages、图片、视频、**音频（TTS/STT/translations + STT WebSocket）**、上传、文件访问、管理 API、function API 和自动注册 API。

## 模型池规则

- **`oidcBuild`（Free CLI）**：`grok-4.5`、`grok-4.5-search`、`grok-composer-2.5-fast`、`grok-composer-2.5-fast-search`。`owned_by` 为 `xai-cli<grok2api@69gg>`。上游 `cli-chat-proxy.grok.com`，OIDC Bearer（**不是**网页 SSO）。
- `ssoBasic`：支持 `grok-4.3-fast`、`grok-imagine-1.0`、`grok-imagine-1.0-edit`、`grok-imagine-image`，以及全部 **Console Chat Playground** 与 **Console Voice（TTS/STT/translations + STT WS）** 模型（见下）。
- `ssoSuper`：保持旧项目模型能力。
- 模型目录与同名模型解析统一按 **CLI > Console > ssoBasic/grok.app > ssoSuper** 优先级处理。
- CLI 请求只轮询 `oidcBuild` 中**已有 OIDC auth** 的账号；后台定时从 `ssoBasic` 静默补铸（`build.auto_init_from_sso_enabled`）；access 按需/定时 refresh。
- Grok 与 Console Chat / Voice / Image 上游调用会按候选池顺序轮询可用 SSO 号；单次请求失败重试时会排除本请求已试过的号，再继续选下一个，429 会标记当前号 cooling 后换号重试。
- CLI free-usage 耗尽：仅 cooling 该 OIDC 号约 24h，不影响同 SSO 的 Console 通道。
- `grok-imagine-image` 是 Console 原生图片模型，支持 `/v1/images/generations` 与 `/v1/images/edits`；旧的 `grok-imagine-image-edit` 只保留为 legacy grok.com 路径。

## 图片输入（Vision）

聊天场景下的「看图对话」与「文生图/图生图」是不同能力，请勿混用 model_id。

### 能力概览

| 通道 / 端点 | 图片输入 | 说明 |
|---|---|---|
| Console 模型 + `/v1/chat/completions` | ✅ | OpenAI `image_url` → 上游 `input_image`（URL 直传，不上传） |
| Console 模型 + `/v1/responses` | ✅ | `input_image` 或 message 内嵌图片 block；多轮 replay 支持 |
| Console 模型 + `/v1/messages` | ✅ | Console 原生 Anthropic Messages 透传 |
| grok.com 模型 + `/v1/chat/completions` | ✅ | 图片先上传 Grok assets，再走 `fileAttachments` |
| grok.com 模型 + `/v1/responses` | ✅ | 内部转换为 Chat 消息后走同一上传链路 |
| `grok-imagine-image` | — | Console 原生文生图 / 图生图专用，不是通用 vision chat |
| `/v1/videos/*` | — | 不接 Console 原生 video；继续使用 grok.com legacy 路径 |

### 请求格式

Chat Completions 用户消息示例：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "描述这张图"},
    {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "high"}}
  ]
}
```

Responses API 可直接传 `input_image`：

```json
{
  "role": "user",
  "content": [
    {"type": "input_text", "text": "描述这张图"},
    {"type": "input_image", "image_url": "https://example.com/a.png"}
  ]
}
```

### 输入限制

- `image_url.url` 支持 **HTTPS/HTTP URL** 或 **`data:<mime>;base64,...` data URI**。
- **裸 base64 字符串会被拒绝**，必须带 `data:` 前缀。
- Console 通道不做本地上传，URL 原样转发 upstream；公网 HTTPS 最稳妥。
- grok.com 通道（如 `grok-4.3-fast`）会下载 URL / 解析 data URI 后上传，需 SSO 号具备 upload 能力。

## Free CLI 原生端点（Grok 4.5）

CLI 模型只改 `Authorization` + Grok CLI 客户端头，body **透传**到 `https://cli-chat-proxy.grok.com/v1`：

| 本地端点 | 上游 | 说明 |
|---|---|---|
| `POST /v1/chat/completions` | `/chat/completions` | 含 SSE |
| `POST /v1/messages` | `/messages` | 含 SSE |
| `POST /v1/responses` | `/responses` | Responses 透传 |

`-search` 别名会在 body 中追加 `{"type":"web_search"}`。device-auth 人机失败时浏览器路径会**刷新页面重试**。

## Console 原生端点

Console 原生端点只重写 SSO Cookie、team id、`x-cluster`、Referer 与浏览器类 headers，然后把请求体透传到 `console.x.ai`。返回体同样直接返回给客户端；上游响应头不会原样透传，内部记录的 `set-cookie`、鉴权类 header 会脱敏。

`grok-imagine-image` 命中 Console image 通道后会优先走 Console 原生 `/v1/images/*`；Console 成功时返回体直接透传给客户端，非流式 Console 失败时允许回退到 grok.com legacy 图片链路。OpenAI 兼容的图片 `quality` 会在转发 Console 前规范化：`standard` / `auto` → `medium`，`hd` → `high`，`low` / `medium` / `high` 原样发送。Console 原生文生图不支持 `size` / `style`，这些字段只保留给 fallback legacy 链路使用，不会发往 Console。

Console 原生请求失败时，日志会输出 method、path、status、stream、content type、当前代理配置键、是否携带 team id、脱敏后的请求/响应 headers，以及截断后的请求体和响应体预览。`Cookie`、`Authorization`、token、secret、password、API key 等字段会在日志中替换为 `<redacted>`。

当前直接透传的 Console 原生路径：

| 本地端点 | Console 上游路径 | 说明 |
|---|---|---|
| `POST /v1/responses` | `/v1/responses` | Console chat 模型原生 Responses |
| `POST /v1/chat/completions` | `/v1/chat/completions` | Console chat 模型原生 Chat Completions |
| `POST /v1/messages` | `/v1/messages` | Console chat 模型原生 Anthropic Messages |
| `POST /v1/images/generations` | `/v1/images/generations` | `grok-imagine-image`，强制 `response_format=b64_json` |
| `POST /v1/images/edits` | `/v1/images/edits` | multipart 图片会转成 JSON data URI 后透传，强制 `response_format=b64_json` |
| `POST /v1/audio/speech` | `/v1/audio/speech` | 优先原生；失败时回退旧 `/v1/tts` 映射 |

项目里仍存在但不是 Console 原生透传的路径：

| 本地能力 | 当前实现 |
|---|---|
| `/v1/models` | 本地模型目录，不请求 Console `/v1/models` |
| `/v1/audio/transcriptions`、`/v1/audio/translations`、`/v1/audio/voices` | 继续使用 Console Voice `/v1/stt`、`/v1/tts/voices` 等兼容映射 |
| `WS /v1/audio/stt/ws` | 继续透传 Console Voice WebSocket `/v1/stt` |
| `/v1/videos/*` | 不接 Console video；继续使用 grok.com legacy video 路径 |
| `/v1/files/*`、assets 上传下载 | 本地/grok.com 兼容能力，不是 Console 原生 |

## Console Chat Playground 模型

通过 `console.x.ai` SSO 逆向的 Playground 免费高级模型。在 `GET /v1/models` 中 `owned_by` 为 `xai-console`。

Console Chat 请求优先使用 `proxy.console_proxy_url`，未配置时回退 `proxy.base_proxy_url`。两个字段都支持逗号分隔多个代理，并沿用现有粘性选择与失败轮换。

Console Chat 的 SSO 逆向请求使用 `console.x.ai/v1/*` 登录态 Cookie，不发送 `Authorization: Bearer anonymous`，并携带 Console Playground 同类的 tracing headers 与 `x-cluster`。服务会剥离 `proxy.cf_cookies` 中浏览器残留的 team 相关 cookie，避免把某个浏览器 team 状态污染到账号池请求。官方 `api.x.ai + xai-... API key` 路径仍按真实 team credits/licenses 计费，不属于这个 SSO 逆向通道。

| model_id | 说明 |
|---|---|
| `grok-4.3` / `grok-4.3-search` | grok-4.3；`-search` 自动启用 web/x 搜索 |
| `grok-build-0.1` / `grok-build-0.1-search` | build 模型 |
| `grok-4.20-0309-non-reasoning` / `...-search` | 支持 frequency/presence penalty |
| `grok-4.20-0309-reasoning` / `...-search` | 内置 reasoning + encrypted CoT |
| `grok-4.20-multi-agent-0309` / `...-search` | multi-agent；**不支持原生 function calling**，见下方 Tool Call 说明 |

### Reasoning / Agent Effort（Console）

`reasoning.effort`（Chat Completions 对应 `reasoning_effort`）在不同模型上语义不同：

| 模型 | 支持 effort | 可选值 | 语义 |
|---|---|---|---|
| `grok-4.3` | 是 | `none` / `minimal`* / `low` / `medium` / `high` / `xhigh` / `max`* | 推理深度；`none` 关闭 reasoning summary |
| `grok-4.20-multi-agent-0309` | 是 | 同上 | **协作 agent 数**，见下表 |
| `grok-build-0.1` | 否 | — | 内置 encrypted reasoning，不接受 `reasoning.effort` |
| `grok-4.20-0309-non-reasoning` | 否 | — | 无 reasoning |
| `grok-4.20-0309-reasoning` | 否 | — | 内置 encrypted reasoning，不接受 `reasoning.effort` |

\* 网关别名：`minimal`→`low`，`max`→`xhigh`（所有模型通用）。

**multi-agent effort → agent 数：**

| effort（规范化后） | agent 数 |
|---|---|
| `none` / 未指定 | 默认 |
| `low` | 4 |
| `medium` | 8 |
| `high` | 12 |
| `xhigh` | 16 |

- multi-agent 不会自动加 `reasoning.summary`；grok-4.3 在 `effort != none` 时默认 `summary: "auto"`。
- 不支持的模型若客户端传入 `reasoning.effort`，网关会在转发前剥离该字段。

### 与 grok.com 通道的区别

- Console Chat / Responses / Messages 当前为原生透传：网关不再做 tool call prompt 注入、CoT 事件重排或 Chat/Messages 兼容转换。
- Tool call、reasoning、usage、搜索等字段以 Console 上游原生响应为准；客户端应按对应原生端点格式处理。
- `-search` 变体只表示模型 id 选择，不在网关侧额外注入搜索 tools。

### 图片输入（Console）

Playground 模型的图片输入按上游原生端点格式透传。Chat Completions 使用 OpenAI `image_url`，Responses 使用 `input_image`，Messages 使用上游支持的 Anthropic content block。

### 客户端多轮对话约定（Console）

Console Chat / Responses / Messages 均为原生透传。多轮、reasoning replay、tool loop 和搜索字段不由网关重写，客户端应按所选原生端点的格式携带完整上下文或上游支持的续轮字段。

## Console Voice（TTS/STT）

通过 `console.x.ai` SSO 逆向的 Voice Playground API，对外暴露 OpenAI 兼容音频端点（REST + STT WebSocket）。在 `GET /v1/models` 中 `owned_by` 为 `xai-console-voice`。

Console Voice 的 REST 与 STT WebSocket 使用同一套 Console 专用代理优先级：`proxy.console_proxy_url` → `proxy.base_proxy_url`。

| model_id | 用途 |
|---|---|
| `grok-tts-1` | 文字转语音（`/v1/audio/speech`） |
| `grok-stt-1` | 语音转文字（`/v1/audio/transcriptions`、`/v1/audio/translations`） |

### 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/audio/speech` | OpenAI TTS 兼容；JSON body，`input`→上游 `text`，`voice`→`voice_id` |
| `POST /v1/audio/transcriptions` | OpenAI STT 兼容；multipart，`file` 必填 |
| `POST /v1/audio/translations` | OpenAI 翻译兼容（**最佳努力**）：内部为 STT + 固定 `language=en` + `format=true`，非独立翻译 API |
| `GET /v1/audio/voices` | 上游音色列表（`eve` / `ara` / `rex` / `sal` / `leo` 等） |
| `WS /v1/audio/stt/ws` | xAI STT WebSocket 流式透传（上游 `wss://console.x.ai/v1/stt`）；query 如 `sample_rate=16000&encoding=pcm&interim_results=true&language=en`；认证：`?api_key=` 或 `Authorization: Bearer` |

### 音色与格式

- **内置音色**：`eve`、`ara`、`rex`、`sal`、`leo`（大小写不敏感）。
- **OpenAI 别名**：`alloy→ara`、`echo→rex`、`fable→sal`、`onyx→leo`、`nova→eve`、`shimmer→sal`。
- **TTS `response_format`**：`mp3`（默认）、`wav`、`pcm`、`opus`（映射为 mp3）。
- **STT `response_format`**：`json`（默认）、`text`、`verbose_json`、`srt`、`vtt`（后两者需上游返回 `words` 时间戳，否则降级为纯文本）。

### 与 OpenAI 的差异（v1）

- **TTS 无 WebSocket 流式**：Console SSO 仅支持 REST `POST /v1/audio/speech`；官方 `wss://api.x.ai/v1/tts` 需 API Key，不在本实现范围。
- `POST /v1/audio/translations` 为 STT 英文输出 alias，非严格语义翻译。
- `instructions`（TTS）、`prompt` / `temperature`（STT/translations）字段会被忽略。
- 依赖 **`ssoBasic`** 池与 **`proxy.cf_clearance`**（与 Console Chat 相同）。

### STT WebSocket 协议

客户端连接 `WS /v1/audio/stt/ws` 后，消息格式与 [xAI STT WebSocket](https://docs.x.ai/docs/guides/voice) 一致：

- **客户端 → 服务端**：binary PCM 音频块；结束时可发 JSON `{"type":"audio.done"}`
- **服务端 → 客户端**：JSON 事件 `transcript.created`、`transcript.partial`、`transcript.done`

### 示例

```bash
# TTS
curl -sS -X POST http://127.0.0.1:8000/v1/audio/speech \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-tts-1","input":"Hello from Grok TTS.","voice":"eve","response_format":"mp3"}' \
  --output speech.mp3

# STT
curl -sS -X POST http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer $API_KEY" \
  -F model=grok-stt-1 \
  -F file=@sample.wav

# Translations (English output via STT)
curl -sS -X POST http://127.0.0.1:8000/v1/audio/translations \
  -H "Authorization: Bearer $API_KEY" \
  -F model=grok-stt-1 \
  -F file=@sample.wav

# STT WebSocket（需 wscat 或自写客户端；发送 PCM binary，结束发 {"type":"audio.done"}）
wscat -c "ws://127.0.0.1:8000/v1/audio/stt/ws?api_key=$API_KEY&sample_rate=16000&encoding=pcm&interim_results=true&language=en"
```

## 检查

```bash
uv run ruff check
uv run mypy
uv run pytest
```
