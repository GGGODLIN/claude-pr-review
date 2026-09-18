#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewGroupReportContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start_marker, end_marker):
    start = self.command.index(start_marker)
    end = self.command.index(end_marker, start)
    return self.command[start:end]

  def group_step(self):
    return self.section("## Step 4.8", "## Step 5: Compile Comparison Report")

  def test_group_post_processing_sits_between_recheck_and_report(self):
    for earlier, later in (
      ("## Step 4.7: Recheck Prior Review Findings", "## Step 4.8"),
      ("## Step 4.8", "## Step 5: Compile Comparison Report"),
    ):
      with self.subTest(earlier=earlier, later=later):
        self.assertLess(self.command.index(earlier), self.command.index(later))
    self.assertIn("完成後接 Step 4.8", self.section("## Step 4.7", "## Step 4.8"))

  def test_group_step_is_multi_target_only_and_keeps_single_pr_path(self):
    step = self.group_step()
    self.assertIn("多目標才跑", step)
    self.assertIn("單輸入沿 Step 4.7 直接進 Step 5", step)
    self.assertIn("報告格式與 UID 一律不變", step)
    self.assertIn("pr-review-group-report.py reconcile", step)
    self.assertIn('"$PREPARATION_PATH"', self.section("### 2.98.1", "## Step 3: Dispatch Dual Reviews"))

  def test_group_step_routes_existing_rules_and_validates_spec_results(self):
    step = self.group_step()
    self.assertIn("CC／非 CC、consensus、strict-liability 與獨立複查規則分流", step)
    self.assertIn("不按 repo 複製整套", step)
    self.assertIn("正式規格（C4）結果仍先經 reducer 核驗", step)

  def test_group_step_binds_identity_versions_and_history(self):
    step = self.group_step()
    self.assertIn("去重帶目標身分", step)
    self.assertIn("不同 target 即使同 file／line／anchor／root cause 也保持各自一條", step)
    self.assertIn("failure_chain_id", step)
    self.assertIn("perspectives", step)
    for field in (
      "verification_verdict",
      "verification_evidence",
      "original_severity",
      "corrected_severity",
      "severity_reason",
    ):
      with self.subTest(field=field):
        self.assertIn(field, step)
    self.assertIn("F-xx finding_uid", step)
    self.assertIn("group_id", step)
    self.assertIn("逐位置 finding_uid 在既有 20-hex 輸入前加上 canonical 目標身分", step)
    self.assertIn("byte-for-byte 不變", step)
    self.assertIn("**Report projection schema**: 3", step)
    self.assertIn("schema 不是 3", step)
    self.assertIn("pr-review-report-projection.py", step)
    self.assertIn("不得直接寫任何 final path", step)
    self.assertIn("不得進任何 fresh reviewer prompt", step)
    self.assertIn("預設 `STALE`", step)
    self.assertIn("不自動擴大目標清單", step)
    self.assertIn("漂移只更新 `target_versions`", step)
    self.assertIn("completeness=INCOMPLETE", step)

  def test_group_step_grants_no_write_authority_and_scopes_cleanup(self):
    step = self.group_step()
    self.assertIn("grants` 一律 `modify_code=false`", step)
    self.assertIn("comment_on_prs=false", step)
    self.assertIn("multi_target_fanout=false", step)
    self.assertIn("pr-review-targets.py cancel", step)
    self.assertIn("保留 `review_root` 給 Step 7", step)
    self.assertIn("pr-review-group-report.py cleanup", step)
    self.assertIn("不刪 keep 清單指定保留的檔", step)
    self.assertIn("其餘本輪材料可清", step)
    self.assertIn("CLEANUP_ROOT_OUT_OF_SCOPE", step)
    self.assertIn("MATERIALS_PARTIALLY_RETAINED", step)
    self.assertIn("其他 run 的目錄一律不動", step)


if __name__ == "__main__":
  unittest.main()
