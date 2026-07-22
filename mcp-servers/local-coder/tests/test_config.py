import shutil
from pathlib import Path

import pytest

import config as config_module


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
