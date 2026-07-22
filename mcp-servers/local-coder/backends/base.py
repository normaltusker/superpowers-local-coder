from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    error: str | None = None


class BackendAdapter(ABC):
    self_commits: bool

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # `self_commits: bool` above is a bare class-level annotation, not
        # an actual class attribute — a subclass could otherwise be defined
        # (and instantiated) without ever setting it, and nothing would
        # catch the omission until something tried to read self_commits and
        # hit an AttributeError at some arbitrary later point. Enforce it
        # at class-definition time instead, so an incomplete adapter fails
        # loudly and immediately.
        if "self_commits" not in vars(cls):
            raise TypeError(
                f"{cls.__name__} must define a class-level 'self_commits: "
                "bool' attribute (BackendAdapter subclasses must state "
                "whether the backend commits its own changes)."
            )

    @abstractmethod
    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> CompletionResult:
        ...
