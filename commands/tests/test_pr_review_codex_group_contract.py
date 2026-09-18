#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))


class PrReviewCodexGroupContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()

  def section(self, start_marker, end_marker):
    start = self.command.index(start_marker)
    end = self.command.index(end_marker, start)
    return self.command[start:end]

  def neutral_multi(self):
    return self.section("#### 多目標整組材料（中性軸）", "#### 多目標整組材料（對抗軸）")

  def adversarial_multi(self):
    return self.section("#### 多目標整組材料（對抗軸）", "### Gemini Pro / Flash Review")

  def test_multi_target_branches_live_inside_the_codex_block_before_step4(self):
    codex_block = self.section("### Codex Review", "### Gemini Pro / Flash Review")
    self.assertIn("#### 多目標整組材料（中性軸）", codex_block)
    self.assertIn("#### 多目標整組材料（對抗軸）", codex_block)
    self.assertLess(
      self.command.index("#### 多目標整組材料（中性軸）"),
      self.command.index("## Step 4: Cross-Axis Verification Pass"),
    )

  def test_neutral_multi_target_keeps_one_native_custom_execution(self):
    step = self.neutral_multi()
    self.assertIn("多目標才跑；單輸入沿上方既有 Codex 段原樣執行", step)
    self.assertIn("codex exec -C \"$RUN_ROOT\" -s read-only --json review --skip-git-repo-check", step)
    self.assertIn("-m \"$CODEX_MODEL\"", step)
    self.assertIn("model_reasoning_effort", step)
    self.assertIn('-c "model_reasoning_effort=\\"$CODEX_NEUTRAL_EFFORT\\"" -', step)
    self.assertIn("整組 instructions 走 stdin", step)
    self.assertIn("絕不帶 `--base`", step)
    self.assertIn("`--commit`", step)
    self.assertIn("`--uncommitted`", step)
    self.assertIn("恰好一次", step)
    self.assertIn("不按 PR 或 repo 數量拆成多次完整審查", step)
    self.assertIn("pr-review-codex-set.mjs", step)
    self.assertIn("materializeTargetDiffs", step)
    self.assertIn("git diff", step)
    self.assertIn("buildNativeCustomInstructions", step)
    self.assertIn("target_identity", step)
    self.assertIn("authored_diff_base", step)

  def test_neutral_multi_target_keeps_the_native_review_task_not_rescue(self):
    step = self.neutral_multi()
    self.assertIn("保留原生 review task／rubric 與輸出契約", step)
    self.assertIn("不改成泛用 `codex task`／rescue", step)

  def test_adversarial_multi_target_reuses_the_installed_plugin_verbatim(self):
    step = self.adversarial_multi()
    self.assertIn("多目標才跑；單輸入沿上方既有對抗軸命令字串", step)
    self.assertIn("${CODEX_PLUGIN_DIR%/}/prompts/adversarial-review.md", step)
    self.assertIn("${CODEX_PLUGIN_DIR%/}/schemas/review-output.schema.json", step)
    self.assertIn("interpolateTemplate", step)
    self.assertIn("runAppServerTurn", step)
    self.assertIn("parseStructuredOutput", step)
    self.assertIn("readOutputSchema", step)
    self.assertIn("只替換 `REVIEW_INPUT`／target labels", step)
    self.assertIn("不複製或改寫 plugin bytes", step)
    self.assertIn("sandbox read-only", step)
    self.assertIn("選定 model／effort", step)
    self.assertIn("不編輯 plugin cache", step)
    self.assertIn("不新增 provider adapter", step)
    self.assertIn("buildAdversarialReviewInput", step)
    self.assertIn("runAdversarialAxis", step)
    self.assertIn("pluginDir=CODEX_PLUGIN_DIR", step)
    self.assertIn("只由 command 解析一次", step)

  def test_both_axes_gate_before_dispatch_and_report_contract_mismatch(self):
    for name, step in (("neutral", self.neutral_multi()), ("adversarial", self.adversarial_multi())):
      with self.subTest(axis=name):
        self.assertIn("派工前", step)
    self.assertIn("NEEDS_DECISION", self.neutral_multi())
    self.assertIn("executor 呼叫 0", self.neutral_multi())
    self.assertIn("不截斷材料", self.neutral_multi())
    self.assertIn("不把未執行的角度算通過", self.neutral_multi())
    self.assertIn("BLOCKED", self.neutral_multi())
    self.assertIn("不悄悄換角色", self.adversarial_multi())

  def test_target_qualified_mapping_never_guesses(self):
    step = self.neutral_multi()
    self.assertIn("恰好一個", step)
    self.assertIn("`review_root`", step)
    self.assertIn("同 head 不同 PR", step)
    self.assertIn("同名檔", step)
    self.assertIn("刪除／rename 舊側", step)
    self.assertIn("target label", step)
    self.assertIn("old path", step)
    self.assertIn("不唯一就標未定位", step)

  def test_axis_failure_stays_local_and_never_reruns_per_repo(self):
    step = self.adversarial_multi()
    self.assertIn("parse failure／partial failure 明報", step.lower())
    self.assertIn("不按 target 重跑", step)
    self.assertIn("保留其他已選軸的有效結果", step)
    self.assertIn("不換 role／model", step)
    self.assertIn("不按 repo 重跑", step)

  def test_results_feed_the_group_report_and_single_pr_path_is_frozen(self):
    step = self.adversarial_multi()
    self.assertIn("toGroupFindings", step)
    self.assertIn("pr-review-group-report.py", step)
    self.assertIn("`source: codex`", step)
    self.assertIn("不冒稱 runtime 模型驗證", step)
    self.assertIn("單 PR 接法與命令字串不變", step)

  def test_native_review_no_longer_claims_the_adversarial_schema(self):
    neutral = self.section("### Codex Review", "### Codex: intentionally kept diff-only")
    self.assertNotIn("review-output.schema.json", neutral)
    self.assertIn("review-output.schema.json", self.adversarial_multi())

  def test_header_registers_the_new_contract(self):
    head = self.command[:self.command.index("# PR Review")]
    self.assertIn("test_pr_review_codex_group_contract.py", head)


if __name__ == "__main__":
  unittest.main()
