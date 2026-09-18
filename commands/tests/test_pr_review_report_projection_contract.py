#!/usr/bin/env python3

import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewReportProjectionContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()
    start = cls.command.index("### Report Structure")
    opening = cls.command.index("````markdown", start)
    closing = cls.command.index("\n````", opening + len("````markdown"))
    cls.report_structure = cls.command[opening:closing]

  def test_step_five_defines_audit_as_canonical_and_main_as_projection(self):
    self.assertIn("拍板主報告＋完整證據副檔", self.command)
    self.assertIn("完整證據副檔是 Step 5 完整報告的 canonical copy", self.command)
    self.assertIn("**Report projection schema**: 2", self.report_structure)
    self.assertIn("**Report generation**: sha256:<64-hex>", self.command)
    self.assertIn("F-01 finding_uid: <20-hex> action=<action>", self.report_structure)
    self.assertIn("inline=none", self.report_structure)
    self.assertIn("主報告只能由 deterministic projection helper 產生", self.command)
    self.assertIn("主報告保留 header（含 coverage／C4／axis state）", self.command)
    self.assertIn("Spec 依據完整內容只留在完整證據副檔", self.command)
    self.assertIn("不得新增模型呼叫", self.command)

  def test_step_six_transactionally_publishes_audit_and_main_from_draft(self):
    self.assertIn("唯一權威來源", self.command)
    self.assertIn("pr-<number>-review.audit.draft.md", self.command)
    self.assertIn("pr-<number>-review.audit.md", self.command)
    self.assertIn("pr-review-report-projection.py", self.command)
    step_six = self.command.index("## Step 6: Output")
    draft = self.command.index("pr-<number>-review.audit.draft.md", step_six)
    helper = self.command.index("pr-review-report-projection.py", draft)
    audit = self.command.index("pr-<number>-review.audit.md", helper)
    main = self.command.index("pr-<number>-review.md", audit)
    self.assertLess(draft, helper)
    self.assertLess(helper, audit)
    self.assertLess(audit, main)

  def test_no_op_comment_payloads_stay_in_audit_only(self):
    self.assertIn("`action=no-op` 的 inline-comment block 只留在完整證據副檔", self.command)
    self.assertIn("發現總覽仍保留所有 finding", self.command)

  def test_fable_shadow_trial_wiring_is_retired(self):
    self.assertIn("Model comparison trials do not run inside `/pr-review`", self.command)
    self.assertNotIn("#### 🔬 Fable 影子對照", self.command)
    self.assertNotIn("## Fable/opus 影子對照", self.command)
    self.assertNotIn("fable-shadow-ledger.jsonl", self.command)
    self.assertNotIn("skipped-chunked", self.command)
    self.assertNotRegex(self.command, re.compile(r'["\']?model["\']?\s*:\s*["\']?fable["\']?', re.IGNORECASE))

  def test_report_is_projected_from_the_selected_set(self):
    for marker in (
      "只投影 `REVIEW_SELECTION` 中 `status=selected` 的席位",
      "cancelled、not-selected、unavailable、needs-material 不得列為 PASS 或必須完成",
      "失敗席位保留 `FAILED`，不得改寫成 PASS、成功或另一個模型",
      "只有選定的初次模型／角度才進入審查工具、Reviewer models 與發現總覽欄",
      "selected set",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)

  def test_report_preserves_conditional_qualification_and_single_seat_state(self):
    for marker in (
      "Formal spec gate 的 `gate=ELIGIBLE` 與 evidence requirements",
      "所有初次結果仍須既有查證與 Main 裁決",
      "選定單席就以單席報告",
      "取消初次 reviewer 不算未完成，也不算已完成",
      "模型識別未知就寫 `UNAVAILABLE`",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)


if __name__ == "__main__":
  unittest.main()
