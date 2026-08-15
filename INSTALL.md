# INSTALL

Agent-executable install guide. If you use Claude Code, you can literally point it at this repo and say *"read INSTALL.md and set me up for &lt;my configuration&gt;"* — every step below is a checkable action.

## Step 0: Pick your configuration

| Configuration | You review PRs on | Extra axes you want | Install packages |
|---|---|---|---|
| **A. Minimal** | GitHub | none (CC reviewers only) | 1, 2, 3 |
| **B. Multi-axis** | GitHub | Codex and/or Gemini | 1, 2, 3, 4 |
| **C. Bitbucket** | Bitbucket (or both) | any | 1, 2, 3, (4), 5 |

All configurations degrade gracefully: a missing optional axis is reported as a gap in the review report, never a hard failure.

⚠️ **Before copying**: the `cp` commands below overwrite same-named files in your `~/.claude/`. If you already have a `pr-review.md` command or agents named `code-reviewer` / `security-reviewer` / `typescript-reviewer` / `python-reviewer` / `spec-compliance-reviewer`, back yours up or diff first.

Platform note: tested on macOS. `scripts/poll-liveness.sh` uses BSD `stat -f%m`; on Linux, port those calls to `stat -c %Y` before relying on the Codex axes.

## Package 1: command (required)

```bash
mkdir -p ~/.claude/commands && cp commands/pr-review.md ~/.claude/commands/
```

## Package 2: reviewer agents (required)

```bash
mkdir -p ~/.claude/agents && cp agents/*.md ~/.claude/agents/
```

Two things to adjust to your own setup:

- **Model pins**: the five reviewer agents pin `model: opus` + high effort (review quality is worth the spend); `skill-verify-auditor` deliberately pins `model: sonnet` + low (it only checks a report against a fixed rubric). If your plan has no Opus access, edit the `model:` line in each reviewer file to a model you can run — the agents work on any capable model, the pin is a quality preference, not a dependency.
- **`mcp__semble__*` in `tools:`**: three agents list an optional semantic-code-search MCP. If you don't run that MCP server, leave the entries or delete them — either way the agents fall back to Grep (the command's Step 2.7 treats semantic search as an optional accelerator).

## Package 3: references + scripts (required)

```bash
mkdir -p ~/.claude/references ~/.claude/scripts
cp references/*.md ~/.claude/references/
cp scripts/* ~/.claude/scripts/
chmod +x ~/.claude/scripts/poll-liveness.sh ~/.claude/scripts/sem-pr-blast-radius.sh
```

`pr-review-c4.py` (formal-spec gate only) needs Python ≥ 3.9 and the `jsonschema` package (`pip3 install jsonschema`). Without them the gate fails on first use instead of degrading — install them, or expect to answer `SKIPPED` on spec-bearing PRs.

`pr-review-report-projection.py` (Step 6 report publication — runs on **every** review) needs the `markdown-it-py` package (`pip3 install markdown-it-py`). It deterministically projects the full-evidence audit report into the decision-facing main report; without it Step 6 cannot publish.

