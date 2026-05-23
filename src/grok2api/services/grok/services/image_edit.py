"""
Grok image edit service.
"""

import asyncio
import os
import random
import re
import time
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterable, Dict, List, Union, Any
from urllib.parse import urlparse

import orjson
from curl_cffi.requests.errors import RequestsError

from grok2api.core.config import get_config
from grok2api.core.exceptions import (
    AppException,
    ErrorType,
    UpstreamException,
    StreamIdleTimeoutError,
)
from grok2api.core.logger import logger
from grok2api.services.grok.utils.process import (
    BaseProcessor,
    _with_idle_timeout,
    _normalize_line,
    _collect_images,
    _is_http2_error,
)
from grok2api.services.grok.utils.upload import UploadService
from grok2api.services.grok.utils.retry import pick_token, rate_limited
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.response import make_response_id, make_chat_chunk, wrap_image_content
from grok2api.services.grok.services.chat import GrokChatService
from grok2api.services.grok.utils.stream import wrap_stream_with_usage
from grok2api.services.token import EffortType

def _compact_preview(value: Any, limit: int = 160) -> str:
    if isinstance(value, (dict, list)):
        try:
            text = orjson.dumps(value).decode("utf-8", errors="ignore")
        except Exception:
            text = str(value or "")
    else:
        text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _preview_items(
    value: Any,
    *,
    limit: int = 3,
    item_limit: int = 120,
) -> List[str]:
    if isinstance(value, list):
        items = value
    elif value in (None, "", [], {}):
        items = []
    else:
        items = [value]
    return [_compact_preview(item, limit=item_limit) for item in items[:limit]]


def _value_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if value in (None, "", [], {}):
        return 0
    return 1


def _add_payload_summary(
    summary: dict[str, Any],
    payload: dict[str, Any],
    *,
    prefix: str = "",
) -> None:
    field_names = {
        "generatedImageUrls": "generated_image_urls",
        "imageUrls": "image_urls",
        "cardAttachmentsJson": "card_attachments_json",
        "fileUris": "file_uris",
        "imageAttachments": "image_attachments",
        "fileAttachmentsMetadata": "file_attachments_metadata",
        "toolResponses": "tool_responses",
        "streamErrors": "stream_errors",
    }
    for field, output_name in field_names.items():
        if field not in payload:
            continue
        value = payload.get(field)
        count = _value_count(value)
        summary[f"{prefix}{output_name}_count"] = count
        if count:
            summary[f"{prefix}{output_name}_preview"] = _preview_items(value)


def _iter_card_payloads(resp: dict[str, Any]) -> List[tuple[str, Any]]:
    payloads: List[tuple[str, Any]] = []

    card = resp.get("cardAttachment")
    if isinstance(card, dict):
        payloads.append(("cardAttachment.jsonData", card.get("jsonData")))

    for source_name in ("modelResponse", "userResponse"):
        payload = resp.get(source_name)
        if not isinstance(payload, dict):
            continue
        for idx, raw in enumerate(payload.get("cardAttachmentsJson") or []):
            payloads.append((f"{source_name}.cardAttachmentsJson[{idx}]", raw))

    return payloads


