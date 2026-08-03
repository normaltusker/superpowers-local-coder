from typing import Callable

from backends.base import BackendAdapter, CompletionResult


class OpenRouterBackend(BackendAdapter):
    self_commits = False  # not yet determined — OpenRouter backend is unresearched

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
        on_tick: Callable[[], None] | None = None,
        on_output: Callable[[str], None] | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("OpenRouter backend not yet implemented")
