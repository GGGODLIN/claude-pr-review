#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewGeminiWebContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start_marker, end_marker):
    start = self.command.index(start_marker)
    end = self.command.index(end_marker, start)
    return self.command[start:end]

  def gemini_multi(self):
    return self.section("#### 多目標整組材料", "#### web GPT Pro 多目標整組材料")

  def web_multi(self):
    return self.section("#### web GPT Pro 多目標整組材料", "## Step 4: Cross-Axis Verification Pass")

  def test_multi_target_sections_live_inside_the_gemini_block_before_step4(self):
    gemini_block = self.section("### Gemini Pro / Flash Review", "## Step 4: Cross-Axis Verification Pass")
    self.assertIn("#### 多目標整組材料", gemini_block)
    self.assertIn("#### web GPT Pro 多目標整組材料", gemini_block)
    self.assertLess(
      self.command.index("#### 多目標整組材料"),
      self.command.index("## Step 4: Cross-Axis Verification Pass"),
    )

  def test_gemini_multi_target_keeps_one_execution_with_every_root_and_version(self):
    step = self.gemini_multi()
    self.assertIn("多目標才跑；單輸入沿上方既有 Gemini 段", step)
    self.assertIn("一次 `agy` 執行取得整組", step)
    self.assertIn("**每一個目標**的 `review_root`", step)
    self.assertIn("authored_diff_base", step)
    self.assertIn("不按 PR 或 repo 數量拆成多次完整審查", step)
    self.assertIn("scripts/pr-review-gemini-web.py materials", step)

  def test_gemini_multi_target_keeps_selected_role_and_model(self):
    step = self.gemini_multi()
    self.assertIn("只保留該 cell 的 selected 角色、requested model", step)
    self.assertIn("Gemini 3.1 Pro (High)", step)
    self.assertIn("$GEMINI_FLASH_MODEL", step)
    self.assertIn("不新增通道、不偷換模型", step)

  def test_capacity_gate_holds_dispatch_before_adjudication(self):
    step = self.gemini_multi()
    self.assertIn("**容量裁決閘**", step)
    self.assertIn("NEEDS_DECISION", step)
    self.assertIn("executor 呼叫 0", step)
    self.assertIn("不截斷材料、不把 skipped 算通過", step)
    self.assertIn("NOT_DISPATCHABLE", step)
    self.assertIn("先過閘、後構材料", step)

  def test_dispatch_gate_reuses_target_authorize_decision(self):
    step = self.gemini_multi()
    self.assertIn("dispatch_decision", step)
    self.assertIn("selection 確實含對應 seat+angle", step)
    self.assertIn("confirmation", step)
    self.assertIn("seat-not-selected", step)
    self.assertIn("awaiting-confirmation", step)
    self.assertIn("selection-required", step)

  def test_gemini_argv_matches_existing_equal_bound_shape(self):
    step = self.gemini_multi()
    self.assertIn("--print=<prompt 全文>", step)
    self.assertIn("packet 是唯一持久 receipt", step)
    self.assertIn("完整 prompt 保留在 packet 的 execution", step)
    self.assertIn("不另寫 `*-prompt.txt`", step)
    self.assertIn("一組兩個 argv（`--add-dir`，`<review_root>`）", step)
    self.assertIn("--model=", step)

  def test_flash_model_comes_from_routing_not_invention(self):
    step = self.gemini_multi()
    self.assertIn("MODEL_UNAVAILABLE", step)
    self.assertIn("$GEMINI_FLASH_MODEL", step)
    self.assertIn("model-routing.sh", step)

  def test_web_fetch_uses_detail_with_conversation_id_placeholder(self):
    step = self.web_multi()
    self.assertIn("detail <id> --markdown true -f json", step)

  def test_head_marker_and_exam_list_register_the_ninth_exam(self):
    head = self.command[:self.command.index("## Step 1: Parse Input")]
    self.assertIn("test_pr_review_gemini_web_contract.py", head)
    self.assertGreaterEqual(head.count("test_pr_review_gemini_web_contract.py"), 3)

  def test_ambiguous_locations_are_never_guessed(self):
    step = self.gemini_multi()
    self.assertIn("**定位回映**", step)
    self.assertIn("不猜 PR", step)
    self.assertIn("unmapped_findings", step)
    self.assertIn("web 通道 findings 缺 target label 同樣標 unmapped", step)

  def test_fixed_failure_kinds_stay_unverified_and_other_channels_survive(self):
    step = self.gemini_multi()
    self.assertIn("**失敗處置**", step)
    self.assertIn("model-mismatch", step)
    self.assertIn("timeout", step)
    self.assertIn("parse-failure", step)
    self.assertIn("unverified", step)
    self.assertIn("其他 selected seat 的有效結果照常進 Step 4／4.8", step)
    self.assertIn("reported_model", step)
    self.assertIn("不冒稱 runtime 模型驗證", step)

  def test_gemini_findings_flow_into_the_common_group_report(self):
    step = self.gemini_multi()
    self.assertIn("**共同報告**", step)
    self.assertIn("source: gemini", step)
    self.assertIn("Step 4.8", step)
    self.assertIn("不得以 CC 或其他通道結果代替", step)
    self.assertIn("未實跑的通道明列未驗", step)

  def test_web_multi_target_sends_one_labeled_full_text_diff_send(self):
    step = self.web_multi()
    self.assertIn("已選角度只送一次", step)
    self.assertIn("--new --wait false --window background -f json", step)
    self.assertIn("detail <id> --markdown true -f json", step)
    self.assertIn("不帶 `--wait true`", step)

  def test_web_multi_target_never_assumes_local_paths(self):
    step = self.web_multi()
    self.assertIn("不假設它可讀本機路徑", step)
    self.assertIn("有 target 標籤的完整文字差異", step)
    self.assertIn("### Target <target_identity>", step)
    self.assertIn("必填 `target` 欄", step)
    self.assertIn("不傳 `$REVIEW_ROOT` 絕對路徑、不用 `--add-dir`", step)

  def test_web_channel_keeps_existing_failure_and_report_contract(self):
    step = self.web_multi()
    self.assertIn("channel `web-gpt`", step)
    self.assertIn("派工閘、定位回映、失敗處置與共同報告規則同上段", step)
    self.assertIn("source: web", step)
    self.assertIn("[web-Pro]", step)
    self.assertIn("不與 Codex 軸合併", step)
    self.assertIn("INCOMPATIBLE_INTERFACE", step)
    self.assertIn("不自動換執行路徑、不偷換模型", step)

  def test_head_contract_note_lists_the_ninth_exam(self):
    head = self.command[:self.command.index("## Step 1: Parse Input")]
    self.assertIn("test_pr_review_gemini_web_contract.py", head)
    self.assertIn("十一套都要跑", head)


if __name__ == "__main__":
  unittest.main()
