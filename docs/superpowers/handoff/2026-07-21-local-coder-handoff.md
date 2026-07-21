# Local-Coder Delegation — Session Handoff

**Purpose of this file:** if this session ends mid-work, read this first —
it should make resumption fast without re-deriving context from the whole
conversation.

## Where things stand

**Phase:** Design is done and approved. Currently starting implementation
planning (`superpowers:writing-plans`) to turn the spec into an executable
plan. **No implementation code has been written yet.**

**Branch:** `local-coder-design`, pushed to `origin`. PR open:
https://github.com/normaltusker/superpowers-local-coder/pull/1
(base: `dev`, which was created fresh from `main` since this fork had no
`dev` branch — see "Branches created this session" below).

**The spec:** `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`
— read this in full before doing anything else. It is the source of truth
for every design decision below. This handoff summarizes it; it does not
replace it.

**Review status:** Gemini Code Assist reviewed PR #1 and found two real
bugs, both fixed and pushed (commit `73a74a1`), replies posted on both
review threads:
1. `self_commits=False` change detection was using `git diff --name-only`
   (misses newly created untracked files) — fixed to diff
   `git status --porcelain` against the pre-run snapshot.
2. `git push -u origin branch` was unconditional — fixed to check for an
   `origin` remote first; no remote = success with `pr_url: null`, not a
   failure.

No further review feedback pending as of this handoff.

## What the design actually says (condensed)

Full detail is in the spec; this is the "read this in 60 seconds" version.

**Two parts:**
1. `mcp-servers/local-coder/` — new FastMCP server, three tools:
   `delegate_implementation`, `configure`, `list_available_models`.
2. Rewire `skills/subagent-driven-development/` so its per-task implementer
   subagent calls `delegate_implementation` instead of using Edit/Write,
   via a new `.claude/agents/local-coder-implementer.md` subagent
   definition that structurally excludes Edit/Write from that subagent's
   toolset (a real harness restriction, not just a prompt instruction).

**Backend phasing — important, easy to get wrong:**
- **Aider is the only backend implemented in Phase 1.** Shells out to
  `aider --model {model} --yes --message "{task}"`, relies on aider's own
  auto-commit (`self_commits=True`).
- **Codex and Gemini are fully designed but NOT implemented yet** — stubs
  raising `NotImplementedError` in Phase 1. Their concrete CLI invocation,
  auth (`OPENAI_API_KEY` / `GEMINI_API_KEY`), and `self_commits=False`
  change-detection contract are already specified in the spec's "Codex and
  Gemini backends" section — a later phase implements them, it should NOT
  need to redesign the interface.
- **OpenRouter is an unresearched stub** — not designed at all yet.
- Neither Codex CLI nor Gemini CLI auto-commits (unlike aider) — this is
  why the `self_commits` flag exists on `BackendAdapter` at all. Gemini CLI
  has no local/Ollama model support (Codex does).

**Key decisions locked in during brainstorming (each was contested/refined
— don't re-litigate without re-reading why in the spec):**
- Model/backend config happens ONLY via chat with Claude Code
  (`list_available_models` → `configure(...)`), never manual YAML editing.
  There is no checkbox UI — MCP tools can't render one.
- `delegate_implementation` has **no `model` parameter** — SDD dispatch is
  autonomous, so there's no point in the loop to inject a per-call
  override. Model is a pre-run `config.yaml` setting only; changing it
  mid-plan requires stopping and resuming.
- `fallback_models`: explicit, human-curated, capped at `max_fallback_models`
  (default 3, itself configurable). Stall detection via
  `stall_timeout_seconds` (default 300s) — no subprocess output for that
  long = killed, next model tried.
- `configure` validates `ollama/`-prefixed models against `ollama list`
  before writing; rejects `backend: gemini` + local-model combos.
- `open_pr` defaults to `false` for the SDD integration — PR creation stays
  solely with `finishing-a-development-branch` at the end of a plan, not
  duplicated per task. `delegate_implementation` always pushes on success
  (if a remote exists).
- One branch for the whole plan, reused across every task's
  `delegate_implementation` call (matches today's SDD behavior).
- `target_repo_path` is never auto-detected — the controller (main Claude
  Code session) resolves it via `using-git-worktrees` and passes it
  explicitly on every call, same as the existing `Work from: [directory]`
  placeholder pattern.

## Branches created this session (state of the repo)

- `main` — untouched, reset back to match `origin/main` after commits were
  accidentally first made directly on it. Clean.
- `dev` — **newly created** this session (didn't exist before), branched
  from `origin/main`, pushed to `origin`. This fork's CLAUDE.md requires
  all PRs to target `dev`, not `main`.
- `local-coder-design` — **the working branch**, all 7 commits of design
  work live here, pushed to `origin`, PR #1 open against `dev`.

If resuming: `git checkout local-coder-design` (or it may already be
checked out — verify with `git branch --show-current`).

## Explored and explicitly ruled out

- **`markelz0r/superpowers-codex`** (a different fork) — investigated per
  user request. It is NOT related to this design: it ports the whole
  Superpowers skill framework to run natively inside the Codex CLI as its
  own harness (Claude Code → Codex CLI harness swap), not "delegate coding
  work from Claude Code out to a Codex backend" (which is what our
  `CodexBackend` design does). Stale (last push 2026-01-26, 395 commits
  behind upstream). User said to ignore it — no action needed, don't
  revisit unless asked again.

## Next step (in progress / not yet done)

Was about to invoke `superpowers:writing-plans` to turn the approved spec
into an executable implementation plan. **This had not been invoked yet as
of this handoff being written** — if resuming and no plan file exists yet
under `docs/superpowers/plans/`, start there:

1. Invoke `superpowers:writing-plans` skill.
2. It will read the spec at
   `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`.
3. Scope the plan to **Phase 1 only** (Aider backend + SDD rewiring) unless
   the user says otherwise when asked — Codex/Gemini are designed but
   explicitly deferred to a later phase per the spec's own scope section.
4. After the plan is written and approved, the natural next skill is
   `superpowers:subagent-driven-development` (same-session execution) or
   `superpowers:executing-plans` (parallel session) — user has not yet
   been asked which they want; ask when the plan is ready, don't assume.

## Environment facts confirmed by the user (still true unless they say
otherwise)

- `gh` CLI authenticated.
- Ollama running locally with `qwen3-coder:30b` pulled.
- `aider` already installed on PATH.
- Target repo for local-coder to edit: comes from the session's cwd/worktree
  at call time (not a fixed path) — see "Repo path resolution" in the spec.
