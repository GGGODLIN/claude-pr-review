import base64
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from bitbucket_pr_workflow.api import ApiError, ApiTransportError
from bitbucket_pr_workflow.core import proposal_sha256
from bitbucket_pr_workflow.executor import (
  ReconciliationRequired,
  apply_proposal,
  ensure_private_directory,
  inspect_existing_pr,
  journal_path,
  preview_create_pr,
  preview_existing_pr,
  read_journal,
  reconcile_journal,
  session_journal_path,
  validate_no_credentials,
  write_journal,
)
from test_support import FakeClient


MANAGED_DESCRIPTION = '<!-- pr-review-testing:start -->\n## Testing\nnone\n<!-- pr-review-testing:end -->'
SOURCE_SHORT_SHA = 'a1b2c3d4e5f6'
DESTINATION_SHORT_SHA = 'b2c3d4e5f6a1'
SOURCE_FULL_SHA = SOURCE_SHORT_SHA + '1' * 28
DESTINATION_FULL_SHA = DESTINATION_SHORT_SHA + '2' * 28


def candidate(description=MANAGED_DESCRIPTION, operation_id='op-1'):
  return {
    'workspace': 'ws',
    'repo': 'repo',
    'pr_id': 7,
    'purpose': 'update description',
    'reviewed_source_sha': 'a' * 40,
    'reviewed_destination_sha': 'b' * 40,
    'operations': [{
      'operation_id': operation_id,
      'type': 'update_description',
      'finding_uid': None,
      'request_body': {'description': description},
    }],
  }


def title_candidate(title='Reworked title', operation_id='op-1'):
  return {
    'workspace': 'ws',
    'repo': 'repo',
    'pr_id': 7,
    'purpose': 'update title',
    'reviewed_source_sha': 'a' * 40,
    'reviewed_destination_sha': 'b' * 40,
    'operations': [{
      'operation_id': operation_id,
      'type': 'update_title',
      'finding_uid': None,
      'request_body': {'title': title},
    }],
  }


def approval(preview, session_id='s1'):
  return {
    'session_id': session_id,
    'user_message_id': 'user-message-after-preview',
    'proposal_sha256': preview.proposal_sha256,
    'approved_operation_ids': [
      operation['operation_id']
      for operation in preview.envelope['proposal']['operations']
    ],
  }


def comment_candidate(
  operation_type,
  body,
  reviewed_source_sha='a' * 40,
  reviewed_destination_sha='b' * 40,
  review_context_changed=True,
  relocation_proof=None,
):
  value = {
    'workspace': 'ws',
    'repo': 'repo',
    'pr_id': 7,
    'purpose': 'post review comments',
    'reviewed_source_sha': reviewed_source_sha,
    'reviewed_destination_sha': reviewed_destination_sha,
    'review_context_changed': review_context_changed,
    'operations': [{
      'operation_id': 'op-1',
      'type': operation_type,
      'finding_uid': 'finding-1',
      'request_body': body,
    }],
  }
  if relocation_proof is not None:
    value['relocation_proof'] = relocation_proof
  return value


def create_candidate():
  return {
    'workspace': 'ws',
    'repo': 'repo',
    'purpose': 'create PR',
    'source_branch': 'feat/a',
    'destination_branch': 'master',
    'operations': [{
      'operation_id': 'op-1',
      'type': 'create_pr',
      'finding_uid': None,
      'request_body': {
        'title': 'Example',
        'description': MANAGED_DESCRIPTION,
        'source': {'branch': {'name': 'feat/a'}},
        'destination': {'branch': {'name': 'master'}},
      },
    }],
  }


def preview_for_type(client, operation_type):
  if operation_type == 'create_pr':
    return preview_create_pr(client, create_candidate())
  if operation_type == 'update_description':
    return preview_existing_pr(client, candidate())
  if operation_type == 'create_inline_comment':
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    return preview_existing_pr(client, comment_candidate(operation_type, body))
  body = {'content': {'raw': 'finding'}}
  return preview_existing_pr(client, comment_candidate(operation_type, body))


def resign(envelope):
  value = deepcopy(envelope)
  digest = proposal_sha256(value['proposal'])
  value['proposal_sha256'] = digest
  value['batch_id'] = digest[:12]
  return value


def stat_mode(path):
  return os.stat(path).st_mode & 0o777


def journal_value(preview, session_id, batch_state, operation_state):
  outcome = None if operation_state == 'started' else operation_state
  return {
    'version': 1,
    'session_id': session_id,
    'batch_id': preview.batch_id,
    'proposal_sha256': preview.proposal_sha256,
    'batch_state': batch_state,
    'target': preview.envelope['proposal']['target'],
    'snapshot': preview.envelope['proposal']['snapshot'],
    'proposal': preview.envelope['proposal'],
    'operations': {
      'op-1': {
        'type': preview.envelope['proposal']['operations'][0]['type'],
        'state': operation_state,
        'outcome': outcome,
        'resource_id': None,
        'resource_url': None,
      },
    },
  }


def multi_operation_preview(client):
  value = candidate()
  value['purpose'] = 'ordered description updates'
  value['operations'] = [
    {
      'operation_id': f'op-{index}',
      'type': 'update_description',
      'finding_uid': None,
      'request_body': {
        'description': MANAGED_DESCRIPTION.replace('none', f'update-{index}'),
      },
    }
    for index in range(1, 4)
  ]
  return preview_existing_pr(client, value)


def journal_with_facts(preview, batch_state, facts):
  proposal = preview.envelope['proposal']
  operations = {}
  for operation, (state, outcome) in zip(proposal['operations'], facts, strict=True):
    operations[operation['operation_id']] = {
      'type': operation['type'],
      'state': state,
      'outcome': outcome,
      'resource_id': None,
      'resource_url': None,
    }
  return {
    'version': 1,
    'session_id': 's1',
    'batch_id': preview.batch_id,
    'proposal_sha256': preview.proposal_sha256,
    'batch_state': batch_state,
    'target': proposal['target'],
    'snapshot': proposal['snapshot'],
    'proposal': proposal,
    'operations': operations,
  }


class RejectingClient(FakeClient):
  def update_pr(self, _workspace, _repo, _pr_id, _body):
    self.write_count += 1
    raise ApiError(409)


class HttpAfterWriteClient(FakeClient):
  def __init__(self, status):
    super().__init__()
    self.status = status

  def update_pr(self, workspace, repo, pr_id, body):
    super().update_pr(workspace, repo, pr_id, body)
    raise ApiError(self.status)


class PreconditionFailureClient(FakeClient):
  def get_user(self):
    raise ApiTransportError('precondition unavailable')


class CountingClient(FakeClient):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
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

  def get_comment(self, workspace, repo, pr_id, comment_id):
    self.get_count += 1
    return super().get_comment(workspace, repo, pr_id, comment_id)


