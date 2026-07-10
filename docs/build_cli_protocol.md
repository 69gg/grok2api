# Free CLI 通道协议笔记（空 profile 抓包，2026-07-10）

探索环境：chrome-devtools **isolatedContext**（全新空 profile），代理 `127.0.0.1:7897`，tmail `https://tmail.pylindex.top`。

## 1. 注册（accounts.x.ai）

| 步 | 请求 | 说明 |
|----|------|------|
| 邮箱 | `POST /auth_mgmt.AuthManagement/CreateEmailValidationCode` | `application/grpc-web+proto`，body 含邮箱；可能夹带 turnstile 大 payload |
| 收信 | tmail `GET /api/mails` Bearer jwt | 验证码在 **Subject**：`{XXX}-{XXX} xAI confirmation code`（如 `10S-U9Z`），HTML 内常无可用 6 位数字 |
| 校验 | `POST /auth_mgmt.AuthManagement/VerifyEmailValidationCode` | 同上 grpc-web |
| 完成 | 表单 + Cloudflare Turnstile + `完成注册` | **人机失败则刷新页面/widget 重试** |
| 会话 | 多跳 `auth.*.com/set-cookie?q=...` | 最终落 `accounts.x.ai/account`；SSO 多为 **HttpOnly** |

与现有 `register/runner.py` 一致：验证码正则 `>XXX-XXX<` 或 Subject 前缀；注册后 setup 浏览器拿 sso。

## 2. OIDC device-auth（Grok Build / free 4.5）

```
POST https://auth.x.ai/oauth2/device/code
  client_id=b1a00492-073a-47ea-816f-4c329264a828
  scope=openid profile email offline_access grok-cli:access api:access
→ device_code, user_code, verification_uri_complete

浏览器（已登录会话）打开 verification_uri_complete
  → 继续 → consent「允许」→ /oauth2/device/done「设备已授权」

POST https://auth.x.ai/oauth2/token
  grant_type=urn:ietf:params:oauth:grant-type:device_code
  device_code=...
  client_id=...
→ access_token, refresh_token, expires_in=21600
```

已登录时 **无需再输账密**；consent 页真实点击「允许」。

## 3. Refresh（全自动、无浏览器）

```
POST https://auth.x.ai/oauth2/token
  grant_type=refresh_token
  client_id=...
  refresh_token=...
```

## 4. 上游 API（cli-chat-proxy）

Base: `https://cli-chat-proxy.grok.com/v1`

| 端点 | 状态 |
|------|------|
| GET /models | 200（本号仅见 `grok-4.5`；composer 视账号/策略） |
| POST /chat/completions | 200 + SSE |
| POST /messages | 200 + SSE |
| POST /responses | 200 |

Headers：`Authorization: Bearer` + `x-grok-client-version` / `x-xai-token-auth` / `x-authenticateresponse` / `x-grok-client-identifier` / `User-Agent: grok-shell/...`

**禁止**默认 `api.x.ai`（无 free Build 额度）。

## 5. 实现策略

- 网关：header 透传三端点；`-search` 别名注入 `web_search`
- 凭证：自动注册（可选）+ device mint；**运行时只靠 refresh 续期**
- 人机：Turnstile 失败 → 刷新重试（浏览器路径）；协议路径复用 register turnstile solver
