#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewCcGroupContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start, end):
    left = self.command.index(start)
    right = self.command.index(end, left)
    return self.command[left:right]

  def test_step_25_creates_each_bound_target_worktree(self):
    step = self.section("## Step 2.5:", "## Step 2.52:")
    self.assertIn("多目標逐一建立 worktree", step)
    self.assertIn("$PREPARATION_PATH", step)
    self.assertIn("target.review_root", step)
    self.assertIn("target.head", step)
    self.assertIn("target.base_ref", step)
    self.assertIn("worktree add --detach", step)
    self.assertIn("單輸入才使用", step)

  def test_step_3_builds_one_cc_dispatch_per_selected_angle(self):
    step = self.section("## Step 3:", "### Codex Review")
    self.assertIn("pr-review-cc-group-flow.py plan", step)
    self.assertIn("每個 selected CC 視角恰一筆", step)
    self.assertIn("不按 target 或 repo 複製", step)
    self.assertIn("file ownership", step)
    self.assertIn("只派 selected", step)
    self.assertIn("context-aware primary", step)
    self.assertIn("security", step)
    self.assertIn("非 CC cell", step)

  def test_step_45_shares_one_repair_round_with_new_interfaces(self):
    step = self.section("## Step 4.5:", "## Step 4.6:")
    self.assertIn("new_interface", step)
    self.assertIn("pr-review-cc-group-flow.py reduce", step)
    self.assertIn("MISSED", step)
    self.assertIn("共用同一輪", step)
    self.assertIn("不重置 repair 額度", step)
    self.assertIn("completed", step)
    self.assertIn("unverified", step)
    self.assertIn("不新增整合席", step)

  def test_command_preserves_single_target_path_and_registers_exam(self):
    step = self.section("## Step 2.5:", "## Step 2.52:")
    self.assertIn("單輸入沿用原流程", step)
    head = self.command[:self.command.index("## Step 1: Parse Input")]
    self.assertIn("test_pr_review_cc_group_contract.py", head)
    self.assertIn("十一套都要跑", head)


if __name__ == "__main__":
  unittest.main()
