import subprocess


class OllamaUnavailableError(Exception):
    pass


def list_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as e:
        raise OllamaUnavailableError("ollama list timed out") from e
    except OSError as e:
        # FileNotFoundError (ollama not on PATH) is itself a subclass of
        # OSError, so this also naturally covers that case. Catching the
        # broader OSError additionally covers e.g. a PermissionError if
        # `ollama` exists but isn't executable — without this, such an
        # error would leak as a raw OSError instead of the documented
        # OllamaUnavailableError.
        raise OllamaUnavailableError(f"could not run ollama: {e}") from e

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
