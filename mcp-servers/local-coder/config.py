import contextlib
import os
import tempfile
import time
from pathlib import Path

import yaml

import ollama as ollama_module
from backends.common import KNOWN_BACKENDS
from paths import plugin_data_dir

# Select the OS-appropriate file-locking primitive at import time. `fcntl`
# does not exist on Windows (importing it unconditionally crashes the
# server there); `msvcrt` is the Windows stdlib equivalent. Both are wrapped
# behind _config_lock() below so callers are platform-agnostic.
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt as _lock_module
else:
    import fcntl as _lock_module

# The bundled default config that ships with the plugin — used as a
# read-only template to seed the real config on first use.
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# The live, user-mutable config lives in the persistent plugin-data dir so
# it survives plugin updates (the plugin root is wiped on update). Falls
# back to the in-tree path outside a plugin (tests/dev) via plugin_data_dir.
CONFIG_PATH = plugin_data_dir() / "config.yaml"

# Sentinel accepted only for target_repo_path, to explicitly clear it back
# to null. A bare `None` override means "don't change this field" (see
# merge_config), so there needs to be a distinct way to say "set it to
# null" for the one field a user might legitimately want to unset via chat.
CLEAR_FIELD = "__clear__"


def _lock_path() -> Path:
    # Derived from CONFIG_PATH (rather than a fixed module-level constant)
    # so tests that monkeypatch CONFIG_PATH to an isolated tmp_path also get
    # an isolated lockfile, instead of every test run contending on one
    # lockfile in the real package directory.
    return CONFIG_PATH.parent / f".{CONFIG_PATH.name}.lock"


@contextlib.contextmanager
def _config_lock():
    """Exclusive file lock guarding the load->merge->validate->save
    sequence, so a concurrent configure() call (or a configure() racing a
    delegate_implementation call reading config) can't interleave and lose
    updates or persist a combination that was never validated together.

    Uses a dedicated lockfile (not CONFIG_PATH itself, so save_config's
    atomic replace of CONFIG_PATH is never affected by the lock's own file
    lifecycle). Cross-platform: fcntl.flock on POSIX, msvcrt.locking on
    Windows — no third-party dependency needed.
    """
    lock_path = _lock_path()
    lock_path.touch(exist_ok=True)
    # Open r+ (never "w"): "w" TRUNCATES, so a second process opening the
    # lockfile would clobber it while the first holds a lock on it.
    with open(lock_path, "r+b") as lock_file:
        if _IS_WINDOWS:
            # Lock 1 byte at offset 0. Do NOT write before locking — on
            # Windows a write into a range another process has locked fails,
            # so writing first made contenders error out instead of waiting.
            # Windows locks a byte RANGE and the range need not contain data,
            # so locking offset 0 of an empty file is valid.
            #
            # LK_LOCK does not block indefinitely: it retries ~10 times at
            # 1-second intervals, then raises OSError. That contradicts this
            # function's "blocks until free" contract, so retry around it —
            # matching flock's indefinite wait on POSIX.
            while True:
                try:
                    lock_file.seek(0)
                    _lock_module.locking(lock_file.fileno(), _lock_module.LK_LOCK, 1)
                    break
                except OSError:
                    # Contention only; keep waiting. Any genuinely fatal
                    # condition (bad fd, permissions) recurs and would spin,
                    # so bound the wait with a short sleep to stay cheap.
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                _lock_module.locking(lock_file.fileno(), _lock_module.LK_UNLCK, 1)
        else:
            _lock_module.flock(lock_file, _lock_module.LOCK_EX)
            try:
                yield
            finally:
                _lock_module.flock(lock_file, _lock_module.LOCK_UN)


