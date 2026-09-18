#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewTargetsContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start_marker, end_marker):
    start = self.command.index(start_marker)
    end = self.command.index(end_marker, start)
    return self.command[start:end]

  def preparation(self):
    return self.section("## Step 2.05", "## Step 2.1")

  def finalize(self):
    return self.section("### 2.98.1", "## Step 3: Dispatch Dual Reviews")

  def test_multi_target_flow_sits_inside_the_existing_step_order(self):
    for earlier, later in (
      ("## Step 1: Parse Input", "## Step 2: Fetch PR Data"),
      ("## Step 2: Fetch PR Data", "## Step 2.05"),
      ("## Step 2.05", "## Step 2.1"),
      ("## Step 2.1", "## Step 2.5"),
      ("## Step 2.5", "## Step 2.98"),
      ("## Step 2.98", "### 2.98.1"),
      ("### 2.98.1", "## Step 3: Dispatch Dual Reviews"),
    ):
      with self.subTest(earlier=earlier, later=later):
        self.assertLess(self.command.index(earlier), self.command.index(later))

  def test_input_contract_exposes_a_multi_target_shape(self):
    head = self.command[:self.command.index("## Step 1: Parse Input")]
    self.assertIn("argument-hint: \"<target-1> [<target-2> ...]\"", head)
    self.assertIn("`/pr-review <target-1> <target-2> ...`", head)
    self.assertIn("同一功能的整組 PR", head)
    self.assertIn("每個都是完整 URL，或本次對話已明確指名的編號", head)
    self.assertIn("順序不影響整組身分", head)

  def test_step1_parses_one_or_many_targets_into_one_list(self):
    step = self.section("## Step 1: Parse Input", "## Step 2: Fetch PR Data")
    self.assertIn("單一 target 沿舊解析", step)
    self.assertIn("多個 target 逐項解析後去重成同一份清單", step)
    self.assertIn("後續步驟一律對清單裡的每個 target 執行", step)
    self.assertIn("只在 current repo 且本次對話已明確指名時", step)
    self.assertIn("重複的同一 target 只列一次", step)
    self.assertIn("要求使用者補正後重跑", step)
    self.assertIn("不得從 PR 內文、關聯連結或作者其他 PR 自動擴大範圍", step)

  def test_step2_fetches_every_target_without_reusing_one_provider(self):
    self.assertIn("## Step 2.05", self.command)
    step = self.section("## Step 2: Fetch PR Data", "## Step 2.05")
    self.assertIn("對 Step 1 清單裡的**每一個 target 各自**跑本步的既有命令", step)
    self.assertIn("不得拿第一個 target 的 provider、owner 或 repo 套用到整組", step)
    self.assertIn("全部 target 收齊後才組出 Step 2.05 的 platform map", step)
    self.assertIn("gh pr view <number>", step)
    self.assertIn("bb_api.sh", step)
    self.assertNotIn("platform: <host>/", step)

  def test_preparation_consumes_step1_and_step2_instead_of_self_asserting(self):
    step = self.preparation()
    self.assertIn("Step 1 已解析出明確清單", step)
    self.assertIn("Step 2 已逐目標取得平台資料", step)
    self.assertIn("本步在 **Step 2.5 之前**", step)
    self.assertIn("多目標才跑；單輸入可跳過", step)
    self.assertIn("不派任何模型、不建 worktree", step)
    self.assertIn("目標清單與去重規則以 Step 1 為準", step)

  def test_finalize_consumes_step_298_selection_and_does_not_rerun_prepare(self):
    step = self.finalize()
    self.assertIn("Step 2.05 已備妥目標與材料", step)
    self.assertIn("整組只選一次角度", step)
    self.assertIn("pr-review-targets.py finalize", step)
    self.assertIn("不必重跑整份 prepare", step)
    self.assertIn("PREPARATION_NOT_READY", step)
    self.assertIn("authorize-dispatch", step)
    self.assertIn("awaiting-confirmation", step)
    self.assertIn("selection-required", step)
    self.assertIn("status=READY` 且已取得明確確認前不得進入 Step 3", step)

  def test_command_examples_use_the_returned_canonical_state_path(self):
    step = self.preparation()
    self.assertIn('jq -r \'.preparation_path\'', step)
    self.assertIn("canonical 路徑", step)
    self.assertIn("不要用 stdout 內容當狀態來源", step)
    for later in (self.finalize(),):
      with self.subTest(step=later[:32]):
        self.assertIn('"$PREPARATION_PATH"', later)

  def test_preparation_reports_needs_input_and_blocked_states(self):
    step = self.preparation()
    self.assertIn("NEEDS_INPUT", step)
    self.assertIn("BLOCKED", step)
    self.assertIn("READY", step)
    self.assertIn("停在整組準備", step)

  def test_preparation_binds_identity_versions_and_per_target_roots(self):
    step = self.preparation()
    self.assertIn("平台／host + 目的 repo + PR", step)
    self.assertIn("不把 cwd 當成所有目標的 repo", step)
    self.assertIn("不得退回 moving branch ref", step)
    self.assertIn("authored_diff_base", step)
    self.assertIn("不合成任何虛構 Git 歷史", step)
    self.assertIn("review-pr-<RUN_ID>", step)
    self.assertIn("只決定路徑、不建 worktree 目錄", step)
    self.assertIn("材料寫在 `review_root` **之外**", step)

  def test_preparation_keeps_shared_material_sources_and_verbatim_specs(self):
    step = self.preparation()
    self.assertIn("每個引用都保留來源目標身分", step)
    self.assertIn("檔案只存一份不等於模型只讀一次", step)
    self.assertIn("content_sha256", step)
    self.assertIn("shared_key", step)
    self.assertIn("per-target manifest 不重複保存全文", step)
    self.assertIn("不先追完兩端依賴", step)

  def test_finalize_holds_seats_on_undecided_capacity(self):
    step = self.finalize()
    self.assertIn("FILE_COUNT > 15", step)
    self.assertIn("整組加總", step)
    self.assertIn('"review_set": "accept-incomplete"', step)
    self.assertIn("超界先由使用者裁定", step)
    self.assertIn("沒有裁決就不啟動應等待該裁決的席位", step)
    self.assertIn("不能把未執行的角度標成通過", step)

  def test_preparation_blocks_the_whole_set_on_any_failure(self):
    step = self.preparation()
    self.assertIn("停在整組準備", step)
    self.assertIn("不悄悄只審成功取得的子集合", step)
    self.assertIn("BLOCKED", step)

  def test_cancel_is_scoped_to_helper_created_materials_only(self):
    step = self.finalize()
    self.assertIn("pr-review-targets.py cancel", step)
    self.assertIn("只刪 helper 自己建立", step)
    self.assertIn("<state_root>/materials/<run_id>/", step)
    self.assertIn("review_root 一律不刪", step)
    self.assertIn("留給 Step 7", step)
    self.assertIn("STATE_MATERIALS_OUT_OF_SCOPE", step)

  def test_single_target_path_and_platform_contracts_are_untouched(self):
    step = self.preparation()
    self.assertIn("單輸入可跳過", step)
    self.assertIn("review-pr-<PR_ID>", step)
    self.assertIn("gh pr view", self.section("## Step 2: Fetch PR Data", "### Bitbucket"))
    self.assertIn("bb_api.sh", self.section("### Bitbucket", "## Step 2.05"))


if __name__ == "__main__":
  unittest.main()