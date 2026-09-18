# claude-pr-review

![claude-pr-review — Five review axes. One verified report.](assets/claude-pr-review-hero.png)

Multi-axis PR review orchestration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — one `/pr-review <PR-URL>` command that runs up to five independent review perspectives against the same pull request, cross-verifies every finding between axes, and compiles a single comparison report with copy-paste-ready inline comments.

Built and battle-tested over months of daily production PR review. Extracted from the author's personal setup; see [Prerequisites](#prerequisites) honestly before expecting it to run as-is.

**Why this design** — the methodology behind the command, told through real catches and misses (a dead Save button four reviewers missed, red-team refutation rates, why consensus still gets verified): [一個模型不夠：五軸交叉審的 code review 工作流](https://gggodlin.github.io/blog/one-model-not-enough/) (zh-TW). The article states the three design philosophies; this repo is the implementation, and has kept evolving since it was written (formal-spec gate, provenance discipline, Codex presets came later) — where they differ, the command file is current.

## Why multi-axis

A single reviewer — human or model — has a single blind spot profile. This command deliberately combines perspectives with *different* blind spots:

| Axis | Runtime | Perspective |
|---|---|---|
| Context-aware reviewers | Claude Code subagents (`agents/`) | Language/domain specialists with full repo search access — catch cross-file gaps |
| Codex neutral | `codex review` (bare CLI) | Diff-only, no context — reads the PR the way a reviewer reads a PR email |
| Codex adversarial | Codex plugin red-team template | Actively attacks the change — catches fail-open, visibility, day-boundary hazards |
| Gemini Flash | `agy` CLI (recommended by default) | Cheap independent pass — has repeatedly caught the only confirmed finding in a round |
| Gemini Pro | `agy` CLI (optional) | Deeper but hallucination-prone — off by default |

The design principle borrowed from security auditing: **the agent that finds an issue never verifies it**. Context-aware findings are verified by the diff-only axis and vice versa (symmetric cross-verification), consensus findings still get a convention-baseline check, and no finding is ever dropped — refuted ones ship in the report with both sides' evidence so the human makes the final call.

## What you get

- **A per-run selection matrix** — Step 2.98 lays the five axes (plus an experimental web GPT Pro path) out as a model×angle matrix, marks each cell `recommended` or `optional`, and waits for your choice. A recommendation is never consent: an unanswered prompt selects nothing, and an axis that fails is reported `FAILED` rather than quietly swapped for another model
- **Multi-PR review sets** — pass more than one PR and the command prepares each target in its own worktree, picks seats once for the whole set, and emits a group report; built for front-end/back-end pairs whose contract defects are invisible when each side is reviewed alone
- **Second-round continuity** — findings carry stable UIDs, so a later review reconciles the previous round into `FIXED` / `STILL_OPEN` / `STALE` instead of starting over. Prior findings are withheld from fresh reviewers so the second pass is not anchored by the first
- **Coverage as set arithmetic, not trust** — with a primary reviewer selected, every changed file must be explicitly accounted for (`finding` / `REVIEWED_NO_ISSUES` / `INTENTIONALLY_SKIPPED`), asserted deterministically after review; cancel that seat and the run records `N-A (primary not selected)` rather than pretending the files were covered
- **Deterministic re-anchoring** — findings carry verbatim source anchors and are re-located by exact match before the report, so line numbers survive model drift
- **Provenance discipline** — on hotfix→staging PRs, files inherited from the default branch are detected and capped so their defects don't land on an innocent author
- **Severity calibration gates** — Must Fix requires a concrete user-visible repro path *and* a shippable-thing broken; severity built on unverified premises gets recomputed without them
- **A formal-spec compliance lane (experimental)** — normative spec clauses (MUST/SHALL, invariants, formulas) are extracted, canonicalized by a deterministic reducer (`scripts/pr-review-c4.py`), dispatched through a hash-bound single-use envelope (optionally enforced by a PreToolUse permit-gate hook), and traced against authored hunks with full hash binding
- **Two-layer report publication** — the full-evidence audit report (`pr-<id>-review.audit.md`) is canonical; the decision-facing main report (`pr-<id>-review.md`) is produced from it by a deterministic projection helper (`scripts/pr-review-report-projection.py`), never rewritten by a model, with a shared generation hash binding the pair
- **Report Self-Verify gate** — before publication a read-only auditor subagent checks the draft against a fixed R1–R10 rubric (input binding, axis states, per-file coverage, severity calibration, silent-skip disclosure); violations must be repaired with evidence before the report ships
- **Traditional Chinese comparison report** with colloquial, paste-ready inline comment blocks (the command's working language is bilingual zh-TW/English; reports render in zh-TW — fork and adjust if you want another language)

## Repo layout

```
commands/pr-review.md            the orchestrator command (install → ~/.claude/commands/)
commands/tests/                  contract tests pinning the command's report/dispatch wiring (run in-repo)
agents/*.md                      five reviewer subagents + one rubric auditor (install → ~/.claude/agents/)
scripts/pr-review-c4.py          deterministic spec-clause reducer + dispatch envelope/permit issuer
scripts/pr-review-profile.py     resolves the optional per-repo profile (trunk / spec globs / conventions docs) from the git remote
scripts/pr-review-report-projection.py   deterministic audit→main report projection (Step 6 publication)
scripts/poll-liveness.sh         background-process poll helper (3-signal: done/dead/stuck)
scripts/pr-review-targets.py     multi-target preparation: identity/version checks, per-target materials, capacity adjudication
scripts/pr-review-cc-group-flow.py       one CC dispatch ledger across a multi-target set
scripts/pr-review-group-report.py        group report assembly + group-wide finding UIDs
scripts/sem-pr-blast-radius.sh   entity-level dependency blast radius (needs `sem`)
model-routing.env                logical model ids resolved at dispatch time (install → ~/.claude/)
hooks/pr-review-c4-dispatch-gate.py     optional PreToolUse permit gate for the formal-spec dispatch
references/severity-calibration.md      security impact×likelihood matrix
references/finding-severity-rules.md    6c/6d gates: Must/Should/Nice calibration (platform-neutral SSOT)
skills/bitbucket-pr-review/      optional Bitbucket adapter, read path (GitHub needs none of this — `gh` covers it)
skills/bitbucket-pr-mutation/    optional Bitbucket adapter, write path (proposal/approval-gated, contract-tested)
```

Install: see [INSTALL.md](INSTALL.md) — an agent-executable guide (point Claude Code at it and say "set me up for my configuration"). Everything lands under `~/.claude/`; the command references its helpers at `~/.claude/scripts/...` and `~/.claude/skills/...` at runtime.

**Optional per-repo profile.** Teams whose development trunk is not the GitHub default branch (`develop`, `stage`, …), or who keep specs somewhere the built-in detection does not look, can describe that once in `~/.claude/pr-review/repos.yaml`, keyed by `owner/repo` as derived from the git remote. Three fields per repo: `trunk` (branch name used for provenance and formal-spec anchoring; without it the entry is ignored), `spec_globs` (extra paths treated as spec/plan context, added on top of the built-in heuristics), and `conventions_docs` (repo-relative files the CC reviewers read before reviewing; missing paths are reported, not fatal). No file, no entry, or no `trunk` means zero configuration: every field falls back to its default, and the report header states which source decided the trunk. Author calibration files and the friction log live beside it under `~/.claude/pr-review/`, outside every reviewed repo.

**Minimum install (GitHub-only)** = `commands/` + `agents/` + `references/` + `scripts/` — the command dispatches reviewers by the agent names defined in `agents/`, reads both reference files during severity calibration, and probes the bundled scripts at fixed steps (they self-skip when their underlying tool is absent, but the files must exist for the skip to be graceful). `skills/bitbucket-*` only if you review Bitbucket PRs.

## Upgrading from an earlier install

Three things move if you already had this installed:

- **Reports land elsewhere.** They now go to `~/.claude/pr-review-reports/<host__owner__repo>/` instead of the repository root, so no report is written into the repo under review. The temporary review worktree is still created under `<checkout>/.worktrees/` and removed at the end, so this is about reports, not about the repo being left untouched. A report written by the previous version stays where it was and is not picked up as a prior round; move it into the new directory if you want second-round continuity.
- **Seats are chosen per run.** Step 2.98 prints the matrix and waits for your pick instead of firing a fixed axis set. Answering "照推薦" takes the recommended cells; leaving it unanswered in an interactive run selects nothing. An unattended run (`/goal` and friends, nobody there to answer) falls back to the recommended set, so seat selection keeps its previous behaviour — but that covers seat selection only, not the whole run: a very large spec now stops for an operator answer (below), and a multi-PR run needs an explicit confirmation before dispatch.
- **The standalone Gemini Pro question is gone.** Both Gemini seats are cells in the matrix now; Flash stays recommended by default and Pro stays optional.
- **Codex presets were renamed and repointed.** `light` and `sol-lite` no longer exist; the set is now `default` / `astra-lite` / `deep` / `ultra`, and all four run `gpt-6-astra` rather than the previous `gpt-5.6-*` models. Picking `default` still works and still means "the normal pass", but it is a different model than before. A saved invocation that names `light` or `sol-lite` has no matching preset — pick `astra-lite` for the cheaper pass.
- **Large specs stop instead of being summarised.** The old flow summarised a spec over roughly 8k tokens before handing it to reviewers. It no longer does, because a summary can silently drop the clause that decides whether a finding is in scope. Past roughly 2,000 lines the run now stops and asks you to choose the full artifact or a named section.
- **Per-file coverage is now conditional.** The `|F| = covered + no-issues + skipped + missed` guarantee applies to the selected primary reviewer. Cancel that seat and the run is still valid, but coverage is recorded as `N-A (primary not selected)` — the other seats do not take over per-file accounting.
- **Cancelling a Codex seat does not mean Codex stays out of the run.** The matrix governs first-pass seats. The cross-verification roles in Step 4.1 and 4.2 are not selectable and still run against whatever findings the selected seats produce.
- **Single-PR reports moved from report schema 1 to 2.** Schema 1 reports are still readable; the new schema adds the prior-round reconciliation header and section.

Reinstall by re-running the copy steps in [INSTALL.md](INSTALL.md) — `cp scripts/*` picks up the new helpers, and `model-routing.env` is a new file the Gemini seats read.

## Prerequisites

Tiered honestly — the command degrades gracefully when an axis is missing (it reports the gap instead of failing the review):

**Required**
- Claude Code with subagent support; `gh` CLI for GitHub PRs
- A local clone of the repo under review (the command builds a temporary `git worktree` pinned to the PR head — the single most important mechanism here; stale local state silently invalidates an entire review)

**Per-axis (optional, skip = axis skipped)**
- Codex axes: OpenAI Codex CLI + the Codex Claude Code plugin
- Gemini axes: `agy` (Google Antigravity CLI) with a signed-in account
- Blast radius: [`sem`](https://github.com/Ataraxy-Labs/sem) indexed for your repo
- React mechanical axis: `npx react-doctor` (auto-skipped on non-React PRs)

**Bitbucket only**
- An Atlassian API token (app passwords are dead since mid-2026, CHANGE-3222); configure your email + workspace per `skills/bitbucket-pr-review/SKILL.md`

## Caution

- The Codex sections mutate `~/.codex/config.toml` during a run (MCP strip + effort override, pristine-backup + restore). Read Step 3 and Step 7 before first use.
- `skills/bitbucket-pr-mutation` is the only write path to Bitbucket and is deliberately ceremony-heavy (typed approval, proposal hashing, read-back). Its contract tests also pin the command's Step 8 wording — run `cd skills/bitbucket-pr-mutation/scripts && python3 -m unittest discover -s tests -q` after editing either file.
- `commands/pr-review.md` is additionally pinned by eleven contract tests in `commands/tests/` (report projection, C4 dispatch envelope, report Self-Verify, repo profile, spec transport, prior-round continuity, multi-target preparation, group report, and the Gemini/web, CC, and Codex group-material paths). Run all eleven after editing the command or the `spec-compliance-reviewer` agent — the command's header lists the exact invocations.
- **Test coverage, stated honestly**: those eleven are *text* contract tests — they assert that specific wording still exists in the command, and do not execute a review, the projection helper, or the dispatch gate. No behavioral test suite ships for `scripts/pr-review-report-projection.py`, `scripts/pr-review-c4.py`, or `hooks/pr-review-c4-dispatch-gate.py`. Treat the C4 dispatch permit lifecycle in particular as unexercised here: a permit is keyed per Claude Code session and is not reissued once one has been granted, so a second formal-spec dispatch in the same session finalizes `SKIPPED` with `C4_DISPATCH_PERMIT_EXISTS`. Start a fresh session for the next PR, and read the permit code before relying on it.
- Costs are real: a default-preset run of a mid-size PR spends tens of minutes wall-clock and millions of Codex tokens. Codex presets (`astra-lite` for a cheaper pass, `deep` / `ultra` when the change deserves it) exist for a reason, and Step 2.98 lets you drop whole axes for a small PR.

## License

MIT
