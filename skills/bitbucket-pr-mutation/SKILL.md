---
name: bitbucket-pr-mutation
description: Use when a user asks Claude Code to create a Bitbucket pull request or perform any Bitbucket PR write, including description, title, state, review-decision or comment changes, merges, teammate-authored PR changes, and unsupported mutations. Do not use for read-only PR review, diff, status, or metadata queries.
---

# Bitbucket PR Mutation

All first-party writes use `scripts/bitbucket_pr_workflow.py`; never use raw curl or another client.

## Route

- Read-only review or diff: use `bitbucket-pr-review`.
- Read-only status or metadata: outside this skill.
- V1: `create_pr`, `update_description`, `update_title`, `create_inline_comment`, `create_pr_comment`.
- Other writes: draft only and state `UNSUPPORTED_OPERATION`.

## Existing PR hard stop

Every existing-PR write first runs `inspect --input ...` with target metadata and optional drafts only. This read-only preflight returns the actor／author snapshot without requiring review basis or operations.

The stop is scoped by operation class, because the classes carry opposite risk. `update_description` and `update_title` overwrite author-owned content and cannot be undone from the API side, so both are **owner-only, OPEN-only**. Comments are additive, attributable to the actor, and deletable, so someone else's PR — or a merged/declined one — is a normal place to leave them.

Title sits with description rather than with comments even though it is one line: it is still the author's wording, and a workspace may feed titles into downstream automation (e.g. `[Release]`-prefixed titles driving release notifications), so renaming someone's PR can reach an audience beyond the PR page. What title does *not* share with description is the ceremony weight — see the tier table below.

| Preflight status                                           | Comments (`create_inline_comment`, `create_pr_comment`) | `update_title` | `update_description` |
| ---------------------------------------------------------- | ------------------------------------------------------- | -------------- | -------------------- |
| `READY_FOR_PROPOSAL` (own PR, OPEN)                        | allowed                                                 | allowed        | allowed              |
| `READY_FOR_COMMENT_ONLY` (foreign author, or state ≠ OPEN) | allowed                                                 | blocked        | blocked              |

A comment-only batch proceeds through the normal scope → preview → display → later-confirmation → apply path with nothing relaxed. A batch containing any owner-only operation against a `READY_FOR_COMMENT_ONLY` target is refused whole — `preview` returns `READ_ONLY_FOREIGN_AUTHOR` or `READ_ONLY_PR_NOT_OPEN`, and no partial subset is offered. Confirmation, admin access, and urgency cannot override the owner-only stop.

`preview` re-derives this independently rather than trusting the `inspect` status, and `apply` re-derives it again from the proposal's own operations. Relaxing one layer alone changes nothing.

For `DRAFT_ONLY_UNMANAGED_DESCRIPTION` or `DRAFT_ONLY_INVALID_MARKERS`, show managed-block drafts and stop with no envelope/proposal mutation.

Exclude customer data, credentials, unredacted orders, and private links from request bodies until redacted or rewritten.

完成後接「Scope and approval」。

## Scope and approval

Ceremony is tiered by operation class for the same reason the hard stop is: a comment is additive, attributable and one-click deletable, while a description write destroys author-owned prose. Running the heavyweight path for a two-comment batch costs more attention than the failure it prevents.

| Step                                            | Comment-only batch                                              | Title-only batch                                    | Any batch containing `update_description` or `create_pr`                                                                              |
| ----------------------------------------------- | --------------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Credential scan                                 | run                                                             | run                                                 | run                                                                                                                                   |
| `preview` (hash, batch ID, read-back contracts) | run                                                             | run                                                 | run                                                                                                                                   |
| `Self-Verify` subagent                          | skip                                                            | skip                                                | run                                                                                                                                   |
| Display before applying                         | compact: per operation, `path:line` plus the exact comment body | compact: the current title and the exact new title  | full exact proposal: target, snapshot, SHAs, operation IDs, methods, endpoints, bodies, read-back fields, `proposal_sha256`, batch ID |
| Confirmation                                    | one user message after the display                              | one user message after the display                  | one user message after the display, separate from the scope message                                                                   |
| `apply` + read-back                             | run                                                             | run                                                 | run                                                                                                                                   |

**Comment-only path:** ask which findings belong in the batch, build the operations, run `preview`, display each comment compactly, and apply on the next message confirming it. The proposal hash and batch ID are still computed and bound into the approval; they just are not read out.

**Title-only path:** same shape as comment-only — display the current title and the proposed one, then apply on the next message confirming it. A title is one line the user can read in full and re-issue if wrong, so printing the whole proposal envelope costs more attention than the mistake it would catch. The owner-only stop, `title_sha256` optimistic lock and read-back all still run; only the display and the subagent verification are lighter.

**Heavyweight path:** scope, then `preview`, then `Self-Verify`, then display the full proposal, then wait for a further independent message. Any scope selection or change is scope-only here, even when the message says apply, directly apply, proceed, urgent, or `auto-fix`. Regenerate and display the proposal, then await another message.

