import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def git_repo(tmp_path):
    """A minimal local git repo with one commit and no remote configured.

    Shared across test_backends_common.py, test_aider.py, and (aliased as
    git_repo_no_remote below) test_server.py, which previously each
    duplicated this same fixture.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo_no_remote(git_repo):
    """Alias for `git_repo`, kept as a distinctly-named fixture for
    test_server.py call sites where "no remote" is the meaningful, explicit
    contrast against git_repo_with_remote below."""
    return git_repo


@pytest.fixture
def git_repo_with_remote(git_repo):
    """Same base repo as `git_repo`, plus a bare remote named "origin"
    with the initial commit already pushed."""
    remote = git_repo.parent / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"], cwd=git_repo, check=True, capture_output=True,
    )
    return git_repo
