---
name: bitbucket-pr-review
description: "Use when user provides a Bitbucket PR URL (bitbucket.org/*/pull-requests/*), asks to review a Bitbucket PR, or says 'code review' with a Bitbucket link. Do NOT use for: GitHub PRs (use gh CLI instead), simple Bitbucket workspace/repo browsing without PR context, PR status queries needing only metadata (call bb_api.sh directly without invoking full review flow), or non-review Bitbucket mutations (delegate those to bitbucket-pr-mutation)."
---

# Bitbucket PR Review

> ⚠️ **本檔在契約測試底下**：`skills/bitbucket-pr-mutation/scripts/tests/test_no_raw_bitbucket_writes.py` 會讀本檔做內容斷言，並掃描全檔有無 raw curl POST/PUT/DELETE。改完跑：
> `cd <repo-root>/skills/bitbucket-pr-mutation/scripts && python3 -m unittest discover -s tests -q`

Review Bitbucket Cloud pull requests by fetching data via REST API.

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

Set `input_binding: verified` only when both repository UUIDs are present, both SHA values are full 40-character hashes, and every diffstat／diff／src request below uses this exact commit pair. Otherwise set `input_binding: unverified`; the report must not claim a Reviewed SHA.

#### 2.1 Load author calibration file (if present)

Slugify `author.display_name` (lowercase, spaces → hyphens, e.g. `Ada Lovelace` → `ada-lovelace`), then try to read from the target repo's local clone:

```
<repo-root>/docs/pr-review-calibration/<author-slug>.md
```

- File exists → load it and keep the author's entries in context; they apply at 6d rule 4 (severity calibration).
- File missing → proceed normally; no calibration applies. Do not create the file. The report still carries one line:「無作者校準檔（<author-slug>.md 不存在）、本輪無套用」— keeps "ran, no file" distinguishable from "step skipped" in report audits.

Calibration entries record how a specific author historically responds to review comments (which finding types they accept vs reject). They tune severity and comment phrasing for that author only — they never introduce new findings and never upgrade severity.

### 3. Fetch Diffstat

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/diffstat/{source_commit}%0D{dest_commit}?from_pullrequest_id={id}&topic=true"
```

Get `source_commit` and `dest_commit` from `review_input_basis`, not from branch names or a later PR response:

- `source_commit` = full `review_input_basis.source_sha`
- `dest_commit` = full `review_input_basis.destination_sha`

The exact commit-pair path is `{source_commit}%0D{dest_commit}`. Do not replace either side with a moving branch ref.

If the diffstat contains any `.jsx` / `.tsx` file → run 3.1 before continuing. Otherwise skip straight to Step 4.

#### 3.1 React Mechanical Scan (react-doctor) — only when diffstat contains `.jsx` / `.tsx`

Run the bundled scan script. It fetches origin, checks out the PR source commit in a temp worktree, and scans only files changed vs dest — never scan your local working tree instead; it may not match the PR's actual code (wrong branch / uncommitted state), and "no new issues" against the wrong target is worse than no scan.

```bash
bash <skill_dir>/scripts/react_doctor_scan.sh <local-repo-path> {source_commit} {dest_commit}
```

Pass the local clone path of the repo the PR belongs to. Takes ~15-60s (npx download + scan).

Output handling:

- `{"skipped": "<reason>"}` → note the reason and continue; the 6e report shows `SKIPPED (<reason>)` in its React-doctor section. Never block the review on scan failure, and never fall back to scanning the local working tree.
- Diagnostics JSON → keep the raw hits; when composing findings (6d/6e), classify each hit against the Step 4 diff: **new** (file:line falls on a `+` line) vs **pre-existing** (changed file, untouched line). Only new hits become review findings — they enter 6d severity calibration like any other finding (mechanical hits are severity 素材, not automatic Must Fix; rule id + file:line already satisfies 6b's evidence requirement). Pre-existing hits appear only as a one-line count.

After this step, continue to Step 4.

### 4. Fetch Diff

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/diff/{source_commit}%0D{dest_commit}?from_pullrequest_id={id}&topic=true&context=5"
```

If diff is too large, fetch per file using diffstat paths.

### 5. Fetch Comments (if any)

```bash
bash <skill_dir>/scripts/bb_api.sh "/repositories/{workspace}/{repo}/pullrequests/{id}/comments"
```

### 6. Perform Code Review

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

