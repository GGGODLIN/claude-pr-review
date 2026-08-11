import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bitbucket_pr_workflow.core import proposal_sha256
from bitbucket_pr_workflow.executor import preview_existing_pr
from test_support import FakeClient


SCRIPT = Path(__file__).resolve().parents[1] / 'bitbucket_pr_workflow.py'
MANAGED_DESCRIPTION = '<!-- pr-review-testing:start -->\n## Testing\nnone\n<!-- pr-review-testing:end -->'


def run_cli(*args, env=None, text=True):
  with tempfile.TemporaryDirectory() as home:
    process_env = {'HOME': home, 'PATH': os.environ['PATH']}
    process_env.update(env or {})
    return subprocess.run(
      ['python3', str(SCRIPT), *args],
      capture_output=True,
      text=text,
      env=process_env,
    )


def credential_env():
  return {
    'BITBUCKET_API_USERNAME': 'loopback-test-user',
    'BITBUCKET_API_TOKEN': 'loopback-test-token',
  }


class CountingClient(FakeClient):
  def __init__(self):
    super().__init__()
    self.get_count = 0

  def get_user(self):
    self.get_count += 1
    return super().get_user()

  def get_repository(self, workspace, repo):
    self.get_count += 1
    return super().get_repository(workspace, repo)

  def get_pr(self, workspace, repo, pr_id):
    self.get_count += 1
    return super().get_pr(workspace, repo, pr_id)


