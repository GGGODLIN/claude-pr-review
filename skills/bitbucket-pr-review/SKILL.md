---
name: bitbucket-pr-review
description: "Use when the user asks to inspect an existing Bitbucket finding, comment, author reply, diff location, or PR evidence without running a full code review. Full PR code review and formal review reports must use `/pr-review`. Do NOT use for GitHub PRs, metadata-only status lookups that need one `bb_api.sh` call, or Bitbucket mutations handled by `bitbucket-pr-mutation`."
---

# Bitbucket PR Review

> ⚠️ **本檔在契約測試底下**：`skills/bitbucket-pr-mutation/scripts/tests/test_no_raw_bitbucket_writes.py` 會讀本檔做內容斷言，並掃描全檔有無 raw curl POST/PUT/DELETE。改完跑：
> `cd <repo-root>/skills/bitbucket-pr-mutation/scripts && python3 -m unittest discover -s tests -q`

Inspect Bitbucket Cloud pull request evidence through read-only REST API calls.

完整 PR review 請改用 `/pr-review`。本 skill 只處理既有 finding、留言、作者回覆、diff 位置或 PR 證據的定點複查，不產出正式 review 報告，也不派報告 auditor。

## Config

- Workspace: `$BITBUCKET_WORKSPACE` (the Bitbucket workspace slug, e.g. `your-workspace`)
- Email: `$BITBUCKET_EMAIL` — your Atlassian account email
- Token: Scoped API token exported as `BITBUCKET_API_TOKEN`
- API Base: `https://api.bitbucket.org/2.0`

## Authentication

**Step 0 — ALWAYS use `bb_api.sh`, NEVER raw curl with hand-crafted auth.** The helper handles token loading + Basic Auth + 302 redirects (`-sL`) in one place. Raw curl with manual `-u` flags has caused repeated 401s + diff-endpoint-returns-empty bugs.

```bash
bash <skill_dir>/scripts/bb_api.sh "<endpoint>"
```

That's it for read endpoints. Do not paste your own `curl -u ...` even "just to test"—the failure modes (401 / empty diff / wrong account) are the exact bugs this skill exists to prevent.

### Background (only relevant when bb_api.sh itself misbehaves)

- `bb_api.sh` reads `BITBUCKET_API_TOKEN` and `BITBUCKET_EMAIL` env-first, then falls back to grepping the shell secrets files listed in `BITBUCKET_SECRETS_FILES` (default `~/.zsh_secrets` then `~/.zshrc` — adjust that list to wherever your own setup keeps exported secrets). Sourcing the rc file instead is not an option: interactive-only rc files routinely fail under non-interactive shells (nvm / compdef / prompt-init errors).
- 🪤 If you see `ERROR: BITBUCKET_API_TOKEN not in env or in: ...`, the token is genuinely unreadable — that is NOT the same as the token being expired. Run `echo ${#BITBUCKET_API_TOKEN}` first: a non-zero length means the credential is alive and only the read path is broken. Moving the token between secrets files while helpers still grep the old one produces exactly this error and is repeatedly misdiagnosed as an expired token.
- Atlassian killed Bitbucket app passwords (CHANGE-3222, returns 410). Anything still using the older username + app password pair is dead; only account email + scoped API token works for REST.
- For git push/pull over HTTPS the same token uses a different username (`x-bitbucket-api-token-auth`), not your account email.

### Write endpoints

`bb_api.sh` only does GET. This review skill never calls POST／PUT／DELETE endpoints. Description updates and comment drafts must be passed to `bitbucket-pr-mutation`, which owns preview, exact proposal confirmation, pre-write recheck, Apply, and GET read-back. Unsupported mutations stay draft-only; do not fall back to raw curl.

If 401, the scoped API token may have expired. Direct user to regenerate at:
https://id.atlassian.com/manage-profile/security/api-tokens
(Use "建立有範圍的 API 權杖", must select Bitbucket scopes including repository read and pullrequest read)

## Steps

### 1. Parse PR URL

Extract workspace, repo, and PR ID from the URL:
`https://bitbucket.org/{workspace}/{repo}/pull-requests/{id}`

