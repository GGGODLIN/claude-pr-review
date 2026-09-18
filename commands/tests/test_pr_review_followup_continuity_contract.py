#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewFollowupContinuityContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()
    recall_start = cls.command.index("## Step 2.52: Load Prior Review Findings")
    recall_end = cls.command.index("## Step 2.55: Authored vs Inherited Provenance", recall_start)
    cls.recall = cls.command[recall_start:recall_end]
    dispatch_start = cls.command.index("## Step 3: Dispatch Dual Reviews")
    recheck_start = cls.command.index("## Step 4.7: Recheck Prior Review Findings", dispatch_start)
    cls.reviewer_path = cls.command[dispatch_start:recheck_start]
    report_start = cls.command.index("### Report Structure")
    report_end = cls.command.index("## Step 6: Output", report_start)
    cls.report = cls.command[report_start:report_end]
    cls.recheck = cls.command[recheck_start:report_start]

  def test_prior_report_recall_is_wired_after_review_worktree(self):
    step_25 = self.command.index("## Step 2.5: Sync review env")
    step_252 = self.command.index("## Step 2.52: Load Prior Review Findings")
    step_255 = self.command.index("## Step 2.55: Authored vs Inherited Provenance")
    self.assertLess(step_25, step_252)
    self.assertLess(step_252, step_255)
    self.assertIn("完成後接 Step 2.52", self.command[step_25:step_252])
    self.assertIn("完成後接 Step 2.55", self.recall)

  def test_prior_report_recall_binds_identity_and_actionable_findings(self):
    for marker in (
      'pr-${PR_ID}-review.audit.md',
      "**Report projection schema**: 1",
      "**Report projection schema**: 2",
      "schema 1 舊報告",
      "**Report generation**: sha256:<64-hex>",
      "source_repo_uuid",
      "destination_repo_uuid",
      "input_binding: verified",
      "finding_uid",
      "action=auto-fix | ask-user",
      "缺少其中一項只讓該候選在 Step 4.7 預設 `STALE`",
      "PRIOR_REVIEW_FINDINGS",
      'merge-base --is-ancestor "$PRIOR_SOURCE_SHA" "$PR_HEAD"',
      "Prior review continuity: N-A",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.recall)

  def test_prior_findings_do_not_anchor_fresh_reviewers(self):
    self.assertIn("不得注入 Step 3／4 reviewer prompt", self.recall)
    self.assertNotIn("PRIOR_REVIEW_FINDINGS", self.reviewer_path)
    self.assertNotIn("上一輪 findings 對帳", self.reviewer_path)

  def test_main_rechecks_prior_findings_without_new_reviewer_dispatch(self):
    for marker in (
      "FIXED",
      "STILL_OPEN",
      "STALE",
      "目前 `$PR_HEAD`",
      "first-hand evidence",
      "不得派新的 reviewer",
      "0 actionable findings",
      "每個 prior finding_uid 恰好一列",
      "完成後接 Step 5",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.recheck)

  def test_report_and_self_verify_keep_prior_finding_accounting(self):
    for marker in (
      "**Prior review continuity**:",
      "## 上一輪 findings 對帳",
      "| 前輪 finding_uid | 問題 | 狀態 | 本輪證據 |",
      "FIXED | STILL_OPEN | STALE",
      "STILL_OPEN",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.report)
    self.assertIn("前輪 finding 對帳", self.command)
    self.assertIn("每個 prior actionable finding_uid", self.command)


if __name__ == "__main__":
  unittest.main()