def load_cli():
  spec = importlib.util.spec_from_file_location('bitbucket_pr_workflow_cli', SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def run_main(module, *args):
  stdout = io.StringIO()
  stderr = io.StringIO()
  original_emit = module.emit

  def capture(value, stream=None):
    original_emit(value, stderr if stream is not None else stdout)

  with patch.object(module, 'emit', side_effect=capture), patch.object(
    sys,
    'argv',
    [str(SCRIPT), *args],
  ):
    return module.main(), stdout.getvalue(), stderr.getvalue()


def candidate():
  return {
    'workspace': 'ws',
    'repo': 'repo',
    'pr_id': 7,
    'purpose': 'update description',
    'reviewed_source_sha': 'a' * 40,
    'reviewed_destination_sha': 'b' * 40,
    'operations': [{
      'operation_id': 'op-1',
      'type': 'update_description',
      'finding_uid': None,
      'request_body': {'description': MANAGED_DESCRIPTION},
    }],
  }


class CliTests(unittest.TestCase):
  def test_parser_exposes_exactly_five_commands_without_inspect(self):
    module = load_cli()
    parser = module.build_parser()
    action = next(value for value in parser._actions if value.dest == 'command')
    self.assertEqual(
      set(action.choices),
      {'render-description', 'review-context', 'preview', 'apply', 'reconcile'},
    )
    self.assertNotIn('inspect', action.choices)

  def test_render_description_outputs_json(self):
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text(json.dumps({
        'description': '',
        'owned_blocks': ['testing'],
        'blocks': {'testing': '## Testing\nnone'},
      }), encoding='utf-8')
      result = run_cli('render-description', '--input', str(input_path))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(json.loads(result.stdout)['put_eligible'])

  def test_review_context_outputs_json(self):
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      sha = 'a' * 40
      destination = 'b' * 40
      snapshot = {
        'source_repo_uuid': '{repo}',
        'destination_repo_uuid': '{repo}',
        'source_branch': 'feat/a',
        'destination_branch': 'master',
        'source_sha': sha,
        'destination_sha': destination,
      }
      input_path.write_text(json.dumps({
        'reviewed': snapshot,
        'current': snapshot,
      }), encoding='utf-8')
      result = run_cli('review-context', '--input', str(input_path))
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(result.stdout)['source_continuity'], 'CURRENT')

  def test_apply_requires_approval_and_session_id(self):
    result = run_cli('apply', '--proposal', '/tmp/missing.json')
    self.assertNotEqual(result.returncode, 0)
    self.assertIn('--approval', result.stderr)
    self.assertIn('--session-id', result.stderr)

  def test_credential_commands_require_both_environment_values(self):
    sentinel = 'credential-sentinel-value'
    with tempfile.TemporaryDirectory() as root:
      value = Path(root) / 'value.json'
      value.write_text('{}', encoding='utf-8')
      commands = (
        ('preview', '--mode', 'existing', '--input', str(value)),
        ('apply', '--proposal', str(value), '--approval', str(value), '--session-id', 's1'),
        ('reconcile', '--session-id', 's1', '--batch-id', 'a' * 12),
      )
      environments = (
        {'BITBUCKET_API_TOKEN': sentinel},
        {'BITBUCKET_API_USERNAME': sentinel},
        {},
      )
      for command in commands:
        for environment in environments:
          with self.subTest(command=command[0], environment=environment):
            result = run_cli(*command, env=environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
              'BITBUCKET_API_USERNAME (or BITBUCKET_EMAIL) and BITBUCKET_API_TOKEN are required',
              result.stderr,
            )
            self.assertNotIn(sentinel, result.stderr)

  def test_bitbucket_email_satisfies_username_credential(self):
    result = run_cli(
      'reconcile',
      '--session-id',
      's1',
      '--batch-id',
      'a' * 12,
      env={'BITBUCKET_EMAIL': 'user@example.com', 'BITBUCKET_API_TOKEN': 'tok'},
    )
    self.assertNotIn('are required', result.stderr)

  def test_existing_preview_with_empty_operations_uses_inspection_only(self):
    module = load_cli()
    client = object()
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text(json.dumps({
        'workspace': 'ws',
        'repo': 'repo',
        'pr_id': 7,
        'operations': [],
      }), encoding='utf-8')
      with patch.object(module, 'client_from_env', return_value=client), patch.object(
        module,
        'inspect_existing_pr',
        return_value={'status': 'READY_FOR_PROPOSAL'},
      ) as inspect, patch.object(module, 'existing_batch_ids') as batches, patch.object(
        module,
        'preview_existing_pr',
      ) as existing:
        result = module.run(SimpleNamespace(
          command='preview', input=str(input_path), mode='existing',
        ))
    self.assertEqual(result, {'status': 'READY_FOR_PROPOSAL'})
    inspect.assert_called_once_with(client, {
      'workspace': 'ws',
      'repo': 'repo',
      'pr_id': 7,
      'operations': [],
    })
    batches.assert_not_called()
    existing.assert_not_called()

  def test_preview_rejects_invalid_candidate_before_network(self):
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text('{}', encoding='utf-8')
      result = run_cli(
        'preview',
        '--mode', 'existing',
        '--input', str(input_path),
        env=credential_env(),
      )
    self.assertNotEqual(result.returncode, 0)
    self.assertIn('missing workspace', result.stderr)
    self.assertNotIn('transport', result.stderr.lower())

  def test_preview_secret_in_drafts_never_reaches_stdout(self):
    secret = 'ghp_abcdefghijklmnopqrstuvwxyz123456'
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text(json.dumps({'drafts': [secret]}), encoding='utf-8')
      result = run_cli(
        'preview',
        '--mode', 'existing',
        '--input', str(input_path),
        env=credential_env(),
      )
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(result.stdout, '')
    self.assertIn('credential-shaped content', result.stderr)
    self.assertNotIn(secret, result.stderr)

  def test_preview_allows_unknown_opaque_bearers_through_deterministic_scan(self):
    module = load_cli()
    values = (
      'Bearer abcdefghijklmnopqrstuvwxyz',
      'Bearer opaque.token.value.2026',
      'Bearer opaqueTokenValue1234567890',
      'Bearer opaque_token-value-2026',
    )
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'candidate.json'
      state_root = Path(root) / 'state'
      for text in values:
        with self.subTest(text=text):
          value = candidate()
          value['operations'][0] = {
            'operation_id': 'op-1',
            'type': 'create_pr_comment',
            'finding_uid': 'finding-1',
            'request_body': {'content': {'raw': text}},
          }
          input_path.write_text(json.dumps(value), encoding='utf-8')
          client = CountingClient()
          with patch.object(
            module,
            'client_from_env',
            return_value=client,
          ), patch.object(
            module,
            'existing_batch_ids',
            return_value=set(),
          ), patch.object(module, 'STATE_ROOT', state_root):
            exit_code, stdout, stderr = run_main(
              module,
              'preview',
              '--mode', 'existing',
              '--input', str(input_path),
            )
          self.assertEqual(exit_code, 0)
          self.assertEqual(json.loads(stdout)['status'], 'READY')
          self.assertEqual(stderr, '')
          self.assertGreater(client.get_count, 0)
          self.assertEqual(client.write_count, 0)
          self.assertFalse(state_root.exists())

  def test_apply_scans_forged_full_proposal_before_state_network_or_output(self):
    module = load_cli()
    secret = 'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature789'
    cases = (
      'operation_id',
      'finding_uid',
      'target_mapping_key',
      'snapshot_extra_field',
    )
    for case in cases:
      with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
        client = CountingClient()
        ready = preview_existing_pr(client, candidate())
        client.get_count = 0
        envelope = deepcopy(ready.envelope)
        proposal = envelope['proposal']
        if case == 'operation_id':
          proposal['operations'][0]['operation_id'] = secret
        elif case == 'finding_uid':
          proposal['operations'][0]['finding_uid'] = secret
        elif case == 'target_mapping_key':
          proposal['target'][secret] = 'value'
        else:
          proposal['snapshot']['extra_field'] = secret
        digest = proposal_sha256(proposal)
        envelope['proposal_sha256'] = digest
        envelope['batch_id'] = digest[:12]
        approved = {
          'session_id': 's1',
          'user_message_id': 'u1',
          'proposal_sha256': digest,
          'approved_operation_ids': [
            operation['operation_id']
            for operation in proposal['operations']
          ],
        }
        proposal_path = Path(root) / 'proposal.json'
        approval_path = Path(root) / 'approval.json'
        state_root = Path(root) / 'state'
        proposal_path.write_text(json.dumps(envelope), encoding='utf-8')
        approval_path.write_text(json.dumps(approved), encoding='utf-8')
        with patch.object(
          module,
          'client_from_env',
          return_value=client,
        ), patch.object(module, 'STATE_ROOT', state_root):
          exit_code, stdout, stderr = run_main(
            module,
            'apply',
            '--proposal', str(proposal_path),
            '--approval', str(approval_path),
            '--session-id', 's1',
          )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, '')
        self.assertIn('credential-shaped content', stderr)
        self.assertNotIn(secret, stderr)
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)
        self.assertFalse(state_root.exists())

  def test_secret_mapping_key_never_reaches_preview_or_apply_stdout(self):
    secret = 'xoxd-1234567890-abcdefghijklmnopqrstuvwxyz'
    with tempfile.TemporaryDirectory() as root:
      preview_path = Path(root) / 'candidate.json'
      preview_value = candidate()
      preview_value[secret] = 'value'
      preview_path.write_text(json.dumps(preview_value), encoding='utf-8')
      preview_result = run_cli(
        'preview',
        '--mode', 'existing',
        '--input', str(preview_path),
        env=credential_env(),
      )

      ready = preview_existing_pr(FakeClient(), candidate())
      envelope = deepcopy(ready.envelope)
      envelope['proposal']['operations'][0]['request_body'][secret] = 'value'
      digest = proposal_sha256(envelope['proposal'])
      envelope['proposal_sha256'] = digest
      envelope['batch_id'] = digest[:12]
      approval = {
        'session_id': 's1',
        'user_message_id': 'u1',
        'proposal_sha256': digest,
        'approved_operation_ids': ['op-1'],
      }
      proposal_path = Path(root) / 'proposal.json'
      approval_path = Path(root) / 'approval.json'
      proposal_path.write_text(json.dumps(envelope), encoding='utf-8')
      approval_path.write_text(json.dumps(approval), encoding='utf-8')
      apply_result = run_cli(
        'apply',
        '--proposal', str(proposal_path),
        '--approval', str(approval_path),
        '--session-id', 's1',
        env=credential_env(),
      )

    for result in (preview_result, apply_result):
      with self.subTest(command=result.args[2]):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('credential-shaped content', result.stderr)
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn('transport', result.stderr.lower())

  def test_apply_rejects_invalid_proposal_before_network(self):
    with tempfile.TemporaryDirectory() as root:
      proposal = Path(root) / 'proposal.json'
      approval = Path(root) / 'approval.json'
      proposal.write_text('{}', encoding='utf-8')
      approval.write_text('{}', encoding='utf-8')
      result = run_cli(
        'apply',
        '--proposal', str(proposal),
        '--approval', str(approval),
        '--session-id', 's1',
        env=credential_env(),
      )
    self.assertNotEqual(result.returncode, 0)
    self.assertIn('invalid proposal envelope', result.stderr)
    self.assertNotIn('transport', result.stderr.lower())

  def test_reconcile_rejects_invalid_ids_before_network(self):
    result = run_cli(
      'reconcile',
      '--session-id', '../other',
      '--batch-id', 'not-a-batch',
      env=credential_env(),
    )
    self.assertNotEqual(result.returncode, 0)
    self.assertIn('invalid session or batch id', result.stderr)
    self.assertNotIn('transport', result.stderr.lower())

  def test_reconcile_rejects_arbitrary_journal_path_flag(self):
    result = run_cli('reconcile', '--journal', '/tmp/other.json')
    self.assertNotEqual(result.returncode, 0)
    self.assertNotIn('--journal JOURNAL', result.stderr)
    self.assertIn('--session-id', result.stderr)
    self.assertIn('--batch-id', result.stderr)

  def test_mutation_commands_reject_target_and_transport_flags(self):
    forbidden = (
      '--journal', '--state-root', '--endpoint', '--base-url',
      '--workspace', '--repo', '--pr-id',
    )
    with tempfile.TemporaryDirectory() as root:
      value = Path(root) / 'value.json'
      value.write_text('{}', encoding='utf-8')
      commands = (
        ('preview', '--mode', 'existing', '--input', str(value)),
        ('apply', '--proposal', str(value), '--approval', str(value), '--session-id', 's1'),
        ('reconcile', '--session-id', 's1', '--batch-id', 'a' * 12),
      )
      for command in commands:
        for flag in forbidden:
          with self.subTest(command=command[0], flag=flag):
            result = run_cli(*command, flag, 'forbidden')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unrecognized arguments', result.stderr)

  def test_preview_apply_and_reconcile_use_fixed_wiring(self):
    module = load_cli()
    client = object()
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root) / 'state'
      preflight = Path(root) / 'preflight.json'
      candidate = Path(root) / 'candidate.json'
      proposal = Path(root) / 'proposal.json'
      approval = Path(root) / 'approval.json'
      preflight.write_text('{"workspace":"ws","repo":"repo","pr_id":7}', encoding='utf-8')
      candidate.write_text('{"kind":"candidate","operations":[{"operation_id":"op-1"}]}', encoding='utf-8')
      proposal.write_text('{"kind":"proposal"}', encoding='utf-8')
      approval.write_text('{"kind":"approval"}', encoding='utf-8')
      module.STATE_ROOT = state_root
      calls = []

      def inspect_result(*_args):
        calls.append('inspect')
        return {'status': 'READ_ONLY_FOREIGN_AUTHOR'}

      def batch_result(*_args):
        calls.append('batches')
        return {'known'}

      def existing_result(*_args):
        calls.append('existing')
        return {'status': 'existing'}

      with patch.object(module, 'client_from_env', return_value=client), patch.object(
        module,
        'inspect_existing_pr',
        side_effect=inspect_result,
      ) as inspect, patch.object(
        module,
        'existing_batch_ids',
        side_effect=batch_result,
      ) as batches, patch.object(
        module,
        'preview_existing_pr',
        side_effect=existing_result,
      ) as existing, patch.object(
        module,
        'preview_create_pr',
        return_value={'status': 'create'},
      ) as create:
        preflight_result = module.run(SimpleNamespace(
          command='preview', input=str(preflight), mode='existing',
        ))
        existing_preview_result = module.run(SimpleNamespace(
          command='preview', input=str(candidate), mode='existing',
        ))
        create_result = module.run(SimpleNamespace(
          command='preview', input=str(candidate), mode='create',
        ))
      self.assertEqual(preflight_result, {'status': 'READ_ONLY_FOREIGN_AUTHOR'})
      self.assertEqual(existing_preview_result, {'status': 'existing'})
      self.assertEqual(create_result, {'status': 'create'})
      inspect.assert_called_once_with(client, {'workspace': 'ws', 'repo': 'repo', 'pr_id': 7})
      self.assertEqual(calls, ['inspect', 'batches', 'existing', 'batches'])
      self.assertEqual(batches.call_count, 2)
      existing.assert_called_once_with(
        client,
        {'kind': 'candidate', 'operations': [{'operation_id': 'op-1'}]},
        {'known'},
      )
      create.assert_called_once_with(
        client,
        {'kind': 'candidate', 'operations': [{'operation_id': 'op-1'}]},
        {'known'},
      )
      with patch.object(module, 'client_from_env', return_value=client), patch.object(
        module,
        'apply_proposal',
        return_value={'batch_state': 'completed'},
      ) as apply_call:
        apply_result = module.run(SimpleNamespace(
          command='apply',
          proposal=str(proposal),
          approval=str(approval),
          session_id='s1',
        ))
      self.assertEqual(apply_result, {'batch_state': 'completed'})
      apply_call.assert_called_once_with(
        client,
        {'kind': 'proposal'},
        {'kind': 'approval'},
        state_root,
        's1',
      )
      expected_path = state_root / 'sessions/s1' / ('a' * 12 + '.json')
      with patch.object(module, 'client_from_env', return_value=client), patch.object(
        module,
        'journal_path',
        return_value=expected_path,
      ) as path_call, patch.object(
        module,
        'reconcile_journal',
        return_value={'candidate_count': 0},
      ) as reconcile:
        reconcile_result = module.run(SimpleNamespace(
          command='reconcile', session_id='s1', batch_id='a' * 12,
        ))
      self.assertEqual(reconcile_result, {'candidate_count': 0})
      path_call.assert_called_once_with(state_root, 's1', 'a' * 12)
      reconcile.assert_called_once_with(client, expected_path)

  def test_apply_exit_code_requires_full_completion(self):
    module = load_cli()
    completed = SimpleNamespace(
      batch_state='completed',
      operations={'op-1': SimpleNamespace(state='completed', outcome='completed')},
    )
    self.assertEqual(module.result_exit_code('apply', completed), 0)
    cases = (
      SimpleNamespace(batch_state='invalid', operations={}),
      SimpleNamespace(
        batch_state='outcome_unknown',
        operations={'op-1': SimpleNamespace(state='outcome_unknown', outcome='outcome_unknown')},
      ),
      SimpleNamespace(
        batch_state='completed',
        operations={'op-1': SimpleNamespace(state='completed', outcome='failed')},
      ),
      SimpleNamespace(
        batch_state='completed',
        operations={'op-1': SimpleNamespace(state='post_write_drift', outcome='post_write_drift')},
      ),
      SimpleNamespace(
        batch_state='completed',
        operations={'op-1': SimpleNamespace(state='not_attempted', outcome='not_attempted')},
      ),
    )
    for result in cases:
      with self.subTest(result=result):
        self.assertEqual(module.result_exit_code('apply', result), 2)
    self.assertEqual(module.result_exit_code('preview', {'status': 'READY'}), 0)
    self.assertEqual(module.result_exit_code('reconcile', {'candidate_count': 0}), 0)

  def test_json_is_strict_and_output_bytes_are_utf8(self):
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text(json.dumps({
        'description': '',
        'owned_blocks': ['testing'],
        'blocks': {'testing': '## Testing\n繁體中文'},
      }), encoding='utf-8')
      result = run_cli(
        'render-description',
        '--input', str(input_path),
        env={'PYTHONIOENCODING': 'latin-1'},
        text=False,
      )
      invalid_path = Path(root) / 'invalid.json'
      invalid_path.write_text('{"description":NaN}', encoding='utf-8')
      invalid = run_cli('render-description', '--input', str(invalid_path), text=False)
    self.assertEqual(result.returncode, 0, result.stderr)
    decoded = result.stdout.decode('utf-8')
    self.assertIn('繁體中文', decoded)
    self.assertEqual(len(decoded.splitlines()), 1)
    self.assertNotEqual(invalid.returncode, 0)
    error = json.loads(invalid.stderr.decode('utf-8'))
    self.assertEqual(error, {'error': 'non-standard JSON constant'})
    self.assertNotIn(b'NaN', invalid.stderr)

  def test_json_errors_are_sanitized_without_traceback(self):
    with tempfile.TemporaryDirectory() as root:
      input_path = Path(root) / 'input.json'
      input_path.write_text('[]', encoding='utf-8')
      result = run_cli('render-description', '--input', str(input_path))
    self.assertNotEqual(result.returncode, 0)
    self.assertEqual(json.loads(result.stderr), {'error': 'JSON root must be an object'})
    self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
  unittest.main()