Use spec content when applying 6b (search-before-flag context) and 6c (design intent ground truth for refactor PRs): if spec marks something as out-of-scope, do NOT flag it even if code seems incomplete. Quote the relevant spec passage in the final report.

**Spec-mapping self-check — before citing spec as evidence AGAINST the code**: quoting a real spec passage is not enough; the passage must describe the same flow as the code being flagged. Ask "does this spec section's subject (the flow/feature it governs) match the code path I am commenting on?" If the spec section governs flow A (e.g. error handling during redemption) and the flagged code implements flow B (e.g. listing already-redeemed items), do not cite it — the citation would be accurate text applied to the wrong target, which is worse than no citation because spec quotes carry authority. Real miss: a review cited spec lines governing redemption-flow errors against fetch-redeemed-list code; the author correctly rejected it as「不一樣的東西」.

If no spec is detected, note it in the report ("此 PR 未附 spec／plan 文件") and proceed normally.

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

#### 6e. Output

**Finding narrative structure — applies to every finding whose trigger path crosses a boundary or spans ≥2 hops** (page↔page, frontend↔server, or a round-trip through an external system like Shopify). Write the finding in two layers, observation first:

1. **Lead with the locally-verifiable defect**: the exact behavior at file:line given a concrete input（「`getDestinationLine` 收到 `countries=[]` + `includeRestOfWorld` 就 render『For 0 countries』」）— the reader can confirm it by opening one file, no data-flow trust required.
2. **Reachability as numbered hops**, each hop with its own file:line, naming every intermediate system explicitly（「① modal 寫入本站 rule → ② server 經 `buildShippingDestination` 建 Shopify discount → ③ Custom reward 頁從 Shopify 抓回 `destinationSelection`」）. When the chain passes through two differently-shaped structures (e.g. the local rule's `countryCodes` vs Shopify's `destinationSelection`), give each structure its own hop and name the conversion between them.

Reason: a compressed cross-system chain reads as the reviewer confusing two structures — the author rejects the whole finding as AI hallucination even when every hop is real, and the indisputable render-site bug sinks with it. Real miss: a finding and its fix were both correct (fix adopted verbatim), but the compressed chain was initially read as「把本站儲存的折扣碼規則和 Shopify 的折扣碼規則混在一起」.

Before rendering the final report, refetch PR details and compare the current source／destination repository UUIDs and full SHA values with `review_input_basis`. Render `source_continuity`, `base_changed`, and `review_context_changed`; list exact new commits when ancestry proves `NEW_COMMITS`. This is a freshness notice only: do not auto-review, remove findings, or change severity. Repeat the same refetch before preparing any mutation preview.

Every verified finding must carry these fields:

```yaml
finding_uid: "sha256(file path + verbatim anchor + normalized root cause)[:20]"
display_ordinal: "F-01"
action: "auto-fix | ask-user | no-op"
action_reason: "one sentence"
plain_consequence: "one sentence explaining what visibly breaks or who absorbs the cost"
```

`finding_uid` is the stable ownership key; `display_ordinal` follows current report order. `plain_consequence` translates the defect into one observable outcome: what the user sees, what stops shipping, or—when runtime is unaffected—which developer／maintenance cost remains. Uncertain classification defaults to `ask-user`. Include this sentence verbatim in the report: `auto-fix 只是處置建議；沒有使用者另行下令，不修改 code、commit、push 或 PR。`

Output in this format (respond in 繁體中文):

```
# PR #{id} Review · SHA {short_source}
```

Use that title only when `input_binding: verified`. Otherwise use `# PR #{id} Review` and state `review input 未驗證；不宣稱 Reviewed SHA`.

