# Local-Coder Delegation — Design Spec

**Status:** Proposed
**Objective:** replace Claude Code's own Edit/Write/Bash-based implementation
step in `subagent-driven-development` with delegation to a local/remote model
(initially Aider + Ollama), while leaving brainstorming, planning, and review
untouched.

## Scope

Two independently shippable pieces, plus a phasing note on backends:

1. `mcp-servers/local-coder/` — a new, source-controlled FastMCP server that
   wraps pluggable coding-agent backends behind three MCP tools:
   `delegate_implementation`, `configure`, and `list_available_models`.
2. A modification to `skills/subagent-driven-development/` so its per-task
   implementer subagent calls local-coder instead of editing files itself,
   with the subagent's tool access structurally restricted at the harness
   level (a new `agents/local-coder-implementer.md` definition — this
   plugin's existing convention, confirmed against other installed plugins
   such as `feature-dev` and `code-simplifier`, is an `agents/` directory
   at the plugin root, auto-discovered by frontmatter; **not**
   `.claude/agents/`, which is a project-local, non-plugin convention and
   would not travel with the plugin when installed elsewhere), not just
   instructed via prompt.

**Backend phasing:** Aider is implemented in this phase (Phase 1) — it's
the only backend this design is initially built and smoke-tested against.
Codex and Gemini are fully designed in this spec (concrete CLI invocation,
auth, change-detection, and failure handling — see "Codex and Gemini
backends" below) but implemented in a later phase; until then, `CodexBackend`
and `GeminiBackend` are stubs raising `NotImplementedError`, same as
`OpenRouterBackend` (which is not designed in this pass at all). Designing
Codex/Gemini now, even though they ship later, is what surfaced the
`self_commits` distinction in the adapter interface below — without it, the
interface built from aider alone would not actually generalize to them.

Out of scope: `brainstorming`, `writing-plans`, `requesting-code-review`,
`executing-plans` (the parallel-session alternative flow), and the review
subagent — none of these change.

## Part 1 — `mcp-servers/local-coder/`

### Layout

```
mcp-servers/local-coder/
  server.py            # FastMCP app; delegate_implementation, configure, list_available_models
  config.py             # load/save config.yaml, partial-merge logic
  ollama.py              # `ollama list` wrapper shared by configure + list_available_models
  config.yaml            # default config (checked in)
  backends/
    base.py              # BackendAdapter ABC, CompletionResult, self_commits flag
    common.py             # shared branch setup + idle/stall monitoring, used by every backend
    aider.py               # real adapter (Phase 1), self_commits=True
    codex.py                # stub in Phase 1 (NotImplementedError); designed, not yet built —
                             # see "Codex and Gemini backends"; self_commits=False when built
    gemini.py                # stub in Phase 1 (NotImplementedError); designed, not yet built —
                              # see "Codex and Gemini backends"; self_commits=False when built
    openrouter.py             # stub, NotImplementedError; not designed this round
  README.md
  requirements.txt      # fastmcp, PyYAML, pytest — installed into a venv, no other packaging
```

### MCP server registration

New file at the repo/plugin root: `.mcp.json`. Confirmed against other
installed plugins that ship an MCP server (e.g. `figma`) — this is the
convention Claude Code plugins actually use for auto-loading a bundled MCP
server; there is no `mcpServers` key inside `.claude-plugin/plugin.json`
itself (an earlier draft of this spec assumed the latter without verifying
it against a real installed plugin; corrected here).

```json
{
  "mcpServers": {
    "local-coder": {
      "type": "stdio",
      "command": "python3",
      "args": ["${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/server.py"]
    }
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` resolves to wherever the plugin is installed, so
this works whether someone clones this fork directly or installs it as a
plugin — matching the original requirement that no manual `claude mcp add`
step is needed. The server is expected to run inside the venv created per
"Prerequisites" in the README (the `python3` on `PATH` at the time Claude
Code launches the server must be one with `requirements.txt` installed —
the README documents activating the venv or using its absolute
interpreter path in `command` if a bare `python3` doesn't resolve
correctly in the user's environment).

### config.yaml

```yaml
backend: "aider"
model: "ollama/qwen3-coder:30b"
fallback_models: []         # tried in order on stall/error before giving up — see "Failover" below
max_fallback_models: 3      # configure rejects a longer fallback_models list — see "Failover" below
stall_timeout_seconds: 300  # no subprocess output for this long = stalled, kill and try next model
target_repo_path: null     # no hardcoded default — see "Repo path resolution" below
branch_prefix: "local-coder/"
open_pr: false              # see "PR ownership" below for why this defaults false
pr_base_branch: "main"
idle_notify_interval_seconds: 20
extra_backend_args: []
```

### Backend adapter interface

```python
@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str]
    commit_sha: str | None
    error: str | None = None

class BackendAdapter(ABC):
    self_commits: bool  # True: backend's own subprocess creates commits (aider).
                         # False: backend only edits files; run_backend must
                         # commit the working-tree diff itself before returning.

    @abstractmethod
    def run_backend(self, task: str, repo_path: str, branch: str, config: dict, model: str | None = None) -> CompletionResult: ...
```

`model` is an internal parameter the server passes on each failover attempt
(see "Model selection and failover" below) — it is not exposed as a
`delegate_implementation` argument; every backend, including the stubs,
shares this signature so the server can drive failover uniformly regardless
of which backend is configured.

**Why `self_commits` exists:** researching Codex CLI and Gemini CLI (see
"Codex and Gemini backends" below) surfaced a real difference from aider,
not just a naming detail. Aider auto-commits each edit itself; Codex CLI and
Gemini CLI only edit files in the working tree and leave committing to the
caller. A single `run_backend` contract that assumed auto-commit (as an
earlier draft of this spec did, using a pure commit-log diff to detect
changes) would silently report `success=False, error="no commits"` for a
Codex/Gemini run that actually did the work — the flag makes this explicit
in the interface instead of hiding it in each backend's implementation.

**Branch setup and idle/stall monitoring are shared, backend-agnostic
steps** (implemented once, in `mcp-servers/local-coder/backends/common.py`,
and called by every backend's `run_backend` rather than duplicated):
1. `git -C repo_path rev-parse --verify branch` to check if the branch
   exists; `git checkout branch` if so, else `git checkout -b branch`.
2. Record `pre_head = git rev-parse HEAD` and a working-tree snapshot
   (`git status --porcelain`, expected clean at this point since each task
   starts from a committed state). `self_commits=False` backends diff
   against this exact snapshot for change detection — see below.
3. Run the backend's subprocess in `repo_path`.
4. While the subprocess runs, a background thread does two independent
   things on every `idle_notify_interval_seconds` tick: (a) calls FastMCP's
   `ctx.report_progress()` (best-effort — harmless no-op if the client isn't
   tracking a progress token) and writes a line to stderr, so there's always
   a visible trace in Claude Code's MCP logs even if progress notifications
   aren't surfaced; (b) checks whether the subprocess has produced any new
   stdout/stderr output since the last tick — if not, increments a
   no-activity counter, and once that counter's elapsed time exceeds
   `stall_timeout_seconds`, kills the subprocess and raises a stall
   condition (see "Failover" below) rather than continuing to wait.
5. Killed for stalling → `success=False`, `error="stalled: no output for
   {stall_timeout_seconds}s"`.
6. Non-zero subprocess exit code → `success=False`, `error` = captured
   stderr tail (or, per-backend, a parsed error from `--json`/`--output-format
   json` output where the backend supports it — see per-backend notes).

**Detecting changes and committing is where `self_commits` branches:**
- `self_commits=True` (aider): after subprocess exit, `post_head = git
  rev-parse HEAD`. If unchanged → `success=False, error="aider made no
  commits"`. Otherwise `files_changed = git diff --name-only pre_head
  post_head`, `commit_sha = post_head`, `success=True`.
- `self_commits=False` (Codex, Gemini): after subprocess exit (exit code
  0), compare the post-run `git status --porcelain` output against the
  pre-run snapshot recorded in shared step 2 — **not** `git diff
  --name-only pre_head`, which only detects changes to already-tracked
  content and silently misses newly created files. Since a coding-agent
  backend creating a new file (a new source file, a new test) is an
  entirely normal outcome, this distinction matters: any path whose
  `git status --porcelain` line is new relative to the pre-run snapshot —
  modified (`" M"`/`"M "`) tracked files or new untracked (`"??"`)
  files — counts as changed. If none → `success=False, error="<backend>
  made no file changes"` (exit 0 with no changes is a documented failure
  mode for both tools — see per-backend notes — so this is not a
  hypothetical edge case). Otherwise stage exactly those detected paths
  (`git add <path> <path> ...`, never `-A`, so the adapter never
  accidentally commits pre-existing unrelated untracked files that were
  already sitting in the working tree before this run) and `git commit -m
  "{task}"`; `files_changed` = the detected paths, `commit_sha` = the new
  commit, `success=True`.

`AiderBackend`: `self_commits=True`. Runs
`aider --model {model} --yes --message "{task}" {*extra_backend_args}`.

### Codex and Gemini backends

Designed now (interface, invocation, auth, and failure handling fully
specified) for implementation in a later phase — see Part 1's phased
scope below. Both share the `self_commits=False` path above.

**`CodexBackend`** (`self_commits=False`):
- Invocation: `codex exec --sandbox workspace-write --skip-git-repo-check
  --model {model} --output-format json {*extra_backend_args} "{task}"`,
  run in `repo_path`. `--sandbox workspace-write` is required — Codex's
  default sandbox is read-only, and there is no separate "auto-approve"
  flag; the sandbox level itself is what allows unattended file edits with
  no interactive prompt.
- Model string: for local models, Codex's own `model_providers` /ollama
  built-in provider config must already be set up in `~/.codex/config.toml`
  on the machine running local-coder (out of scope for local-coder to
  manage) — `config["model"]` for this backend is Codex's model identifier
  under that provider, not the `ollama/...` string aider expects. This is a
  real asymmetry between backends (see "Model string format" below).
- Auth: `OPENAI_API_KEY` env var, inherited from the local-coder server's
  own process environment — local-coder does not manage or store this key,
  the README documents that it must already be set in the environment
  the MCP server runs in.
- Change detection: exit 0 does not guarantee edits were made (a known
  Codex CLI behavior) — always diff the working tree per the shared logic
  above, never trust exit code alone as a "did work happen" signal.
- Local-model support: yes, via Codex's own provider config — this backend
  CAN point at Ollama, unlike Gemini CLI below.

**`GeminiBackend`** (`self_commits=False`):
- Invocation: `gemini -p "{task}" --approval-mode=yolo --model {model}
  --output-format json {*extra_backend_args}`, run in `repo_path`.
  `--approval-mode=yolo` is required for unattended use (auto-approves file
  edits and shell commands; the default mode prompts interactively, which
  would hang a subprocess with no TTY attached).
- Auth: `GEMINI_API_KEY` env var, inherited the same way as Codex's
  `OPENAI_API_KEY` above — not managed by local-coder.
- Change detection: same working-tree diff as Codex, for the same
  reason (exit code alone is not a reliable "made changes" signal, and
  non-interactive mode has documented cases of hanging when a tool call
  needs approval outside what `--approval-mode` granted — the shared
  stall-timeout logic in step 4/5 above is what protects against that
  specific failure mode for this backend particularly).
- Local-model support: **no.** Gemini CLI is Google-Gemini-only as of this
  design's research — there is no built-in provider abstraction for
  Ollama or other local/self-hosted endpoints. If `config["backend"] ==
  "gemini"` and `config["model"]` starts with `"ollama/"`, `configure`
  rejects the combination at write time with an error explaining Gemini
  CLI has no local-model support (same validation layer as the existing
  `ollama list` check, extended to also check backend/model compatibility,
  not just whether an `ollama/` model is pulled).

**Model string format (a cross-backend inconsistency, stated plainly rather
than hidden):** aider's `model` config is a LiteLLM-style string
(`ollama/qwen3-coder:30b`) that encodes both provider and model in one
value. Codex and Gemini each have their own model-naming conventions tied
to their own provider config, unrelated to LiteLLM's format. This means
`fallback_models` entries are only interchangeable within the same
backend — switching `backend` in `configure` effectively requires
reviewing `model`/`fallback_models` for that backend's format too.
`configure` does not attempt to validate Codex/Gemini model strings against
a live list (no equivalent of `ollama list` exists for either), consistent
with the existing "accepted as-is with no local validation" behavior for
non-Ollama model strings.

`OpenRouterBackend`: remains a stub (`NotImplementedError`) — not
researched or designed in this pass, since it wasn't part of what you asked
to have designed end-to-end this round. `self_commits` is not yet
determined for it.

### Model selection and failover

**Model is a pre-run config setting, not a per-call argument.** Once
`subagent-driven-development` is executing a plan, dispatch is autonomous —
the implementer subagent calls `delegate_implementation` on its own, with no
point in the loop where you're present to inject a value into that specific
call. So `delegate_implementation` does **not** take a `model` parameter.
The model in effect for an entire plan run is whatever `config.yaml` held
when execution started. To use a different model, set it with
`configure(model=...)` **before** kicking off `subagent-driven-development`;
to change it mid-plan, you must interrupt/stop the running skill, reconfigure,
and resume — there is no live override path while it's running. (This
differs from `target_repo_path`, which the controller — not the subagent —
resolves once per plan and passes explicitly; `model` has no equivalent
per-plan pass-through today, it's pulled from config at call time.)

The `configure` tool remains the only way to change `model`, `backend`, or
`fallback_models`, at any time (including standalone use of local-coder
outside SDD, where reconfiguring between individual calls is fine since
there's a human in the loop between them). `list_available_models` (below)
is how you discover what's pulled before choosing.

**Failover shortlist:** `config.yaml`'s `fallback_models` is an explicitly
curated, ordered list (empty by default — failover is opt-in, uncapped
length would let one bad run cost `stall_timeout_seconds` per entry before
`delegate_implementation` gives up, so `configure` caps the list at
`max_fallback_models`, default 3 — the cap itself is changeable via
`configure(max_fallback_models=...)` if you want more), maintained via
`configure(fallback_models=[...])`, e.g.:

```yaml
fallback_models:
  - "ollama/qwen2.5-coder:14b"
  - "ollama/deepseek-coder-v2:16b"
```

At default settings (`stall_timeout_seconds: 300`, `max_fallback_models: 3`),
the worst case before `delegate_implementation` gives up entirely is
primary + 3 fallbacks all stalling ≈ 20 minutes; a hard error on any attempt
fails over immediately, without waiting out the stall timeout.

You choose this list yourself (typically after calling
`list_available_models` to see what's actually pulled) — the server never
adds to it on its own, and at runtime it never expands the list dynamically
or substitutes a model you didn't put there. `configure` validates each
`ollama/`-prefixed entry against `ollama list` at write time (see "MCP
tools" below) purely to reject typos/unpulled models up front; it does not
auto-populate the list. So an unattended run can never pick up a model you
haven't explicitly vetted for this purpose (a huge/slow model silently
becoming a "fallback" could otherwise hang for a very long time).

**Failover sequence in `delegate_implementation`:** build the attempt order
as `[config["model"]] + config["fallback_models"]`. Try each in order via
`run_backend`. An attempt fails over to the next
model when `run_backend` returns `success=False` for **either** reason:
a stall (killed after `stall_timeout_seconds` of no output, per step 4/7
above) or a hard backend error (non-zero exit, subprocess launch failure).
Before each retry, the branch/repo state is left exactly as `run_backend`
left it (no commit on a failed/stalled attempt, since aider only commits on
its own success) — so each retry starts from the same `pre_head`, no
special rollback needed. If every model in the attempt order fails,
`delegate_implementation` returns `success=False` with an `error` that lists
each model tried and why it failed (stalled vs. error), so the caller isn't
left guessing which of several models was the problem. The returned `dict`
also includes `model_used` on success, so the caller knows which model in
the list actually completed the task.

### MCP tools

**`delegate_implementation(task: str, branch: str, target_repo_path: str | None = None) -> dict`**

1. Resolve repo path: argument if given, else `config["target_repo_path"]`,
   else raise a clear error (no silent cwd guessing — see "Repo path
   resolution").
2. Load full config, instantiate the configured backend adapter.
3. Build the model attempt order (`config["model"]`, then
   `config["fallback_models"]` in order) and try each via `run_backend`
   until one succeeds or all fail (see "Model selection and failover").
4. On success, check whether the repo has an `origin` remote first
   (`git remote` — or `git remote get-url origin`) before attempting
   anything network-facing. `target_repo_path` is an arbitrary local repo,
   not guaranteed to have any remote configured, so this cannot be assumed:
   - **No `origin` remote:** skip push and PR creation entirely (not a
     failure — the commit already exists locally, on the correct branch,
     which is the actual deliverable for a local-only repo). Return
     success with `pr_url: null` and a `note` field explaining no remote
     was configured so nothing was pushed.
   - **`origin` exists:** `git push -u origin branch`. If the push itself
     fails (e.g. rejected, network error — a different failure mode from
     "no remote configured"), return `{"success": false, "error": "..."}`;
     the commit still exists locally (see "Error handling" below). If the
     push succeeds and `config["open_pr"]` is true AND `gh pr view branch`
     finds no existing open PR, run `gh pr create --fill --head branch
     --base {pr_base_branch}` and capture its URL; otherwise `pr_url` is
     `null`.
5. Return `{"pr_url": ..., "branch": ..., "files_changed": [...], "model_used": ..., "summary": "..."}`
   on success (`pr_url` may be `null` per above). On total failure (every
   model in the attempt order failed, or a configured push failed), return
   `{"success": false, "error": "..."}` (error lists each model tried and
   why, for the model-failure case) with no PR attempted.

**`configure(backend=None, model=None, fallback_models=None, max_fallback_models=None, stall_timeout_seconds=None, target_repo_path=None, branch_prefix=None, open_pr=None, pr_base_branch=None, idle_notify_interval_seconds=None, extra_backend_args=None) -> dict`**

**Correction (discovered during Phase 1 implementation, see the plan's
Task 6 section):** every `config.yaml` key is now an explicit named
parameter — no `**overrides` catch-all. The installed FastMCP (3.4.4)
rejects any `@mcp.tool()`-decorated function with a `**kwargs`-style
parameter at decoration time; since the config-key set is fixed and
known (11 keys), enumerating them explicitly loses nothing and gives MCP
clients a properly typed schema per field instead of an opaque
passthrough. Any other reference in this document implying a catch-all
`**overrides` is superseded by this signature.

Merges only the provided keys into `config.yaml` (untouched keys keep their
current value), writes it back, returns the full resulting config so the
caller can confirm what changed. If `model` or any entry in `fallback_models`
is provided and starts with the `"ollama/"` prefix, `configure` runs
`ollama list` and rejects the call with a clear error naming the missing
model and the models that ARE available, if that model isn't actually
pulled — before writing anything to `config.yaml`. Model strings for other
backends (e.g. `openrouter/...`, or future Codex/Gemini model ids) are
accepted as-is with no local validation, since the server has no way to
verify what's valid for a remote API. If `fallback_models` is provided with
more entries than the current `max_fallback_models` (default 3), `configure`
rejects the call with an error stating the limit and how to raise it
(`configure(max_fallback_models=...)`), rather than silently truncating the
list.

**`list_available_models() -> dict`**

Runs `ollama list`, parses it, and returns
`{"models": ["ollama/qwen3-coder:30b", "ollama/qwen2.5-coder:14b", ...]}`
(each entry already prefixed to match the format `model`/`fallback_models`
expect, so the output can be copy-pasted straight into a `configure` call).
Read-only — makes no changes. If Ollama isn't running or `ollama` isn't on
PATH, returns a clear error instead of an empty list, so "no models" isn't
confused with "Ollama unreachable."

### How configuration actually happens

Both tools above are regular MCP tools — you never edit `config.yaml` by
hand and never run anything outside Claude Code. The intended flow is
entirely in chat:

```
You: what models do I have available for local-coder?
Claude Code: [calls mcp__local-coder__list_available_models]
             "You have 3 pulled: qwen3-coder:30b, qwen2.5-coder:14b, deepseek-coder-v2:16b"
You: use qwen2.5-coder:14b as primary, deepseek as fallback
Claude Code: [calls mcp__local-coder__configure(
                model="ollama/qwen2.5-coder:14b",
                fallback_models=["ollama/deepseek-coder-v2:16b"])]
             "Updated. Current config: model=ollama/qwen2.5-coder:14b, fallback_models=[...]"
```

There is no checkbox UI — MCP tools are function calls, not rendered forms,
so Claude Code cannot show clickable checkboxes for model selection. The
numbered/named list from `list_available_models`, answered in plain
language, is the closest equivalent this environment supports, and it's
what the flow above already provides: Claude Code presents the options,
you pick by name, Claude Code translates the pick into the `configure(...)`
call.

This removes the manual-YAML-editing failure mode entirely: you can't
typo a path or leave the YAML malformed, because you're never touching the
file — Claude Code calls the tool, the tool validates against what's
actually pulled, and the tool reports back the resulting full config so you
can confirm the change landed correctly before starting (or resuming) a
`subagent-driven-development` plan.

### Repo path resolution

`target_repo_path` is **not** auto-detected inside the server — it has no
visibility into which project the calling Claude Code session is working on.
This mirrors how `subagent-driven-development` already works today: the
*controller* session (not the subagent) resolves the working directory via
`superpowers:using-git-worktrees` (`git rev-parse --show-toplevel` /
`--git-common-dir`) and fills it into the dispatch prompt's `Work from:
[directory]` line as plain text. Under this design, the controller passes
that same resolved path as the `target_repo_path` argument on every
`delegate_implementation` call. `config.yaml`'s `target_repo_path: null` stays
the documented fallback for standalone/manual use of the tool outside the
subagent-driven-development flow.

### PR ownership

`subagent-driven-development` reuses **one branch across all tasks in a
plan** (see "Branch granularity" below) and already ends the whole plan with
`superpowers:finishing-a-development-branch`, which itself offers to open the
PR. If `delegate_implementation` also opened a PR after every task, the
first task's call would create it and later calls would try to recreate or
redundantly re-touch it, racing with the final skill's own PR step. To avoid
this:

- `config.yaml` ships with `open_pr: false` by default.
- `delegate_implementation` always pushes the branch on success regardless
  of `open_pr`, provided the repo has an `origin` remote configured (see
  "MCP tools" above) — a local-only `target_repo_path` with no remote is
  not an error, it just means nothing to push.
- `open_pr: true` remains fully supported for standalone/non-SDD use of the
  tool (e.g. someone invoking local-coder directly outside a Superpowers
  plan) — the guard against duplicate PRs (`gh pr view` check) covers that
  case too.
- The SDD integration in Part 2 does not change this default; it relies on
  it.

### Branch granularity

One branch is created for the whole plan (as today, via
`using-git-worktrees` / the existing SDD flow) and reused across every
task's `delegate_implementation` call — the tool checks out the existing
branch rather than creating a new one once it exists. This matches today's
SDD behavior of one feature branch with sequential task commits and a single
final PR, and avoids diverging SDD's completion story
(`finishing-a-development-branch` still governs the single, final PR).

### Idle timeout note

MCP stdio tool calls have a default idle timeout in Claude Code
(`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, default ~30 min). Long aider runs on
larger tasks can exceed this even with progress notifications firing,
because the *notification* doesn't reset a client-side timeout that isn't
listening for it. The README will tell users running long tasks to raise or
disable this env var; the periodic progress/stderr ping is a best-effort
mitigation, not a guarantee.

### README contents

- Prerequisites: `aider` on PATH, the configured backend's runtime running
  (Ollama + the configured model, and every model listed in
  `fallback_models`, pulled — for the default config), `gh` CLI
  authenticated (`gh auth status`).
- How to discover and set models entirely from chat: call
  `list_available_models` to see what's pulled, then `configure(model=...,
  fallback_models=[...])` to select primary/fallbacks — never edit
  `config.yaml` by hand. Includes the example conversation from "How
  configuration actually happens" above.
- A note that `model`/`backend`/`fallback_models` are pre-run settings for
  an in-progress `subagent-driven-development` plan: `configure` must be
  called *before* starting the plan to take effect; changing it mid-plan
  requires stopping execution, reconfiguring, and resuming — there's no live
  per-task override while the plan is running.
- `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` note as above.
- (Added when Codex/Gemini ship in a later phase, not Phase 1) Per-backend
  prerequisites: Codex needs `codex` on PATH and `OPENAI_API_KEY` set in the
  environment the local-coder server runs in (or a prior `codex login`);
  Gemini needs `gemini` on PATH and `GEMINI_API_KEY` similarly set. Neither
  key is stored or managed by local-coder — it only inherits the process
  environment. A note that Gemini CLI cannot target local/Ollama models
  (Codex can, via its own `model_providers` config, separate from
  local-coder's own config).

## Part 2 — Rewiring `subagent-driven-development`

### New file: `agents/local-coder-implementer.md`

A Claude Code subagent definition at the plugin root (this fork currently
has no `agents/` directory — this is new). Verified against other installed
plugins (`feature-dev`, `code-simplifier`, `coderabbit`, `sonarqube`), all
of which ship `agents/<name>.md` at plugin root with this same frontmatter
shape, auto-discovered without any reference from `plugin.json`. This is
**not** `.claude/agents/` — that path is a project-local convention for a
single repo's own Claude Code config, not something a plugin ships:

```yaml
---
name: local-coder-implementer
description: Delegates implementation of a single SDD task to local-coder; verifies the result. Does not edit files directly.
tools: Read, Grep, Glob, Bash, mcp__local-coder__delegate_implementation
---
```

This is a structural restriction enforced by the harness: this subagent
literally does not have Edit/Write in its toolset, so it cannot implement
directly even if it wanted to. `Bash` cannot be scoped to a read-only
command allowlist purely via frontmatter in this harness, so the agent body
text will explicitly instruct read-only/inspection use only (`git log`,
`git diff`, `git status`, `git show`, `ls`, test-running commands to verify,
etc.) — this one piece remains an instruction, not a hard gate, and the
design doc calls that out rather than overclaiming enforcement.

### Modified: `skills/subagent-driven-development/implementer-prompt.md`

- Dispatch changes from `subagent_type: general-purpose` to
  `subagent_type: local-coder-implementer`.
- "Your Job" section rewritten: instead of "1. Implement exactly what the
  task specifies... 2. Write tests...", the subagent's job becomes: call
  `mcp__local-coder__delegate_implementation` with the task brief's
  description and the current working branch (both filled in by the
  controller, same as today's `[directory]`/brief-path placeholders), wait
  for the result, then use Read/Grep/Glob and read-only Bash to verify the
  changed files satisfy the brief (and that any tests the brief calls for
  pass, run via Bash — running tests is read-only even though it's not
  strictly git-inspection, so the agent's Bash instructions permit
  test-execution commands specifically alongside git-inspection ones).
- Report format is unchanged (DONE/DONE_WITH_CONCERNS/BLOCKED/NEEDS_CONTEXT,
  commits, test summary, concerns, report file path) — from the controller's
  perspective the contract is identical; only *how* the work happens changed.
- A fork-divergence header comment added at the top of the file.

### Modified: `skills/subagent-driven-development/SKILL.md`

- Wherever prose or diagram nodes currently describe the implementer as
  editing/testing/committing directly (e.g. "Implementer subagent
  implements, tests, commits, self-reviews"), update to reflect that it
  delegates via local-coder and then verifies.
- Add the fork-divergence header comment.
- No change to the reviewer flow, model-selection guidance, ledger
  mechanism, or file-handoff conventions — those are backend-agnostic.

### Fork-divergence marker

Every modified file gets, at the top:

```
<!--
FORK DIVERGENCE from upstream obra/superpowers:
This file was modified to delegate implementation to the local-coder MCP
server instead of using Edit/Write directly. See
docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md.
Reconcile carefully on upstream merges.
-->
```

(For `.md` files this is an HTML comment, invisible in rendered form but
visible in source — consistent with how the rest of the skill files are
plain markdown with no other front-matter convention for this kind of note.)

## Part 3 — Smoke test (manual, after both parts ship)

Run, end-to-end, in this repo (chosen since it's the fork itself and already
has `README.md` to edit) or the confirmed-ready target repo:

1. Brainstorm a trivial task: "add a one-line comment to README.md".
2. `writing-plans` produces a one-task plan.
3. `subagent-driven-development` dispatches the (now-restricted)
   `local-coder-implementer` subagent for that task.
4. Verify: the subagent calls `delegate_implementation`, not Edit/Write; the
   MCP server invokes aider against the local Ollama model; a commit lands
   on the shared branch; the subagent verifies via Read and reports DONE.
5. Task reviewer reviews the diff as normal (no change to review skill).
6. `finishing-a-development-branch` opens the PR (local-coder itself does
   not, per the PR-ownership decision above).
7. Confirm the PR exists, contains exactly the one-line change, and the full
   chain (brainstorm → plan → delegated execution → review → PR) worked
   without any direct Edit/Write from the controller or implementer.

Environment confirmed ready for this: `gh` CLI authenticated, Ollama running
with `qwen3-coder:30b` pulled, `aider` installed on PATH.

This smoke test exercises the single-model path with `fallback_models`
empty (the default) — it does not exercise failover. Failover is covered by
the unit tests in "Testing approach"; a manual failover drill (e.g.
temporarily configuring an unpulled model as primary to force a fallback) is
optional and left to you to run ad hoc if you want to see it live.

## Testing approach

- `mcp-servers/local-coder/` gets unit tests for: config partial-merge
  (`configure`, including `fallback_models`), `configure`'s `max_fallback_models`
  cap enforcement (a `fallback_models` list at the cap is accepted; one
  entry over is rejected with no write; raising `max_fallback_models` first
  permits a longer list), `configure`'s `ollama list`
  validation (mocked: accepts a pulled `ollama/` model, rejects an unpulled
  one with a clear error, ignores non-`ollama/` prefixes entirely),
  `list_available_models` (mocked `ollama list` output parsed and prefixed
  correctly; clear error when `ollama` isn't reachable), the Aider adapter's
  branch-create-vs-checkout logic and success/failure detection (mocking the
  subprocess call), the stall-detection timer (mocking elapsed time and
  subprocess output activity, not real sleeps), the failover sequence in
  `delegate_implementation` (first model stalls → second model succeeds;
  all models fail → aggregated error naming each), and the
  `delegate_implementation` PR-guard logic (`open_pr` off, `open_pr` on with
  no existing PR, `open_pr` on with an existing PR), and the `origin`-remote
  check (mocked `git remote`: no remote → success with `pr_url: null` and no
  push attempted; remote present → push attempted; push failure with a
  remote present → returned as failure, distinct from the no-remote case)
  — using Python's `unittest`/`pytest` with subprocess, time, and
  `gh`/`git`/`ollama` calls mocked, not live.
- No new tests needed for the skill-file changes themselves (they're prompt
  content); Part 3's manual smoke test is the acceptance check for the
  rewired flow.
- When Codex/Gemini move from stub to implementation in a later phase, their
  test suites must cover the `self_commits=False` path specifically: exit 0
  with no `git status --porcelain` changes relative to the pre-run snapshot
  (no false-positive success), exit 0 with only newly created untracked
  files (must be detected and committed — this is the specific gap a
  `git diff --name-only`-only check would miss), exit 0 with only modified
  tracked files, exit 0 with both, and non-zero exit (treated as a failed
  attempt, same as aider's non-zero exit today). This isn't built in Phase
  1, but the design commits to it so Phase 2 doesn't have to re-derive the
  contract.

## Error handling

- Missing `target_repo_path` (neither arg nor config) → `delegate_implementation`
  returns a clear error, no subprocess run.
- Aider produces no new commit → treated as failure, no push, no PR attempt,
  and (per the failover sequence) the next model in the attempt order is
  tried before the call gives up.
- A model stalls (no subprocess output for `stall_timeout_seconds`) → the
  subprocess is killed, treated as a failed attempt, next model tried.
- `fallback_models` is empty (the default) → no failover occurs; a single
  failed/stalled attempt on the primary model fails the call immediately,
  same as today's single-model behavior.
- No `origin` remote configured on `target_repo_path` → not an error; push
  and PR creation are skipped, the call still returns success with
  `pr_url: null` and a `note` explaining why (the commit is the real
  deliverable for a local-only repo).
- `git push` failure when `origin` DOES exist (e.g. remote rejected,
  network error) → returned as failure; the commit still exists locally, so
  the controller can inspect and retry manually rather than losing work.
  This is a distinct case from "no remote configured" above — the former
  is expected/benign, this one is a real failure worth surfacing.
- Codex/Gemini/OpenRouter selected via `configure` before they're
  implemented → `delegate_implementation` surfaces the adapter's
  `NotImplementedError` message as a clean `error` field, not a stack trace.
- (Once implemented) Codex or Gemini exits 0 but the working-tree diff is
  empty → treated as a failed attempt (`error="<backend> made no file
  changes"`), same failover behavior as aider's "no commits" case.
- `configure` called with `backend: "gemini"` and a `model`/`fallback_models`
  entry starting with `"ollama/"` → rejected before writing `config.yaml`,
  error explains Gemini CLI has no local-model support (Codex has no such
  restriction and is not rejected this way).
- `configure` called with an `ollama/`-prefixed `model` or `fallback_models`
  entry that `ollama list` doesn't show as pulled → rejected before writing
  `config.yaml`, error names the requested model and what IS available.
- `list_available_models` called while Ollama isn't running/reachable →
  clear error, not an empty list (avoids reading "no models pulled" when the
  real problem is "Ollama isn't up").
- `configure` called with `fallback_models` longer than `max_fallback_models`
  → rejected before writing `config.yaml`, error states the limit and that
  `configure(max_fallback_models=...)` raises it.
