from backends.base import BackendAdapter, CompletionResult


class CodexBackend(BackendAdapter):
    self_commits = False

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("Codex backend not yet implemented")
