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
    end = cls.command.index("Do NOT run domain reviewers", start)
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


if __name__ == "__main__":
  unittest.main()
