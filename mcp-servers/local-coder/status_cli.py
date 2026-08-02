"""CLI for local-coder observability: `status` (poll delegations) and
`setup` (readiness checks). Invoked by the /local-coder:status and
/local-coder:setup plugin commands; also runnable directly for debugging.

All output goes to stdout. Exit code is 0 for `status`, and for `setup` it
is 0 when every check passes and 1 when any check fails (so the command can
be scripted).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import config
import status
from paths import plugin_data_dir


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _target_repo(cwd: str | None = None) -> str:
    """Resolve the repo whose delegations we report on: the git top-level of
    cwd, falling back to cwd itself when git can't answer."""
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


def _is_positive_finite_number(value) -> bool:
    """True only for a real, strictly-positive, finite number. Rejects bool
    (a subclass of int — True would otherwise read as 1) and inf/nan."""
    import math
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value > 0


def _venv_python() -> Path | None:
    """Return the provisioned venv interpreter path if it exists, else None.
    Mirrors hooks/ensure-local-coder-venv: bin/python (POSIX) or
    Scripts/python.exe (Windows)."""
    venv = plugin_data_dir() / ".venv"
    for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------

def _elapsed(record: dict) -> str:
    start = record.get("createdAt") or ""
    end = record.get("completedAt") or record.get("updatedAt") or ""
    # Timestamps are ISO-8601 UTC strings; a lexical value is enough to show,
    # but for a human-friendly duration we parse them. Keep it defensive.
    try:
        import datetime as _dt
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        s = _dt.datetime.strptime(start, fmt).replace(tzinfo=_dt.timezone.utc)
        e = (_dt.datetime.strptime(end, fmt).replace(tzinfo=_dt.timezone.utc)
             if end else _dt.datetime.now(_dt.timezone.utc))
        secs = int((e - s).total_seconds())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return ""


