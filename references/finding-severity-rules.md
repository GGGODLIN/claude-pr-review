# Finding Severity Rules（6c / 6d）— platform-neutral SSOT

Shared by the `/pr-review` multi-axis command (Action Items → Severity calibration) and the single-axis `bitbucket-pr-review` skill (steps 6c / 6d). Section codes `6c` / `6d-1..4` are kept verbatim so existing cross-references stay resolvable. Install to `~/.claude/references/`.

Strict-liability classes referenced below = typo-level defects, hardcoded secrets, SQL injection via string concat, `eval` on user input, unsanitized HTML injection — these are flagged immediately and exempt from both gates.

#### 6c. Refactor Intent Gate — for findings that assert "PR removed / weakened existing protection"

**Why this gate exists**: `refactor(...)` / `feat(...)` commits often change the contract on purpose, and the old codebase's "logical-coupling filter" gets pulled apart in the process. Reading those deletions as "defense lost" is a common source of false Must-Fix findings — particularly from fresh-eyes review axes (Codex / agy) that never see the commit message and therefore default to "diff comparison = regression." This gate must be run by whichever axis has access to the design intent (or by the multi-axis synthesizer at the arbitration step).

**Trigger**: any finding asserting "the PR previously had X protection and now it is gone / weakened." The reviewer judges the category by the finding's argument shape, not by keyword match — phrasings like "guard removed", "filter weakened", "validation dropped", "defensive check missing", or any synonym all count.

**Exception — skip this gate**: strict-liability classes (see header) are flagged immediately even when the surface argument looks like "existing protection removed."

For every triggered finding, walk three questions in order:

