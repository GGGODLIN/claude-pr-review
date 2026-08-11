import unittest

from bitbucket_pr_workflow.core import (
  canonical_json_bytes,
  make_finding_uid,
  proposal_sha256,
  unique_batch_id,
  validate_approval,
)


class CoreTests(unittest.TestCase):
  def test_canonical_json_is_key_order_independent(self):
    self.assertEqual(
      canonical_json_bytes({'b': 2, 'a': 1}),
      canonical_json_bytes({'a': 1, 'b': 2}),
    )

  def test_proposal_hash_covers_exact_request_body(self):
    first = {'operations': [{'request_body': {'content': {'raw': 'A'}}}]}
    second = {'operations': [{'request_body': {'content': {'raw': 'B'}}}]}
    self.assertNotEqual(proposal_sha256(first), proposal_sha256(second))

  def test_batch_id_extends_on_prefix_collision(self):
    digest = 'a' * 12 + 'b' * 52
    self.assertEqual(unique_batch_id(digest, {'a' * 12}), 'a' * 12 + 'b')

  def test_finding_uid_preserves_anchor_and_normalizes_root_cause(self):
    first = make_finding_uid('src/a.ts', 'return  value', 'missing   guard')
    same_cause = make_finding_uid('src/a.ts', 'return  value', 'missing guard')
    different_anchor = make_finding_uid('src/a.ts', 'return value', 'missing guard')
    self.assertEqual(first, same_cause)
    self.assertNotEqual(first, different_anchor)

  def test_approval_requires_exact_schema_and_types(self):
    valid = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': 'p1',
      'approved_operation_ids': ['op-1', 'op-2'],
    }
    validate_approval('s1', 'p1', ['op-1', 'op-2'], valid)
    invalid = (
      {**valid, 'user_message_id': 7},
      {**valid, 'approved_operation_ids': 'op-1'},
      {**valid, 'approved_operation_ids': ('op-1', 'op-2')},
      {**valid, 'approved_operation_ids': ['op-1', 2]},
      {**valid, 'approved_operation_ids': ['op-1', 'op-1']},
      {**valid, 'approved': False},
      {**valid, 'session_id': ''},
      {**valid, 'proposal_sha256': ''},
    )
    for approval in invalid:
      with self.subTest(approval=approval):
        with self.assertRaises(ValueError):
          validate_approval('s1', 'p1', ['op-1', 'op-2'], approval)
