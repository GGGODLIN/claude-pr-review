#!/usr/bin/env python3

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND_PATH = Path(os.environ.get("PR_REVIEW_COMMAND_PATH", ROOT / "commands/pr-review.md"))
AGENT_PATH = Path(os.environ.get("SPEC_COMPLIANCE_AGENT_PATH", ROOT / "agents/spec-compliance-reviewer.md"))


class PrReviewC4DispatchContractTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.command = COMMAND_PATH.read_text()
    cls.agent = AGENT_PATH.read_text()
    start = cls.command.index("- **`spec-compliance-reviewer`**")
    end = cls.command.index("### Codex Review", start)
    cls.c4 = cls.command[start:end]

  def test_c4_dispatch_uses_one_deterministic_envelope(self):
    self.assertIn("pr-review-c4.py dispatch-envelope", self.c4)
    self.assertIn("single deterministic dispatch envelope", self.c4)
    self.assertIn("four fields copied byte-for-byte from `envelope.agent`", self.c4)
    self.assertIn("envelope.runtime_input", self.c4)
    self.assertIn("single-use permit", self.c4)
    self.assertIn("consumes the current session's permit exactly once", self.c4)
    self.assertIn("field set is exactly those four keys", self.c4)
    self.assertIn("do not add `resume`, `run_in_background`, `isolation`", self.c4)

  def test_main_session_has_no_prompt_or_schema_authoring_step(self):
    self.assertIn("does not write, append, summarize, or reinterpret", self.c4)
    self.assertNotIn("Copy that two-line stdout block verbatim", self.c4)
    self.assertNotIn("Instruct each finding's", self.c4)
    self.assertNotIn("Mark all packet text", self.c4)

  def test_runtime_input_reuses_envelope_packet_identity(self):
    self.assertIn("do not reconstruct its packet, dispatch ID, packet hash, prompt hash, model, or effort fields", self.c4)
    self.assertIn("complete text SHA-256 equals `envelope.runtime_input.prompt_sha256`", self.c4)

  def test_agent_contract_matches_envelope_shape(self):
    for marker in (
      "Do not use tools",
      "Return exactly one JSON object",
      '"contract_accounting"',
      '"findings"',
      '"spec_file_accounting"',
      '"summary"',
      '"errors"',
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.agent)
    self.assertNotIn('"observations"', self.agent)

  def test_web_gpt_axis_collects_the_specified_conversation(self):
    axis_line = next(line for line in self.command.splitlines() if line.startswith("**加選軸"))
    collector = axis_line.split("再用 `", 1)[1].split("` 收", 1)[0]
    self.assertEqual(
      "opencli chatgpt detail <id> --markdown true -f json",
      collector,
    )
    self.assertNotIn("opencli chatgpt read --conversation", axis_line)

  def test_web_gpt_axis_never_waits_on_detail_stabilization(self):
    axis_line = next(line for line in self.command.splitlines() if line.startswith("**加選軸"))
    collector = axis_line.split("再用 `", 1)[1].split("` 收", 1)[0]
    self.assertNotIn("--wait true", collector)
    self.assertNotIn("--stable", collector)
    self.assertIn("不要對 detail 用 `--wait true`", axis_line)

  def test_multi_target_packet_is_assembled_from_step_205_state(self):
    for marker in (
      "Step 2.05 preparation state",
      "one `dispatch_id` and one dispatch for the whole set",
      "not multiplied by the number of targets",
      "never enter the reviewer packet",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.c4)

  def test_multi_target_bindings_must_reference_declared_targets(self):
    for marker in (
      "naming one of the targets declared",
      "canonical `target_identity`",
      "per-target",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.c4)

  def test_multi_target_validate_consumes_targets_map_per_target(self):
    for marker in (
      "`targets` map",
      "that target's own `review_root`, `authored_diff_base`, and `review_head`",
      "fails closed",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.c4)

  def test_c4_admission_stamps_target_identity_before_group_routing(self):
    for marker in (
      "exactly one authored code binding target",
      "`C4_CODE_TARGET_AMBIGUOUS`",
      "`group_findings`",
      "`reducer_validated`",
      "main forwards this projection unchanged",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.c4)

  def test_gate_reads_each_target_root_from_preparation_state(self):
    start = self.command.index("## Step 2.65")
    end = self.command.index("## Step 2.7", start)
    gate = self.command[start:end]
    for marker in (
      "$PREPARATION_PATH",
      "canonical `target_identity`",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, gate)

  def test_agent_input_contract_describes_target_qualified_packet(self):
    self.assertIn("a `target_identity` field on every clause", self.agent)
    self.assertIn("mixed qualified／unqualified entries are invalid", self.agent)
    self.assertIn("declared targets", self.agent)
    self.assertIn("its own target's Git context", self.agent)
    self.assertIn("Do not use tools", self.agent)
    for marker in (
      "REVIEW_SELECTION",
      "模型／執行路徑為列、審查角度為欄",
      "初次 reviewer 席位",
      "selected | not-selected | cancelled | unavailable | needs-material",
      "推薦不等於必跑",
      "typescript-reviewer",
      "python-reviewer",
      "code-reviewer",
      "security-reviewer",
      "spec-compliance-reviewer",
      "Codex 中性",
      "Codex 對抗",
      "Gemini Flash",
      "Gemini Pro",
      "web GPT Pro",
      "Formal spec gate",
      "React-doctor",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)

  def test_dispatch_uses_only_selected_seats_without_hidden_primary_defaults(self):
    dispatch_start = self.command.index("## Step 3: Dispatch Dual Reviews")
    dispatch_end = self.command.index("## Step 4: Cross-Axis Verification Pass", dispatch_start)
    dispatch = self.command[dispatch_start:dispatch_end]
    for marker in (
      "只對 `REVIEW_SELECTION` 中 `status=selected` 的席位派工",
      "未選定或 cancelled 的席位不得派工",
      "不把 preset、語言判定或條件式 trigger 當成使用者選擇",
      "原本的 primary reviewer 也必須明確選定",
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, dispatch)
    self.assertNotIn("preset 一律含對抗", dispatch)


if __name__ == "__main__":
  unittest.main()
