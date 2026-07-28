from typing import Callable

from backends.base import BackendAdapter, CompletionResult


class GeminiBackend(BackendAdapter):
    self_commits = False

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
        on_tick: Callable[[], None] | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("Gemini backend not yet implemented")