References are read during severity calibration on every run. `pr-review-report-projection.py` runs at Step 6 on every review; the other scripts only execute when their axis is enabled (`poll-liveness.sh` → Codex axes; `pr-review-c4.py` → formal-spec gate; `sem-pr-blast-radius.sh` → auto-skips unless [`sem`](https://github.com/Ataraxy-Labs/sem) is installed and indexed).

## Package 3b: C4 dispatch permit gate (optional, defense-in-depth)

The formal-spec gate's dispatch envelope issues a single-use session permit; this PreToolUse hook makes Claude Code **enforce** it — the `spec-compliance-reviewer` Agent call is denied unless it matches the permit's pre-issued hash byte-for-byte. Without the hook the same four-field contract binds by convention and the runtime receipt is the backstop, so this package is optional but recommended if you use the formal-spec gate.

```bash
mkdir -p ~/.claude/hooks && cp hooks/pr-review-c4-dispatch-gate.py ~/.claude/hooks/ && chmod +x ~/.claude/hooks/pr-review-c4-dispatch-gate.py
```

Then register it in `~/.claude/settings.json` (merge into your existing `hooks` section):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/pr-review-c4-dispatch-gate.py || exit 2",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The hook is inert for every Agent call except `subagent_type: spec-compliance-reviewer`, so it adds no friction to normal work.

## Package 4: external axis tools (optional, per axis)

- **Codex neutral + adversarial axes**: install the [OpenAI Codex CLI](https://github.com/openai/codex), then the `openai-codex` plugin from the Claude Code plugin marketplace (`/plugin` in Claude Code) and sign in. The command locates the plugin at `~/.claude/plugins/cache/openai-codex/codex/*/` and executes its `codex-companion.mjs` — that path existing is the install-success signal. The adversarial axis additionally shells out to `node` and `sqlite3`: `sqlite3` ships with macOS, but **Node is not preinstalled** — install it yourself (e.g. `brew install node`). The command mutates `~/.codex/config.toml` during a run (pristine backup + restore in Step 7) — read Step 3 before first use.
- **Gemini axes**: install `agy` (the [Google Antigravity](https://antigravity.google.com) CLI) and sign in. Verify: `command -v agy`. No further config; the command handles its CLI quirks.
- **React mechanical axis**: needs Node/npm on PATH (`npx react-doctor` downloads on first use); nothing else to install.

## Package 5: Bitbucket adapter (only if you review Bitbucket PRs)

```bash
mkdir -p ~/.claude/skills
cp -R skills/bitbucket-pr-review skills/bitbucket-pr-mutation ~/.claude/skills/
```

Then configure credentials (an Atlassian **API token** — app passwords are dead):

1. Create a token at https://id.atlassian.com/manage-profile/security/api-tokens
2. Export in your shell profile or secrets file:
   ```bash
   export BITBUCKET_EMAIL="you@example.com"        # your Atlassian account email
   export BITBUCKET_API_TOKEN="..."
   ```
   Both the read path (`bb_api.sh`) and the write path (`bitbucket_pr_workflow.py`) accept `BITBUCKET_EMAIL`; the write path also honors `BITBUCKET_API_USERNAME` if you already use that name.
3. Set your workspace: see the Config section at the top of `skills/bitbucket-pr-review/SKILL.md`.

The mutation package requires **Python ≥ 3.10** (uses `zip(strict=True)` and PEP 604 unions) — stricter than the ≥ 3.9 needed by the formal-spec gate.

## Verify

```bash
# All configurations
ls ~/.claude/commands/pr-review.md ~/.claude/references/finding-severity-rules.md ~/.claude/references/severity-calibration.md
ls ~/.claude/agents/{typescript,python,code,security,spec-compliance}-reviewer.md ~/.claude/agents/skill-verify-auditor.md   # all 6 exist
test -x ~/.claude/scripts/poll-liveness.sh && test -x ~/.claude/scripts/sem-pr-blast-radius.sh && echo "scripts ok"
command -v gh   # GitHub PRs

# Report projection (required — Step 6 publication)
python3 -c 'import markdown_it; print("projection deps ok")'

# Formal-spec gate (optional)
python3 -c 'import sys, jsonschema; assert sys.version_info >= (3, 9); print("c4 deps ok")'
test -x ~/.claude/hooks/pr-review-c4-dispatch-gate.py && echo "c4 permit gate installed (optional)"

# Codex axes (optional)
command -v codex && command -v node && command -v sqlite3
CODEX_PLUGIN_DIR=$(ls -d ~/.claude/plugins/cache/openai-codex/codex/*/ 2>/dev/null | sort -V | tail -1)
test -f "${CODEX_PLUGIN_DIR}scripts/codex-companion.mjs" && echo "codex plugin ok"

# Gemini axes (optional)
command -v agy

# React axis (optional)
command -v npx

# Bitbucket adapter (Configuration C)
python3 -c 'import sys; assert sys.version_info >= (3, 10); print("bitbucket adapter python ok")'
```

Then run a smoke review on a small real PR: `/pr-review <URL of a 1-3 file PR>`. Expect: worktree created under `.worktrees/review-pr-<id>`, the two preset questions (Gemini Pro opt-in, Codex preset — answer with defaults), a report Self-Verify pass, then two zh-TW reports at `<repo-root>`: the decision-facing `pr-<id>-review.md` plus the full-evidence `pr-<id>-review.audit.md`, and the worktree cleaned up afterwards. Missing optional axes appear as noted gaps in the report header.

Bitbucket installs: additionally run the contract tests once — `cd skills/bitbucket-pr-mutation/scripts && python3 -m unittest discover -s tests -q` (expect `OK`, one skip is normal).

If you edit `commands/pr-review.md` or the agents in this repo, run the repo's contract tests before shipping the edit: `python3 commands/tests/test_pr_review_report_projection_contract.py && python3 commands/tests/test_pr_review_c4_dispatch_contract.py && python3 commands/tests/test_pr_review_self_verify_contract.py`.