def _parse_card_payload(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty_card_payload"
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None, "json_decode_error"
    if not isinstance(parsed, dict):
        return None, f"unexpected_card_payload_type:{type(parsed).__name__}"
    return parsed, None


def _collect_card_reference_hints(
    value: Any,
    *,
    limit: int = 10,
) -> List[str]:
    hints: List[str] = []
    seen = set()
    interesting_terms = (
        "uuid",
        "uri",
        "path",
        "file",
        "image",
        "asset",
        "download",
        "source",
        "signed",
        "original",
        "thumb",
        "url",
    )

    def add(key: str, item: Any):
        if len(hints) >= limit:
            return
        text = f"{key}={_compact_preview(item, limit=96)}"
        if text in seen:
            return
        seen.add(text)
        hints.append(text)

    def walk(node: Any):
        if len(hints) >= limit:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                key_lower = str(key).lower()
                if (
                    isinstance(item, (str, int, float, bool))
                    and item not in ("", None)
                    and any(term in key_lower for term in interesting_terms)
                ):
                    add(str(key), item)
                walk(item)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return hints


def _summarize_image_response(resp: dict) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "keys": sorted(resp.keys()),
        "has_model_response": isinstance(resp.get("modelResponse"), dict),
        "has_card_attachment": isinstance(resp.get("cardAttachment"), dict),
        "has_streaming_progress": isinstance(
            resp.get("streamingImageGenerationResponse"), dict
        ),
        "has_tool_usage_card": "toolUsageCard" in resp,
        "tool_usage_card_id": str(resp.get("toolUsageCardId") or "")[:32],
        "message_tag": str(resp.get("messageTag") or "")[:64],
        "message_step_id": str(resp.get("messageStepId") or "")[:64],
    }

    mr = resp.get("modelResponse")
    if isinstance(mr, dict):
        summary["model_response_keys"] = sorted(mr.keys())
        summary["model_response_message_preview"] = _compact_preview(
            mr.get("message")
        )
        _add_payload_summary(summary, mr)

    card = resp.get("cardAttachment")
    if isinstance(card, dict):
        summary["card_attachment_keys"] = sorted(card.keys())
        summary["card_attachment_json_preview"] = _compact_preview(
            card.get("jsonData")
        )

    user_response = resp.get("userResponse")
    if isinstance(user_response, dict):
        summary["user_response_keys"] = sorted(user_response.keys())
        _add_payload_summary(summary, user_response, prefix="user_")
        summary["user_response_message_preview"] = _compact_preview(
            user_response.get("message")
        )
    elif user_response is not None:
        summary["user_response_preview"] = _compact_preview(user_response)

    progress = resp.get("streamingImageGenerationResponse")
    if isinstance(progress, dict):
        summary["progress_keys"] = sorted(progress.keys())
        summary["progress_value"] = progress.get("progress")

    token_value = resp.get("token")
    if token_value:
        summary["token_preview"] = _compact_preview(token_value, limit=80)

    card_diagnostics = _collect_response_card_diagnostics(resp)
    if card_diagnostics:
        summary["card_diagnostics"] = card_diagnostics[:3]

    return summary


def _is_generated_image_card(card_data: dict[str, Any]) -> bool:
    card_type = str(card_data.get("cardType") or "").strip().lower()
    render_type = str(card_data.get("type") or "").strip().lower()
    return (
        card_type == "generated_image_card"
        or render_type == "render_generated_image"
    )


def _is_likely_asset_reference(text: str, parent_key: str = "") -> bool:
    value = str(text or "").strip()
    if not value:
        return False

    lower_value = value.lower()
    if lower_value.startswith(("http://", "https://")):
        return True
    if lower_value.startswith(("data:", "<grok:", "<xai:")):
        return False
    if any(ch.isspace() for ch in value):
        return False

    key = str(parent_key or "").lower()
    key_suggests_asset = (
        key in {
            "url",
            "original",
            "imageurl",
            "image_url",
            "downloadurl",
            "download_url",
            "src",
            "source",
            "uri",
            "fileuri",
            "file_uri",
            "path",
            "imagepath",
            "image_path",
        }
        or any(term in key for term in ("url", "uri", "path", "file"))
    )
    if not key_suggests_asset:
        return False

    # App-chat generated image cards currently expose asset paths like:
    # users/<user-id>/generated/<image-id>/image.webp
    if value.startswith("/"):
        return True
    if "/" in value:
        return True

    return False


