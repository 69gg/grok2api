# Architecture

新版项目保留旧 Python API 能力，但移除 WebUI 和多部署栈。

- `src/grok2api/api`：FastAPI router 和接口 schema。
- `src/grok2api/services`：业务编排、token、注册、Grok 服务。
- `src/grok2api/services/register`：自动注册、Turnstile solver 进程管理和注册后账号初始化。
- `src/grok2api/services/reverse`：Grok 上游反向调用细节。
- `src/grok2api/core`：配置、日志、认证、异常、存储和路径。
- `scripts/turnstile_solver`：本地 solver HTTP 服务，提供 `/turnstile`、`/grok_setup`、`/cf_clearance` 和 `/result`。

运行数据默认落到 `data/grok2api.db`。根目录 `config.toml` 是本地配置，不提交。

## Token 选择

模型目录和同名模型解析按固定优先级处理：**CLI（free Grok 4.5 / Composer）> Console Chat / Voice / Image > Basic grok.com app > ssoSuper**。这个顺序同时影响 `GET /v1/models` 的返回顺序和 `ModelService.get()` 对同名 `model_id` 的解析。

Grok 与 Console Chat / Voice / Image 通道都使用进程内轮询游标：每次上游调用都会在候选池中推进到下一个可用 SSO 号。单次请求内的失败重试会排除已尝试的号，继续轮询后续可用号；grok.com 通道的 429 会标记当前号为 cooling 后换号重试，Console 通道的 429 只在当前请求内换号。

### Free CLI Build 通道（`Channel.CLI` / 池 `oidcBuild`）

- 上游：`cli-chat-proxy.grok.com/v1`，OpenAI chat / Anthropic messages / Responses **header 透传**（仅改 Bearer 与 Grok CLI 客户端头）。
- 凭证：OIDC `access_token` + `refresh_token`（**不是**网页 SSO JWT）。SSO ≠ OIDC。
- **代理**：与 Console 共用 `proxy.console_proxy_url`（空则 `base_proxy_url`），无独立 build 代理项。
- **默认开启**：`[build]` 全开，pull 后无需改配置即可用。
- **后台静默铸 OIDC**（对齐 Console `console_team_id`）：定时扫描 `ssoBasic` 中尚未关联 OIDC 的账号，device-auth mint 后写入 `oidcBuild`（`sso_source` 关联）。
- **按需刷新**：请求前若 access 将过期则 `refresh_token` 换新；调度器也周期刷新。
- **请求轮询**：只从**已有 auth** 的 `oidcBuild` 账号 round-robin；无可用号时才触发 on-demand mint。
- 额度耗尽（`free-usage-exhausted`）：该 OIDC 号 cooling 约 24h，不影响同号 Console/SSO 其它通道。
- 协议细节见 `docs/build_cli_protocol.md`。

Console 通道对上游失败做更细的账号状态区分：429、普通 403 和 Cloudflare HTML challenge 只在当前请求内换下一个号重试，不禁用账号；只有明确的 blocked-user 响应（例如 `unauthorized:blocked-user` / `User is blocked`）才会立即标记该 Console 号失效。这个规则同时适用于 Console 原生请求和缺失 `console_team_id` 时的 `CreateTeam` 初始化请求。

## 代理选择

Console Chat / Responses / Messages / Images 与 Console Voice（TTS、STT、voices、STT WebSocket）请求优先使用 `proxy.console_proxy_url`，为空时回退 `proxy.base_proxy_url`。这两个配置都支持逗号分隔多个代理，并复用现有粘性选择和失败轮换；grok.com、assets、注册与 CF 刷新等非 Console 请求仍按各自原有代理配置运行。

Console 原生逆向请求复刻 Console 登录态路径：使用 SSO Cookie、`x-cluster` 与 Sentry tracing headers，不发送 `Authorization: Bearer anonymous`、`x-statsig-id` 或 `x-xai-request-id`。服务会剥离 `proxy.cf_cookies` 中浏览器残留的 `last-team-id` 与 playground/voice team 计数 cookie，避免把某个浏览器 team 状态污染到账号池请求。官方 `api.x.ai` API key 路径不属于这个 SSO 逆向通道，仍会按真实 team credits/licenses 校验。

每个 SSO 号可以携带独立的 `console_team_id`。当 Console 请求选中缺少该字段的账号时，服务会先用该账号的 `sso/sso-rw` 通过 `auth_mgmt.AuthManagement/CreateTeam` 创建 team，解析 gRPC-Web 响应里的 team id，持久化到该 token 后再请求 Console 原生端点。后台默认每 60 秒主动扫描 `ssoBasic` 中缺失 `console_team_id` 的可用账号并补全，避免流量请求首次命中时才创建。`CreateTeam` 与 Console 请求都优先使用 `proxy.console_proxy_url`，为空时回退 `proxy.base_proxy_url`。

Console 原生响应返回给客户端时只透传 body 和必要的 content type；上游响应头只在内部 metadata 中保留脱敏版本，`set-cookie`、鉴权类 header 不会暴露给调用方。

`grok-imagine-image` 属于 Console image 通道，`/v1/images/generations` 和 `/v1/images/edits` 会优先调用 Console 原生路径并在成功时透传 native body；非流式 Console 失败时允许回退到 grok.com legacy 图片服务。图片 `quality` 在进入 Console 原生路径前会从 OpenAI 兼容值规范化到 Console 接受的 `low` / `medium` / `high`，避免默认 `standard` 触发上游 422。Console 原生文生图不接收 `size` / `style`，网关会在 Console payload 中剥离这些 OpenAI/legacy 字段。

Console 原生失败日志会记录 method、path、status、stream、content type、代理配置键、team id 是否存在、脱敏后的请求/响应 headers，以及截断后的请求体和响应体预览。请求体中的 token、cookie、secret、password、API key 等常见敏感字段会递归替换为 `<redacted>`。

## CF 自动刷新

`proxy.enabled = true` 时，`TokenRefreshScheduler` 会单独启动 `cf_clearance` 刷新循环。这个循环在启动后立即刷新一次，后续按 `proxy.refresh_interval` 续期；它复用 `register.solver_url`，本机地址且 `register.auto_start_solver = true` 时会自动启动内置 solver。

刷新任务持有明确生命周期：停止服务时会设置 `stop_event` 中断轮询、等待后台任务收敛，并停止由本进程启动的 solver 进程组，避免浏览器子进程和线程池任务残留。多 worker 场景下通过存储锁避免重复刷新。
