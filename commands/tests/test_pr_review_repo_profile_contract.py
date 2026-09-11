#!/usr/bin/env python3

import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewRepoProfileContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start_marker, end_marker):
    start = self.command.index(start_marker)
    end = self.command.index(end_marker, start)
    return self.command[start:end]

  def test_zsh_poll_word_splits_rollout_paths(self):
    self.assertIn("${=ROLLOUTS}", self.command)
    self.assertNotRegex(self.command, r"--deadline \d+ \$ROLLOUTS\b")
    self.assertNotIn("ls -S $ROLLOUTS", self.command)

  def test_zsh_companion_plugin_dir_joins_with_slash(self):
    self.assertIn("${CODEX_PLUGIN_DIR%/}/scripts/codex-companion.mjs", self.command)
    self.assertNotIn("${CODEX_PLUGIN_DIR}scripts/", self.command)
    self.assertNotIn("${CODEX_PLUGIN_DIR}schemas/", self.command)
    self.assertIn("ls -d */", self.command)
    self.assertIn("結尾斜線", self.command)

  def test_cwd_persistence_claim_and_codex_launch_use_git_dash_c(self):
    lock = self.section("### 2.5.3", "### 2.5.4")
    self.assertNotIn("不會持久", lock)
    self.assertIn("可能跨 call 殘留", lock)
    self.assertNotRegex(self.command, r'(?m)^cd "\$REVIEW_ROOT" &&')
    self.assertNotIn('`cd "$REVIEW_ROOT" &&` prefix', self.command)
    self.assertIn("git -C \"$REVIEW_ROOT\" merge-base", self.command)
    self.assertRegex(self.command, r"\(cd \"\$REVIEW_ROOT\" && nohup codex review")
    self.assertRegex(self.command, r"\(cd \"\$REVIEW_ROOT\" && env -u CODEX_COMPANION_SESSION_ID")

  def test_github_file_list_is_paginated_in_step2_and_step295(self):
    step2 = self.section("## Step 2: Fetch PR Data", "## Step 2.1")
    inventory = self.section("## Step 2.95", "### Chunking decision")
    for name, block in (("step2", step2), ("inventory", inventory)):
      with self.subTest(block=name):
        self.assertIn("gh api --paginate", block)
        self.assertIn("pulls/<number>/files?per_page=100", block)
        self.assertIn(".[].filename", block)
    self.assertIn("100 檔", step2)
    self.assertNotIn("gh pr view <number> --json files", inventory)

  def test_c4_scans_live_normative_sources_before_skipping(self):
    gate = self.section("## Step 2.65", "## Step 2.7")
    scan = gate.index("### 2.65.1")
    reducer = gate.index("resolve-authority")
    self.assertLess(scan, reducer)
    self.assertIn("git -C \"$REVIEW_ROOT\" ls-files", gate)
    self.assertIn("openspec/specs/**/spec.md", gate)
    self.assertNotIn("openspec/changes/archive/**/spec.md", gate[scan:reducer])
    self.assertIn("MUST|SHALL|NEVER", gate)
    self.assertIn("NORMATIVE_SCAN=", gate)
    self.assertIn("AUTHORED_FILES=$(git -C \"$REVIEW_ROOT\" diff --name-only", gate)
    self.assertIn('[ -z "$AUTHORED_IDS" ]', gate)
    self.assertIn("先跑 2.65.1", gate[reducer:])
    self.assertIn("2.65.1", self.section("**Dispatch checklist", " Review (Multi-Agent Routing)"))
    self.assertIn("bad substitution", self.command)

  def test_c4_skipped_reason_has_three_states(self):
    for state in (
      "SKIPPED (no normative source)",
      "SKIPPED (normative sources scanned, none intersect)",
      "SKIPPED (scan not run: <原因>)",
    ):
      with self.subTest(state=state):
        self.assertIn(state, self.section("## Step 2.65", "## Step 2.7"))
        self.assertIn(state, self.section("**Formal spec traceability (2.65)**", "**Quota"))
        self.assertIn(state, self.section("## Spec 依據", "## 變更概要"))

  def test_c4_authority_source_unchanged(self):
    gate = self.section("## Step 2.65", "## Step 2.7")
    self.assertIn("unpromoted `openspec/changes/<name>/specs/**` delta specs are also accepted as authority", gate)
    self.assertIn("`openspec/changes/archive/**` is an alias only", gate)
    self.assertNotIn("spec_globs", gate)
    self.assertNotIn("conventions_docs", gate)

  def test_step25_resolves_trunk_from_profile_then_origin_head_then_master(self):
    sync = self.section("## Step 2.5:", "### 2.5.1")
    self.assertIn("pr-review-profile.py", sync)
    self.assertIn("REPO_PROFILE=", sync)
    self.assertIn("TRUNK=<x> (source: profile <path> | origin/HEAD | default master)", sync)
    self.assertIn("**Trunk**: `TRUNK=", self.command)

  def test_provenance_triggers_on_base_not_equal_trunk(self):
    head = self.section("## Step 2.55", "```bash")
    self.assertIn("TRUNK_BRANCH", head)
    self.assertNotRegex(head, r"master ?/ ?main")
    self.assertNotIn("base = master", head)
    self.assertNotIn("base ≠ master", head)
    self.assertIn("base = trunk", head)
    self.assertNotIn("≠ master", self.command)
    self.assertNotIn("base = master", self.command)

  def test_spec_detection_appends_profile_globs(self):
    detect = self.section("### Detection heuristic", "### What to do with detected specs")
    self.assertIn("spec_globs", detect)
    self.assertIn("不是 C4 權威來源", detect)

  def test_conventions_docs_only_reach_cc_shared_prompt(self):
    shared = self.section("#### Shared prompt", "### Codex Review")
    self.assertIn("conventions_docs", shared)
    self.assertIn("沒做的部分", shared)
    for name, block in (
      ("codex", self.section("### Codex Review", "### Gemini Pro / Flash Review")),
      ("gemini", self.section("### Gemini Pro / Flash Review", "## Step 4")),
      ("c4", self.section("## Step 2.65", "## Step 2.7")),
      ("c4-packet", self.section("spec-compliance-reviewer` dispatch", "#### Shared prompt")),
    ):
      with self.subTest(block=name):
        self.assertNotIn("conventions_docs", block)

  def test_private_files_live_under_home_pr_review_dir(self):
    self.assertIn("~/.claude/pr-review/calibration/<author-slug>.md", self.command)
    self.assertIn("~/.claude/pr-review/friction.md", self.command)


if __name__ == "__main__":
  unittest.main()
