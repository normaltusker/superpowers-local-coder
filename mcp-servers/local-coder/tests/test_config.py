import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import config as config_module
import ollama as ollama_module


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Copy the real default config.yaml into a temp dir and point
    CONFIG_PATH at the copy, so tests never mutate the checked-in file."""
    real_config = Path(__file__).parent.parent / "config.yaml"
    temp_config = tmp_path / "config.yaml"
    shutil.copy(real_config, temp_config)
    monkeypatch.setattr(config_module, "CONFIG_PATH", temp_config)
    return temp_config


def test_load_config_returns_defaults(isolated_config):
    cfg = config_module.load_config()
    assert cfg["backend"] == "aider"
    assert cfg["model"] == "ollama/qwen3-coder:30b"
    assert cfg["fallback_models"] == []
    assert cfg["max_fallback_models"] == 3
    assert cfg["open_pr"] is False


def test_save_config_persists_changes(isolated_config):
    cfg = config_module.load_config()
    cfg["model"] = "ollama/qwen2.5-coder:14b"
    config_module.save_config(cfg)

    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/qwen2.5-coder:14b"


def test_merge_config_updates_only_provided_keys(isolated_config):
    result = config_module.merge_config({"model": "ollama/deepseek-coder-v2:16b"})

    assert result["model"] == "ollama/deepseek-coder-v2:16b"
    assert result["backend"] == "aider"  # untouched key retains its value
    assert result["open_pr"] is False    # untouched key retains its value

    # confirm it was actually written, not just returned in-memory
    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/deepseek-coder-v2:16b"


def test_merge_config_with_empty_overrides_is_a_noop(isolated_config):
    before = config_module.load_config()
    result = config_module.merge_config({})
    assert result == before


def test_configure_with_validation_rejects_unpulled_ollama_model(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b"]):
        with pytest.raises(config_module.ConfigValidationError, match="not pulled|not found|unavailable"):
            config_module.configure_with_validation({"model": "ollama/does-not-exist:1b"})


def test_configure_with_validation_accepts_pulled_ollama_model(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen2.5-coder:14b"]):
        result = config_module.configure_with_validation({"model": "ollama/qwen2.5-coder:14b"})
    assert result["model"] == "ollama/qwen2.5-coder:14b"


def test_configure_with_validation_skips_check_for_non_ollama_prefix(isolated_config):
    # no mock needed — should never call list_ollama_models for a non-ollama/ model
    with patch.object(ollama_module, "list_ollama_models") as mock_list:
        result = config_module.configure_with_validation({"model": "openrouter/some-model"})
        mock_list.assert_not_called()
    assert result["model"] == "openrouter/some-model"


def test_configure_with_validation_rejects_fallback_list_over_cap(isolated_config):
    # include the config's existing default model ("qwen3-coder:30b") in the
    # mock's available list since the merged/effective model is now
    # re-validated on every call, not just when overridden.
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b", "a:1b", "b:1b", "c:1b", "d:1b"]):
        with pytest.raises(config_module.ConfigValidationError, match="max_fallback_models|limit"):
            config_module.configure_with_validation({
                "fallback_models": ["ollama/a:1b", "ollama/b:1b", "ollama/c:1b", "ollama/d:1b"]
            })


def test_configure_with_validation_accepts_fallback_list_at_cap(isolated_config):
    # include the config's existing default model ("qwen3-coder:30b") in the
    # mock's available list since the merged/effective model is now
    # re-validated on every call, not just when overridden.
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b", "a:1b", "b:1b", "c:1b"]):
        result = config_module.configure_with_validation({
            "fallback_models": ["ollama/a:1b", "ollama/b:1b", "ollama/c:1b"]
        })
    assert len(result["fallback_models"]) == 3


def test_configure_with_validation_rejects_gemini_with_ollama_model(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b"]):
        with pytest.raises(config_module.ConfigValidationError, match="[Gg]emini"):
            config_module.configure_with_validation({
                "backend": "gemini", "model": "ollama/qwen3-coder:30b"
            })


def test_configure_with_validation_revalidates_existing_model_not_just_overrides(isolated_config):
    # Set up: config already has an ollama/-prefixed model, validated at the
    # time it was set.
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b"]):
        config_module.configure_with_validation({"model": "ollama/qwen3-coder:30b"})

    # Now the model is no longer pulled (e.g. `ollama rm`), and a later call
    # changes an unrelated field without touching `model`. This should still
    # re-validate the merged/effective model and reject it as stale.
    with patch.object(ollama_module, "list_ollama_models", return_value=[]):
        with pytest.raises(config_module.ConfigValidationError, match="not pulled|not found|unavailable"):
            config_module.configure_with_validation({"max_fallback_models": 5})


def test_configure_with_validation_rejects_duplicate_fallback_models(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="[Dd]uplicate"):
        config_module.configure_with_validation({
            "fallback_models": ["openrouter/a", "openrouter/a"]
        })


def test_configure_with_validation_rejects_fallback_model_matching_primary(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="[Dd]uplicate"):
        config_module.configure_with_validation({
            "model": "openrouter/a", "fallback_models": ["openrouter/a"]
        })


def test_configure_with_validation_rejects_empty_string_model(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="empty|whitespace"):
        config_module.configure_with_validation({"model": ""})


def test_configure_with_validation_rejects_whitespace_only_model(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="empty|whitespace"):
        config_module.configure_with_validation({"model": "   "})


def test_configure_with_validation_rejects_empty_string_fallback_model(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="empty|whitespace"):
        config_module.configure_with_validation({"fallback_models": ["openrouter/a", ""]})


def test_configure_with_validation_rejects_unknown_backend(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="[Uu]nknown backend|backend"):
        config_module.configure_with_validation({"backend": "aidee"})


def test_configure_with_validation_accepts_known_backends(isolated_config):
    # Use a non-ollama model throughout so this doesn't collide with the
    # separate gemini+ollama incompatibility check.
    config_module.configure_with_validation({"model": "openrouter/some-model"})
    for name in ("aider", "codex", "gemini", "openrouter"):
        result = config_module.configure_with_validation({"backend": name})
        assert result["backend"] == name


def test_configure_with_validation_clears_target_repo_path(isolated_config):
    config_module.configure_with_validation({"target_repo_path": "/some/path"})
    result = config_module.configure_with_validation({"target_repo_path": config_module.CLEAR_FIELD})
    assert result["target_repo_path"] is None


def test_list_available_models_with_prefix_prefixes_correctly(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b", "qwen2.5-coder:14b"]):
        models = config_module.list_available_models_with_prefix()
    assert models == ["ollama/qwen3-coder:30b", "ollama/qwen2.5-coder:14b"]


def test_list_available_models_with_prefix_propagates_unavailable_error(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", side_effect=ollama_module.OllamaUnavailableError("no ollama")):
        with pytest.raises(ollama_module.OllamaUnavailableError):
            config_module.list_available_models_with_prefix()