No path ever applies on the same message that chose the scope, and none skips `preview`, the credential scan, or read-back. Mixed batches take the heaviest tier present — a batch pairing a title change with a description rewrite is a heavyweight batch.

## Self-Verify

Applies to the heavyweight path only. Comment-only and title-only batches run the credential scan (inside `preview`) and skip the subagent.

Run the local deterministic credential scan first. It blocks strict JWT Bearer values, valid Basic `user:password` values, private-key literals, known provider formats, and sensitive keys. It intentionally passes unknown opaque Bearer values and long technical identifiers. On failure, show only category/path and stop. It does not judge customer data, order details, private links, or ambiguous opaque credentials; route those checks to R4.

Then use Agent with `subagent_type: skill-verify-auditor` (sonnet+low, Read-only, pinned in the agent definition) and description containing `skill-verify:bitbucket-pr-mutation`. Its prompt Reads the proposal/candidate JSON and this `SKILL.md`, returning PASS/FAIL plus one evidence sentence per rule:

- R1: no owner-only operation (`update_description`, `update_title`) targets a foreign-authored or non-OPEN PR, and unmanaged-description or invalid-marker status has no mutation in envelope/proposal. Comment operations on such a PR are expected and are not a violation.
- R2: exact proposal fields, hashes, batch ID, operation IDs, methods, endpoints, bodies, and read-back contracts are complete.
- R3: operations are V1-only; unsupported writes are drafts only.
- R4: semantically inspect every Bearer-like or opaque-credential string in request bodies, including unknown opaque Bearer values passed by the deterministic scan; request bodies contain no customer data, credentials, unredacted order details, or private links.
- R5: approval JSON does not exist and scope was not treated as approval.

Require final `VERDICT: PASS` or `VERDICT: FAIL`. Without PASS, never present the proposal as approvable, create approval, or apply. Fix FAIL and rerun; disclose agent errors as `SKIPPED` and stop.

## Apply

Store proposal and approval JSON in session-private files. Run `apply` with the current Claude Code `SESSION_ID`; never invent or reuse one. Credentials come only from `BITBUCKET_API_USERNAME` (or `BITBUCKET_EMAIL`) and `BITBUCKET_API_TOKEN` and are never printed.

Report every outcome. Never call partial completion successful or retry `outcome_unknown`; run `reconcile` and request inspection.

## 踩過的坑

- Unsupported and foreign-author writes must still trigger this skill before the hard stop.
- Scope and apply in one message still means scope-only.
- Raw curl or a generic client is never a shortcut.
- The author/state stop was originally one blanket check sitting above operation parsing, so it blocked comments on a teammate's PR — the single most common real use. Every other guard in the same function was already per-operation; this one was just placed too high. Gate placement decides scope: a check that runs before it knows the operation can only be all-or-nothing.
- That same stop was enforced in three places — `inspect`, `preview`, and `_snapshot_matches` on the apply path. Relaxing only `preview` would have left `apply` refusing the batch as `invalid`. When changing a gate here, grep every enforcement point first.
- Inline read-back asserted `other not in actual_inline`, but Bitbucket returns the unused anchor side as `"from": null` rather than omitting it, so every real inline comment failed read-back and aborted the rest of the batch. The suite missed it because `FakeClient.create_comment` echoed the request body verbatim and therefore never produced the null key. A test double that only replays what it was given cannot catch a response-shape assumption — mirror the provider's actual envelope.
- The first version of this skill ran one ceremony level for every operation. Posting two review comments cost a subagent verification plus a two-phase approval, which is more attention than a deletable comment can justify. Risk-tier the ceremony the same way the hard stop is tiered; uniform process on non-uniform risk reads as bureaucracy and trains people to want the whole thing bypassed.
- Adding `update_title` needed twelve edits, and three of them were missed on the first pass — all three the same shape, a **fall-through dispatcher**. `_read_back_resource`, `_read_back_matches` and `_reconcile_operation` each end with the comment branch and no operation-type guard, so an unhandled type is not rejected, it is silently treated as a comment (`KeyError: 7`, then `KeyError: 'inline'`, then a comment-id lookup during reconcile). Adding an operation type means enumerating every `operation_type ==` / `operation['type'] ==` site with grep, not only the ones that name the operation being copied — and the last of the three was found by that grep rather than by a failing test, because no existing case covered reconcile for a non-comment operation. Write the case anyway; an unexercised branch is where the next one hides.
- Title is the first operation whose hard-stop tier and ceremony tier differ (owner-only stop, comment-weight ceremony). Keep the two decisions separate when adding future operations — asking "can a non-author do this?" and "how much display does this deserve?" as one question is what produced the uniform-ceremony mistake above.
- (environment) The skill ships no venv. If there is no global `pytest`, run the suite with `PYTHONPATH=<skill>/scripts uv run --with pytest --python 3.11 pytest tests/ -q` from `scripts/`; without `PYTHONPATH` every module fails collection with `ModuleNotFoundError: bitbucket_pr_workflow`. The stdlib path is `python3 -m unittest discover -s tests -q` from the same directory.
