import os
from pathlib import Path

import paths


def test_plugin_data_dir_uses_env_var_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    result = paths.plugin_data_dir()
    assert result == tmp_path / "local-coder"
    # It must create the directory (subprocesses write into it immediately).
    assert result.is_dir()


def test_plugin_data_dir_falls_back_to_in_tree_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = paths.plugin_data_dir()
    # Fallback is the directory containing paths.py itself.
    assert result == Path(paths.__file__).parent


def test_plugin_data_dir_falls_back_when_env_empty(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "")
    result = paths.plugin_data_dir()
    assert result == Path(paths.__file__).parent
