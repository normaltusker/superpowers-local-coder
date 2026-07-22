from pathlib import Path

import yaml

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