### 2. Fetch PR Details

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/pullrequests/{id}"
```

Parse with python3 to extract: title, state, author, source branch, destination branch, description, full `source.commit.hash`, full `destination.commit.hash`, `source.repository.uuid`, and `destination.repository.uuid`.

Create one immutable `review_input_basis` before fetching review inputs:

```yaml
source_repo_uuid: "{source.repository.uuid}"
source_sha: "{full source.commit.hash}"
destination_repo_uuid: "{destination.repository.uuid}"
destination_sha: "{full destination.commit.hash}"
input_binding: verified
reviewed_at: "{timestamp}"
```

Set `input_binding: verified` only when both repository UUIDs are present, both SHA values are full 40-character hashes, and every diffstat／diff／src request below uses this exact commit pair. Otherwise set `input_binding: unverified`; the focused verdict must not claim a Reviewed SHA.

#### 2.1 Load author calibration file (if present)

Slugify `author.display_name` (lowercase, spaces → hyphens, e.g. `Ada Lovelace` → `ada-lovelace`), then try to read from the target repo's local clone:

```
<repo-root>/docs/pr-review-calibration/<author-slug>.md
```

- File exists → load it and keep the author's entries in context; they apply at 6d rule 4 (severity calibration).
- File missing → proceed normally; no calibration applies. Do not create the file. The focused verdict still carries one line:「無作者校準檔（<author-slug>.md 不存在）、本輪無套用」— keeps "ran, no file" distinguishable from "step skipped".

Calibration entries record how a specific author historically responds to review comments (which finding types they accept vs reject). They tune severity and comment phrasing for that author only — they never introduce new findings and never upgrade severity.

### 3. Fetch Diffstat

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/diffstat/{source_commit}%0D{dest_commit}?from_pullrequest_id={id}&topic=true"
```

Get `source_commit` and `dest_commit` from `review_input_basis`, not from branch names or a later PR response:

- `source_commit` = full `review_input_basis.source_sha`
- `dest_commit` = full `review_input_basis.destination_sha`

The exact commit-pair path is `{source_commit}%0D{dest_commit}`. Do not replace either side with a moving branch ref.

Diffstat 只用來定位使用者點名的檔案與 spec／plan；standalone 定點複查不執行全 PR React-doctor，也不從未點名的檔案產生新 finding。完整機械掃描由 `/pr-review` 負責。

### 4. Fetch Diff

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/diff/{source_commit}%0D{dest_commit}?from_pullrequest_id={id}&topic=true&context=5"
```

If diff is too large, fetch per file using diffstat paths.

### 5. Fetch Comments (if any)

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/pullrequests/{id}/comments"
```

### 6. Focused Follow-up

只針對使用者點名的既有 finding、留言、作者回覆或 diff 位置查證。若請求需要完整逐檔 review、多軸比較或正式報告，停止本流程並改用 `/pr-review`。

下列 6a–6d 是 `/pr-review` 引用的查證與校準規則；定點複查只套用與目標 finding 直接相關的部分，不展開正式報告。

#### 6a. Detect Spec / Plan Docs in PR

PRs produced via the Superpowers workflow (brainstorming → writing-plans → executing-plans) often ship with a markdown spec/plan/design doc that declares intent, scope, and explicit non-goals. Reviewers should use these as ground truth for "what this PR is supposed to do" before flagging "missing X".

From the diffstat returned in Step 3, flag a `.md` file as a spec if ANY of:

- Path contains `/specs/`, `/plans/`, `/brainstorm/`, `/design/`, `/proposals/`, `.claude/plans/`
- Filename matches `*-spec.md`, `*-plan.md`, `*-design.md`, `*-brainstorm.md`, `*-requirements.md`, `*-proposal.md`, or `YYYY-MM-DD-*.md`
- File starts with frontmatter containing `type: plan` / `type: spec` / `type: design` / `phase:` / `goals:` / `non_goals:`

Excluded (not specs): `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `LICENSE.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.

