import unittest

from bitbucket_pr_workflow.review import (
  compute_review_context,
  render_review_basis,
  render_risk,
  render_testing,
  resolve_finding_action,
)


CURRENT = {
  'source_repo_uuid': '{source-repo}',
  'destination_repo_uuid': '{destination-repo}',
  'source_branch': 'feat/a',
  'destination_branch': 'master',
  'source_sha': 'c' * 40,
  'destination_sha': 'b' * 40,
}


class ReviewTests(unittest.TestCase):
  def test_clean_run_is_attributed_to_sha(self):
    text = render_testing([{
      'command': 'npx next build',
      'status': 'passed',
      'exit_code': 0,
      'repository_uuid': '{repo}',
      'head_before': 'a' * 40,
      'head_after': 'a' * 40,
      'index_clean_before': True,
      'worktree_clean_before': True,
      'index_clean_after': True,
      'worktree_clean_after': True,
      'dirty_patch_sha256': None,
      'observed_at': '2026-07-27T10:00:00+08:00',
      'summary': 'build completed',
      'run_ref': 'tool-1',
      'provenance': 'agent-observed',
    }], [])
    self.assertIn('clean SHA `aaaaaaa`', text)

  def test_missing_exit_result_or_run_ref_downgrades_to_unknown(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': None,
      'run_ref': None,
    }], [])
    self.assertIn('結果未知', text)
    self.assertNotIn('通過，clean SHA', text)

  def test_dirty_run_is_not_attributed_to_clean_sha(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': 0,
      'head_before': 'a' * 40,
      'head_after': 'a' * 40,
      'index_clean_before': True,
      'worktree_clean_before': False,
      'index_clean_after': True,
      'worktree_clean_after': False,
      'dirty_patch_sha256': 'b' * 64,
      'run_ref': 'tool-2',
      'provenance': 'agent-observed',
    }], [])
    self.assertIn('含未提交變更', text)
    self.assertIn('bbbbbbb', text)
    self.assertNotIn('clean SHA', text)

  def test_dirty_run_without_stable_head_downgrades_to_unknown(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': 0,
      'worktree_clean_before': False,
      'index_clean_before': True,
      'worktree_clean_after': False,
      'index_clean_after': True,
      'dirty_patch_sha256': 'b' * 64,
      'run_ref': 'tool-6',
      'provenance': 'agent-observed',
    }], [])
    self.assertIn('結果未知', text)
    self.assertNotIn('含未提交變更', text)

  def test_clean_worktree_with_dirty_patch_downgrades_to_unknown(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': 0,
      'head_before': 'a' * 40,
      'head_after': 'a' * 40,
      'index_clean_before': True,
      'worktree_clean_before': True,
      'index_clean_after': True,
      'worktree_clean_after': True,
      'dirty_patch_sha256': 'b' * 64,
      'run_ref': 'tool-7',
      'provenance': 'agent-observed',
    }], [])
    self.assertIn('結果未知', text)
    self.assertNotIn('clean SHA', text)

  def test_boolean_exit_code_downgrades_to_unknown(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': False,
      'run_ref': 'tool-3',
      'provenance': 'agent-observed',
      'head_before': 'a' * 40,
      'head_after': 'a' * 40,
      'index_clean_before': True,
      'worktree_clean_before': True,
      'index_clean_after': True,
      'worktree_clean_after': True,
    }], [])
    self.assertIn('結果未知', text)

  def test_missing_worktree_evidence_downgrades_to_unknown(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': 0,
      'run_ref': 'tool-4',
      'provenance': 'agent-observed',
    }], [])
    self.assertIn('結果未知', text)
    self.assertNotIn('含未提交變更', text)

  def test_manual_e2e_without_version_says_not_provided(self):
    text = render_testing([], [{
      'scenario': '測試下單',
      'environment': 'Dev store',
      'result': 'passed',
      'evidence': None,
      'reported_by': 'user',
      'reported_at': '2026-07-27T10:05:00+08:00',
      'source_ref': 'user-1',
      'source_summary': '使用者確認通過',
      'tested_source_sha': None,
      'deployment_ref': None,
    }])
    self.assertIn('通過（使用者確認）', text)
    self.assertIn('被測版本：未提供', text)

  def test_manual_e2e_without_source_ref_is_not_claimed(self):
    text = render_testing([], [{
      'scenario': '測試下單',
      'environment': 'Dev store',
      'result': 'passed',
      'reported_by': 'user',
    }])
    self.assertIn('未提供', text)
    self.assertNotIn('通過（使用者確認）', text)

  def test_unverified_manual_e2e_does_not_report_version(self):
    text = render_testing([], [{
      'scenario': '測試下單',
      'environment': 'Dev store',
      'result': 'passed',
      'reported_by': 'user',
      'evidence': 'agent screenshot',
      'tested_source_sha': 'a' * 40,
      'deployment_ref': 'deploy-1',
    }])
    self.assertIn('被測版本：未提供', text)
    self.assertNotIn('agent screenshot', text)
    self.assertNotIn('deploy-1', text)

  def test_invalid_sha_does_not_resolve_snapshot_or_review_basis(self):
    current = dict(CURRENT, source_sha='z' * 40)
    result = compute_review_context(
      reviewed={},
      current=current,
      is_ancestor=lambda _old, _new: None,
      list_new_commits=lambda _old, _new: None,
    )
    text = render_review_basis({
      'status': 'reviewed',
      'input_binding': 'verified',
      'source_sha': 'z' * 40,
    })
    self.assertFalse(result.snapshot_resolved)
    self.assertIn('review input 未驗證', text)

  def test_invalid_sha_does_not_attribute_automated_run(self):
    text = render_testing([{
      'command': 'yarn test',
      'status': 'passed',
      'exit_code': 0,
      'run_ref': 'tool-5',
      'provenance': 'agent-observed',
      'head_before': 'z' * 40,
      'head_after': 'z' * 40,
      'index_clean_before': True,
      'worktree_clean_before': True,
      'index_clean_after': True,
      'worktree_clean_after': True,
    }], [])
    self.assertIn('結果未知', text)
    self.assertNotIn('clean SHA', text)

  def test_risk_unknowns_render_as_unconfirmed(self):
    text = render_risk({
      'possible_impact': '結帳頁',
      'unaffected': None,
      'rollback': None,
    })
    self.assertIn('- 不影響：未確認', text)
    self.assertIn('- 回復方式：未確認', text)

  def test_unverified_input_never_claims_reviewed_sha(self):
    text = render_review_basis({
      'status': 'reviewed',
      'input_binding': 'unverified',
      'source_sha': 'a' * 40,
      'reviewed_at': '2026-07-27',
    })
    self.assertIn('review input 未驗證', text)
    self.assertNotIn('Reviewed SHA', text)

  def test_uncertain_finding_defaults_to_ask_user(self):
    self.assertEqual(resolve_finding_action('auto-fix', True), 'ask-user')
    self.assertEqual(resolve_finding_action(None, False), 'ask-user')

  def test_unknown_ancestry_does_not_unresolve_snapshot(self):
    result = compute_review_context(
      reviewed={'source_sha': 'a' * 40, 'destination_sha': 'b' * 40},
      current=CURRENT,
      is_ancestor=lambda _old, _new: None,
      list_new_commits=lambda _old, _new: None,
    )
    self.assertTrue(result.snapshot_resolved)
    self.assertEqual(result.source_continuity, 'UNKNOWN')
    self.assertTrue(result.review_context_changed)

  def test_new_commits_are_listed_exactly(self):
    result = compute_review_context(
      reviewed={'source_sha': 'a' * 40, 'destination_sha': 'b' * 40},
      current=CURRENT,
      is_ancestor=lambda _old, _new: True,
      list_new_commits=lambda _old, _new: ('c1 fix: first', 'c2 test: second'),
    )
    self.assertEqual(result.source_continuity, 'NEW_COMMITS')
    self.assertEqual(result.new_commits, ('c1 fix: first', 'c2 test: second'))

  def test_destination_change_marks_review_context_changed(self):
    result = compute_review_context(
      reviewed={'source_sha': 'c' * 40, 'destination_sha': 'a' * 40},
      current=CURRENT,
      is_ancestor=lambda _old, _new: None,
      list_new_commits=lambda _old, _new: None,
    )
    self.assertEqual(result.source_continuity, 'CURRENT')
    self.assertTrue(result.base_changed)
    self.assertTrue(result.review_context_changed)

  def test_missing_current_full_sha_blocks_apply_snapshot(self):
    current = dict(CURRENT, source_sha='short')
    result = compute_review_context(
      reviewed={},
      current=current,
      is_ancestor=lambda _old, _new: None,
      list_new_commits=lambda _old, _new: None,
    )
    self.assertFalse(result.snapshot_resolved)
    self.assertEqual(result.source_continuity, 'UNKNOWN')

  def test_non_ancestor_is_history_rewrite_without_commit_list(self):
    result = compute_review_context(
      reviewed={'source_sha': 'a' * 40, 'destination_sha': 'b' * 40},
      current=CURRENT,
      is_ancestor=lambda _old, _new: False,
      list_new_commits=lambda _old, _new: ('must not appear',),
    )
    self.assertEqual(result.source_continuity, 'HISTORY_REWRITE')
    self.assertEqual(result.new_commits, ())
