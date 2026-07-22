from pathlib import Path

import yaml

import ollama as ollama_module
from backends.common import KNOWN_BACKENDS

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Sentinel accepted only for target_repo_path, to explicitly clear it back
# to null. A bare `None` override means "don't change this field" (see
# merge_config), so there needs to be a distinct way to say "set it to
# null" for the one field a user might legitimately want to unset via chat.
CLEAR_FIELD = "__clear__"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    # Write atomically: to a temp file in the same directory, then
    # os.replace() onto the real path, so a crash mid-write can't leave
    # config.yaml partially written/corrupted (os.replace is atomic on
    # POSIX when source and destination are on the same filesystem, which
    # a same-directory temp file guarantees).
    import os
    import tempfile

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
    current = load_config()

    # Determine the final values for validation
    backend = overrides.get("backend", current.get("backend"))
    model = overrides.get("model", current.get("model"))
    fallback_models = overrides.get("fallback_models", current.get("fallback_models", []))
    max_fallback = overrides.get("max_fallback_models", current.get("max_fallback_models", 3))

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
