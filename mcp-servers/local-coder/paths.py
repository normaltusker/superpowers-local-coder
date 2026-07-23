import os
from pathlib import Path


def plugin_data_dir() -> Path:
    """Directory for the server's persistent runtime state (config.yaml,
    per-call log files, the provisioned venv).

    In an installed Claude Code plugin, ${CLAUDE_PLUGIN_DATA} is set and
    points at a directory that survives plugin updates — the documented
    home for Python virtualenvs, config, and caches. Outside a plugin
    (pytest, standalone/dev use), the env var is unset, so fall back to the
    in-tree directory next to this file, preserving existing behavior.
    """
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        d = Path(data) / "local-coder"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).parent
