"""OpenAI-compatible audio WebSocket API (Console Voice STT streaming)."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from grok2api.core.auth import get_admin_api_keys, is_valid_admin_api_key
from grok2api.core.config import config
from grok2api.core.logger import logger
from grok2api.services.api_keys import api_key_manager
from grok2api.services.grok.services.console_voice import ConsoleVoiceService

router = APIRouter(tags=["Audio"])


async def _verify_ws_api_key(websocket: WebSocket) -> bool:
    await config.ensure_loaded()
    api_keys = get_admin_api_keys()
    if not api_keys:
        return True

    token = str(websocket.query_params.get("api_key") or "").strip()
    if not token:
        auth_header = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token:
        return False
    if is_valid_admin_api_key(token):
        return True
    try:
        await api_key_manager.init()
        if api_key_manager.validate_key(token):
            return True
    except Exception as e:
        logger.warning(f"Audio WS api_key validation fallback failed: {e}")
    return False


@router.websocket("/audio/stt/ws")
async def stt_websocket(websocket: WebSocket) -> None:
    """Stream speech-to-text via xAI WebSocket protocol (console.x.ai upstream)."""
    if not await _verify_ws_api_key(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    model = str(websocket.query_params.get("model") or "grok-stt-1").strip()
    query_params = dict(websocket.query_params)

    try:
        await ConsoleVoiceService.relay_stt_websocket(
            websocket,
            model=model,
            query_params=query_params,
        )
    except Exception as e:
        logger.warning(f"STT WebSocket session ended with error: {e}")
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(code=1011)


__all__ = ["router"]