def _collect_generated_card_urls(value: Any) -> List[str]:
    primary: List[str] = []
    secondary: List[str] = []
    seen = set()

    def add(url: str, *, preferred: bool = True):
        cleaned = str(url or "").strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        if preferred:
            primary.append(cleaned)
        else:
            secondary.append(cleaned)

    def walk(node: Any, parent_key: str = ""):
        if isinstance(node, dict):
            for key, item in node.items():
                walk(item, key)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, parent_key)
            return
        if not isinstance(node, str):
            return

        text = node.strip()
        if not _is_likely_asset_reference(text, parent_key):
            return

        key = parent_key.lower()
        preferred = (
            key in {"url", "original", "imageurl", "image_url", "downloadurl", "download_url", "src", "source"}
            and "thumbnail" not in key
        )

        if not (text.startswith("http://") or text.startswith("https://")):
            add(text, preferred=preferred)
            return

        host = (urlparse(text).hostname or "").lower()

        # Prefer Grok / xAI asset hosts, but still allow other explicit URLs from generated cards.
        if host.endswith("assets.grok.com") or host.endswith("grok.com") or host.endswith("x.ai"):
            add(text, preferred=True)
        else:
            add(text, preferred=preferred)

    walk(value)
    return primary + secondary


def _normalize_candidate_url_path(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        path = parsed.path or ""
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path
    return value


def _candidate_group_key(url: str) -> str:
    path = _normalize_candidate_url_path(url).split("?", 1)[0].lower()
    normalized = re.sub(
        r"(/generated/[^/]+)-part-\d+(/[^/]+)$",
        r"\1\2",
        path,
    )
    match = re.match(r"^(.*?/generated/[^/]+)(?:/.*)?$", normalized)
    if match:
        return match.group(1)
    return normalized


def _candidate_priority(url: str) -> int:
    path = _normalize_candidate_url_path(url).lower()
    score = 100

    if "/generated/" in path:
        score -= 20
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
        score -= 5
    if re.search(r"/image\.(jpg|jpeg|png|webp|avif)(?:\?|$)", path):
        score -= 10
    if re.search(r"/(original|download|full|fullres|fullsize)\.(jpg|jpeg|png|webp|avif)(?:\?|$)", path):
        score -= 35
    if "/original/" in path or "download=" in path or "download/" in path:
        score -= 20
    if "thumbnail" in path or "thumb" in path:
        score += 40
    if re.search(r"-part-\d+(?=/)", path):
        score += 60

    return score


def _prefer_best_candidate_urls(urls: List[str]) -> List[str]:
    chosen: dict[str, tuple[int, int, str]] = {}
    first_seen_order: dict[str, int] = {}

    for index, raw_url in enumerate(urls):
        url = str(raw_url or "").strip()
        if not url:
            continue

        group_key = _candidate_group_key(url)
        if group_key not in first_seen_order:
            first_seen_order[group_key] = index

        priority = _candidate_priority(url)
        current = chosen.get(group_key)
        candidate = (priority, first_seen_order[group_key], url)
        if current is None or priority < current[0]:
            chosen[group_key] = candidate

    return [
        item[2]
        for item in sorted(
            chosen.values(),
            key=lambda item: (item[1], item[0], item[2]),
        )
    ]


def _summarize_card_payload(source: str, raw: Any) -> dict[str, Any]:
    card_data, parse_error = _parse_card_payload(raw)
    if not isinstance(card_data, dict):
        return {
            "source": source,
            "parse_error": parse_error,
            "raw_preview": _compact_preview(raw),
        }

    summary: dict[str, Any] = {
        "source": source,
        "id": _compact_preview(card_data.get("id"), limit=48),
        "type": _compact_preview(card_data.get("type"), limit=48),
        "card_type": _compact_preview(card_data.get("cardType"), limit=48),
        "keys": sorted(card_data.keys()),
        "is_generated_image_card": _is_generated_image_card(card_data),
    }

    image_chunk = card_data.get("image_chunk")
    if isinstance(image_chunk, dict):
        summary["image_chunk_keys"] = sorted(image_chunk.keys())
        if image_chunk.get("imageUuid"):
            summary["image_uuid"] = _compact_preview(
                image_chunk.get("imageUuid"), limit=64
            )

    urls = _collect_generated_card_urls(card_data)
    summary["candidate_url_count"] = len(urls)
    if urls:
        summary["candidate_url_preview"] = _preview_items(urls, limit=4)

    hints = _collect_card_reference_hints(card_data)
    if hints:
        summary["field_hints"] = hints

    return summary


def _collect_response_card_diagnostics(resp: dict[str, Any]) -> List[dict[str, Any]]:
    diagnostics: List[dict[str, Any]] = []
    for source, raw in _iter_card_payloads(resp):
        diagnostics.append(_summarize_card_payload(source, raw))
    return diagnostics


def _extract_generated_card_urls_from_response(resp: dict[str, Any]) -> List[str]:
    urls: List[str] = []
    seen = set()

    def add_all(candidate_urls: List[str]):
        for url in candidate_urls:
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)

    for _source, raw in _iter_card_payloads(resp):
        card_data, _parse_error = _parse_card_payload(raw)
        if isinstance(card_data, dict) and _is_generated_image_card(card_data):
            add_all(_collect_generated_card_urls(card_data))

    return urls


