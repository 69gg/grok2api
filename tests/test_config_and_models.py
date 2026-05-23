from __future__ import annotations

from pathlib import Path
import tomllib

from grok2api.services.grok.model import ModelService as LegacyModelService
from grok2api.services.grok.services.model import ModelService
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


def test_basic_pool_model_policy() -> None:
    basic_models = {
        model.model_id
        for model in ModelService.list()
        if ModelService.pool_for_model(model.model_id) == "ssoBasic"
    }
    assert "grok-4.3-fast" in basic_models
    assert "grok-imagine-1.0" in basic_models
    assert "grok-4.3" in basic_models
    assert ModelService.pool_candidates_for_model("grok-4") == ["ssoSuper"]
    assert "ssoBasic" in ModelService.pool_candidates_for_model("grok-4.3-fast")


def test_image_models_use_grok_43_internally() -> None:
    assert ModelService.get("grok-imagine-1.0").grok_model == "grok-4.3"
    assert ModelService.get("grok-imagine-1.0-edit").grok_model == "grok-4.3"
    assert LegacyModelService.get("grok-imagine-1.0").grok_model == "grok-4.3"
    assert LegacyModelService.get("grok-imagine-1.0-edit").grok_model == "grok-4.3"


def test_no_super_token_for_super_only_model_returns_model_not_found() -> None:
    exc = no_token_error("grok-4")
    assert exc.status_code == 400
    assert exc.code == "model_not_found"
    assert exc.param == "model"


def test_no_basic_token_for_basic_allowed_model_returns_rate_limit() -> None:
    exc = no_token_error("grok-4.3-fast")
    assert exc.status_code == 429
    assert exc.code == "rate_limit_exceeded"
