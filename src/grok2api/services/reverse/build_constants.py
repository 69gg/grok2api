"""Constants for free Grok Build / cli-chat-proxy reverse channel."""

from __future__ import annotations

# Free Build promo path (NOT api.x.ai)
DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
ISSUER = "https://auth.x.ai"

# Grok CLI / Grok Build public OAuth client
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

REFRESH_LEAD_SEC = 300
DEFAULT_EXPIRES_IN = 21600
FREE_USAGE_COOLDOWN_SEC = 86400

# Pool name for OIDC credentials
CLI_POOL_NAME = "oidcBuild"

DEFAULT_CLIENT_HEADERS: dict[str, str] = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

# Upstream models exposed by free cli-chat-proxy (availability may vary)
UPSTREAM_MODEL_GROK_45 = "grok-4.5"
UPSTREAM_MODEL_COMPOSER = "grok-composer-2.5-fast"

WEB_SEARCH_TOOL: dict[str, str] = {"type": "web_search"}

__all__ = [
    "CLIENT_ID",
    "CLI_POOL_NAME",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_HEADERS",
    "DEFAULT_EXPIRES_IN",
    "DEVICE_CODE_URL",
    "FREE_USAGE_COOLDOWN_SEC",
    "ISSUER",
    "REFRESH_LEAD_SEC",
    "SCOPE",
    "TOKEN_ENDPOINT",
    "UPSTREAM_MODEL_COMPOSER",
    "UPSTREAM_MODEL_GROK_45",
    "WEB_SEARCH_TOOL",
]
