"""Hardcoded console.x.ai Playground settings."""

CONSOLE_BASE_URL = "https://console.x.ai"
CONSOLE_RESPONSES_API = f"{CONSOLE_BASE_URL}/v1/responses"
CONSOLE_DEFAULT_CLUSTER = "https://us-east-1.api.x.ai"
CONSOLE_TIMEOUT = 120
CONSOLE_STRICT_PARAM_VALIDATION = False
CONSOLE_SEARCH_TOOLS = ("web_search", "x_search")
CONSOLE_ALLOWED_INCLUDE = frozenset({"reasoning.encrypted_content"})

__all__ = [
    "CONSOLE_ALLOWED_INCLUDE",
    "CONSOLE_BASE_URL",
    "CONSOLE_DEFAULT_CLUSTER",
    "CONSOLE_RESPONSES_API",
    "CONSOLE_SEARCH_TOOLS",
    "CONSOLE_STRICT_PARAM_VALIDATION",
    "CONSOLE_TIMEOUT",
]
