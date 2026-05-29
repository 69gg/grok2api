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

保留旧 Python 版 API 能力，包括 OpenAI 兼容聊天、Responses、Anthropic Messages、图片、视频、**音频（TTS/STT/translations + STT WebSocket）**、上传、文件访问、管理 API、function API 和自动注册 API。

## 模型池规则

- `ssoBasic`：支持 `grok-4.3-fast`、`grok-imagine-1.0`、`grok-imagine-1.0-edit`，以及全部 **Console Chat Playground** 与 **Console Voice（TTS/STT/translations + STT WS）** 模型（见下）。
- `ssoSuper`：保持旧项目模型能力。
- 图片生成/编辑内部使用 `grok-4.3`。

## 图片输入（Vision）

聊天场景下的「看图对话」与「文生图/图生图」是不同能力，请勿混用 model_id。

### 能力概览

| 通道 / 端点 | 图片输入 | 说明 |
|---|---|---|
| Console 模型 + `/v1/chat/completions` | ✅ | OpenAI `image_url` → 上游 `input_image`（URL 直传，不上传） |
| Console 模型 + `/v1/responses` | ✅ | `input_image` 或 message 内嵌图片 block；多轮 replay 支持 |
| Console 模型 + `/v1/messages` | ❌ | Anthropic 路径暂不支持 user `image` block |
| grok.com 模型 + `/v1/chat/completions` | ✅ | 图片先上传 Grok assets，再走 `fileAttachments` |
| grok.com 模型 + `/v1/responses` | ✅ | 内部转换为 Chat 消息后走同一上传链路 |
| `grok-imagine-*` | — | 文生图 / 图生图专用，不是通用 vision chat |
| `/v1/videos/*` | — | 可将图片作为视频 reference，不是聊天 vision |

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

## Console Chat Playground 模型

通过 `console.x.ai` SSO 逆向的 Playground 免费高级模型。在 `GET /v1/models` 中 `owned_by` 为 `xai-console`。

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

- 除 `grok-4.20-multi-agent-0309` 外，Console 模型走 **xAI Responses API 原生 tool call**。
- `grok-4.20-multi-agent-0309`（含 `-search` 变体）走 **prompt tool call**：客户端 function tools 写入 `instructions`（与 grok.com 相同的 `<call>` 协议），响应由网关解析为 OpenAI `tool_calls`；无 tools 时不注入 tool prompt。
- `-search` 变体在 prompt tool call 之外，额外向上游注入 `web_search` / `x_search` 开启内置搜索；**不会**因此改用原生 function calling。
- Token 用量来自上游 `response.completed.usage`，不是本地估算。

### Tool Call（Console）

| 模型 | function tools | 搜索 |
|---|---|---|
| `grok-4.3` 等（非 multi-agent） | 原生 Responses API `tools` + `tool_choice` | 仅 `-search` 变体自动注入 |
| `grok-4.20-multi-agent-0309` | prompt → `instructions`（`<call>` 协议） | 无 |
| `grok-4.20-multi-agent-0309-search` | 同上 prompt tool call | 额外注入 `web_search` / `x_search` |

- multi-agent 多轮 tool loop：历史中的 `tool` / `tool_calls` 消息会转换为 prompt 可读文本后再 replay。
- multi-agent prompt 模式下，客户端 function 的 `tool_choice` **不会**转发到上游（避免与仅含 search tools 的 payload 冲突）。

### 图片输入（Console）

Playground 五个模型均支持 Image input。网关行为见上文 **图片输入（Vision）** 章节；Console 侧要点：

- **`/v1/chat/completions`**：`image_url` 自动映射为 Responses `input_image`（含 `detail`）。
- **`/v1/responses`**：原生 `input_image` item 直接 replay。
- **`/v1/messages`**：暂不支持；需改用 Chat Completions 或 Responses。

### 客户端多轮对话约定（对齐 OpenAI Responses API）

1. Playground 上游默认 `store=false`，**不要依赖** `previous_response_id`；无状态多轮应携带 **完整 `input[]` history**（reasoning / function_call / function_call_output / message items）。
2. **Reasoning 标准字段**（见 [OpenAI Reasoning 指南](https://developers.openai.com/api/docs/guides/reasoning)）：
   - **`/v1/responses`**：`output[]` 中 `{type:"reasoning", summary?, content?, encrypted_content?}` item；**不是** `reasoning_content`
   - **`/v1/chat/completions`**：网关兼容层映射为 `message.reasoning_content` / `delta.reasoning_content`（便于旧客户端；OpenAI 官方仍推荐 Responses）
   - **`/v1/messages`**：映射为 `{type:"thinking", thinking:"..."}` block
3. 无状态续轮：请求带 `include: ["reasoning.encrypted_content"]`，并将上一轮 **完整 reasoning item**（含 `encrypted_content`）原样放入 `input[]`。
4. Summary：grok-4.3 等模型在 `reasoning.effort != none` 时默认请求 `reasoning.summary: "auto"`（与 OpenAI 一致，需显式开启才返回 summary）。
5. `grok-4.3` 返回 **明文 summary**（`summary_text` / 流式 `response.reasoning_summary_text.delta`）；build/4.20 等以 **encrypted 透传** 为主。
6. Assistant message 的 `phase`（`commentary` / `final_answer`）在 replay 时会原样保留。
7. `-search` 变体会自动注入 `web_search` 与 `x_search`，无需客户端手动添加搜索 tools。

## Console Voice（TTS/STT）

通过 `console.x.ai` SSO 逆向的 Voice Playground API，对外暴露 OpenAI 兼容音频端点（REST + STT WebSocket）。在 `GET /v1/models` 中 `owned_by` 为 `xai-console-voice`。

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
