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
    ) -> CompletionResult:
        raise NotImplementedError("OpenRouter backend not yet implemented")
