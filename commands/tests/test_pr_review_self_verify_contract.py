#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewSelfVerifyContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def test_self_verify_runs_once_between_draft_and_projection(self):
    step_six = self.command.index("## Step 6: Output")
    error_handling = self.command.index("## Error Handling", step_six)
    output = self.command[step_six:error_handling]
    draft = output.index("pr-<number>-review.audit.draft.md")
    marker = output.index("skill-verify:pr-review", draft)
    helper = output.index("pr-review-report-projection.py", marker)
    self.assertLess(draft, marker)
    self.assertLess(marker, helper)
    self.assertEqual(1, output.count("skill-verify:pr-review"))
    self.assertEqual(1, output.count("subagent_type: skill-verify-auditor"))
    self.assertNotIn("skill-verify:bitbucket-pr-review", self.command)

  def test_self_verify_is_read_only_and_report_bounded(self):
    self.assertIn("subagent_type: skill-verify-auditor", self.command)
    self.assertIn("不得重新審查 diff、API、Git 或 transcript", self.command)
    self.assertIn("完整證據草稿全文", self.command)
    self.assertIn("固定 rubric 全文", self.command)

  def test_self_verify_rubric_covers_report_proof_obligations(self):
    for marker in (
      "review input 綁定",
      "審查軸狀態",
      "逐檔覆蓋",
      "C4／spec 狀態",
      "finding UID／action",
      "條件式 N-A",
      "失敗軸",
      "沒做的部分",
      "零 finding",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)

  def test_auditor_output_requires_complete_consistent_rubric(self):
    for marker in (
      "必須恰好含 R1–R10 各一行",
      "缺行、重複、順序錯、狀態不合法",
      "FAIL 集合不一致",
      "只有 verdict 無逐條證據",
      "不得只信最後一行",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)

  def test_agent_error_blocks_projection_but_preserves_cleanup(self):
    self.assertIn("Self-Verify: BLOCKED (agent error)", self.command)
    self.assertIn("不得執行投影 helper", self.command)
    self.assertIn("跳至 Step 7 cleanup", self.command)
    self.assertIn("不得消耗 draft", self.command)
    blocked = self.command.index("Self-Verify: BLOCKED (agent error)")
    helper = self.command.index("pr-review-report-projection.py", blocked)
    cleanup = self.command.index("## Step 7:", helper)
    self.assertLess(blocked, helper)
    self.assertLess(helper, cleanup)

  def test_failures_are_repaired_without_claiming_reverification(self):
    self.assertIn("有執行證據就補寫", self.command)
    self.assertIn("沒有執行證據就補跑", self.command)
    self.assertIn("修正後不重派 auditor", self.command)
    self.assertIn("未經第二次獨立稽查", self.command)


if __name__ == "__main__":
  unittest.main()