For each detected spec, fetch its full content via:

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/src/{source_commit}/{path_to_spec}"
```

If combined spec content > ~8000 tokens, summarise before feeding into the review context (keep goals, non-goals, decisions, constraints — drop prose).

Use spec content when applying 6b (search-before-flag context) and 6c (design intent ground truth for refactor PRs): if spec marks something as out-of-scope, do NOT uphold the target finding on that basis. Quote the relevant spec passage in the focused verdict.

**Spec-mapping self-check — before citing spec as evidence AGAINST the code**: quoting a real spec passage is not enough; the passage must describe the same flow as the code being flagged. Ask "does this spec section's subject (the flow/feature it governs) match the code path I am commenting on?" If the spec section governs flow A (e.g. error handling during redemption) and the flagged code implements flow B (e.g. listing already-redeemed items), do not cite it — the citation would be accurate text applied to the wrong target, which is worse than no citation because spec quotes carry authority. Real miss: a review cited spec lines governing redemption-flow errors against fetch-redeemed-list code; the author correctly rejected it as「不一樣的東西」.

If no spec is detected, note it in the focused verdict ("此 PR 未附 spec／plan 文件") and proceed normally.

After this step, continue to 6b — Context-Gathering Discipline.

#### 6b. Context-Gathering Discipline (MANDATORY before flagging)

Before flagging findings of the form "missing X" / "should handle Y" / "no validation" / "not tested" / "reinventing wheel" / "missing auth check", you MUST verify the gap is real by searching the codebase first. Upstream middleware, framework defaults, existing utilities, and tests in other files often cover what looks missing in the diff alone.

**Runtime-behavior assertions are in scope too** — findings of the form "this will crash" / "this button does nothing" / "this renders undefined" / "會炸 / 按了沒反應". These assert an observable behavior, and the diff alone cannot prove them: the surrounding machinery often makes the code correct. Before flagging, trace the mechanism the assertion depends on — at minimum whichever of these apply:

- **Form library defaults**: does `useForm` `defaultValues` (or equivalent) already supply the value claimed to be undefined? (Real miss: flagged `useWatch` destructure crash while `DEFAULT_FORM_VALUES` provided `additionalEntries: {}`)
- **Native platform behavior**: is the "dead" button inside a `<form>` where its default `type="submit"` triggers the parent `onSubmit`? A sibling button carrying explicit `type="button"` is a strong signal the omission is deliberate. (Real miss: flagged missing `handleSave` while the button submitted the enclosing form)
- **Framework/library internals**: does the claimed failure mode match what the library actually does? Read the library source or docs when the assertion hinges on it (e.g. what a provider renders before activation). (Real miss: described "flash of English" where Lingui actually renders `null` pre-activate)

The finding must state which mechanism was traced and why the assertion still holds. If tracing shows the behavior is covered → drop the finding silently, same as the "missing X" rule.

**Suggested-fix embedded assumptions are in scope too** — a finding that survives verification can still get refuted through its _suggested fix_: fix text routinely asserts behavior of paths / APIs / options the diff never touched（「把 Y 分支也加上 X」隱含「Y 支援 X」）. The finding carries an evidence chain while the fix rides along unverified; when the author refutes the fix half, the whole comment's credibility goes down with it. Before attaching a suggested fix, verify every path / API behavior assumption embedded in it with the same search tools below (or library docs / source); where verification isn't feasible, narrow the fix to the verified branch and phrase the rest as「需確認 X 是否支援 Y」instead of asserting it. (Real miss: finding "classic 登入掉 locale 前綴" was correct and accepted, but the suggested fix also prefixed `/customer_authentication/login` on the unverified assumption that the path accepts a locale prefix — it 404s, Shopify handles locale itself; the author refuted the fix half)

**Mechanism-chain claims must be verified station-by-station** — a finding whose argument is a causal chain（「line N 的 filter 會把 X 濾掉 → 判定函式回 false → 卡在未完成」）is only as strong as its weakest link. Citing a file:line is not verification — misreading the cited line is exactly how the chain breaks. Before flagging, read every function the chain passes through and confirm its actual semantics: what a `filter` predicate actually excludes (the element itself vs a field of the element), what a completion/validation predicate actually compares (`hasOwnProperty` checks key existence, not value). The finding must quote the chain's key predicates, not just their line numbers. (Real miss: flagged "switching the primary entry leaves the base tier stuck incomplete" citing a filter line, but that filter excludes the base _tier_ from the rewrite path rather than filtering the tier's _entries_, and the completion predicate uses `hasOwnProperty` — key existence, not value — which passes for the all-zero structure)

**A repro path's first station needs the same evidence as its last** — the chain rule above covers what happens once execution reaches the flagged line; it does not cover how a user gets there. A finding is routinely written as「使用者只要在 X 狀態下做 Y，就會 Z」, where Z is executed and verified but X is assumed. X is a producer claim and needs a producer: a code path in this repo that generates that state (cite file:line), an external system documented to generate it (cite the doc), or an explicitly attacker-crafted input (say so, and accept the 6d-1 hedge cap that follows). Plausibility is not a producer — "URLs like this happen all the time" is the shape to watch for. If no producer can be named, either drop the concreteness (「目前找不到會產生這種輸入的路徑」) or drop the finding; do not invent a scenario to make a verified mechanism feel reachable. Note the severity consequence: an invented producer is usually the only thing separating a defensive-hardening note from a Must Fix, so this failure inflates rather than merely decorates. (Real miss: flagged an unguarded `new URL()` as Must Fix on the strength of「使用者只要在帶重複斜線的網址上」— the throw was real and executed, but a repo-wide grep showed every in-repo URL concatenation is `root + 'path'` with no doubled slash, and whether the hosting platform even serves such a path was never checked; without that producer the finding caps at Nice to Have under 6d)

**Rider-clause absence assertions are in scope too** — the "missing X" search-proof rule applies to subordinate clauses riding on a main finding（「…而且沒地方修」「…也沒有 fallback」）, not only to headline findings. A rider asserting that a UI entry point / config / escape hatch doesn't exist needs the same search-proof; if unverified, drop the clause or phrase it as「未確認是否有入口」. (Real miss: the same finding as above appended "而且沒地方修" while the resource's edit page exposes exactly that field)

Search tools in preference order:

1. `mcp__semble__search` — semantic search, when the target repo is indexed locally; pass `repo` = your repo root. Best for NL queries like "where is X handled" / "how does Y work" / symbol lookup across languages.
2. `Grep` with keyword/regex — always available fallback. Best for exhaustive literal matches, finding all imports, finding specific error strings.
3. Local git clone direct grep if MCP unavailable.

Every such finding must attach in the review output:

- What you searched for (query + tool used)
- What you found (file:line citations, even partial matches)
- Why the finding still stands given what was found

If the pattern IS already handled elsewhere → do NOT flag. Drop it silently.

If genuinely missing → flag with search-proof. Example:

> 搜尋 "zod schema validation for PATCH body" 用 `mcp__semble__search`；只找到 `src/routes/users.ts:14` 對 POST 有 zod schema，新增的 `PATCH /users/:id` handler 在 `src/routes/users.ts:88` 沒有對應 validation

Scope exclusions (rule does NOT apply to):

- Bugs within the diff itself (logic errors, typos visible in changed lines)
- Style / formatting
- Hardcoded secrets, SQL injection, eval, unsanitized HTML — these are strict-liability, flag immediately
- Items explicitly marked out-of-scope or non-goal in the spec from 6a — do not flag even if code looks incomplete; reference the spec passage instead if relevant

Rationale: false-positive findings from diff-only review waste the reviewer's time and erode trust. Search-before-flag plus spec-awareness raises the signal ratio.

After this step, continue to 6c — Refactor Intent Gate.

#### 6c. Refactor Intent Gate / 6d. Severity Calibration

Both gates live in the platform-neutral SSOT `~/.claude/references/finding-severity-rules.md` (in this repo: `references/finding-severity-rules.md`) — shared with the `/pr-review` multi-axis command. Run 6c (design-intent verification for "protection removed/weakened" findings) then 6d (Must / Should / Nice calibration; in this single-axis flow rules 1, 3, 4 apply and rule 2 is N/A) exactly as written there, then continue to 6e.

#### 6e. Focused Verdict

Before rendering the verdict, refetch PR details and compare the current source／destination repository UUIDs and full SHA values with `review_input_basis`. Render `source_continuity`, `base_changed`, and `review_context_changed`; list exact new commits when ancestry proves `NEW_COMMITS`. This is a freshness notice only: do not widen the requested scope or turn the follow-up into a full review. Repeat the same refetch before preparing any mutation preview.

Respond in 繁體中文 with only the evidence needed for the requested follow-up:

```markdown
## 定點複查結果