def render_status(target_repo, job_id=None, all_sessions=False,
                  session_id=None, as_json=False) -> str:
    records = status.list_records(target_repo, all_sessions=all_sessions,
                                  session_id=session_id)

    if job_id:
        match = next((r for r in records if r.get("id") == job_id), None)
        if as_json:
            return json.dumps(match, indent=2, sort_keys=True)
        if match is None:
            return f"No local-coder job found with id {job_id!r}."
        return json.dumps(match, indent=2, sort_keys=True)

    if as_json:
        return json.dumps(records, indent=2, sort_keys=True)

    if not records:
        return "No local-coder delegations recorded for this repository."

    header = "| id | status | phase | latest activity | elapsed | model | output log |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    rows = [header, sep]
    for r in records:
        activity = (r.get("latestActivity") or "").replace("|", "\\|")
        if len(activity) > 60:
            activity = activity[:57] + "..."
        rows.append(
            f"| {r.get('id','')} | {r.get('status','')} | {r.get('phase','')} "
            f"| {activity} | {_elapsed(r)} | {r.get('model','')} "
            f"| {r.get('outputLog','')} |"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# setup subcommand — four readiness checks
# ---------------------------------------------------------------------------

def check_venv() -> dict:
    """venv provisioned: interpreter present AND the requirements stamp exists."""
    python = _venv_python()
    stamp = plugin_data_dir() / ".venv" / "requirements.installed.txt"
    if python is None:
        return {"name": "venv", "ok": False,
                "detail": "no provisioned venv interpreter found",
                "nextStep": "run a delegation once — the venv auto-provisions "
                            "via hooks/ensure-local-coder-venv"}
    if not stamp.exists():
        return {"name": "venv", "ok": False,
                "detail": f"venv python present but stamp {stamp.name} missing",
                "nextStep": "re-run provisioning; the venv looks incomplete"}
    return {"name": "venv", "ok": True, "detail": str(python), "nextStep": None}


def check_aider() -> dict:
    """aider importable in the venv (catches the scipy/dyld macOS-27 crash)."""
    python = _venv_python()
    if python is None:
        return {"name": "aider", "ok": False,
                "detail": "no venv to import aider from",
                "nextStep": "provision the venv first (see the venv check)"}
    try:
        proc = subprocess.run(
            [str(python), "-c", "import aider"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"name": "aider", "ok": False, "detail": f"could not run venv python: {e}",
                "nextStep": "check the venv interpreter is executable"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        last = tail[-1] if tail else "import failed"
        return {"name": "aider", "ok": False, "detail": last,
                "nextStep": "aider failed to import — on macOS 27 this is often "
                            "the scipy/dyld crash; reinstall aider in the venv "
                            "or pin a working scipy"}
    return {"name": "aider", "ok": True, "detail": "import aider succeeded",
            "nextStep": None}


def check_ollama(model: str) -> dict:
    """ollama reachable AND the configured model pulled. Uses the local
    `ollama` CLI (which talks only to the local daemon) via the existing
    ollama module — no network host is contacted directly and no credentials
    are involved."""
    import ollama as ollama_module
    try:
        available = ollama_module.list_ollama_models()
    except ollama_module.OllamaUnavailableError as e:
        return {"name": "ollama", "ok": False, "detail": str(e),
                "nextStep": "start ollama (the daemon isn't reachable)"}
    if model and model.startswith("ollama/"):
        bare = model[len("ollama/"):]
        if bare not in available:
            return {"name": "ollama", "ok": False,
                    "detail": f"model {model!r} not pulled; available: "
                              f"{', '.join(available) or '(none)'}",
                    "nextStep": f"ollama pull {bare}"}
    return {"name": "ollama", "ok": True,
            "detail": f"daemon up; {len(available)} model(s) available",
            "nextStep": None}


def check_config() -> dict:
    """config.yaml parses and has a sane model + positive stall timeout."""
    try:
        cfg = config.load_config()
    except Exception as e:
        return {"name": "config", "ok": False, "detail": f"config failed to load: {e}",
                "nextStep": "fix config.yaml so it parses"}
    # A parseable-but-non-mapping config (e.g. a YAML list or scalar) would
    # crash .get with a traceback; report it as a failed check instead.
    if not isinstance(cfg, dict):
        return {"name": "config", "ok": False,
                "detail": f"config is not a mapping (got {type(cfg).__name__})",
                "nextStep": "config.yaml must be a mapping of settings"}
    model = cfg.get("model")
    if not model or not str(model).strip():
        return {"name": "config", "ok": False, "detail": "model is empty",
                "nextStep": "set a non-empty `model` in config.yaml"}
    stall = cfg.get("stall_timeout_seconds")
    if stall is not None and not _is_positive_finite_number(stall):
        return {"name": "config", "ok": False,
                "detail": f"stall_timeout_seconds is {stall!r}",
                "nextStep": "set `stall_timeout_seconds` to a positive, finite number"}
    return {"name": "config", "ok": True, "detail": f"model={model}", "nextStep": None}


def run_setup(as_json=False) -> tuple[str, bool]:
    cfg_model = None
    try:
        cfg_model = config.load_config().get("model")
    except Exception:
        pass

    reports = [
        check_venv(),
        check_aider(),
        check_ollama(cfg_model or ""),
        check_config(),
    ]
    all_ok = all(r["ok"] for r in reports)

    if as_json:
        return json.dumps({"ready": all_ok, "checks": reports}, indent=2), all_ok

    lines = []
    for r in reports:
        mark = "PASS" if r["ok"] else "FAIL"
        lines.append(f"[{mark}] {r['name']}: {r['detail']}")
        if not r["ok"] and r.get("nextStep"):
            lines.append(f"       -> {r['nextStep']}")
    lines.append("")
    lines.append("READY" if all_ok else "NOT READY — resolve the failing checks above")
    return "\n".join(lines), all_ok


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="status_cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show recorded delegations")
    p_status.add_argument("job_id", nargs="?", default=None)
    p_status.add_argument("--all", action="store_true", dest="all_sessions")
    p_status.add_argument("--json", action="store_true", dest="as_json")

    p_setup = sub.add_parser("setup", help="check local-coder readiness")
    p_setup.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)

    if args.command == "status":
        session_id = os.environ.get("CLAUDE_SESSION_ID")
        out = render_status(
            _target_repo(), job_id=args.job_id,
            all_sessions=args.all_sessions, session_id=session_id,
            as_json=args.as_json,
        )
        print(out)
        return 0

    # setup
    out, ok = run_setup(as_json=args.as_json)
    print(out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
