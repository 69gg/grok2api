"""Shared Grok service error helpers."""

from __future__ import annotations

from grok2api.core.exceptions import AppException, ErrorType
from grok2api.services.grok.services.model import ModelService


def no_token_error(model_id: str) -> AppException:
    """Return the public error for an unavailable token pool."""
    if ModelService.pool_candidates_for_model(model_id) == ["ssoSuper"]:
        return AppException(
            message=f"The model `{model_id}` does not exist or you do not have access to it.",
            error_type=ErrorType.INVALID_REQUEST.value,
            code="model_not_found",
            param="model",
            status_code=400,
        )

    return AppException(
        message="No available tokens. Please try again later.",
        error_type=ErrorType.RATE_LIMIT.value,
        code="rate_limit_exceeded",
        status_code=429,
    )


__all__ = ["no_token_error"]