- **目標**: {使用者點名的 finding／留言／作者回覆／diff 位置}
- **Review input**: source `{full source SHA}` / destination `{full destination SHA}`; input_binding={verified|unverified}
- **Continuity**: {source_continuity}; base_changed={true|false|unknown}; review_context_changed={true|false}
- **Verdict**: {成立｜不成立｜部分成立｜證據不足}
- **證據**: {API 回應、comment、diff 或 source 的精確位置與關鍵內容}
- **理由**: {一句白話結論}
- **沒做的部分**: {未查證項目；沒有則寫「無」}
```

若使用者要回覆作者或更新 PR，只產生候選文字並接 Step 9；本 skill 不直接 mutation。

### 7. Fallback: Local Git

If API fails and the branch exists locally, fall back to git diff:

```bash
git fetch origin
git cat-file -e "{destination_sha}^{commit}"
git cat-file -e "{source_sha}^{commit}"
git diff "{destination_sha}...{source_sha}" --stat
git diff "{destination_sha}...{source_sha}"
```

`destination_sha` 與 `source_sha` 必須逐字來自 `review_input_basis`。任一 commit 無法取得時回報「證據不足」，不得退回 moving branch ref。

### 8. 呈現

把定點複查結果全文放在回合最終訊息；不可寫「結果如上」指涉中段文字。這條路徑不產出正式 review 報告，也不派 Self-Verify auditor。

### 9. Mutation delegation

This skill may prepare structured description／comment candidates, but it never writes them. Before any candidate is previewed, refetch the current PR snapshot and render continuity／base-changed status again. Pass candidates with full reviewed source／destination SHA to `bitbucket-pr-mutation`. When the target originated from a formal `/pr-review` finding, preserve its stable `finding_uid`; a standalone comment／reply／diff follow-up without a formal finding may omit `finding_uid` because the mutation helper treats it as optional. Only `bitbucket-pr-mutation` may produce an exact proposal, collect later typed approval, apply allowlisted operations, and report per-operation outcomes.
