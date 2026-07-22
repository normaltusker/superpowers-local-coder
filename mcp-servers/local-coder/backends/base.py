from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    error: str | None = None


class BackendAdapter(ABC):
    self_commits: bool

    @abstractmethod
    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        ...
