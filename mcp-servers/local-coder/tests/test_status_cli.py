import json
import pytest

import config
import ollama as ollama_module
import status
import status_cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    data = tmp_path / "plugindata"; data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    r = tmp_path / "repo"; r.mkdir()
    return str(r)


# ---- status rendering ------------------------------------------------------

def test_status_table_renders_records(repo):
    rec = status.create_record(repo, "dev", "ollama/qwen2.5-coder:7b",
                               "/tmp/x.log", session_id="sess1")
    out = status_cli.render_status(repo, all_sessions=True, as_json=False)
    assert "phase" in out.lower()          # header present
    assert rec["id"] in out                # the record shows up
    assert "ollama/qwen2.5-coder:7b" in out


def test_status_json_returns_list(repo):
    status.create_record(repo, "dev", "m", "/tmp/x.log")
    out = status_cli.render_status(repo, all_sessions=True, as_json=True)
    parsed = json.loads(out)
    assert isinstance(parsed, list) and len(parsed) == 1


def test_status_empty_repo_is_friendly(repo):
    out = status_cli.render_status(repo, all_sessions=True)
    assert "No local-coder delegations" in out


# ---- setup: ollama check ---------------------------------------------------

def test_setup_ollama_uses_local_cli_only(monkeypatch):
    # The ollama check must go through the local `ollama` CLI wrapper, never a
    # network host. We assert it calls list_ollama_models and nothing else.
    called = {"n": 0}
    def fake_list():
        called["n"] += 1
        return ["qwen2.5-coder:7b"]
    monkeypatch.setattr(ollama_module, "list_ollama_models", fake_list)
    report = status_cli.check_ollama("ollama/qwen2.5-coder:7b")
    assert report["ok"] is True
    assert called["n"] == 1


def test_setup_ollama_flags_missing_model(monkeypatch):
    monkeypatch.setattr(ollama_module, "list_ollama_models", lambda: ["other-model"])
    report = status_cli.check_ollama("ollama/qwen2.5-coder:7b")
    assert report["ok"] is False
    assert "ollama pull qwen2.5-coder:7b" in report["nextStep"]


def test_setup_ollama_daemon_down(monkeypatch):
    def boom():
        raise ollama_module.OllamaUnavailableError("could not run ollama")
    monkeypatch.setattr(ollama_module, "list_ollama_models", boom)
    report = status_cli.check_ollama("ollama/qwen2.5-coder:7b")
    assert report["ok"] is False
    assert "start ollama" in report["nextStep"]


# ---- setup: config check ---------------------------------------------------

def test_setup_config_flags_empty_model(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"model": "", "stall_timeout_seconds": 300})
    report = status_cli.check_config()
    assert report["ok"] is False and "model" in report["nextStep"].lower()


def test_setup_config_flags_nonpositive_stall(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"model": "ollama/m", "stall_timeout_seconds": 0})
    report = status_cli.check_config()
    assert report["ok"] is False and "stall_timeout_seconds" in report["nextStep"]


def test_setup_config_passes_for_sane_config(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"model": "ollama/m", "stall_timeout_seconds": 300})
    report = status_cli.check_config()
    assert report["ok"] is True


# ---- setup: venv check -----------------------------------------------------

def test_setup_venv_missing(repo):
    # repo fixture points CLAUDE_PLUGIN_DATA at an empty dir -> no venv.
    report = status_cli.check_venv()
    assert report["ok"] is False
    assert "auto-provision" in report["nextStep"]


def _provision_fake_venv(monkeypatch):
    """Lay out a venv exactly as hooks/ensure-local-coder-venv does:
    - interpreter at   plugin_data_dir()/.venv/bin/python
    - success stamp at plugin_data_dir()/requirements.installed.txt  (DATA_DIR),
      NOT inside .venv. check_venv must look where the hook actually writes it.
    """
    from paths import plugin_data_dir
    data = plugin_data_dir()
    (data / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    py = data / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    (data / "requirements.installed.txt").write_text("aider-chat==0.0.0\n")
    return data


def test_setup_venv_passes_when_provisioned_at_hook_paths(repo, monkeypatch):
    # Regression: check_venv must read the stamp from the SAME path the
    # provisioning hook writes it — DATA_DIR/requirements.installed.txt, not
    # DATA_DIR/.venv/requirements.installed.txt. The old code looked inside
    # .venv/ and false-failed a fully-provisioned venv, so /setup reported
    # NOT READY on a working install.
    _provision_fake_venv(monkeypatch)
    report = status_cli.check_venv()
    assert report["ok"] is True, report
    assert report["nextStep"] is None


def test_setup_venv_fails_when_stamp_missing(repo, monkeypatch):
    # Interpreter present but no success stamp -> incomplete provisioning.
    from paths import plugin_data_dir
    data = plugin_data_dir()
    (data / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    py = data / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n"); py.chmod(0o755)
    report = status_cli.check_venv()
    assert report["ok"] is False
    assert "stamp" in report["detail"]


# ---- run_setup aggregate ---------------------------------------------------

def test_run_setup_reports_not_ready_when_a_check_fails(repo, monkeypatch):
    monkeypatch.setattr(ollama_module, "list_ollama_models", lambda: [])
    monkeypatch.setattr(config, "load_config",
                        lambda: {"model": "ollama/m", "stall_timeout_seconds": 300})
    out, ok = status_cli.run_setup(as_json=False)
    assert ok is False
    assert "NOT READY" in out
