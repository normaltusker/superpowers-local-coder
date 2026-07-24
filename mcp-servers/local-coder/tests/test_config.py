import os
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


def test_save_config_writes_atomically_via_temp_file_and_replace(isolated_config, monkeypatch):
    # Confirm save_config never writes directly to CONFIG_PATH in place —
    # it must go through a temp file in the same directory followed by
    # os.replace(), so a crash mid-write can't leave config.yaml partially
    # written/corrupted.
    import os as os_module

    replace_calls = []
    real_replace = os_module.replace

    def spying_replace(src, dst):
        # At the moment of replace, the temp source file must already be
        # fully written and exist as a sibling of CONFIG_PATH (same dir).
        assert Path(src).parent == isolated_config.parent
        assert Path(src).exists()
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", spying_replace)

    cfg = config_module.load_config()
    cfg["model"] = "ollama/atomic-write-test:1b"
    config_module.save_config(cfg)

    assert len(replace_calls) == 1
    assert str(replace_calls[0][1]) == str(isolated_config)
    # the temp file must be gone after a successful replace
    leftover_temps = list(isolated_config.parent.glob(".config.yaml.*.tmp"))
    assert leftover_temps == []

    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/atomic-write-test:1b"


def test_save_config_cleans_up_temp_file_on_write_failure(isolated_config, monkeypatch):
    import yaml as yaml_module

    def blowup_dump(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(yaml_module, "safe_dump", blowup_dump)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        config_module.save_config({"model": "x"})

    leftover_temps = list(isolated_config.parent.glob(".config.yaml.*.tmp"))
    assert leftover_temps == []
    # the original file must be untouched
    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/qwen3-coder:30b"


def test_configure_with_validation_uses_lock_around_compound_operation(isolated_config):
    # Basic sanity check that the lock is actually acquired and released
    # around the compound operation — not a true concurrency test (hard to
    # do deterministically), just confirms the lockfile is created and
    # usable, and that configure_with_validation completes normally under
    # it (i.e. the lock doesn't deadlock or leak across calls).
    result1 = config_module.configure_with_validation({"max_fallback_models": 5})
    result2 = config_module.configure_with_validation({"max_fallback_models": 6})
    assert result1["max_fallback_models"] == 5
    assert result2["max_fallback_models"] == 6

    lock_path = config_module._lock_path()
    assert lock_path.exists()


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


def test_configure_with_validation_rejects_non_positive_stall_timeout(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="stall_timeout_seconds"):
        config_module.configure_with_validation({"stall_timeout_seconds": 0})
    with pytest.raises(config_module.ConfigValidationError, match="stall_timeout_seconds"):
        config_module.configure_with_validation({"stall_timeout_seconds": -5})


def test_configure_with_validation_accepts_positive_stall_timeout(isolated_config):
    result = config_module.configure_with_validation({"stall_timeout_seconds": 600})
    assert result["stall_timeout_seconds"] == 600


def test_configure_with_validation_rejects_non_positive_idle_notify_interval(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="idle_notify_interval_seconds"):
        config_module.configure_with_validation({"idle_notify_interval_seconds": 0})
    with pytest.raises(config_module.ConfigValidationError, match="idle_notify_interval_seconds"):
        config_module.configure_with_validation({"idle_notify_interval_seconds": -1})


def test_configure_with_validation_accepts_positive_idle_notify_interval(isolated_config):
    result = config_module.configure_with_validation({"idle_notify_interval_seconds": 30})
    assert result["idle_notify_interval_seconds"] == 30


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


def test_config_lock_selects_platform_lock_module():
    # The module must pick the OS-appropriate lock primitive at import:
    # fcntl on POSIX, msvcrt on Windows. This guards against the current
    # unconditional `import fcntl`, which crashes import on Windows.
    import config as config_module
    if config_module._IS_WINDOWS:
        import msvcrt  # noqa: F401 — must be importable on Windows
        assert config_module._lock_module.__name__ == "msvcrt"
    else:
        import fcntl  # noqa: F401
        assert config_module._lock_module.__name__ == "fcntl"


def test_config_lock_still_guards_the_critical_section(isolated_config):
    # The lock must still actually serialize: acquiring it, then confirming
    # the guarded save round-trips a value, proves the context manager
    # yields and releases cleanly on this platform.
    import config as config_module
    with config_module._config_lock():
        config_module.save_config({"backend": "aider", "model": "ollama/x",
                                   "fallback_models": [], "max_fallback_models": 3,
                                   "stall_timeout_seconds": 300, "target_repo_path": None,
                                   "branch_prefix": "local-coder/", "open_pr": False,
                                   "pr_base_branch": "main",
                                   "idle_notify_interval_seconds": 20,
                                   "extra_backend_args": []})
    assert config_module.load_config()["model"] == "ollama/x"


def test_load_config_seeds_from_default_template_when_missing(tmp_path, monkeypatch):
    # In a fresh install, the persistent config.yaml doesn't exist yet.
    # load_config() must seed it from the bundled default template rather
    # than crashing on a missing file.
    import config as config_module
    fresh = tmp_path / "config.yaml"
    assert not fresh.exists()
    monkeypatch.setattr(config_module, "CONFIG_PATH", fresh)

    cfg = config_module.load_config()

    assert fresh.exists()  # seeded
    assert cfg["backend"] == "aider"  # matches the bundled default template
    assert cfg["model"] == "ollama/qwen3-coder:30b"


def test_load_config_uses_existing_file_when_present(tmp_path, monkeypatch):
    import config as config_module
    existing = tmp_path / "config.yaml"
    existing.write_text("backend: aider\nmodel: ollama/custom\nfallback_models: []\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", existing)

    cfg = config_module.load_config()

    assert cfg["model"] == "ollama/custom"  # existing file wins, not re-seeded


def test_load_config_seeding_never_clobbers_a_concurrently_created_config(
    tmp_path, monkeypatch
):
    # Two servers can start at once and both observe a missing config. The
    # loser of that race must NOT overwrite the winner's file — doing so
    # would silently reset a user's configured model back to defaults.
    # Simulated by creating the file after the exists() check has passed.
    import config as config_module
    fresh = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", fresh)

    real_link = config_module.os.link

    def link_but_a_rival_got_there_first(src, dst, *args, **kwargs):
        # Stand in for another process creating the real config in the
        # window between the exists() check and this link.
        if not os.path.exists(dst):
            with open(dst, "w") as f:
                f.write(
                    "backend: aider\nmodel: ollama/rival-wrote-this\n"
                    "fallback_models: []\n"
                )
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(config_module.os, "link", link_but_a_rival_got_there_first)

    cfg = config_module.load_config()

    assert cfg["model"] == "ollama/rival-wrote-this"  # not reset to the template


def test_load_config_seeding_never_publishes_a_partial_file(tmp_path, monkeypatch):
    # The os.link path is skipped on filesystems without hard links, so the
    # fallback must be atomic too: a concurrent reader must never observe an
    # empty or half-written config.yaml. Force the fallback by making
    # os.link fail, and assert the file is complete the instant it appears.
    import config as config_module
    fresh = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", fresh)
    monkeypatch.setattr(
        config_module.os,
        "link",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no hard links here")),
    )

    observed = []
    real_replace = config_module.os.replace

    def watch_replace(src, dst, *a, **k):
        # Before publish, the destination must not exist at all.
        observed.append(os.path.exists(dst))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(config_module.os, "replace", watch_replace)

    cfg = config_module.load_config()

    assert observed == [False]  # never published before the content was ready
    assert cfg["backend"] == "aider"
    assert fresh.read_text().strip() != ""


def test_load_config_seeding_leaves_no_temp_files_behind(tmp_path, monkeypatch):
    # Seeding writes a temp file then links it into place; the temp file
    # must be cleaned up, not left littering the persistent data dir.
    import config as config_module
    fresh = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", fresh)

    config_module.load_config()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftovers == []
