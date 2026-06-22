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

默认 Grok 通道沿用通用 token 选择策略。Console Chat / Voice 通道使用进程内轮询游标：每次上游调用都会在候选池中推进到下一个可用 SSO 号；单次请求内的失败重试会排除已尝试的号，继续轮询后续可用号。

## CF 自动刷新

`proxy.enabled = true` 时，`TokenRefreshScheduler` 会单独启动 `cf_clearance` 刷新循环。这个循环在启动后立即刷新一次，后续按 `proxy.refresh_interval` 续期；它复用 `register.solver_url`，本机地址且 `register.auto_start_solver = true` 时会自动启动内置 solver。

刷新任务持有明确生命周期：停止服务时会设置 `stop_event` 中断轮询、等待后台任务收敛，并停止由本进程启动的 solver 进程组，避免浏览器子进程和线程池任务残留。多 worker 场景下通过存储锁避免重复刷新。