```markdown
review_input_basis:
source_repo_uuid: "{full source repository UUID}"
source_sha: "{full reviewed source SHA}"
destination_repo_uuid: "{full destination repository UUID}"
destination_sha: "{full reviewed destination SHA}"
input_binding: "verified | unverified"
reviewed_at: "{timestamp}"

| 項目                  | 內容                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------- |
| **Author**            | {author}                                                                                    |
| **Branch**            | `{source}` → `{dest}`                                                                       |
| **Status**            | {state}                                                                                     |
| **Review continuity** | {source_continuity}; base_changed={true/false/unknown}; review_context_changed={true/false} |
| **檔案**              | {file list with +added/-removed}                                                            |
| **Spec / Plan**       | {spec filename(s) or 「未附」}                                                              |

### Spec 依據

{If spec detected: key goals / non-goals / decisions. Otherwise: 「此 PR 未附 spec／plan 文件」}

### 變更摘要

{Group changes by logical purpose}

### React-doctor 機械掃描

{Only when diffstat contains .jsx/.tsx — one of: (a) new hits as list of rule id + file:line + one-line fix hint, (b)「本次 PR 未引入新問題（既有命中 N 條不計）」, (c)「SKIPPED (<reason>)」. Omit this section entirely for non-React PRs.}

### Review 發現

{List issues found, categorized by severity. For each finding, note whether spec directly addresses it. Cross-boundary / multi-hop findings follow the narrative structure rule at the top of 6e.}

For every finding show `display_ordinal`, severity, summary, `plain_consequence`, `action`, and `action_reason`. Render `plain_consequence` with the label `白話後果` before technical mechanism details. Preserve `finding_uid` in the structured finding metadata used by downstream proposals; do not substitute the display ordinal as the operation ownership key.

`auto-fix 只是處置建議；沒有使用者另行下令，不修改 code、commit、push 或 PR。`

### 總結

{Overall assessment}
```

### 7. Fallback: Local Git

If API fails and the branch exists locally, fall back to git diff:

```bash
git fetch origin
git diff origin/{dest}...origin/{source} --stat
git diff origin/{dest}...origin/{source}
```

### 8. Self-Verify（mandatory — 報告成稿後、呈現使用者前）

Review 報告（6e）成稿後（成稿 = 內部草稿，此時**不要**先輸出報告文字——harness 對 tool call 之間的文字不保證顯示，verify 的 Agent call 會把先輸出的報告吞掉），派一個 verify subagent 檢查報告是否遵守本 skill 的 gate 規則。這步不可跳過、不可用「報告很簡單」「這次沒 finding」為由省略——零 finding 的報告也要驗（R5/R6 結構規則仍適用）。Verify 回來後接 Step 9 呈現。

Dispatch 規格：

- `Agent` tool、`subagent_type: skill-verify-auditor`（sonnet+low、tools 僅 Read，定義檔隨本 repo `agents/` 出貨；抽樣品質不足再換更強模型的 judge agent 重跑）
- description 固定含 marker 字串 `skill-verify:bitbucket-pr-review`（採用率統計 grep 用，不要改字）
- prompt 內嵌：(1) 完整 review 報告原文 (2) 下方 verify prompt template 全文。不需餵 diff 或 transcript——所有 rubric 都設計成可從報告文本判定

Verify prompt template（原文嵌入、`{report}` 換成報告全文）：

