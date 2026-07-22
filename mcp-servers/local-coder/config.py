from pathlib import Path

import yaml

import ollama as ollama_module

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


def merge_config(overrides: dict) -> dict:
    current = load_config()
    for key, value in overrides.items():
        if value is not None:
            current[key] = value
    save_config(current)
    return current


class ConfigValidationError(Exception):
    pass


def _validate_ollama_model(model: str) -> None:
    if not model.startswith("ollama/"):
        return
    bare_name = model[len("ollama/"):]
    available = ollama_module.list_ollama_models()
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

    # Only validate models that are being overridden
    if "model" in overrides and overrides["model"]:
        _validate_ollama_model(overrides["model"])

    if "fallback_models" in overrides:
        for fb in overrides["fallback_models"]:
            _validate_ollama_model(fb)

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
