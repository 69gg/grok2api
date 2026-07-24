# Architecture

新版项目保留旧 Python API 能力，但移除 WebUI 和多部署栈。

- `src/grok2api/api`：FastAPI router 和接口 schema。
- `src/grok2api/services`：业务编排、token、注册、Grok 服务。
- `src/grok2api/services/register`：自动注册、Turnstile solver 进程管理和注册后账号初始化。
  - 本地 Turnstile 解码调试：`register.solver_debug = true` 会给 solver 传 `--debug`，并在主进程 `TurnstileService` 打印 create/poll/CAPTCHA_FAIL 详情；solver 进程会输出页面快照（url/title/iframe/token 长度/console error）及失败原因。
  - 注册 init：先抓 `accounts.x.ai/sign-up` 解析 action_id（带 impersonate 轮换与重试），**再**启动本地 solver，避免 curl_cffi 与 camoufox 并发时偶发 `curl 35 OPENSSL_internal` 直接整 job 失败。
  - 注册 job：默认 `max_errors=0` / `max_runtime_minutes=0` 表示**持续重试直到完成 target**；失败不主动停 job、job 结束也不关 solver（仅进程退出 `stop_job(stop_solver=True)` 时关闭）。
  - 浏览器回收：每个池内浏览器在完成 `register.solver_browser_recycle_tasks` 个任务或存活 `register.solver_browser_recycle_seconds` 秒后重建，避免长期进程持续保留浏览器运行时的内存高水位；两个条件均支持设为 `0` 禁用。
- `src/grok2api/services/reverse`：Grok 上游反向调用细节。
- `src/grok2api/core`：配置、日志、认证、异常、存储和路径。
- `scripts/turnstile_solver`：本地 solver HTTP 服务，提供 `/turnstile`、`/grok_setup`、`/cf_clearance` 和 `/result`。

运行数据默认落到 `data/grok2api.db`。根目录 `config.toml` 是本地配置，不提交。

### SQLite 并发

默认 SQLite 后端会在连接时启用：

- `PRAGMA journal_mode=WAL`（读写并发更友好）
- `PRAGMA busy_timeout=30000`（短暂锁等待，而不是立刻 `database is locked`）
- `NullPool` + **按锁名**的进程内 `asyncio.Lock` / `fcntl` 文件锁（`acquire_lock`；超时只作用于抢锁阶段，不取消已持有的临界区）
- `load_tokens` / `save_tokens_delta` 对短暂 `database is locked` 自动退避重试

高并发多 worker 场景仍建议使用 MySQL/PostgreSQL 或 Redis。

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
- **refresh 被吊销**：`invalid_grant` / `Refresh token has been revoked` 时，若有 `sso_source` 则自动 device-auth **重新 mint**（`build.remint_on_revoked`，默认开）；失败则清空该号 OIDC 密钥并按 `build.remint_cooldown_sec`（默认 300s）退避，避免死循环。
- **请求轮询**：只从**已有 auth** 的 `oidcBuild` 账号 round-robin；无可用号时才触发 on-demand mint。
- **有界传输重试**：单个 OIDC 账号的连接错误由 `build.transport_max_retry` 独立限制；每次重试前关闭旧的 `curl_cffi` session，避免与跨账号轮询形成资源和次数的乘法放大。
- **后台失败退避**：auto-init / refresh 每轮最多尝试配置数量，而不是以成功数计数；失败账号和全失败任务都会从 `build.background_failure_backoff_initial_sec` 指数退避到 `build.background_failure_backoff_max_sec`，新账号优先于到期重试账号，避免 TLS/代理故障时形成请求风暴。
- 额度耗尽（`free-usage-exhausted` / `personal-team-blocked:spending-limit` / run out of credits）：该 OIDC 号 cooling 约 24h（`build.free_usage_cooldown_sec`），不影响同号 Console/SSO 其它通道。
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
