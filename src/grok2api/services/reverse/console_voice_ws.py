"""Console Voice WebSocket reverse proxy (STT only)."""

from __future__ import annotations

import asyncio
from typing import Dict, Mapping, Optional
from urllib.parse import urlencode

import aiohttp
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.services.reverse.console_constants import CONSOLE_STT_WS, CONSOLE_VOICE_TIMEOUT
from grok2api.services.reverse.utils.headers import build_console_voice_ws_headers
from grok2api.services.reverse.utils.proxy import CONSOLE_PROXY_KEYS
from grok2api.services.reverse.utils.websocket import WebSocketClient


def build_stt_ws_url(query_params: Optional[Mapping[str, str]] = None) -> str:
    """Build upstream STT WebSocket URL with optional query string."""
    if not query_params:
        return CONSOLE_STT_WS
    qs = urlencode(dict(query_params))
    return f"{CONSOLE_STT_WS}?{qs}" if qs else CONSOLE_STT_WS


async def _relay_bidirectional(client_ws: WebSocket, upstream_ws: aiohttp.ClientWebSocketResponse) -> None:
    """Bidirectionally relay messages between client and upstream STT WebSocket."""

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("type") != "websocket.receive":
                    continue
                data = msg.get("bytes")
                if data is not None:
                    await upstream_ws.send_bytes(data)
                    continue
                text = msg.get("text")
                if text is not None:
                    await upstream_ws.send_str(text)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"STT WS client->upstream relay ended: {e}")
        finally:
            if not upstream_ws.closed:
                await upstream_ws.close()

    async def upstream_to_client() -> None:
        try:
            async for msg in upstream_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if client_ws.client_state == WebSocketState.CONNECTED:
                        await client_ws.send_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    if client_ws.client_state == WebSocketState.CONNECTED:
                        await client_ws.send_bytes(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except Exception as e:
            logger.debug(f"STT WS upstream->client relay ended: {e}")
        finally:
            if client_ws.client_state == WebSocketState.CONNECTED:
                await client_ws.close()

    await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)


async def relay_stt_websocket(
    client_ws: WebSocket,
    token: str,
    query_params: Optional[Dict[str, str]] = None,
) -> None:
    """Connect to console.x.ai STT WebSocket and relay traffic to the client."""
    url = build_stt_ws_url(query_params)
    headers = build_console_voice_ws_headers(token)
    client = WebSocketClient(proxy_config_keys=CONSOLE_PROXY_KEYS)
    conn = None
    try:
        conn = await client.connect(url, headers=headers, timeout=CONSOLE_VOICE_TIMEOUT)
        await _relay_bidirectional(client_ws, conn.ws)
    except aiohttp.WSServerHandshakeError as e:
        status = getattr(e, "status", None) or 502
        raise UpstreamException(
            message=f"STT WebSocket handshake failed: {e.message}",
            details={"status": status, "url": url},
        ) from e
    except UpstreamException:
        raise
    except Exception as e:
        raise UpstreamException(
            message=f"STT WebSocket relay failed: {e}",
            details={"url": url},
        ) from e
    finally:
        if conn is not None:
            await conn.close()


__all__ = ["build_stt_ws_url", "relay_stt_websocket"]