class ExecutorTests(unittest.TestCase):
  def test_inspect_foreign_author_returns_snapshot_without_operations(self):
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    result = inspect_existing_pr(client, {
      'workspace': 'ws',
      'repo': 'repo',
      'pr_id': 7,
    })
    self.assertEqual(result.status, 'READY_FOR_COMMENT_ONLY')
    self.assertEqual(result.snapshot['actor_uuid'], '{actor}')
    self.assertEqual(result.snapshot['author_uuid'], '{other}')
    self.assertEqual(client.write_count, 0)

  def test_inspect_non_open_pr_is_comment_only(self):
    client = FakeClient(state='MERGED')
    result = inspect_existing_pr(client, {
      'workspace': 'ws',
      'repo': 'repo',
      'pr_id': 7,
    })
    self.assertEqual(result.status, 'READY_FOR_COMMENT_ONLY')
    self.assertEqual(client.write_count, 0)

  def test_foreign_author_inline_comment_is_allowed(self):
    """known-good fixture: additive comment on someone else's PR must reach READY."""
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    result = preview_existing_pr(client, comment_candidate(
      'create_inline_comment',
      body,
      review_context_changed=False,
    ))
    self.assertEqual(result.status, 'READY')
    self.assertIsNotNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_foreign_author_pr_comment_is_allowed(self):
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    result = preview_existing_pr(client, comment_candidate(
      'create_pr_comment',
      {'content': {'raw': 'finding'}},
    ))
    self.assertEqual(result.status, 'READY')
    self.assertIsNotNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_non_open_pr_comment_is_allowed(self):
    client = FakeClient(state='MERGED')
    result = preview_existing_pr(client, comment_candidate(
      'create_pr_comment',
      {'content': {'raw': 'finding'}},
    ))
    self.assertEqual(result.status, 'READY')
    self.assertIsNotNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_non_open_pr_description_is_still_blocked(self):
    client = FakeClient(state='MERGED')
    result = preview_existing_pr(client, candidate())
    self.assertEqual(result.status, 'READ_ONLY_PR_NOT_OPEN')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_foreign_author_comment_applies_end_to_end(self):
    """The apply path re-gated on author independently of preview; prove it now passes."""
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    preview = preview_existing_pr(client, comment_candidate(
      'create_pr_comment',
      {'content': {'raw': 'finding'}},
    ))
    self.assertEqual(preview.status, 'READY')
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(client.write_count, 1)

  def test_foreign_author_description_still_blocked_at_apply(self):
    """Even with a forged READY envelope, apply must refuse a foreign description write."""
    own_client = FakeClient()
    preview = preview_existing_pr(own_client, candidate())
    self.assertEqual(preview.status, 'READY')
    foreign_client = FakeClient(
      actor_uuid='{actor}',
      author_uuid='{other}',
      description=MANAGED_DESCRIPTION,
    )
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        foreign_client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(foreign_client.write_count, 0)

  def test_inline_readback_accepts_bitbucket_null_opposite_anchor(self):
    """Bitbucket returns the unused anchor side as null, not absent."""
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    preview = preview_existing_pr(client, comment_candidate(
      'create_inline_comment',
      body,
      review_context_changed=False,
    ))
    self.assertEqual(preview.status, 'READY')
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.operations['op-1'].state, 'completed')
    stored_inline = client.comments[1]['inline']
    self.assertIsNone(stored_inline['from'])
    self.assertEqual(stored_inline['to'], 10)

  def test_inline_readback_rejects_opposite_anchor_with_a_value(self):
    """A comment that came back anchored on the deletion side is still a mismatch."""
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    preview = preview_existing_pr(client, comment_candidate(
      'create_inline_comment',
      body,
      review_context_changed=False,
    ))
    client.comment_readback_override = {'inline': {
      'path': 'src/a.ts',
      'to': 10,
      'from': 4,
    }}
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertNotEqual(result.operations['op-1'].state, 'completed')

  def test_foreign_author_mixed_batch_blocks_whole_batch(self):
    """boundary fixture: a description op must not ride in behind allowed comments."""
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    value = comment_candidate('create_pr_comment', {'content': {'raw': 'finding'}})
    value['operations'].append({
      'operation_id': 'op-2',
      'type': 'update_description',
      'finding_uid': None,
      'request_body': {'description': MANAGED_DESCRIPTION},
    })
    result = preview_existing_pr(client, value)
    self.assertEqual(result.status, 'READ_ONLY_FOREIGN_AUTHOR')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_inspect_own_open_pr_is_ready_without_review_basis_or_operations(self):
    client = FakeClient()
    result = inspect_existing_pr(client, {
      'workspace': 'ws',
      'repo': 'repo',
      'pr_id': 7,
    })
    self.assertEqual(result.status, 'READY_FOR_PROPOSAL')
    self.assertEqual(result.snapshot['source_sha'], 'a' * 40)
    self.assertEqual(result.snapshot['destination_sha'], 'b' * 40)
    self.assertEqual(client.commit_requests, [])
    self.assertEqual(client.write_count, 0)

  def test_short_pr_hashes_are_enriched_and_bound_to_each_side(self):
    client = FakeClient(
      source_pr_sha=SOURCE_SHORT_SHA,
      destination_pr_sha=DESTINATION_SHORT_SHA,
      source_commit_sha=SOURCE_FULL_SHA,
      destination_commit_sha=DESTINATION_FULL_SHA,
    )
    value = candidate()
    value['reviewed_source_sha'] = SOURCE_FULL_SHA
    value['reviewed_destination_sha'] = DESTINATION_FULL_SHA
    result = preview_existing_pr(client, value)
    proposal = result.envelope['proposal']
    self.assertEqual(result.status, 'READY')
    self.assertEqual(proposal['snapshot']['source_sha'], SOURCE_FULL_SHA)
    self.assertEqual(proposal['snapshot']['destination_sha'], DESTINATION_FULL_SHA)
    self.assertEqual(proposal['reviewed_source_sha'], SOURCE_FULL_SHA)
    self.assertEqual(proposal['reviewed_destination_sha'], DESTINATION_FULL_SHA)
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ])
    self.assertEqual(client.write_count, 0)

  def test_short_hash_enrichment_remains_bound_during_apply(self):
    client = FakeClient(
      source_pr_sha=SOURCE_SHORT_SHA,
      destination_pr_sha=DESTINATION_SHORT_SHA,
      source_commit_sha=SOURCE_FULL_SHA,
      destination_commit_sha=DESTINATION_FULL_SHA,
    )
    value = candidate()
    value['reviewed_source_sha'] = SOURCE_FULL_SHA
    value['reviewed_destination_sha'] = DESTINATION_FULL_SHA
    preview = preview_existing_pr(client, value)
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(result.operations['op-1'].outcome, 'completed')
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ] * 4)
    self.assertEqual(client.write_count, 1)

  def test_commit_lookup_failures_are_closed_before_write(self):
    cases = (
      ('missing_hash', {}),
      ('non_string_hash', {'hash': 123}),
      ('mismatched_hash', {'hash': 'c' * 40}),
      ('not_more_complete', {'hash': SOURCE_SHORT_SHA}),
    )
    for name, response in cases:
      with self.subTest(name=name):
        client = FakeClient(
          source_pr_sha=SOURCE_SHORT_SHA,
          destination_pr_sha=DESTINATION_SHORT_SHA,
          source_commit_sha=SOURCE_FULL_SHA,
          destination_commit_sha=DESTINATION_FULL_SHA,
        )
        client.commit_readback_override[SOURCE_SHORT_SHA] = response
        with self.assertRaises(ValueError):
          inspect_existing_pr(client, {
            'workspace': 'ws',
            'repo': 'repo',
            'pr_id': 7,
          })
        self.assertEqual(
          client.commit_requests,
          [('ws', 'repo', SOURCE_SHORT_SHA)],
        )
        self.assertEqual(client.write_count, 0)

  def test_destination_commit_lookup_mismatch_is_closed_before_write(self):
    client = FakeClient(
      source_pr_sha=SOURCE_SHORT_SHA,
      destination_pr_sha=DESTINATION_SHORT_SHA,
      source_commit_sha=SOURCE_FULL_SHA,
      destination_commit_sha=DESTINATION_FULL_SHA,
    )
    client.commit_readback_override[DESTINATION_SHORT_SHA] = {
      'hash': SOURCE_FULL_SHA,
    }
    with self.assertRaises(ValueError):
      inspect_existing_pr(client, {
        'workspace': 'ws',
        'repo': 'repo',
        'pr_id': 7,
      })
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ])
    self.assertEqual(client.write_count, 0)

  def test_foreign_author_returns_read_only_without_write(self):
    client = FakeClient(
      actor_uuid='{actor}',
      author_uuid='{other}',
      source_pr_sha=SOURCE_SHORT_SHA,
      destination_pr_sha=DESTINATION_SHORT_SHA,
      source_commit_sha=SOURCE_FULL_SHA,
      destination_commit_sha=DESTINATION_FULL_SHA,
    )
    result = preview_existing_pr(client, candidate())
    self.assertEqual(result.status, 'READ_ONLY_FOREIGN_AUTHOR')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ])
    self.assertEqual(client.write_count, 0)

  def test_unmanaged_description_is_draft_only(self):
    client = FakeClient(description='作者文字')
    result = preview_existing_pr(client, candidate())
    self.assertEqual(result.status, 'DRAFT_ONLY_UNMANAGED_DESCRIPTION')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_scope_answer_is_not_typed_approval(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError):
        apply_proposal(client, preview.envelope, {'scope': 'all'}, Path(root), 's1')
    self.assertEqual(client.write_count, 0)

  def test_preview_rejects_real_credential_shapes_before_network_without_leaking(self):
    basic = base64.b64encode(b'user:pass').decode('ascii')
    secrets = (
      'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature789',
      f'Basic {basic}',
      '-----BEGIN PRIVATE KEY-----\nprivate-material',
      'ghp_abcdefghijklmnopqrstuvwxyz123456',
      'github_pat_abcdefghijklmnopqrstuvwxyz123456',
      'sk-abcdefghijklmnopqrstuvwxyz123456',
      'AKIAABCDEFGHIJKLMNOP',
      'xoxb-' + '1234567890-abcdefghijklmnopqrstuvwxyz',  # literal split so GitHub push protection does not flag the fixture as a real Slack token
    )
    for secret in secrets:
      with self.subTest(secret=secret[:12]):
        client = CountingClient()
        value = comment_candidate(
          'create_pr_comment',
          {'content': {'raw': secret}},
        )
        with self.assertRaises(ValueError) as raised:
          preview_existing_pr(client, value)
        self.assertIn('credential-shaped content', str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_basic_credentials_with_trailing_punctuation_are_rejected(self):
    token = base64.b64encode(b'user:pass').decode('ascii')
    punctuation = (
      '!', '"', '#', '$', '%', '&', "'", '(', ')', '*', ',', '.', ':', ';',
      '<', '>', '?', '@', '[', '\\', ']', '^', '`', '{', '|', '}', '~',
    )
    for suffix in punctuation:
      with self.subTest(suffix=suffix):
        secret = f'Basic {token}{suffix}'
        client = CountingClient()
        value = comment_candidate(
          'create_pr_comment',
          {'content': {'raw': secret}},
        )
        with self.assertRaises(ValueError) as raised:
          preview_existing_pr(client, value)
        self.assertIn('basic_credential', str(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_provider_token_variants_are_rejected(self):
    secrets = (
      'xoxc-1234567890-abcdefghijklmnopqrstuvwxyz',
      'xoxd-1234567890-abcdefghijklmnopqrstuvwxyz',
    )
    for secret in secrets:
      with self.subTest(secret=secret[:12]):
        client = CountingClient()
        value = comment_candidate(
          'create_pr_comment',
          {'content': {'raw': secret}},
        )
        with self.assertRaises(ValueError) as raised:
          preview_existing_pr(client, value)
        self.assertIn('credential-shaped content', str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_secret_mapping_keys_are_rejected_before_preview_or_apply_network(self):
    secret = 'xoxc-1234567890-abcdefghijklmnopqrstuvwxyz'
    preview_client = CountingClient()
    preview_candidate = candidate()
    preview_candidate[secret] = {'nested': 'value'}
    with self.assertRaises(ValueError) as preview_error:
      preview_existing_pr(preview_client, preview_candidate)
    self.assertIn('provider_token', str(preview_error.exception))
    self.assertIn('<key:7>', str(preview_error.exception))
    self.assertNotIn(secret, str(preview_error.exception))
    self.assertEqual(preview_client.get_count, 0)
    self.assertEqual(preview_client.write_count, 0)

    apply_client = CountingClient()
    preview = preview_existing_pr(apply_client, candidate())
    apply_client.get_count = 0
    envelope = deepcopy(preview.envelope)
    envelope['proposal']['operations'][0]['request_body'][secret] = 'value'
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError) as apply_error:
        apply_proposal(apply_client, envelope, approved, Path(root), 's1')
    self.assertIn('provider_token', str(apply_error.exception))
    self.assertIn('<key:1>', str(apply_error.exception))
    self.assertNotIn(secret, str(apply_error.exception))
    self.assertEqual(apply_client.get_count, 0)
    self.assertEqual(apply_client.write_count, 0)

  def test_credential_error_path_keeps_safe_keys_and_masks_unsafe_keys(self):
    with self.assertRaises(ValueError) as raised:
      validate_no_credentials({
        'safe_context': {
          'authorization': 'present',
        },
      })
    message = str(raised.exception)
    self.assertIn('$.safe_context.<key:0>', message)
    self.assertNotIn('authorization', message)

  def test_preview_scans_entire_candidate_before_foreign_author_or_drafts(self):
    secret = 'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature789'
    for value, previewer in (
      ({**candidate(), 'drafts': [secret]}, preview_existing_pr),
      ({**create_candidate(), 'drafts': [secret]}, preview_create_pr),
    ):
      with self.subTest(previewer=previewer.__name__):
        client = CountingClient(actor_uuid='{actor}', author_uuid='{other}')
        with self.assertRaises(ValueError) as raised:
          previewer(client, value)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_apply_rejects_forged_provider_token_before_write_without_leaking(self):
    secret = 'ghp_abcdefghijklmnopqrstuvwxyz123456'
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    envelope = deepcopy(preview.envelope)
    envelope['proposal']['operations'][0]['request_body']['description'] = secret
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError) as raised:
        apply_proposal(client, envelope, approved, Path(root), 's1')
    self.assertIn('credential-shaped content', str(raised.exception))
    self.assertNotIn(secret, str(raised.exception))
    self.assertEqual(client.write_count, 0)

  def test_credential_scan_allows_technical_prose_and_unknown_opaque_bearers(self):
    values = (
      'Basic authentication cleanup',
      'Basic behavior remains unchanged',
      'Please document Basic authentication before release.',
      'Bearer abcdefghijklmnopqrstuvwxyz',
      'Bearer opaque.token.value.2026',
      'Bearer opaqueTokenValue1234567890',
      'Bearer opaque_token-value-2026',
      'Bearer opaqueTokenValue1234567890=',
      'Bearer opaqueTokenValue1234567890==',
      'Basic dXNlcjpwYXNz_not-a-single-token',
      'build_artifact_identifier_20260727_release_candidate_001',
      'Please rename token_count to item_count.',
    )
    for text in values:
      with self.subTest(text=text):
        client = FakeClient()
        body = {'content': {'raw': text}}
        preview = preview_existing_pr(
          client,
          comment_candidate('create_pr_comment', body),
        )
        self.assertEqual(preview.status, 'READY')
        self.assertEqual(client.write_count, 0)

  def test_snapshot_drift_performs_zero_writes(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.pr['source']['commit']['hash'] = 'c' * 40
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(client.write_count, 0)

  def test_two_batches_for_same_target_are_serialized_and_second_rechecks(self):
    client = FakeClient()
    first = preview_existing_pr(client, candidate(MANAGED_DESCRIPTION.replace('none', 'first')))
    second = preview_existing_pr(client, candidate(MANAGED_DESCRIPTION.replace('none', 'second'), 'op-2'))
    client.block_first_write = True
    results = {}
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      one = threading.Thread(target=lambda: results.setdefault('first', apply_proposal(client, first.envelope, approval(first), state_root, 's1')))
      two = threading.Thread(target=lambda: results.setdefault('second', apply_proposal(client, second.envelope, approval(second), state_root, 's1')))
      one.start()
      self.assertTrue(client.write_started.wait(timeout=5))
      two.start()
      client.release_write.set()
      one.join(timeout=5)
      two.join(timeout=5)
    self.assertEqual(results['first'].batch_state, 'completed')
    self.assertEqual(results['second'].batch_state, 'invalid')
    self.assertEqual(client.write_count, 1)

  def test_timeout_after_send_is_outcome_unknown_and_not_retried(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.transport_after_write = True
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'outcome_unknown')
    self.assertEqual(result.operations['op-1'].state, 'outcome_unknown')
    self.assertEqual(client.write_count, 1)

  def test_post_write_drift_stops_remaining_operations(self):
    client = FakeClient()
    value = candidate()
    value['purpose'] = 'post review comments'
    value['operations'] = [
      {'operation_id': 'op-1', 'type': 'create_pr_comment', 'finding_uid': 'f1', 'request_body': {'content': {'raw': 'one'}}},
      {'operation_id': 'op-2', 'type': 'create_pr_comment', 'finding_uid': 'f2', 'request_body': {'content': {'raw': 'two'}}},
    ]
    preview = preview_existing_pr(client, value)
    client.drift_after_write = True
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(result.operations['op-1'].outcome, 'post_write_drift')
    self.assertEqual(result.operations['op-2'].state, 'not_attempted')
    self.assertEqual(client.write_count, 1)

  def test_create_pr_read_back_requires_preview_actor(self):
    client = FakeClient()
    preview = preview_create_pr(client, create_candidate())
    client.create_author_override = '{other}'
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.operations['op-1'].outcome, 'failed')
    self.assertEqual(client.write_count, 1)

  def test_create_pr_short_hash_read_back_completes_after_commit_lookup(self):
    client = FakeClient(
      source_pr_sha=SOURCE_FULL_SHA,
      destination_pr_sha=DESTINATION_FULL_SHA,
      create_source_pr_sha=SOURCE_SHORT_SHA,
      create_destination_pr_sha=DESTINATION_SHORT_SHA,
      create_source_commit_sha=SOURCE_FULL_SHA,
      create_destination_commit_sha=DESTINATION_FULL_SHA,
    )
    preview = preview_create_pr(client, create_candidate())
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(result.operations['op-1'].state, 'completed')
    self.assertEqual(result.operations['op-1'].outcome, 'completed')
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ] * 2)
    self.assertEqual(client.write_count, 1)

  def test_create_pr_short_hash_lookup_mismatch_is_outcome_unknown(self):
    client = FakeClient(
      source_pr_sha=SOURCE_FULL_SHA,
      destination_pr_sha=DESTINATION_FULL_SHA,
      create_source_pr_sha=SOURCE_SHORT_SHA,
      create_destination_pr_sha=DESTINATION_SHORT_SHA,
      create_source_commit_sha=SOURCE_FULL_SHA,
      create_destination_commit_sha=DESTINATION_FULL_SHA,
    )
    client.commit_readback_override[SOURCE_SHORT_SHA] = {'hash': 'c' * 40}
    preview = preview_create_pr(client, create_candidate())
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'outcome_unknown')
    self.assertEqual(result.operations['op-1'].state, 'outcome_unknown')
    self.assertEqual(result.operations['op-1'].outcome, 'outcome_unknown')
    self.assertEqual(
      client.commit_requests,
      [('ws', 'repo', SOURCE_SHORT_SHA)],
    )
    self.assertEqual(client.write_count, 1)

  def test_description_read_back_mismatch_is_failed(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.pr_readback_override = {'description': 'different'}
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.operations['op-1'].state, 'failed')
    self.assertEqual(client.write_count, 1)

  def test_inline_context_comes_from_reviewed_shas_not_candidate_claim(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    unchanged = preview_existing_pr(
      client,
      comment_candidate(
        'create_inline_comment',
        body,
        review_context_changed=True,
      ),
    )
    stale = preview_existing_pr(
      client,
      comment_candidate(
        'create_inline_comment',
        body,
        reviewed_source_sha='c' * 40,
        review_context_changed=False,
      ),
    )
    fake_relocation = preview_existing_pr(
      client,
      comment_candidate(
        'create_inline_comment',
        body,
        reviewed_source_sha='c' * 40,
        review_context_changed=False,
        relocation_proof={
          'source_sha': 'a' * 40,
          'destination_sha': 'b' * 40,
          'path': 'src/a.ts',
          'to': 10,
          'anchor_text': 'return value',
          'source_summary': 'claimed current diff lookup',
        },
      ),
    )
    self.assertEqual(unchanged.status, 'READY')
    self.assertEqual(stale.status, 'STALE_INLINE_REQUIRES_FALLBACK')
    self.assertIsNone(stale.envelope)
    self.assertEqual(fake_relocation.status, 'STALE_INLINE_REQUIRES_FALLBACK')
    self.assertIsNone(fake_relocation.envelope)
    self.assertEqual(client.write_count, 0)

  def test_missing_or_invalid_reviewed_shas_are_rejected_without_write(self):
    for field, value in (
      ('reviewed_source_sha', None),
      ('reviewed_source_sha', 'abc123'),
      ('reviewed_destination_sha', None),
      ('reviewed_destination_sha', 'def456'),
    ):
      with self.subTest(field=field, value=value):
        client = FakeClient()
        current = candidate()
        if value is None:
          del current[field]
        else:
          current[field] = value
        with self.assertRaises(ValueError):
          preview_existing_pr(client, current)
        self.assertEqual(client.write_count, 0)

  def test_forged_inline_review_basis_is_rejected_before_write(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    preview = preview_existing_pr(
      client,
      comment_candidate('create_inline_comment', body),
    )
    envelope = deepcopy(preview.envelope)
    envelope['proposal']['reviewed_source_sha'] = 'c' * 40
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError):
        apply_proposal(client, envelope, approved, Path(root), 's1')
    self.assertEqual(client.write_count, 0)

  def test_null_review_basis_allows_non_inline_mutations(self):
    for operation_type in ('update_description', 'create_pr_comment'):
      with self.subTest(operation_type=operation_type):
        client = FakeClient()
        if operation_type == 'update_description':
          value = candidate()
        else:
          value = comment_candidate(
            operation_type,
            {'content': {'raw': 'finding'}},
          )
        value['reviewed_source_sha'] = None
        value['reviewed_destination_sha'] = None
        preview = preview_existing_pr(client, value)
        self.assertEqual(preview.status, 'READY')
        self.assertIsNone(preview.envelope['proposal']['reviewed_source_sha'])
        self.assertIsNone(preview.envelope['proposal']['reviewed_destination_sha'])
        with tempfile.TemporaryDirectory() as root:
          result = apply_proposal(
            client,
            preview.envelope,
            approval(preview),
            Path(root),
            's1',
          )
        self.assertEqual(result.batch_state, 'completed')
        self.assertEqual(result.operations['op-1'].outcome, 'completed')
        self.assertEqual(client.write_count, 1)

  def test_mixed_null_and_full_review_basis_is_rejected_without_write(self):
    for source, destination in ((None, 'b' * 40), ('a' * 40, None)):
      with self.subTest(source=source, destination=destination):
        client = FakeClient()
        value = candidate()
        value['reviewed_source_sha'] = source
        value['reviewed_destination_sha'] = destination
        with self.assertRaises(ValueError):
          preview_existing_pr(client, value)
        self.assertEqual(client.write_count, 0)

  def test_inline_null_review_basis_is_rejected_in_preview_and_apply(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    value = comment_candidate('create_inline_comment', body)
    value['reviewed_source_sha'] = None
    value['reviewed_destination_sha'] = None
    preview = preview_existing_pr(client, value)
    self.assertEqual(preview.status, 'STALE_INLINE_REQUIRES_FALLBACK')
    self.assertIsNone(preview.envelope)
    legal = preview_existing_pr(
      client,
      comment_candidate('create_inline_comment', body),
    )
    envelope = deepcopy(legal.envelope)
    envelope['proposal']['reviewed_source_sha'] = None
    envelope['proposal']['reviewed_destination_sha'] = None
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError):
        apply_proposal(client, envelope, approved, Path(root), 's1')
    self.assertEqual(client.write_count, 0)

  def test_reviewed_shas_are_hashed_into_immutable_proposal(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}}
    current = preview_existing_pr(
      client,
      comment_candidate('create_pr_comment', body),
    )
    stale = preview_existing_pr(
      client,
      comment_candidate(
        'create_pr_comment',
        body,
        reviewed_source_sha='c' * 40,
      ),
    )
    self.assertEqual(current.status, 'READY')
    self.assertEqual(stale.status, 'READY')
    self.assertEqual(
      current.envelope['proposal']['reviewed_source_sha'],
      'a' * 40,
    )
    self.assertEqual(
      current.envelope['proposal']['reviewed_destination_sha'],
      'b' * 40,
    )
    self.assertNotEqual(current.proposal_sha256, stale.proposal_sha256)
    create = preview_create_pr(client, create_candidate())
    self.assertIsNone(create.envelope['proposal']['reviewed_source_sha'])
    self.assertIsNone(create.envelope['proposal']['reviewed_destination_sha'])

  def test_inline_comment_read_back_anchor_mismatch_is_failed(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}, 'inline': {'path': 'src/a.ts', 'to': 10}}
    preview = preview_existing_pr(client, comment_candidate('create_inline_comment', body))
    client.comment_readback_override = {'inline': {'path': 'src/a.ts', 'to': 11}}
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.operations['op-1'].outcome, 'failed')

  def test_pr_level_comment_read_back_rejects_inline_shape(self):
    client = FakeClient()
    body = {'content': {'raw': 'finding'}}
    preview = preview_existing_pr(client, comment_candidate('create_pr_comment', body))
    client.comment_readback_override = {'inline': {'path': 'src/a.ts', 'to': 10}}
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.operations['op-1'].outcome, 'failed')

  def test_get_failure_after_known_write_is_outcome_unknown(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.get_failure_after_write = True
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'outcome_unknown')
    self.assertEqual(client.write_count, 1)

  def test_actor_switch_and_unknown_operation_write_zero_requests(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.actor_uuid = '{other}'
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'invalid')
    bad = candidate()
    bad['operations'][0]['type'] = 'merge'
    unsupported_client = FakeClient()
    unsupported = preview_existing_pr(unsupported_client, bad)
    self.assertEqual(unsupported.status, 'UNSUPPORTED_OPERATION')
    self.assertIsNone(unsupported.envelope)
    self.assertEqual(client.write_count + unsupported_client.write_count, 0)

  def test_started_journal_requires_read_only_reconciliation(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      write_journal(
        path,
        journal_value(preview, 's1', 'applying', 'started'),
      )
      with self.assertRaises(ReconciliationRequired):
        apply_proposal(client, preview.envelope, approval(preview), state_root, 's1')
      report = reconcile_journal(client, path)
    self.assertEqual(report['batch_state'], 'applying')
    self.assertEqual(client.write_count, 0)

  def test_completed_journal_cannot_be_applied_again(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      first = apply_proposal(client, preview.envelope, approval(preview), state_root, 's1')
      second = apply_proposal(client, preview.envelope, approval(preview), state_root, 's1')
    self.assertEqual(first.batch_state, 'completed')
    self.assertEqual(second.batch_state, 'invalid')
    self.assertEqual(client.write_count, 1)

  def test_symlink_state_root_is_rejected_before_write(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      actual = Path(root) / 'actual'
      actual.mkdir()
      linked = Path(root) / 'linked'
      os.symlink(actual, linked)
      with self.assertRaises(ValueError):
        apply_proposal(client, preview.envelope, approval(preview), linked, 's1')
    self.assertEqual(client.write_count, 0)

  def test_existing_directory_below_symlink_parent_is_rejected_before_write(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      base = Path(root)
      actual = base / 'actual'
      child = actual / 'child'
      child.mkdir(parents=True)
      linked = base / 'linked'
      os.symlink(actual, linked)
      with self.assertRaises(ValueError):
        apply_proposal(client, preview.envelope, approval(preview), linked / 'child', 's1')
    self.assertEqual(client.write_count, 0)

  def test_invalid_description_markers_are_draft_only(self):
    client = FakeClient(description='<!-- pr-review-testing:start -->\nbroken')
    result = preview_existing_pr(client, candidate())
    self.assertEqual(result.status, 'DRAFT_ONLY_INVALID_MARKERS')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_create_pr_with_unmanaged_description_is_draft_only(self):
    client = FakeClient()
    value = create_candidate()
    value['operations'][0]['request_body']['description'] = '作者文字'
    result = preview_create_pr(client, value)
    self.assertEqual(result.status, 'DRAFT_ONLY_UNMANAGED_DESCRIPTION')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_rehashed_method_endpoint_and_read_back_smuggling_is_rejected(self):
    for field, value in (
      ('method', 'DELETE'),
      ('endpoint', '/repositories/ws/repo/pullrequests/7/merge'),
      ('read_back', {'fields': ['state']}),
    ):
      with self.subTest(field=field):
        client = FakeClient()
        preview = preview_existing_pr(client, candidate())
        envelope = deepcopy(preview.envelope)
        envelope['proposal']['operations'][0][field] = value
        envelope = resign(envelope)
        approved = {
          'session_id': 's1',
          'user_message_id': 'u1',
          'proposal_sha256': envelope['proposal_sha256'],
          'approved_operation_ids': ['op-1'],
        }
        with tempfile.TemporaryDirectory() as root:
          state_root = Path(root)
          with self.assertRaises(ValueError):
            apply_proposal(client, envelope, approved, state_root, 's1')
          self.assertFalse((state_root / 'locks').exists())
        self.assertEqual(client.write_count, 0)

  def test_create_branch_target_smuggling_is_rejected_before_write(self):
    client = FakeClient()
    preview = preview_create_pr(client, create_candidate())
    envelope = deepcopy(preview.envelope)
    envelope['proposal']['operations'][0]['request_body']['source']['branch']['name'] = 'other'
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError):
        apply_proposal(client, envelope, approved, Path(root), 's1')
    self.assertEqual(client.write_count, 0)

  def test_rehashed_request_body_side_effect_smuggling_is_rejected(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    envelope = deepcopy(preview.envelope)
    envelope['proposal']['operations'][0]['request_body']['title'] = 'smuggled'
    envelope = resign(envelope)
    approved = {
      'session_id': 's1',
      'user_message_id': 'u1',
      'proposal_sha256': envelope['proposal_sha256'],
      'approved_operation_ids': ['op-1'],
    }
    with tempfile.TemporaryDirectory() as root:
      with self.assertRaises(ValueError):
        apply_proposal(client, envelope, approved, Path(root), 's1')
    self.assertEqual(client.write_count, 0)

  def test_dot_session_ids_are_rejected_and_cannot_replay_approval(self):
    for session_id in ('.', '..'):
      with self.subTest(session_id=session_id):
        client = FakeClient()
        preview = preview_existing_pr(client, candidate())
        approved = approval(preview, session_id)
        with tempfile.TemporaryDirectory() as root:
          state_root = Path(root)
          for _attempt in range(2):
            with self.assertRaises(ValueError):
              apply_proposal(client, preview.envelope, approved, state_root, session_id)
          with self.assertRaises(ValueError):
            session_journal_path(state_root, session_id, preview.batch_id)
          self.assertEqual(list(state_root.iterdir()), [])
        self.assertEqual(client.write_count, 0)

  def test_session_journal_parent_is_exact_session_directory(self):
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', 'a' * 12)
      self.assertEqual(path.parent, state_root / 'sessions' / 's1')

  def test_invalid_session_id_is_rejected_before_state_touch(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    approved = approval(preview, '../other')
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      with self.assertRaises(ValueError):
        apply_proposal(client, preview.envelope, approved, state_root, '../other')
      self.assertEqual(list(state_root.iterdir()), [])
    self.assertEqual(client.write_count, 0)

  def test_applying_journal_blocks_same_batch_from_another_session(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      write_journal(
        path,
        journal_value(preview, 's1', 'applying', 'started'),
      )
      with self.assertRaises(ReconciliationRequired):
        apply_proposal(
          client,
          preview.envelope,
          approval(preview, 's2'),
          state_root,
          's2',
        )
    self.assertEqual(client.write_count, 0)

  def test_outcome_unknown_journal_requires_reconciliation_across_sessions(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      write_journal(
        path,
        journal_value(preview, 's1', 'outcome_unknown', 'outcome_unknown'),
      )
      with self.assertRaises(ReconciliationRequired):
        apply_proposal(
          client,
          preview.envelope,
          approval(preview, 's2'),
          state_root,
          's2',
        )
    self.assertEqual(client.write_count, 0)

  def test_pending_journal_hash_mismatch_is_invalid_without_write(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      write_journal(path, {
        'version': 1,
        'session_id': 's1',
        'batch_id': preview.batch_id,
        'proposal_sha256': '0' * 64,
        'batch_state': 'pending',
        'proposal': preview.envelope['proposal'],
        'operations': {'op-1': {'state': 'not_attempted', 'outcome': 'not_attempted'}},
      })
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        state_root,
        's1',
      )
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(client.write_count, 0)

  def test_pending_journal_missing_session_is_invalid_without_write(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      value = journal_value(preview, 's1', 'pending', 'not_attempted')
      del value['session_id']
      write_journal(path, value)
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview),
        state_root,
        's1',
      )
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(client.write_count, 0)

  def test_pending_journal_cannot_cross_session_boundary(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', preview.batch_id)
      write_journal(path, {
        'version': 1,
        'session_id': 's1',
        'batch_id': preview.batch_id,
        'proposal_sha256': preview.proposal_sha256,
        'batch_state': 'pending',
        'proposal': preview.envelope['proposal'],
        'operations': {'op-1': {'state': 'not_attempted', 'outcome': 'not_attempted'}},
      })
      result = apply_proposal(
        client,
        preview.envelope,
        approval(preview, 's2'),
        state_root,
        's2',
      )
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(client.write_count, 0)

  def test_journal_and_directories_have_private_modes(self):
    with tempfile.TemporaryDirectory() as root:
      path = session_journal_path(Path(root), 's1', 'a' * 12)
      write_journal(path, {'batch_state': 'pending'})
      self.assertEqual(stat_mode(path), 0o600)
      self.assertEqual(stat_mode(path.parent), 0o700)
      self.assertEqual(stat_mode(path.parent.parent), 0o700)

  def test_private_directory_creation_is_concurrency_safe(self):
    failures = []
    barrier = threading.Barrier(16)
    with tempfile.TemporaryDirectory() as root:
      path = Path(root) / 'state' / 'sessions' / 's1'

      def create():
        barrier.wait(timeout=5)
        try:
          ensure_private_directory(path)
        except Exception as error:
          failures.append(type(error).__name__)

      threads = [threading.Thread(target=create) for _ in range(16)]
      for thread in threads:
        thread.start()
      for thread in threads:
        thread.join(timeout=5)
      self.assertEqual(failures, [])
      self.assertEqual(stat_mode(path), 0o700)

  def test_symlink_journal_is_rejected(self):
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      path = session_journal_path(state_root, 's1', 'a' * 12)
      target = state_root / 'target.json'
      target.write_text('{}', encoding='utf-8')
      os.symlink(target, path)
      with self.assertRaises(ValueError):
        write_journal(path, {'batch_state': 'pending'})

  def test_precondition_transport_failure_records_invalid_without_write(self):
    preview = preview_existing_pr(FakeClient(), candidate())
    client = PreconditionFailureClient()
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
      journal = read_journal(Path(result.journal_path))
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(journal['batch_state'], 'invalid')
    self.assertEqual(client.write_count, 0)

  def test_ambiguous_http_status_after_side_effect_is_unknown_and_not_retried(self):
    for status in (408, 500, 504):
      with self.subTest(status=status):
        client = HttpAfterWriteClient(status)
        preview = preview_existing_pr(client, candidate())
        with tempfile.TemporaryDirectory() as root:
          state_root = Path(root)
          result = apply_proposal(
            client,
            preview.envelope,
            approval(preview),
            state_root,
            's1',
          )
          with self.assertRaises(ReconciliationRequired):
            apply_proposal(
              client,
              preview.envelope,
              approval(preview),
              state_root,
              's1',
            )
        self.assertEqual(result.batch_state, 'outcome_unknown')
        self.assertEqual(result.operations['op-1'].state, 'outcome_unknown')
        self.assertEqual(client.pr['description'], MANAGED_DESCRIPTION)
        self.assertEqual(client.write_count, 1)

  def test_explicit_http_rejection_is_known_failed_outcome(self):
    client = RejectingClient()
    preview = preview_existing_pr(client, candidate())
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(result.operations['op-1'].state, 'failed')
    self.assertEqual(result.operations['op-1'].outcome, 'failed')
    self.assertEqual(client.write_count, 1)

  def test_malformed_pr_snapshot_after_write_is_outcome_unknown(self):
    client = FakeClient()
    preview = preview_existing_pr(client, candidate())
    client.pr_readback_override = {'source': {}}
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'outcome_unknown')
    self.assertEqual(result.operations['op-1'].state, 'outcome_unknown')
    self.assertEqual(client.write_count, 1)

  def test_unknown_read_back_reconciles_get_only_without_retry(self):
    client = CountingClient()
    preview = preview_existing_pr(client, candidate())
    client.get_failure_after_write = True
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root)
      result = apply_proposal(client, preview.envelope, approval(preview), state_root, 's1')
      journal = read_journal(Path(result.journal_path))
      self.assertEqual(journal['batch_state'], 'outcome_unknown')
      client.get_failure_after_write = False
      client.get_count = 0
      report = reconcile_journal(client, Path(result.journal_path))
      reconcile_gets = client.get_count
      with self.assertRaises(ReconciliationRequired):
        apply_proposal(client, preview.envelope, approval(preview), state_root, 's1')
    encoded = json.dumps(report, sort_keys=True)
    self.assertNotIn('proposal', report)
    self.assertNotIn('snapshot', report)
    self.assertEqual(report['candidate_count'], 1)
    self.assertFalse(report['ambiguous'])
    self.assertEqual(len(report['candidates']), 1)
    self.assertTrue(report['candidates'][0]['matches'])
    self.assertGreater(reconcile_gets, 0)
    self.assertEqual(client.write_count, 1)

  def test_create_pr_reconcile_short_hashes_match_after_commit_lookup(self):
    client = FakeClient(
      source_pr_sha=SOURCE_FULL_SHA,
      destination_pr_sha=DESTINATION_FULL_SHA,
      create_source_pr_sha=SOURCE_SHORT_SHA,
      create_destination_pr_sha=DESTINATION_SHORT_SHA,
      create_source_commit_sha=SOURCE_FULL_SHA,
      create_destination_commit_sha=DESTINATION_FULL_SHA,
    )
    preview = preview_create_pr(client, create_candidate())
    operation = preview.envelope['proposal']['operations'][0]
    client.create_pr('ws', 'repo', operation['request_body'])
    with tempfile.TemporaryDirectory() as root:
      path = session_journal_path(Path(root), 's1', preview.batch_id)
      journal = journal_value(
        preview,
        's1',
        'outcome_unknown',
        'outcome_unknown',
      )
      journal['operations']['op-1']['resource_id'] = 8
      write_journal(path, journal)
      report = reconcile_journal(client, path)
    self.assertEqual(report['candidate_count'], 1)
    self.assertFalse(report['ambiguous'])
    self.assertTrue(report['candidates'][0]['matches'])
    self.assertEqual(client.commit_requests, [
      ('ws', 'repo', SOURCE_SHORT_SHA),
      ('ws', 'repo', DESTINATION_SHORT_SHA),
    ])
    self.assertEqual(client.write_count, 1)

  def test_create_pr_reconcile_commit_lookup_mismatch_is_ambiguous(self):
    client = FakeClient(
      source_pr_sha=SOURCE_FULL_SHA,
      destination_pr_sha=DESTINATION_FULL_SHA,
      create_source_pr_sha=SOURCE_SHORT_SHA,
      create_destination_pr_sha=DESTINATION_SHORT_SHA,
      create_source_commit_sha=SOURCE_FULL_SHA,
      create_destination_commit_sha=DESTINATION_FULL_SHA,
    )
    preview = preview_create_pr(client, create_candidate())
    operation = preview.envelope['proposal']['operations'][0]
    client.create_pr('ws', 'repo', operation['request_body'])
    client.commit_readback_override[SOURCE_SHORT_SHA] = {'hash': 'c' * 40}
    with tempfile.TemporaryDirectory() as root:
      path = session_journal_path(Path(root), 's1', preview.batch_id)
      journal = journal_value(
        preview,
        's1',
        'outcome_unknown',
        'outcome_unknown',
      )
      journal['operations']['op-1']['resource_id'] = 8
      write_journal(path, journal)
      report = reconcile_journal(client, path)
    self.assertEqual(report['candidate_count'], 0)
    self.assertTrue(report['ambiguous'])
    self.assertEqual(
      client.commit_requests,
      [('ws', 'repo', SOURCE_SHORT_SHA)],
    )
    self.assertEqual(client.write_count, 1)

  def test_reconcile_accepts_valid_ordered_active_journals_get_only(self):
    cases = (
      (
        'applying',
        (('completed', 'completed'), ('started', None), ('not_attempted', 'not_attempted')),
      ),
      (
        'outcome_unknown',
        (
          ('completed', 'completed'),
          ('outcome_unknown', 'outcome_unknown'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
    )
    for batch_state, facts in cases:
      with self.subTest(batch_state=batch_state), tempfile.TemporaryDirectory() as root:
        client = CountingClient(description=MANAGED_DESCRIPTION.replace('none', 'update-2'))
        preview = multi_operation_preview(client)
        client.get_count = 0
        state_root = Path(root)
        path = session_journal_path(state_root, 's1', preview.batch_id)
        write_journal(path, journal_with_facts(preview, batch_state, facts))
        report = reconcile_journal(client, path)
        self.assertEqual(report['journal_state'], 'valid')
        self.assertEqual(report['candidate_count'], 1)
        self.assertGreater(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_reconcile_rejects_unreachable_cross_operation_states_before_get(self):
    invalid_cases = (
      (
        'mixed_unknown_and_started',
        'outcome_unknown',
        (
          ('outcome_unknown', 'outcome_unknown'),
          ('started', None),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'two_started',
        'applying',
        (('started', None), ('started', None), ('not_attempted', 'not_attempted')),
      ),
      (
        'completed_after_started',
        'applying',
        (('started', None), ('completed', 'completed'), ('not_attempted', 'not_attempted')),
      ),
      (
        'completed_after_failure',
        'completed',
        (('failed', 'failed'), ('completed', 'completed'), ('not_attempted', 'not_attempted')),
      ),
      (
        'completed_after_post_write_drift',
        'completed',
        (
          ('completed', 'post_write_drift'),
          ('completed', 'completed'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'two_unknown',
        'outcome_unknown',
        (
          ('outcome_unknown', 'outcome_unknown'),
          ('outcome_unknown', 'outcome_unknown'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'failed_while_applying',
        'applying',
        (('completed', 'completed'), ('failed', 'failed'), ('not_attempted', 'not_attempted')),
      ),
      (
        'failure_in_invalid_batch',
        'invalid',
        (('completed', 'completed'), ('failed', 'failed'), ('not_attempted', 'not_attempted')),
      ),
    )
    for name, batch_state, facts in invalid_cases:
      with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
        client = CountingClient()
        preview = multi_operation_preview(client)
        client.get_count = 0
        state_root = Path(root)
        path = session_journal_path(state_root, 's1', preview.batch_id)
        write_journal(path, journal_with_facts(preview, batch_state, facts))
        report = reconcile_journal(client, path)
        self.assertEqual(report['journal_state'], 'invalid')
        self.assertTrue(report['ambiguous'])
        self.assertEqual(report['candidate_count'], 0)
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_reconcile_accepts_only_reachable_terminal_journal_sequences(self):
    valid_cases = (
      (
        'completed_all',
        'completed',
        (('completed', 'completed'),) * 3,
      ),
      (
        'completed_failed_suffix',
        'completed',
        (
          ('completed', 'completed'),
          ('failed', 'failed'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'completed_drift_suffix',
        'completed',
        (
          ('completed', 'completed'),
          ('completed', 'post_write_drift'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'invalid_completed_prefix',
        'invalid',
        (
          ('completed', 'completed'),
          ('not_attempted', 'not_attempted'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
      (
        'applying_between_operations',
        'applying',
        (
          ('completed', 'completed'),
          ('not_attempted', 'not_attempted'),
          ('not_attempted', 'not_attempted'),
        ),
      ),
    )
    for name, batch_state, facts in valid_cases:
      with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
        client = CountingClient()
        preview = multi_operation_preview(client)
        client.get_count = 0
        state_root = Path(root)
        path = session_journal_path(state_root, 's1', preview.batch_id)
        write_journal(path, journal_with_facts(preview, batch_state, facts))
        report = reconcile_journal(client, path)
        self.assertEqual(report['journal_state'], 'valid')
        self.assertEqual(report['candidate_count'], 0)
        self.assertEqual(client.get_count, 0)
        self.assertEqual(client.write_count, 0)

  def test_reconcile_rejects_forged_journals_before_network(self):
    preview = preview_existing_pr(FakeClient(), candidate())
    cases = (
      'proposal_hash',
      'batch_id',
      'path_filename',
      'path_session',
      'target',
      'snapshot',
      'operation_id',
      'operation_type',
      'operation_state',
      'missing_field',
      'credential',
    )
    for case in cases:
      with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
        state_root = Path(root)
        value = journal_value(preview, 's1', 'applying', 'started')
        session_id = 's1'
        batch_id = preview.batch_id
        if case == 'proposal_hash':
          value['proposal_sha256'] = '0' * 64
        elif case == 'batch_id':
          value['batch_id'] = 'b' * 12
        elif case == 'path_filename':
          batch_id = 'b' * 12
        elif case == 'path_session':
          session_id = 's2'
        elif case == 'target':
          value['target']['repo'] = 'other'
        elif case == 'snapshot':
          value['snapshot']['source_sha'] = 'c' * 40
        elif case == 'operation_id':
          value['operations']['other'] = value['operations'].pop('op-1')
        elif case == 'operation_type':
          value['operations']['op-1']['type'] = 'create_pr_comment'
        elif case == 'operation_state':
          value['operations']['op-1']['state'] = 'unknown'
        elif case == 'missing_field':
          del value['version']
        else:
          value['proposal']['operations'][0]['request_body']['description'] = (
            'ghp_abcdefghijklmnopqrstuvwxyz123456'
          )
          digest = proposal_sha256(value['proposal'])
          value['proposal_sha256'] = digest
          value['batch_id'] = digest[:12]
          batch_id = value['batch_id']
        path = session_journal_path(state_root, session_id, batch_id)
        write_journal(path, value)
        client = CountingClient()
        report = reconcile_journal(client, path)
        self.assertEqual(report['journal_state'], 'invalid')
        self.assertTrue(report['ambiguous'])
        self.assertEqual(report['candidate_count'], 0)
        self.assertEqual(client.get_count, 0)
        self.assertNotIn('ghp_abcdefghijklmnopqrstuvwxyz123456', json.dumps(report))

  def test_missing_reconcile_journal_does_not_create_directories(self):
    client = FakeClient()
    with tempfile.TemporaryDirectory() as root:
      state_root = Path(root) / 'missing-state'
      path = journal_path(state_root, 's1', 'a' * 12)
      with self.assertRaises(ValueError):
        reconcile_journal(client, path)
      self.assertFalse(state_root.exists())

  def test_all_allowlisted_operations_complete_with_exact_read_back(self):
    for operation_type in ('create_pr', 'update_description', 'create_inline_comment', 'create_pr_comment'):
      with self.subTest(operation_type=operation_type):
        client = FakeClient()
        preview = preview_for_type(client, operation_type)
        with tempfile.TemporaryDirectory() as root:
          result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
        self.assertEqual(result.operations['op-1'].state, 'completed')
        self.assertEqual(result.operations['op-1'].outcome, 'completed')
        self.assertTrue(result.operations['op-1'].resource_url)
        self.assertEqual(client.write_count, 1)

  def test_all_allowlisted_operations_treat_read_back_get_failure_as_unknown(self):
    for operation_type in ('create_pr', 'update_description', 'create_inline_comment', 'create_pr_comment'):
      with self.subTest(operation_type=operation_type):
        client = FakeClient()
        preview = preview_for_type(client, operation_type)
        client.get_failure_after_write = True
        with tempfile.TemporaryDirectory() as root:
          result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
        self.assertEqual(result.batch_state, 'outcome_unknown')
        self.assertEqual(result.operations['op-1'].state, 'outcome_unknown')
        self.assertEqual(client.write_count, 1)

  def test_read_back_mismatch_takes_precedence_over_commit_drift(self):
    for operation_type in (
      'create_pr',
      'update_description',
      'create_inline_comment',
      'create_pr_comment',
    ):
      with self.subTest(operation_type=operation_type):
        client = FakeClient()
        preview = preview_for_type(client, operation_type)
        if operation_type == 'create_pr':
          client.create_author_override = '{other}'
        elif operation_type == 'update_description':
          client.pr_readback_override = {'description': 'different'}
        elif operation_type == 'create_inline_comment':
          client.comment_readback_override = {
            'inline': {'path': 'src/a.ts', 'to': 11},
          }
        else:
          client.comment_readback_override = {
            'inline': {'path': 'src/a.ts', 'to': 10},
          }
        client.drift_after_write = True
        with tempfile.TemporaryDirectory() as root:
          result = apply_proposal(
            client,
            preview.envelope,
            approval(preview),
            Path(root),
            's1',
          )
        self.assertEqual(result.batch_state, 'completed')
        self.assertEqual(result.operations['op-1'].state, 'failed')
        self.assertEqual(result.operations['op-1'].outcome, 'failed')
        self.assertEqual(client.write_count, 1)

  def test_all_allowlisted_operations_report_post_write_drift(self):
    for operation_type in ('create_pr', 'update_description', 'create_inline_comment', 'create_pr_comment'):
      with self.subTest(operation_type=operation_type):
        client = FakeClient()
        preview = preview_for_type(client, operation_type)
        client.drift_after_write = True
        with tempfile.TemporaryDirectory() as root:
          result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
        self.assertEqual(result.batch_state, 'completed')
        self.assertEqual(result.operations['op-1'].state, 'completed')
        self.assertEqual(result.operations['op-1'].outcome, 'post_write_drift')
        self.assertEqual(client.write_count, 1)


class UpdateTitleTests(unittest.TestCase):
  """Title is author-owned and feeds release notifications, so it takes the owner-only
  stop rather than the additive-comment path — these prove that at every enforcement
  point, not just at preview."""

  def test_update_title_applies_end_to_end(self):
    client = FakeClient()
    preview = preview_existing_pr(client, title_candidate())
    self.assertEqual(preview.status, 'READY')
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'completed')
    self.assertEqual(result.operations['op-1'].outcome, 'completed')
    self.assertEqual(client.write_count, 1)
    self.assertEqual(client.pr['title'], 'Reworked title')

  def test_update_title_leaves_description_untouched(self):
    """A title PUT must not blank the description the way a whole-object write would."""
    client = FakeClient(description=MANAGED_DESCRIPTION)
    preview = preview_existing_pr(client, title_candidate())
    with tempfile.TemporaryDirectory() as root:
      apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(client.pr['description'], MANAGED_DESCRIPTION)

  def test_foreign_author_title_is_blocked_at_preview(self):
    client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    result = preview_existing_pr(client, title_candidate())
    self.assertEqual(result.status, 'READ_ONLY_FOREIGN_AUTHOR')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_non_open_pr_title_is_blocked_at_preview(self):
    client = FakeClient(state='MERGED')
    result = preview_existing_pr(client, title_candidate())
    self.assertEqual(result.status, 'READ_ONLY_PR_NOT_OPEN')
    self.assertIsNone(result.envelope)
    self.assertEqual(client.write_count, 0)

  def test_foreign_author_title_still_blocked_at_apply(self):
    """Forge a READY envelope on an own PR, then apply it against a foreign one."""
    preview = preview_existing_pr(FakeClient(), title_candidate())
    self.assertEqual(preview.status, 'READY')
    foreign_client = FakeClient(actor_uuid='{actor}', author_uuid='{other}')
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(
        foreign_client,
        preview.envelope,
        approval(preview),
        Path(root),
        's1',
      )
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(foreign_client.write_count, 0)

  def test_title_drift_between_preview_and_apply_performs_zero_writes(self):
    """title_sha256 is the optimistic lock: someone else renaming the PR after preview
    must invalidate the batch instead of silently overwriting their title."""
    client = FakeClient()
    preview = preview_existing_pr(client, title_candidate())
    self.assertEqual(preview.status, 'READY')
    client.pr['title'] = 'Renamed by someone else'
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
    self.assertEqual(result.batch_state, 'invalid')
    self.assertEqual(client.write_count, 0)
    self.assertEqual(client.pr['title'], 'Renamed by someone else')

  def test_empty_title_is_rejected(self):
    client = FakeClient()
    with self.assertRaises(ValueError):
      preview_existing_pr(client, title_candidate(title=''))
    self.assertEqual(client.write_count, 0)

  def test_unknown_title_read_back_reconciles_against_the_pr(self):
    """reconcile's else-branch treats an unhandled type as a comment and looks up a
    comment id; a title must resolve through get_pr instead. matches=True proves it."""
    client = CountingClient()
    preview = preview_existing_pr(client, title_candidate())
    client.get_failure_after_write = True
    with tempfile.TemporaryDirectory() as root:
      result = apply_proposal(client, preview.envelope, approval(preview), Path(root), 's1')
      journal = read_journal(Path(result.journal_path))
      self.assertEqual(journal['batch_state'], 'outcome_unknown')
      client.get_failure_after_write = False
      report = reconcile_journal(client, Path(result.journal_path))
    self.assertEqual(report['candidate_count'], 1)
    self.assertFalse(report['ambiguous'])
    self.assertTrue(report['candidates'][0]['matches'])
    self.assertEqual(client.write_count, 1)

  def test_title_body_rejects_extra_fields(self):
    candidate_with_extra = title_candidate()
    candidate_with_extra['operations'][0]['request_body']['description'] = 'sneaky'
    client = FakeClient()
    with self.assertRaises(ValueError):
      preview_existing_pr(client, candidate_with_extra)
    self.assertEqual(client.write_count, 0)


if __name__ == '__main__':
  unittest.main()
