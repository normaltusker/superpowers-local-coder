import subprocess


class OllamaUnavailableError(Exception):
    pass


def list_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True
        )
    except FileNotFoundError as e:
        raise OllamaUnavailableError("ollama is not on PATH") from e

    if result.returncode != 0:
        raise OllamaUnavailableError(
            f"ollama list failed: {result.stderr.strip()}"
        )

    lines = result.stdout.strip().splitlines()
    if len(lines) <= 1:
        return []

    models = []
    for line in lines[1:]:  # skip header row
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models