```
你是 adversarial 合規審查員，檢查一份 Bitbucket PR review 報告是否遵守其 SKILL.md 的 gate 規則。你的偏置是「找違規」：任一條規則無法從報告文本確認有遵守，判 FAIL，不要善意推定。

報告原文：
{report}

逐條檢查（每條回 PASS / FAIL / N-A + 一句證據引述）：

R1【6b search-proof】報告中每一個「缺 X / 該處理 Y / 沒 validation / 沒測試 / 重造輪子 / 缺 auth」型 finding，是否都附了搜尋證據鏈（搜了什麼 query + 用什麼工具 + 找到什麼 file:line + 為何 finding 仍成立）？「會 crash / 按了沒反應 / 會 render undefined」型 runtime 斷言 finding，是否都交代了追過哪個周邊機制（form defaultValues / 上層 form native submit / framework 內部行為）且斷言仍成立？缺任一環 = FAIL。報告無此兩型 finding = N-A。

R2【6d-1 hedge 降級】含假設性措辭（「需繞過 UI validation」「假設 API 回 X」「未來如果」「若有人 Y」等同義）的 finding，severity 是否都 ≤ Should Fix？（strict-liability 類豁免：hardcoded secret / SQL injection / eval / unsanitized HTML）

R3【6d-3 repro path + release-blocking】每一個 Must Fix finding 是否 (a) 附具體 user-visible 重現路徑（「到頁面 X、按 Y、觀察到 Z」形式）且 (b) 不修就會壞掉「會出貨的東西」（runtime 行為 / 資料正確性 / build・CI pipeline）？寫不出重現路徑、或後果只是死測試／死 config／文件與 code 不符（不阻擋發布）卻標 Must Fix = FAIL——consensus / cross-axis CONFIRMED 不豁免此檢查。

R4【6c refactor gate】主張「PR 移除 / 削弱了既有保護」的 finding，是否有交代設計意圖查證（引 spec / PR description / commit message 任一層）、或明確改寫成「scope unclear, ask author」？

R5【6a spec 依據】報告是否含 Spec / Plan 欄位與「Spec 依據」段（偵測到 spec 引其內容、否則明寫「此 PR 未附 spec／plan 文件」）？另外：引 spec 段落判 code 違規的 finding，引文所描述的流程是否與被 flag 的 code path 是同一件事（spec 講流程 A、code 是流程 B = FAIL）？

R6【6e 結構】報告是否含完整骨架：資訊表（Author / Branch / Status / 檔案 / Spec）+ 變更摘要 + Review 發現（按 severity 分類）+ 總結，且以繁體中文輸出？

R7【3.1 react-doctor】報告「檔案」欄含 .jsx / .tsx 檔時，是否含「React-doctor 機械掃描」段（新引入命中列表、或「未引入新問題」、或 SKIPPED + 原因，三者擇一）？檔案欄無 .jsx/.tsx = N-A。

R8【6b fix 假設】每個附「建議修法」的 finding，修法中引用的路徑／API／選項行為假設（「把 Y 分支也加上 X」隱含「Y 支援 X」）是否都有驗證痕跡（search-proof／library docs 或 source 引據）、或已把未驗證部分限縮成「需確認 X 是否支援 Y」語式而非斷言？修法夾帶未驗證的行為斷言 = FAIL。報告無任何修法建議 = N-A。

R9【6b 機制鏈＋附屬子句】(a) 因果鏈型 finding（「A 會導致 B 導致 C」）是否對鏈上每一站交代了實際語意（引 file:line 之外還引述該站關鍵 predicate 的行為——filter 濾掉的是什麼、判定函式比較的是 key 還是值）？只引行號不解語意 = FAIL。(b) 主 finding 尾巴掛的 absence 附屬子句（「而且沒地方修」「也沒有 X」）是否同樣附 search-proof、或已改寫成「未確認是否有入口」語式？附屬子句夾帶未驗證的 absence 斷言 = FAIL。報告無此兩型 = N-A。

R10【6e 白話後果】每個 finding 是否都有 `白話後果`，用一句話交代使用者會看到什麼、什麼會停止出貨，或 runtime 不受影響時由誰承擔哪種維護成本？只重述函式名、資料流或技術機制，沒有可理解的結果 = FAIL。報告無 finding = N-A。

輸出格式（固定，最後一行必須是 verdict 行）：

R1: PASS|FAIL|N-A — <一句證據>
R2: PASS|FAIL|N-A — <一句證據>
R3: PASS|FAIL|N-A — <一句證據>
R4: PASS|FAIL|N-A — <一句證據>
R5: PASS|FAIL — <一句證據>
R6: PASS|FAIL — <一句證據>
R7: PASS|FAIL|N-A — <一句證據>
R8: PASS|FAIL|N-A — <一句證據>
R9: PASS|FAIL|N-A — <一句證據>
R10: PASS|FAIL|N-A — <一句證據>
VERDICT: COMPLIANT | VIOLATIONS: <R 編號逗號列表>
```

### 9. 呈現（唯一輸出點 — verify 完成後）

完整報告全文放在 verify 之後的**回合最終訊息**輸出（最終訊息 = 後面不再有任何 tool call 的那則，是唯一保證顯示給使用者的位置）。不論 verify 結果如何，最終訊息都必須含報告全文，不可寫「報告如上」指涉中段文字。

按 verify 結果收尾：

- 全 PASS / N-A → 報告末尾附一行「🔎 self-verify: COMPLIANT (10/10)」
- 任一 FAIL → **先修報告再發**（補 search-proof / 降 severity / 補 repro path），修完不重驗、在報告末尾如實列出「🔎 self-verify 抓到並已修正：R3（xxx finding 原標 Must Fix 無 repro、已降 Should Fix）」
- Verify subagent 失敗（timeout / error）→ 報告照發、末尾標「🔎 self-verify: SKIPPED (agent error)」，不要靜默省略這行

### 10. Mutation delegation

This skill may prepare structured description／comment candidates, but it never writes them. Before any candidate is previewed, refetch the current PR snapshot and render continuity／base-changed status again. Pass candidates with full reviewed source／destination SHA and stable `finding_uid` values to `bitbucket-pr-mutation`. Only that skill may produce an exact proposal, collect later typed approval, apply allowlisted operations, and report per-operation outcomes.