1. **What is the design intent?** Consult these layers in priority order — stop at the first layer that gives a signal:

   - (a) **Spec / plan doc** detected during spec detection — the author's declared scope and non-goals. Highest authority; quote the relevant passage in the report.
   - (b) **PR description** — the author's stated purpose, used when no spec covers the change.
   - (c) **Per-commit message** of the commit that introduced the removed/weakened line. Use `git blame` (or the platform's commit API for the hash you find) to locate that commit, then read its message. Multi-commit PRs must drill to this layer rather than apply the whole PR description to a single change.
   - If all three layers are silent on this change → rewrite the finding as "scope unclear, ask author" instead of judging it Must Fix.

2. **Is the removed/weakened line a "logical-coupling artifact" or a "cross-contract defensive guard"?** Test: "Pretend you don't know what the old code looked like. Looking only at the new code, would this line make sense to exist?"

   - Does not make sense in the new model → logical-coupling artifact (tied to the old data model / interface and necessarily rewritten when the contract changes) → drop the finding.
   - Still makes sense → genuine defensive guard that should survive the contract change → continue to question 3.
   - Example: old `{ type: amount }` paired with `filter(amount > 0)` is the coupled expression of "amount > 0 means enabled"; once the new model splits to `{ enabled, amount }`, the filter naturally migrates to `filter(enabled)` rather than retaining `amount > 0`.

3. **Under the new contract, who owns the invariant the old behavior protected?** Trace the normal user flow end-to-end — typically UI input → form validate → handleSubmit → transform → API.
   - A layer in the new flow already enforces the invariant (UI validate rule / form schema / server-side validation) → defense-in-depth migrated, not a regression → drop the finding.
   - No layer enforces it → real regression → keep the finding.
   - Same step: confirm the suggested fix is compatible with the new design intent. If reinstating the old guard would silently drop user input, conflict with the new semantics, or undo what the refactor was for, the suggested fix itself is wrong — rewrite the suggestion.

If the finding survives all three questions, continue to 6d — Severity Calibration to set its rating.

#### 6d. Severity Calibration — runs whenever you decide a finding's Must / Should / Nice rating

**Why this gate exists**: severity ratings drift upward under the reviewer's own confirmation bias. Three failure modes recur — hedge-laden findings, single-axis lone findings, and findings with no concrete repro path — so this gate applies three calibrations corresponding to each, plus an author-specific pass (rule 4) when an author calibration file was loaded. In a single-axis review, rules 1, 3, and 4 run; rule 2 is N/A. In a multi-axis review, all four run at the synthesizer / arbitration step where findings are merged into the Fix table.

North star for what survives calibration well: findings that are **introduced by this PR + concretely triggerable + small to fix** have the highest acceptance rate; findings missing any of the three tend to get rejected or deferred by authors regardless of how correct the reasoning is.

Walk the rules in order:

1. **Hedge-word downgrade**: if the finding comment itself contains hypothetical framing — phrases like "需繞過 UI validation", "程式直接 call", "假設 API 回 X", "未來如果", "若有人 Y" — the severity caps at Should Fix. Reason: a hedge is the reviewer admitting the risk is not on the normal flow; placing it at Must Fix contradicts the hedge. Exception: strict-liability classes keep Must Fix even when stated hypothetically.

2. **Single-axis lone finding — judgment, not mechanical downgrade** (multi-axis review only — single-axis review skips this rule): a lone finding = exactly one axis raised it AND no other axis flagged the same hunk / root cause (a different failure mode on the same hunk counts as corroboration, not lone). Do NOT drop severity automatically — in low-overlap reviews "other axes stayed silent" is weak evidence (in one measured review 87% of findings were lone, including the top CONFIRMED HIGH; mechanical downgrade would have gutted the review). Instead judge: (a) cross-axis verification verdict is CONFIRMED and a plausible one-line explanation exists for why the other axes missed it (diff-only axis can't see cross-file interaction / axis didn't read library source) → keep severity, attach the explanation; (b) verdict is PARTIAL/INCONCLUSIVE AND no plausible miss-explanation surfaces → only then downgrade one step, citing both reasons; (c) hard to judge → dispatch a lightweight check (subagent) answering "N axes walked this diff — why did they miss it: plausible blind spot, or counter-evidence?" then apply (a)/(b). In the pr-review command this runs as Step 4.3b. Reason for the rewrite: the old rule assumed high inter-axis overlap; measured overlap is low, so silence alone is not counter-evidence — but a lone finding that ALSO fails verification still deserves the downgrade.

3. **Must Fix requires a user-visible repro path AND a release-blocking consequence**: any finding that remains at Must Fix must satisfy both halves.

   - **Repro half**: expressible as a concrete user-visible reproduction — "navigate to page X, click button Y, observe error Z / crash". If a concrete repro cannot be written, drop the severity to Should Fix or Nice to Have. Reason: when no repro can be written the finding is abstract reasoning rather than an observed behavior delta — the reviewer cannot demonstrate it, the PR author cannot reproduce it, and a fix cannot be verified.
   - **Release-blocking half**: merging without the fix must break something that ships — runtime behavior, data correctness, or the build/CI pipeline itself. Dead tests, dead config, doc claims that don't match code, and verification-story gaps are real defects but do not block a release → cap at Should Fix. Reason: Must Fix means "author cannot merge until this is fixed"; a finding whose absence of fix changes nothing that ships cannot carry that demand, no matter how many axes flagged it.
   - **Multi-axis consensus is confidence 素材, not a severity floor**: consensus (or a CONFIRMED cross-axis verdict) makes a finding trustworthy, but it still walks this rule — a consensus finding that fails the release-blocking half lands at Should Fix. (Anti-pattern this guards against: a 3-axis consensus on "new test can never run" was mechanically labeled Must Fix; nothing shipping was affected, and it was downgraded.)

4. **Author calibration** (only when an author calibration file was loaded for this PR's author): apply the author's entries to matching findings — typically further downgrading finding types this author consistently rejects, or adjusting comment phrasing (e.g. listing fix options instead of prescribing one). Constraints: calibration may only **downgrade severity or reword** — it never upgrades severity, never drops strict-liability findings, and never suppresses a finding entirely (floor is Nice to Have / 參考用). Each adjustment made must be noted in the report with the calibration entry it came from, so the user can audit and prune stale entries.
