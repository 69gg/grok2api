"""
Responses API 路由 (OpenAI compatible).
"""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from grok2api.core.streaming import safe_responses_stream
from grok2api.core.exceptions import ValidationException
from grok2api.services.grok.services.console_capabilities import normalize_reasoning_effort
from grok2api.services.grok.services.console_channel import ConsoleChannelService
from grok2api.services.grok.services.model import ModelService
from grok2api.services.grok.services.responses import ResponsesService
from grok2api.services.reverse.console_payload import strip_console_client_extra


router = APIRouter(tags=["Responses"])


class ResponseCreateRequest(BaseModel):
    model: str = Field(..., description="Model name")
    input: Optional[Any] = Field(None, description="Input content")
    instructions: Optional[str] = Field(None, description="System instructions")
    stream: Optional[bool] = Field(False, description="Stream response")
    max_output_tokens: Optional[int] = Field(None, description="Max output tokens")
    temperature: Optional[float] = Field(None, description="Sampling temperature")
    top_p: Optional[float] = Field(None, description="Nucleus sampling")
    tools: Optional[List[Dict[str, Any]]] = Field(None, description="Tool definitions")
    tool_choice: Optional[Union[str, Dict[str, Any]]] = Field(None, description="Tool choice")
    parallel_tool_calls: Optional[bool] = Field(True, description="Allow parallel tool calls")
    reasoning: Optional[Dict[str, Any]] = Field(None, description="Reasoning options")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata")
    user: Optional[str] = Field(None, description="User identifier")
    store: Optional[bool] = Field(None, description="Store response")
    previous_response_id: Optional[str] = Field(None, description="Previous response id")
    truncation: Optional[str] = Field(None, description="Truncation behavior")

    class Config:
        extra = "allow"


@router.post("/responses")
async def create_response(request: ResponseCreateRequest):
    if not request.model:
        raise ValidationException(message="model is required", param="model", code="invalid_request_error")

    if request.input is None:
        raise ValidationException(message="input is required", param="input", code="invalid_request_error")

    reasoning_effort = None
    reasoning = request.reasoning
    if isinstance(reasoning, dict):
        reasoning_effort = reasoning.get("effort") or reasoning.get("reasoning_effort")
    raw_extra = request.model_dump(
        exclude={
            "model",
            "input",
            "instructions",
            "stream",
            "max_output_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "reasoning",
            "metadata",
            "user",
            "store",
            "previous_response_id",
            "truncation",
        },
        exclude_none=True,
    )
    if reasoning_effort is None and raw_extra.get("reasoning_effort"):
        reasoning_effort = raw_extra.get("reasoning_effort")
    if reasoning_effort and not isinstance(reasoning, dict):
        reasoning = {"effort": reasoning_effort}
    elif reasoning_effort and isinstance(reasoning, dict) and reasoning.get("effort") is None:
        reasoning = {**reasoning, "effort": reasoning_effort}
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        reasoning = {**reasoning, "effort": normalize_reasoning_effort(reasoning.get("effort"))}

    extra_fields = strip_console_client_extra(raw_extra)

    if ModelService.is_console(request.model):
        result = await ConsoleChannelService.responses(
            model=request.model,
            input_value=request.input,
            instructions=request.instructions,
            stream=bool(request.stream),
            temperature=request.temperature,
            top_p=request.top_p,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
            reasoning=reasoning,
            max_output_tokens=request.max_output_tokens,
            include=extra_fields.pop("include", None),
            text=extra_fields.pop("text", None),
            frequency_penalty=extra_fields.pop("frequency_penalty", None),
            presence_penalty=extra_fields.pop("presence_penalty", None),
            store=request.store,
            previous_response_id=request.previous_response_id,
            **extra_fields,
        )
    else:
        result = await ResponsesService.create(
            model=request.model,
            input_value=request.input,
            instructions=request.instructions,
            stream=bool(request.stream),
            temperature=request.temperature,
            top_p=request.top_p,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
            reasoning_effort=reasoning_effort,
            max_output_tokens=request.max_output_tokens,
            metadata=request.metadata,
            user=request.user,
            store=request.store,
            previous_response_id=request.previous_response_id,
            truncation=request.truncation,
        )

    if request.stream:
        return StreamingResponse(
            safe_responses_stream(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return JSONResponse(content=result)


__all__ = ["router"]