def load_config() -> dict:
    # First use in a fresh install: the persistent config doesn't exist yet.
    # Seed it from the bundled default template. (Skip when the resolved
    # path IS the template itself — the in-tree/dev fallback — to avoid a
    # pointless self-copy.)
    if not CONFIG_PATH.exists() and CONFIG_PATH != _DEFAULT_CONFIG_PATH:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Seed atomically, like save_config(): write a same-directory temp
        # file and link it into place. Two servers starting at once could
        # otherwise both see the file missing and have a reader observe a
        # partially-written YAML.
        #
        # os.link() rather than os.replace() because this must NOT clobber:
        # if another process (or a configure() call) created a real config
        # between the exists() check and here, replacing it would silently
        # reset the user's settings to defaults. link() fails with
        # FileExistsError instead, and losing that race is success — the
        # config now exists, which is all this block wanted.
        fd, tmp_path = tempfile.mkstemp(
            dir=CONFIG_PATH.parent, prefix=".config.yaml.seed.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_DEFAULT_CONFIG_PATH.read_text())
            try:
                os.link(tmp_path, CONFIG_PATH)
            except FileExistsError:
                pass
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    # Write atomically: to a temp file in the same directory, then
    # os.replace() onto the real path, so a crash mid-write can't leave
    # config.yaml partially written/corrupted (os.replace is atomic on
    # POSIX when source and destination are on the same filesystem, which
    # a same-directory temp file guarantees).
    fd, tmp_path = tempfile.mkstemp(
        dir=CONFIG_PATH.parent, prefix=".config.yaml.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def merge_config(overrides: dict) -> dict:
    current = load_config()
    for key, value in overrides.items():
        if key == "target_repo_path" and value == CLEAR_FIELD:
            current[key] = None
        elif value is not None:
            current[key] = value
    save_config(current)
    return current


class ConfigValidationError(Exception):
    pass


def _validate_ollama_model(model: str, available: list[str]) -> None:
    if not model.startswith("ollama/"):
        return
    bare_name = model[len("ollama/"):]
    if bare_name not in available:
        raise ConfigValidationError(
            f"Model '{model}' is not pulled. Available: "
            f"{', '.join('ollama/' + m for m in available) or '(none)'}"
        )


def configure_with_validation(overrides: dict) -> dict:
    with _config_lock():
        return _configure_with_validation_locked(overrides)


def _configure_with_validation_locked(overrides: dict) -> dict:
    current = load_config()

    # Determine the final values for validation
    backend = overrides.get("backend", current.get("backend"))
    model = overrides.get("model", current.get("model"))
    fallback_models = overrides.get("fallback_models", current.get("fallback_models", []))
    max_fallback = overrides.get("max_fallback_models", current.get("max_fallback_models", 3))
    stall_timeout_seconds = overrides.get(
        "stall_timeout_seconds", current.get("stall_timeout_seconds")
    )
    idle_notify_interval_seconds = overrides.get(
        "idle_notify_interval_seconds", current.get("idle_notify_interval_seconds")
    )

    # A zero or negative stall_timeout_seconds would kill every backend
    # attempt near-instantly; a zero or negative idle_notify_interval_seconds
    # would busy-loop the tick/stall poll and flood progress notifications.
    # Reject both at config-write time rather than letting them silently
    # break every delegate_implementation call.
    if stall_timeout_seconds is not None and stall_timeout_seconds <= 0:
        raise ConfigValidationError(
            f"stall_timeout_seconds must be strictly positive, got {stall_timeout_seconds!r}"
        )
    if idle_notify_interval_seconds is not None and idle_notify_interval_seconds <= 0:
        raise ConfigValidationError(
            "idle_notify_interval_seconds must be strictly positive, got "
            f"{idle_notify_interval_seconds!r}"
        )

    # Reject an unrecognized backend name immediately at config-write time
    # rather than letting it fail later, less helpfully, at
    # delegate_implementation call time.
    if backend is not None and backend not in KNOWN_BACKENDS:
        raise ConfigValidationError(
            f"Unknown backend: {backend!r}. Known backends: "
            f"{', '.join(KNOWN_BACKENDS)}"
        )

    # Reject empty/whitespace-only model strings cleanly here, rather than
    # letting them fail later with a less clear "no model specified" error
    # from a backend's own run_backend.
    if model is not None and not model.strip():
        raise ConfigValidationError("model must not be an empty/whitespace-only string")
    for fb in fallback_models:
        if not fb or not fb.strip():
            raise ConfigValidationError(
                "fallback_models entries must not be empty/whitespace-only strings"
            )

    # Reject duplicate fallback entries, or a fallback that repeats the
    # primary model — both defeat the purpose of failover (retrying the
    # exact same failed/stalled model).
    if len(fallback_models) != len(set(fallback_models)):
        raise ConfigValidationError(
            f"fallback_models contains duplicate entries: {fallback_models}"
        )
    if model and model in fallback_models:
        raise ConfigValidationError(
            f"fallback_models must not duplicate the primary model ({model!r})"
        )

    # Validate the merged/effective values, not just what's being overridden.
    # This ensures a config's already-persisted model gets re-checked even
    # when a later configure() call doesn't touch the `model` field itself.
    # Fetch the ollama model list at most once per call — only if something
    # actually needs it — rather than once per ollama/-prefixed model.
    needs_ollama_check = (model and model.startswith("ollama/")) or any(
        fb.startswith("ollama/") for fb in fallback_models
    )
    available = ollama_module.list_ollama_models() if needs_ollama_check else []

    if model:
        _validate_ollama_model(model, available)
    for fb in fallback_models:
        _validate_ollama_model(fb, available)

    # Check fallback list against cap (using the merged values)
    if len(fallback_models) > max_fallback:
        raise ConfigValidationError(
            f"fallback_models has {len(fallback_models)} entries, exceeding "
            f"max_fallback_models={max_fallback}. Raise the cap first with "
            f"configure(max_fallback_models=...) if you want more."
        )

    # Check for gemini + ollama incompatibility (using the final merged values)
    if backend == "gemini":
        gemini_models = [m for m in [model, *fallback_models] if m and m.startswith("ollama/")]
        if gemini_models:
            raise ConfigValidationError(
                "Gemini CLI has no local-model support — "
                f"cannot use backend='gemini' with {gemini_models}"
            )

    return merge_config(overrides)


def list_available_models_with_prefix() -> list[str]:
    return [f"ollama/{m}" for m in ollama_module.list_ollama_models()]
