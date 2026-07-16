from __future__ import annotations

from pathlib import Path
import tomllib

from grok2api.services.grok.model import ModelService as LegacyModelService
from grok2api.services.grok.services.model import (
    Channel,
    ModelService,
    SUPER_GROK_MODEL_IDS,
)
from grok2api.services.grok.utils.errors import no_token_error


ROOT = Path(__file__).resolve().parents[1]


def test_config_toml_is_ignored_and_example_is_tracked() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "config.toml" in gitignore
    assert (ROOT / "config.toml.example").exists()


def test_solver_runtime_dependencies_are_default_dependencies() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert any(dep.startswith("camoufox>=") for dep in dependencies)
    assert any(dep.startswith("playwright>=") for dep in dependencies)
    assert any(dep.startswith("quart>=") for dep in dependencies)
    assert any(dep.startswith("rich>=") for dep in dependencies)


def test_cf_refresh_config_uses_local_solver_route() -> None:
    config_example = tomllib.loads((ROOT / "config.toml.example").read_text(encoding="utf-8"))
    assert "flaresolverr_url" not in config_example["proxy"]
    assert config_example["proxy"]["refresh_interval"] == 1500
    assert config_example["proxy"]["cf_solver_threads"] == 1
    assert config_example["register"]["solver_url"] == "http://127.0.0.1:5072"


def test_console_proxy_config_defaults_to_empty() -> None:
    config_example = tomllib.loads((ROOT / "config.toml.example").read_text(encoding="utf-8"))
    assert config_example["proxy"]["console_proxy_url"] == ""


def test_console_team_auto_init_config_defaults() -> None:
    config_example = tomllib.loads((ROOT / "config.toml.example").read_text(encoding="utf-8"))
    token_config = config_example["token"]
    assert token_config["console_team_auto_init_enabled"] is True
    assert token_config["console_team_auto_init_interval_sec"] == 60
    assert token_config["console_team_auto_init_concurrency"] == 5
    assert token_config["console_team_auto_init_batch_size"] == 100
    assert token_config["console_team_name_prefix"] == "Grok2API team"


def test_retry_config_defaults_to_more_token_attempts() -> None:
    config_example = tomllib.loads((ROOT / "config.toml.example").read_text(encoding="utf-8"))
    assert config_example["retry"]["max_retry"] == 30


def test_build_transport_retry_default_is_bounded() -> None:
    config_example = tomllib.loads((ROOT / "config.toml.example").read_text(encoding="utf-8"))
    assert config_example["build"]["transport_max_retry"] == 3


def test_grok_app_owned_by_includes_platform() -> None:
    assert ModelService.get("grok-4.3-fast").owned_by == "grok-app<grok2api@69gg>"
    assert ModelService.get("grok-4.20-auto").owned_by == "grok-app<grok2api@69gg>"


def test_basic_pool_model_policy() -> None:
    basic_models = {
        model.model_id
        for model in ModelService.list(has_super_tokens=True)
        if ModelService.pool_for_model(model.model_id) == "ssoBasic"
    }
    assert "grok-4.3-fast" in basic_models
    assert "grok-4.3" in basic_models
    assert "grok-imagine-image" in basic_models
    assert ModelService.pool_candidates_for_model("grok-imagine-image") == [
        "ssoBasic",
        "ssoSuper",
    ]
    assert ModelService.pool_candidates_for_model("grok-4.20-auto") == ["ssoSuper"]
    assert ModelService.is_super_pool_only("grok-4.20-auto") is True
    assert ModelService.is_super_pool_only("grok-4.3-fast") is False
    assert "ssoBasic" in ModelService.pool_candidates_for_model("grok-4.3-fast")


def test_super_grok_model_catalog() -> None:
    assert SUPER_GROK_MODEL_IDS == {
        "grok-4.20-0309",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309-non-reasoning-super",
        "grok-4.20-0309-super",
        "grok-4.20-0309-reasoning-super",
        "grok-4.20-auto",
        "grok-4.20-expert",
        "grok-4.3-beta",
        "grok-imagine-image-pro",
        "grok-imagine-image-edit",
        "grok-imagine-1.0-video",
    }


def test_model_list_hides_super_only_models_without_super_tokens() -> None:
    all_ids = {m.model_id for m in ModelService.list(has_super_tokens=True)}
    basic_ids = {m.model_id for m in ModelService.list(has_super_tokens=False)}
    assert "grok-4.20-auto" in all_ids
    assert "grok-imagine-image" in all_ids
    assert "grok-4.20-auto" not in basic_ids
    assert "grok-imagine-image" in basic_ids
    assert "grok-4.3-fast" in basic_ids
    assert "grok-4.3" in basic_ids


def test_model_catalog_uses_channel_priority_order() -> None:
    models = ModelService.list(has_super_tokens=True)
    # CLI > Console family > basic grok.app > super
    priority = {
        Channel.CLI: 0,
        Channel.CONSOLE: 1,
        Channel.CONSOLE_VOICE: 1,
        Channel.CONSOLE_IMAGE: 1,
        Channel.GROK: 2,
    }
    ranks = [
        priority[model.channel]
        if model.model_id == "grok-4.3-fast"
        or model.channel
        in {
            Channel.CLI,
            Channel.CONSOLE,
            Channel.CONSOLE_VOICE,
            Channel.CONSOLE_IMAGE,
        }
        else 3
        for model in models
    ]
    assert ranks == sorted(ranks)


def test_duplicate_model_ids_keep_highest_priority_channel() -> None:
    model = ModelService.get("grok-4.20-0309-reasoning")
    assert model is not None
    assert model.channel == Channel.CONSOLE
    assert model.owned_by == "xai-console<grok2api@69gg>"


def test_image_model_prefers_console_channel() -> None:
    model = ModelService.get("grok-imagine-image")
    assert model is not None
    assert model.channel == Channel.CONSOLE_IMAGE
    assert model.owned_by == "xai-console-image<grok2api@69gg>"


def test_image_models_use_declared_upstream_names() -> None:
    assert ModelService.get("grok-imagine-image").grok_model == "grok-imagine-image"
    assert ModelService.get("grok-imagine-image-edit").grok_model == "grok-imagine-image"
    assert LegacyModelService.get("grok-imagine-image").grok_model == "grok-imagine-image"
    assert LegacyModelService.get("grok-imagine-image-edit").grok_model == "grok-imagine-image"


def test_no_super_token_for_super_only_model_returns_model_not_found() -> None:
    exc = no_token_error("grok-4.20-auto")
    assert exc.status_code == 400
    assert exc.code == "model_not_found"
    assert exc.param == "model"


def test_no_basic_token_for_basic_allowed_model_returns_rate_limit() -> None:
    exc = no_token_error("grok-4.3-fast")
    assert exc.status_code == 429
    assert exc.code == "rate_limit_exceeded"
