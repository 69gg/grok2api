"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import platform
import sys
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from grok2api.api.v1.admin import router as admin_router
from grok2api.api.v1.audio import router as audio_router
from grok2api.api.v1.audio_ws import router as audio_ws_router
from grok2api.api.v1.chat import router as chat_router
from grok2api.api.v1.files import router as files_router
from grok2api.api.v1.function import router as function_router
from grok2api.api.v1.image import router as image_router
from grok2api.api.v1.messages import router as messages_router
from grok2api.api.v1.models import router as models_router
from grok2api.api.v1.response import router as responses_router
from grok2api.api.v1.uploads import router as uploads_router
from grok2api.api.v1.video import router as video_router
from grok2api.core.auth import verify_api_key
from grok2api.core.config import config, get_config, register_defaults
from grok2api.core.exceptions import register_exception_handlers
from grok2api.core.logger import logger, setup_logging
from grok2api.core.paths import PROJECT_ROOT
from grok2api.core.response_middleware import ResponseLoggerMiddleware
from grok2api.core.storage import StorageFactory
from grok2api.services.grok.defaults import get_grok_defaults
from grok2api.services.token import get_scheduler


env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    json_console=False,
    file_logging=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown resources."""
    register_defaults(get_grok_defaults())
    await config.ensure_loaded()

    logger.info("Starting Grok2API...")
    logger.info(f"Platform: {platform.system()} {platform.release()}")
    logger.info(f"Python: {sys.version.split()[0]}")

    token_refresh_enabled = bool(get_config("token.auto_refresh", True))
    cf_refresh_enabled = bool(get_config("proxy.enabled", False))
    console_team_init_enabled = bool(
        get_config("token.console_team_auto_init_enabled", True)
    )
    scheduler = None
    if token_refresh_enabled or cf_refresh_enabled or console_team_init_enabled:
        basic_interval = float(get_config("token.refresh_interval_hours", 8) or 8)
        super_interval = float(get_config("token.super_refresh_interval_hours", 2) or 2)
        scheduler = get_scheduler(min(basic_interval, super_interval))
        scheduler.start(
            token_refresh_enabled=token_refresh_enabled,
            cf_refresh_enabled=cf_refresh_enabled,
            console_team_init_enabled=console_team_init_enabled,
        )

    logger.info("Application startup complete.")
    try:
        yield
    finally:
        logger.info("Shutting down Grok2API...")
        try:
            from grok2api.services.register import get_auto_register_manager

            await get_auto_register_manager().stop_job()
        except Exception:
            pass

        if scheduler is not None:
            await scheduler.stop()

        if StorageFactory._instance:
            await StorageFactory._instance.close()


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="Grok2API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ResponseLoggerMiddleware)

    @app.middleware("http")
    async def ensure_config_loaded(request: Request, call_next):
        await config.ensure_loaded()
        return await call_next(request)

    register_exception_handlers(app)

    app.include_router(chat_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(audio_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(audio_ws_router, prefix="/v1")
    app.include_router(image_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(models_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(responses_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(video_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(uploads_router, prefix="/v1", dependencies=[Depends(verify_api_key)])
    app.include_router(files_router, prefix="/v1/files")
    app.include_router(messages_router, prefix="/v1")
    app.include_router(admin_router, prefix="/v1/admin")
    app.include_router(function_router, prefix="/v1/function")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


__all__ = ["app", "create_app"]