@dataclass
class ImageEditResult:
    stream: bool
    data: Union[AsyncGenerator[str, None], List[str]]


class ImageEditService:
    """Image edit orchestration service."""

    @staticmethod
    def _build_request_overrides(n: int) -> Dict[str, Any]:
        return {"imageGenerationCount": max(1, int(n or 1))}

    async def edit(
        self,
        *,
        token_mgr: Any,
        token: str,
        model_info: Any,
        prompt: str,
        images: List[str],
        n: int,
        response_format: str,
        stream: bool,
        chat_format: bool = False,
    ) -> ImageEditResult:
        if len(images) > 3:
            logger.info(
                "Image edit received %d references; using the most recent 3",
                len(images),
            )
            images = images[-3:]

        max_token_retries = int(get_config("retry.max_retry") or 3)
        tried_tokens: set[str] = set()
        last_error: Exception | None = None

        for attempt in range(max_token_retries):
            preferred = token if attempt == 0 else None
            current_token = await pick_token(
                token_mgr, model_info.model_id, tried_tokens, preferred=preferred
            )
            if not current_token:
                if last_error:
                    raise last_error
                raise no_token_error(model_info.model_id)

            tried_tokens.add(current_token)
            try:
                file_attachments = await self._upload_images(images, current_token)
                tool_overrides: Dict[str, Any] | None = None
                request_overrides = self._build_request_overrides(n)

                if stream:
                    response = await GrokChatService().chat(
                        token=current_token,
                        message=prompt,
                        model=model_info.grok_model,
                        mode=model_info.model_mode,
                        stream=True,
                        file_attachments=file_attachments,
                        tool_overrides=tool_overrides,
                        request_overrides=request_overrides,
                    )
                    processor = ImageStreamProcessor(
                        model_info.model_id,
                        current_token,
                        n=n,
                        response_format=response_format,
                        chat_format=chat_format,
                    )
                    return ImageEditResult(
                        stream=True,
                        data=wrap_stream_with_usage(
                            processor.process(response),
                            token_mgr,
                            current_token,
                            model_info.model_id,
                        ),
                    )

                images_out = await self._collect_images(
                    token=current_token,
                    prompt=prompt,
                    n=n,
                    response_format=response_format,
                    file_attachments=file_attachments,
                    tool_overrides=tool_overrides,
                    grok_model=model_info.grok_model,
                    model_mode=model_info.model_mode,
                    model_id=model_info.model_id,
                )
                try:
                    effort = (
                        EffortType.HIGH
                        if (model_info and model_info.cost.value == "high")
                        else EffortType.LOW
                    )
                    await token_mgr.consume(current_token, effort)
                    logger.debug(
                        f"Image edit completed, recorded usage (effort={effort.value})"
                    )
                except Exception as e:
                    logger.warning(f"Failed to record image edit usage: {e}")
                return ImageEditResult(stream=False, data=images_out)

            except UpstreamException as e:
                last_error = e
                if rate_limited(e):
                    await token_mgr.mark_rate_limited(current_token)
                    logger.warning(
                        f"Token {current_token[:10]}... rate limited (429), "
                        f"trying next token (attempt {attempt + 1}/{max_token_retries})"
                    )
                    continue
                raise

        if last_error:
            raise last_error
        raise no_token_error(model_info.model_id)

    async def _upload_images(self, images: List[str], token: str) -> List[str]:
        file_attachments: List[str] = []
        upload_service = UploadService()
        try:
            for image in images:
                file_id, _ = await upload_service.upload_file(image, token)
                if file_id:
                    file_attachments.append(file_id)
        finally:
            await upload_service.close()

        if not file_attachments:
            raise AppException(
                message="Image upload failed",
                error_type=ErrorType.SERVER.value,
                code="upload_failed",
            )

        return file_attachments

    async def _collect_images(
        self,
        *,
        token: str,
        prompt: str,
        n: int,
        response_format: str,
        file_attachments: List[str],
        tool_overrides: dict,
        grok_model: str,
        model_mode: str,
        model_id: str,
    ) -> List[str]:
        per_call = 2
        calls_needed = max(1, (n + per_call - 1) // per_call)

        async def _call_edit():
            response = await GrokChatService().chat(
                token=token,
                message=prompt,
                model=grok_model,
                mode=model_mode,
                stream=True,
                file_attachments=file_attachments,
                tool_overrides=tool_overrides,
                request_overrides=self._build_request_overrides(per_call),
            )
            processor = ImageCollectProcessor(
                model_id, token, response_format=response_format
            )
            return await processor.process(response)

        last_error: Exception | None = None
        rate_limit_error: Exception | None = None

        if calls_needed == 1:
            all_images = await _call_edit()
        else:
            tasks = [_call_edit() for _ in range(calls_needed)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_images: List[str] = []
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Concurrent call failed: {result}")
                    last_error = result
                    if rate_limited(result):
                        rate_limit_error = result
                elif isinstance(result, list):
                    all_images.extend(result)

        if not all_images:
            if rate_limit_error:
                raise rate_limit_error
            if last_error:
                raise last_error
            raise UpstreamException(
                "Image edit returned no results", details={"error": "empty_result"}
            )

        if len(all_images) >= n:
            return all_images[:n]

        selected_images = all_images.copy()
        while len(selected_images) < n:
            selected_images.append("error")
        return selected_images


class ImageStreamProcessor(BaseProcessor):
    """HTTP image stream processor."""

    def __init__(
        self, model: str, token: str = "", n: int = 1, response_format: str = "b64_json", chat_format: bool = False
    ):
        super().__init__(model, token)
        self.partial_index = 0
        self.n = n
        self.target_index = 0 if n == 1 else None
        self.response_format = response_format
        self.chat_format = chat_format
        self._id_generated = False
        self._response_id = ""
        if response_format == "url":
            self.response_field = "url"
        elif response_format == "base64":
            self.response_field = "base64"
        else:
            self.response_field = "b64_json"

    def _sse(self, event: str, data: dict) -> str:
        """Build SSE response."""
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """Process stream response."""
        candidate_urls: List[str] = []
        seen_candidate_urls: set[str] = set()
        final_images = []
        emitted_chat_chunk = False
        idle_timeout = get_config("image.stream_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                # Image generation progress
                if img := resp.get("streamingImageGenerationResponse"):
                    image_index = img.get("imageIndex", 0)
                    progress = img.get("progress", 0)

                    if self.n == 1 and image_index != self.target_index:
                        continue

                    out_index = 0 if self.n == 1 else image_index

                    if not self.chat_format:
                        yield self._sse(
                            "image_generation.partial_image",
                            {
                                "type": "image_generation.partial_image",
                                self.response_field: "",
                                "index": out_index,
                                "progress": progress,
                            },
                        )
                    continue

                # modelResponse
                if mr := resp.get("modelResponse"):
                    urls = _collect_images(mr)
                    urls.extend(_extract_generated_card_urls_from_response(resp))
                    if urls:
                        for url in urls:
                            cleaned = str(url or "").strip()
                            if not cleaned or cleaned in seen_candidate_urls:
                                continue
                            seen_candidate_urls.add(cleaned)
                            candidate_urls.append(cleaned)
                    continue

            selected_urls = _prefer_best_candidate_urls(candidate_urls)
            if candidate_urls and selected_urls != candidate_urls:
                logger.debug(
                    "Image stream prioritized candidate URLs: original={} selected={}",
                    _preview_items(candidate_urls, limit=6),
                    _preview_items(selected_urls, limit=6),
                )

            for url in selected_urls:
                if self.response_format == "url":
                    processed = await self.process_url(url, "image")
                    if processed:
                        final_images.append(processed)
                    continue
                try:
                    dl_service = self._get_dl()
                    base64_data = await dl_service.parse_b64(
                        url, self.token, "image"
                    )
                    if base64_data:
                        if "," in base64_data:
                            b64 = base64_data.split(",", 1)[1]
                        else:
                            b64 = base64_data
                        final_images.append(b64)
                except Exception as e:
                    logger.warning(
                        f"Failed to convert image to base64, falling back to URL: {e}"
                    )
                    processed = await self.process_url(url, "image")
                    if processed:
                        final_images.append(processed)

            for index, img_data in enumerate(final_images):
                if self.n == 1:
                    if index != self.target_index:
                        continue
                    out_index = 0
                else:
                    out_index = index

                # Wrap in markdown format for chat
                output = img_data
                if self.chat_format and output:
                    output = wrap_image_content(output, self.response_format)

                if not self._id_generated:
                    self._response_id = make_response_id()
                    self._id_generated = True

                if self.chat_format:
                    # OpenAI ChatCompletion chunk format
                    emitted_chat_chunk = True
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            output,
                            index=out_index,
                            is_final=True,
                        ),
                    )
                else:
                    # Original image_generation format
                    yield self._sse(
                        "image_generation.completed",
                        {
                            "type": "image_generation.completed",
                            self.response_field: img_data,
                            "index": out_index,
                            "usage": {
                                "total_tokens": 0,
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "input_tokens_details": {
                                    "text_tokens": 0,
                                    "image_tokens": 0,
                                },
                            },
                        },
                    )

            if self.chat_format:
                if not self._id_generated:
                    self._response_id = make_response_id()
                    self._id_generated = True
                if not emitted_chat_chunk:
                    yield self._sse(
                        "chat.completion.chunk",
                        make_chat_chunk(
                            self._response_id,
                            self.model,
                            "",
                            index=0,
                            is_final=True,
                        ),
                    )
                yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.debug("Image stream cancelled by client")
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Image stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image: {e}")
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Image stream request error: {e}")
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Image stream processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class ImageCollectProcessor(BaseProcessor):
    """HTTP image non-stream processor."""

    def __init__(self, model: str, token: str = "", response_format: str = "b64_json"):
        if response_format == "base64":
            response_format = "b64_json"
        super().__init__(model, token)
        self.response_format = response_format
        self._debug_seen = 0
        self._recent_summaries: list[dict[str, Any]] = []
        self._seen_candidate_urls: set[str] = set()

    def _remember_summary(self, summary: dict[str, Any]) -> None:
        self._recent_summaries.append(summary)
        if len(self._recent_summaries) > 10:
            self._recent_summaries = self._recent_summaries[-10:]

    def _collect_candidate_urls(
        self,
        candidate_urls: List[str],
        urls: List[str],
        *,
        source: str,
    ) -> int:
        unique_urls: List[str] = []
        for url in urls:
            cleaned = str(url or "").strip()
            if not cleaned or cleaned in self._seen_candidate_urls:
                continue
            self._seen_candidate_urls.add(cleaned)
            unique_urls.append(cleaned)

        if not unique_urls:
            return 0

        logger.debug(
            "Image collect candidate URLs: source={} count={} preview={}",
            source,
            len(unique_urls),
            _preview_items(unique_urls, limit=4),
        )
        candidate_urls.extend(unique_urls)
        return len(unique_urls)

    async def process(self, response: AsyncIterable[bytes]) -> List[str]:
        """Process and collect images."""
        candidate_urls: List[str] = []
        idle_timeout = get_config("image.stream_timeout")
        saw_model_response = False
        saw_card_attachment = False
        saw_streaming_progress = False

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})
                summary = _summarize_image_response(resp)
                self._remember_summary(summary)
                if self._debug_seen < 5:
                    self._debug_seen += 1
                    logger.debug(
                        "Image collect frame[{}]: {}",
                        self._debug_seen,
                        summary,
                    )

                if (
                    summary.get("has_model_response")
                    or summary.get("has_card_attachment")
                    or summary.get("has_streaming_progress")
                ):
                    logger.debug("Image collect key-frame: {}", summary)

                if resp.get("streamingImageGenerationResponse"):
                    saw_streaming_progress = True
                if isinstance(resp.get("cardAttachment"), dict):
                    saw_card_attachment = True

                if mr := resp.get("modelResponse"):
                    saw_model_response = True
                    self._collect_candidate_urls(
                        candidate_urls,
                        _collect_images(mr),
                        source="modelResponse.standard_fields",
                    )

                if ur := resp.get("userResponse"):
                    self._collect_candidate_urls(
                        candidate_urls,
                        _collect_images(ur),
                        source="userResponse.standard_fields",
                    )

                self._collect_candidate_urls(
                    candidate_urls,
                    _extract_generated_card_urls_from_response(resp),
                    source="generated_image_cards",
                )

        except asyncio.CancelledError:
            logger.debug("Image collect cancelled by client")
        except StreamIdleTimeoutError as e:
            logger.warning(f"Image collect idle timeout: {e}")
        except RequestsError as e:
            if _is_http2_error(e):
                logger.warning(f"HTTP/2 stream error in image collect: {e}")
            else:
                logger.error(f"Image collect request error: {e}")
        except Exception as e:
            logger.error(
                f"Image collect processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
        finally:
            await self.close()

        selected_urls = _prefer_best_candidate_urls(candidate_urls)
        if candidate_urls and selected_urls != candidate_urls:
            logger.debug(
                "Image collect prioritized candidate URLs: original={} selected={}",
                _preview_items(candidate_urls, limit=6),
                _preview_items(selected_urls, limit=6),
            )

        images: List[str] = []
        for url in selected_urls:
            if self.response_format == "url":
                processed = await self.process_url(url, "image")
                if processed:
                    images.append(processed)
                continue
            try:
                dl_service = self._get_dl()
                base64_data = await dl_service.parse_b64(url, self.token, "image")
                if base64_data:
                    if "," in base64_data:
                        b64 = base64_data.split(",", 1)[1]
                    else:
                        b64 = base64_data
                    images.append(b64)
            except Exception as e:
                logger.warning(
                    "Failed to convert finalized image to base64, falling back to URL: {}",
                    e,
                )
                processed = await self.process_url(url, "image")
                if processed:
                    images.append(processed)

        if not images:
            logger.warning(
                "Image collect returned no images: saw_model_response={} saw_card_attachment={} saw_streaming_progress={} recent_frames={} card_diagnostics={}",
                saw_model_response,
                saw_card_attachment,
                saw_streaming_progress,
                self._recent_summaries,
                [
                    frame.get("card_diagnostics")
                    for frame in self._recent_summaries
                    if frame.get("card_diagnostics")
                ],
            )
        return images


__all__ = ["ImageEditService", "ImageEditResult"]
