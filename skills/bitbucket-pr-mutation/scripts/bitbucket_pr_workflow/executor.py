from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bitbucket_pr_workflow.api import ApiError, ApiTransportError
from bitbucket_pr_workflow.core import (
  canonical_json_bytes,
  proposal_sha256,
  unique_batch_id,
  validate_approval,
)
from bitbucket_pr_workflow.description import is_put_eligible, parse_description
from bitbucket_pr_workflow.review import is_full_sha


SESSION_ID = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
BATCH_ID = re.compile(r'^[0-9a-f]{12,64}$')
OPERATION_KEYS = frozenset({
  'operation_id',
  'type',
  'finding_uid',
  'method',
  'endpoint',
  'request_body',
  'read_back',
})
SENSITIVE_KEYS = frozenset({
  'authorization',
  'cookie',
  'token',
  'accesstoken',
  'refreshtoken',
  'password',
  'secret',
  'apikey',
  'privatekey',
  'clientsecret',
})
BASIC_BOUNDARY = r'(?![A-Za-z0-9+/=_-])'
BEARER_BOUNDARY = r'(?![A-Za-z0-9._~+/=-])'
BASIC_VALUE = re.compile(
  r'(?i)\bBasic[ \t]+([A-Za-z0-9+/]+={0,2})' + BASIC_BOUNDARY,
)
BEARER_VALUE = re.compile(
  r'(?i)\bBearer[ \t]+([A-Za-z0-9._~+/-]+={0,2})'
  + BEARER_BOUNDARY,
)
PRIVATE_KEY_LITERAL = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----')
SAFE_PATH_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,63}$')
PROVIDER_TOKEN_PATTERNS = (
  re.compile(r'\bghp_[A-Za-z0-9]{20,}\b'),
  re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
  re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b'),
  re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
  re.compile(r'\bxox[a-z]-[A-Za-z0-9-]{20,}\b'),
)
OPERATION_RULES = {
  'create_pr': {
    'method': 'POST',
    'read_back': (
      'author.uuid',
      'repo_uuid',
      'branches',
      'source_sha',
      'destination_sha',
      'title',
      'description',
      'state',
      'links.html.href',
    ),
  },
  'update_description': {
    'method': 'PUT',
    'read_back': ('description',),
  },
  'update_title': {
    'method': 'PUT',
    'read_back': ('title',),
  },
  'create_inline_comment': {
    'method': 'POST',
    'read_back': ('content.raw', 'inline.path', 'inline.to', 'inline.from'),
  },
  'create_pr_comment': {
    'method': 'POST',
    'read_back': ('content.raw', 'inline'),
  },
}


JOURNAL_KEYS = frozenset({
  'version',
  'session_id',
  'batch_id',
  'proposal_sha256',
  'batch_state',
  'target',
  'snapshot',
  'proposal',
  'operations',
})
JOURNAL_OPERATION_KEYS = frozenset({
  'type',
  'state',
  'outcome',
  'resource_id',
  'resource_url',
})
BATCH_STATES = frozenset({
  'pending',
  'applying',
  'outcome_unknown',
  'completed',
  'invalid',
})
OPERATION_STATE_OUTCOMES = {
  'not_attempted': frozenset({'not_attempted'}),
  'started': frozenset({None}),
  'outcome_unknown': frozenset({'outcome_unknown'}),
  'failed': frozenset({'failed'}),
  'completed': frozenset({'completed', 'post_write_drift'}),
}


class ReconciliationRequired(RuntimeError):
  pass


@dataclass(frozen=True)
class InspectResult:
  status: str
  snapshot: Mapping[str, Any]
  drafts: tuple[str, ...]


@dataclass(frozen=True)
class PreviewResult:
  status: str
  envelope: Mapping[str, Any] | None
  proposal_sha256: str | None
  batch_id: str | None
  drafts: tuple[str, ...]


@dataclass(frozen=True)
class OperationResult:
  state: str
  outcome: str
  resource_url: str | None


@dataclass(frozen=True)
class ApplyResult:
  batch_state: str
  operations: Mapping[str, OperationResult]
  journal_path: str


def _absolute(path: Path) -> Path:
  return Path(os.path.abspath(os.fspath(path.expanduser())))


def _valid_session_id(value: str) -> bool:
  return value not in {'.', '..'} and SESSION_ID.fullmatch(value) is not None


def _normalized_key(value: object) -> str:
  return re.sub(r'[^a-z0-9]', '', str(value).lower())


def _is_basic_credential(value: str) -> bool:
  for match in BASIC_VALUE.finditer(value):
    token = match.group(1)
    try:
      decoded = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
      continue
    if b':' in decoded:
      return True
  return False


def _is_jwt_token(token: str) -> bool:
  parts = token.split('.')
  if len(parts) != 3 or not all(parts):
    return False
  if any(re.fullmatch(r'[A-Za-z0-9_-]+', part) is None for part in parts):
    return False
  decoded = []
  for part in parts[:2]:
    padding = '=' * (-len(part) % 4)
    try:
      value = json.loads(base64.urlsafe_b64decode(part + padding))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
      return False
    decoded.append(value)
  return all(isinstance(value, Mapping) for value in decoded)


def _is_bearer_credential(value: str) -> bool:
  for match in BEARER_VALUE.finditer(value):
    token = match.group(1)
    candidates = (token, token.rstrip('.')) if token.endswith('.') else (token,)
    if any(_is_jwt_token(candidate) for candidate in candidates):
      return True
  return False


def _credential_literal_category(value: str) -> str | None:
  if PRIVATE_KEY_LITERAL.search(value):
    return 'private_key_literal'
  if _is_basic_credential(value):
    return 'basic_credential'
  if _is_bearer_credential(value):
    return 'bearer_credential'
  if any(pattern.search(value) for pattern in PROVIDER_TOKEN_PATTERNS):
    return 'provider_token'
  return None


def _mapping_key_path(path: str, key: object, index: int) -> str:
  masked = f'{path}.<key:{index}>'
  if not isinstance(key, str) or SAFE_PATH_KEY.fullmatch(key) is None:
    return masked
  if _normalized_key(key) in SENSITIVE_KEYS:
    return masked
  if _credential_literal_category(key) is not None:
    return masked
  if (
    len(key) >= 20
    and any(character.isalpha() for character in key)
    and any(character.isdigit() for character in key)
  ):
    return masked
  return f'{path}.{key}'


def credential_issue(value: Any, path: str = '$') -> tuple[str, str] | None:
  if isinstance(value, Mapping):
    for index, (key, item) in enumerate(value.items()):
      masked_path = f'{path}.<key:{index}>'
      key_category = _credential_literal_category(str(key))
      if key_category is not None:
        return key_category, masked_path
      item_path = _mapping_key_path(path, key, index)
      if _normalized_key(key) in SENSITIVE_KEYS and item not in (None, ''):
        return 'sensitive_key', masked_path
      issue = credential_issue(item, item_path)
      if issue is not None:
        return issue
    return None
  if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
    for index, item in enumerate(value):
      issue = credential_issue(item, f'{path}[{index}]')
      if issue is not None:
        return issue
    return None
  if isinstance(value, str):
    category = _credential_literal_category(value)
    if category is not None:
      return category, path
  return None


def validate_no_credentials(value: Any, path: str = '$') -> None:
  issue = credential_issue(value, path)
  if issue is not None:
    category, issue_path = issue
    raise ValueError(f'credential-shaped content ({category}) at {issue_path}')


def _ancestry_anchor(path: Path) -> Path:
  for anchor in (_absolute(Path.home()), _absolute(Path(tempfile.gettempdir()))):
    try:
      path.relative_to(anchor)
      return anchor
    except ValueError:
      continue
  return Path(path.anchor)


def _reject_symlink_ancestry(path: Path) -> None:
  absolute = _absolute(path)
  anchor = _ancestry_anchor(absolute)
  current = anchor
  for part in absolute.relative_to(anchor).parts:
    current = current / part
    try:
      mode = os.lstat(current).st_mode
    except FileNotFoundError:
      return
    if stat.S_ISLNK(mode):
      raise ValueError('state directory has a symlink ancestor')


def ensure_private_directory(path: Path) -> None:
  absolute = _absolute(path)
  _reject_symlink_ancestry(absolute)
  if absolute.exists():
    if absolute.is_symlink() or not absolute.is_dir():
      raise ValueError('unsafe state directory')
    os.chmod(absolute, 0o700)
    return
  missing = []
  ancestor = absolute
  while not ancestor.exists():
    missing.append(ancestor)
    parent = ancestor.parent
    if parent == ancestor:
      raise ValueError('unsafe state directory')
    ancestor = parent
  if ancestor.is_symlink() or not ancestor.is_dir():
    raise ValueError('state directory has a symlink ancestor')
  for directory in reversed(missing):
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
      raise ValueError('unsafe state directory')
    os.chmod(directory, 0o700)


def journal_path(root: Path, session_id: str, batch_id: str) -> Path:
  if not _valid_session_id(session_id) or not BATCH_ID.fullmatch(batch_id):
    raise ValueError('invalid session or batch id')
  sessions = _absolute(root) / 'sessions'
  session = sessions / session_id
  path = session / f'{batch_id}.json'
  if session.parent != sessions or path.parent != session:
    raise ValueError('invalid journal parent')
  return path


def session_journal_path(root: Path, session_id: str, batch_id: str) -> Path:
  path = journal_path(root, session_id, batch_id)
  ensure_private_directory(path.parent.parent.parent)
  ensure_private_directory(path.parent.parent)
  ensure_private_directory(path.parent)
  return path


def target_lock_name(target: Mapping[str, Any]) -> str:
  return hashlib.sha256(canonical_json_bytes(target)).hexdigest()


@contextmanager
def target_lock(root: Path, target: Mapping[str, Any]) -> Iterator[None]:
  absolute_root = _absolute(root)
  ensure_private_directory(absolute_root)
  locks = absolute_root / 'locks'
  ensure_private_directory(locks)
  path = locks / f'{target_lock_name(target)}.lock'
  flags = os.O_RDWR | os.O_CREAT
  if hasattr(os, 'O_NOFOLLOW'):
    flags |= os.O_NOFOLLOW
  descriptor = os.open(path, flags, 0o600)
  with os.fdopen(descriptor, 'a+', encoding='utf-8') as handle:
    mode = os.fstat(handle.fileno()).st_mode
    if not stat.S_ISREG(mode):
      raise ValueError('unsafe target lock')
    os.fchmod(handle.fileno(), 0o600)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_journal(path: Path, value: Mapping[str, Any]) -> None:
  absolute = _absolute(path)
  if absolute.is_symlink():
    raise ValueError('journal path must not be a symlink')
  ensure_private_directory(absolute.parent)
  descriptor, temporary_name = tempfile.mkstemp(
    dir=absolute.parent,
    prefix=f'.{absolute.name}.',
  )
  temporary = Path(temporary_name)
  try:
    os.fchmod(descriptor, 0o600)
    payload = canonical_json_bytes(value)
    offset = 0
    while offset < len(payload):
      offset += os.write(descriptor, payload[offset:])
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = -1
    if absolute.is_symlink():
      raise ValueError('journal path must not be a symlink')
    os.replace(temporary, absolute)
    os.chmod(absolute, 0o600)
    directory_flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
      directory_flags |= os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
      directory_flags |= os.O_NOFOLLOW
    directory = os.open(absolute.parent, directory_flags)
    try:
      os.fsync(directory)
    finally:
      os.close(directory)
  finally:
    if descriptor >= 0:
      os.close(descriptor)
    if temporary.exists():
      temporary.unlink()


def read_journal(path: Path) -> Mapping[str, Any] | None:
  absolute = _absolute(path)
  _reject_symlink_ancestry(absolute.parent)
  if not absolute.exists():
    return None
  flags = os.O_RDONLY
  if hasattr(os, 'O_NOFOLLOW'):
    flags |= os.O_NOFOLLOW
  try:
    descriptor = os.open(absolute, flags)
  except OSError as error:
    raise ValueError('unsafe journal path') from error
  with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
    mode = os.fstat(handle.fileno()).st_mode
    if not stat.S_ISREG(mode):
      raise ValueError('unsafe journal path')
    value = json.load(handle)
  if not isinstance(value, dict):
    raise ValueError('invalid journal')
  return value


def existing_batch_ids(root: Path) -> set[str]:
  sessions = _absolute(root) / 'sessions'
  if not sessions.exists() or sessions.is_symlink() or not sessions.is_dir():
    return set()
  result = set()
  for session in sessions.iterdir():
    if session.is_symlink() or not session.is_dir():
      continue
    for path in session.iterdir():
      if path.is_symlink() or not path.is_file() or path.suffix != '.json':
        continue
      if BATCH_ID.fullmatch(path.stem):
        result.add(path.stem)
  return result


def expected_endpoint(operation_type: str, target: Mapping[str, Any]) -> str:
  workspace = urllib.parse.quote(_required_string(target, 'workspace'), safe='')
  repo = urllib.parse.quote(_required_string(target, 'repo'), safe='')
  base = f'/repositories/{workspace}/{repo}/pullrequests'
  if operation_type == 'create_pr':
    return base
  pr_id = target.get('pr_id')
  if type(pr_id) is not int or pr_id <= 0:
    raise ValueError('invalid pull request id')
  if operation_type in {'update_description', 'update_title'}:
    return f'{base}/{pr_id}'
  if operation_type in {'create_inline_comment', 'create_pr_comment'}:
    return f'{base}/{pr_id}/comments'
  raise ValueError('unsupported operation type')


def validate_operation(operation: Mapping[str, Any], target: Mapping[str, Any]) -> None:
  if set(operation) != OPERATION_KEYS:
    raise ValueError('operation fields mismatch')
  operation_type = operation.get('type')
  rule = OPERATION_RULES.get(operation_type)
  if rule is None:
    raise ValueError('unsupported operation type')
  if operation.get('method') != rule['method']:
    raise ValueError('operation method mismatch')
  if operation.get('endpoint') != expected_endpoint(operation_type, target):
    raise ValueError('operation endpoint mismatch')
  read_back = operation.get('read_back')
  if not isinstance(read_back, Mapping):
    raise ValueError('read-back contract mismatch')
  if set(read_back) != {'fields'}:
    raise ValueError('read-back contract mismatch')
  if tuple(read_back.get('fields', ())) != rule['read_back']:
    raise ValueError('read-back contract mismatch')
  operation_id = operation.get('operation_id')
  request_body = operation.get('request_body')
  if not isinstance(operation_id, str) or not operation_id:
    raise ValueError('operation is not self-contained')
  if not isinstance(request_body, Mapping):
    raise ValueError('operation is not self-contained')
  _validate_request_body(operation_type, request_body)
  if operation_type == 'create_pr':
    if _branch_body(request_body['source']) != target.get('source_branch'):
      raise ValueError('create source branch mismatch')
    if _branch_body(request_body['destination']) != target.get('destination_branch'):
      raise ValueError('create destination branch mismatch')


def _resolve_pr_commit_sha(
  client: Any,
  workspace: str,
  repo: str,
  pr: Mapping[str, Any],
  side: str,
) -> str:
  original = _nested_string(pr, side, 'commit', 'hash')
  if is_full_sha(original):
    return original
  commit = client.get_commit(workspace, repo, original)
  resolved = _nested_string(commit, 'hash')
  if len(resolved) <= len(original):
    raise ValueError(f'{side} commit lookup did not enrich hash')
  if not resolved.lower().startswith(original.lower()):
    raise ValueError(f'{side} commit lookup hash mismatch')
  if not is_full_sha(resolved):
    raise ValueError(f'{side} commit lookup did not return a full hash')
  return resolved


def _resolved_pr_commit_pair(
  client: Any,
  workspace: str,
  repo: str,
  pr: Mapping[str, Any],
) -> tuple[str, str]:
  return (
    _resolve_pr_commit_sha(client, workspace, repo, pr, 'source'),
    _resolve_pr_commit_sha(client, workspace, repo, pr, 'destination'),
  )


def _pr_with_resolved_commits(
  client: Any,
  workspace: str,
  repo: str,
  pr: Mapping[str, Any],
) -> Mapping[str, Any]:
  source_sha, destination_sha = _resolved_pr_commit_pair(
    client,
    workspace,
    repo,
    pr,
  )
  source = pr.get('source')
  destination = pr.get('destination')
  if not isinstance(source, Mapping) or not isinstance(destination, Mapping):
    raise ValueError('pull request commit sides are missing')
  source_commit = source.get('commit')
  destination_commit = destination.get('commit')
  if not isinstance(source_commit, Mapping) or not isinstance(destination_commit, Mapping):
    raise ValueError('pull request commits are missing')
  return {
    **pr,
    'source': {
      **source,
      'commit': {**source_commit, 'hash': source_sha},
    },
    'destination': {
      **destination,
      'commit': {**destination_commit, 'hash': destination_sha},
    },
  }


def snapshot_from_pr(
  actor: Mapping[str, Any],
  repository: Mapping[str, Any],
  pr: Mapping[str, Any],
  source_sha: str,
  destination_sha: str,
) -> Mapping[str, Any]:
  description = pr.get('description')
  if not isinstance(description, str):
    raise ValueError('invalid pull request description')
  title = pr.get('title')
  if not isinstance(title, str):
    raise ValueError('invalid pull request title')
  return {
    'workspace': _nested_string(repository, 'workspace', 'slug'),
    'repo': _required_string(repository, 'slug'),
    'repo_uuid': _required_string(repository, 'uuid'),
    'pr_id': _required_positive_int(pr, 'id'),
    'actor_uuid': _required_string(actor, 'uuid'),
    'author_uuid': _nested_string(pr, 'author', 'uuid'),
    'state': _required_string(pr, 'state'),
    'source_branch': _nested_string(pr, 'source', 'branch', 'name'),
    'destination_branch': _nested_string(pr, 'destination', 'branch', 'name'),
    'source_repo_uuid': _nested_string(pr, 'source', 'repository', 'uuid'),
    'destination_repo_uuid': _nested_string(pr, 'destination', 'repository', 'uuid'),
    'source_sha': source_sha,
    'destination_sha': destination_sha,
    'description_sha256': hashlib.sha256(description.encode('utf-8')).hexdigest(),
    'title_sha256': hashlib.sha256(title.encode('utf-8')).hexdigest(),
  }


def _existing_pr_snapshot(
  client: Any,
  candidate: Mapping[str, Any],
) -> tuple[tuple[str, ...], Mapping[str, Any], Mapping[str, Any]]:
  validate_no_credentials(candidate)
  drafts = _drafts(candidate)
  workspace = _required_string(candidate, 'workspace')
  repo = _required_string(candidate, 'repo')
  pr_id = _required_positive_int(candidate, 'pr_id')
  actor = client.get_user()
  repository = client.get_repository(workspace, repo)
  pr = client.get_pr(workspace, repo, pr_id)
  source_sha, destination_sha = _resolved_pr_commit_pair(
    client,
    workspace,
    repo,
    pr,
  )
  return (
    drafts,
    snapshot_from_pr(actor, repository, pr, source_sha, destination_sha),
    pr,
  )


OWNER_ONLY_OPERATIONS = frozenset({'update_description', 'update_title'})


def _comment_only_snapshot(snapshot: Mapping[str, Any]) -> bool:
  """Foreign-authored or non-open PRs accept additive comments but no author-owned write.

  Comments cannot destroy author-owned text and stay attributable to the actor, so the
  historical blanket read-only stop only needs to cover the operations that overwrite
  author-owned fields: update_description and update_title. Title carries less prose than
  description but is still the author's, and it feeds release notifications downstream, so
  it takes the same owner-only stop rather than the additive-comment path.
  """
  return (
    snapshot['actor_uuid'] != snapshot['author_uuid']
    or snapshot['state'] != 'OPEN'
  )


def _blocked_status_for_snapshot(snapshot: Mapping[str, Any]) -> str:
  if snapshot['actor_uuid'] != snapshot['author_uuid']:
    return 'READ_ONLY_FOREIGN_AUTHOR'
  return 'READ_ONLY_PR_NOT_OPEN'


def inspect_existing_pr(
  client: Any,
  candidate: Mapping[str, Any],
) -> InspectResult:
  drafts, snapshot, _pr = _existing_pr_snapshot(client, candidate)
  if _comment_only_snapshot(snapshot):
    return InspectResult('READY_FOR_COMMENT_ONLY', snapshot, drafts)
  return InspectResult('READY_FOR_PROPOSAL', snapshot, drafts)


def preview_existing_pr(
  client: Any,
  candidate: Mapping[str, Any],
  existing_batch_ids: set[str] | None = None,
) -> PreviewResult:
  drafts, snapshot, pr = _existing_pr_snapshot(client, candidate)
  workspace = _required_string(candidate, 'workspace')
  repo = _required_string(candidate, 'repo')
  pr_id = _required_positive_int(candidate, 'pr_id')
  if 'reviewed_source_sha' not in candidate or 'reviewed_destination_sha' not in candidate:
    raise ValueError('review basis fields are required')
  reviewed_source_sha = candidate.get('reviewed_source_sha')
  reviewed_destination_sha = candidate.get('reviewed_destination_sha')
  null_review_basis = reviewed_source_sha is None and reviewed_destination_sha is None
  full_review_basis = (
    is_full_sha(reviewed_source_sha)
    and is_full_sha(reviewed_destination_sha)
  )
  if not null_review_basis and not full_review_basis:
    raise ValueError('review basis must be both null or both full hashes')
  review_context_changed = (
    null_review_basis
    or reviewed_source_sha != snapshot['source_sha']
    or reviewed_destination_sha != snapshot['destination_sha']
  )
  operations = _candidate_operations(candidate)
  unsupported = [operation for operation in operations if operation.get('type') not in OPERATION_RULES]
  if unsupported:
    return PreviewResult('UNSUPPORTED_OPERATION', None, None, None, drafts)
  if any(operation.get('type') == 'create_pr' for operation in operations):
    return PreviewResult('UNSUPPORTED_OPERATION', None, None, None, drafts)
  if _comment_only_snapshot(snapshot) and any(
    operation.get('type') in OWNER_ONLY_OPERATIONS for operation in operations
  ):
    return PreviewResult(_blocked_status_for_snapshot(snapshot), None, None, None, drafts)
  if any(operation.get('type') == 'create_inline_comment' for operation in operations):
    if review_context_changed:
      return PreviewResult('STALE_INLINE_REQUIRES_FALLBACK', None, None, None, drafts)
  for operation in operations:
    if operation.get('type') == 'update_description':
      current_status = _description_status(pr.get('description'))
      if current_status is not None:
        return PreviewResult(current_status, None, None, None, drafts)
      body = operation.get('request_body')
      proposed = body.get('description') if isinstance(body, Mapping) else None
      proposed_status = _description_status(proposed)
      if proposed_status is not None:
        return PreviewResult(proposed_status, None, None, None, drafts)
  target = {
    'workspace': workspace,
    'repo': repo,
    'repo_uuid': snapshot['repo_uuid'],
    'pr_id': pr_id,
  }
  normalized = _normalize_operations(operations, target)
  proposal = {
    'version': 1,
    'purpose': _required_string(candidate, 'purpose'),
    'target': target,
    'snapshot': snapshot,
    'reviewed_source_sha': reviewed_source_sha,
    'reviewed_destination_sha': reviewed_destination_sha,
    'operations': normalized,
  }
  return _ready_preview(proposal, drafts, existing_batch_ids)


def preview_create_pr(
  client: Any,
  candidate: Mapping[str, Any],
  existing_batch_ids: set[str] | None = None,
) -> PreviewResult:
  validate_no_credentials(candidate)
  drafts = _drafts(candidate)
  workspace = _required_string(candidate, 'workspace')
  repo = _required_string(candidate, 'repo')
  source_branch = _required_string(candidate, 'source_branch')
  destination_branch = _required_string(candidate, 'destination_branch')
  operations = _candidate_operations(candidate)
  if len(operations) != 1 or operations[0].get('type') != 'create_pr':
    raise ValueError('create preview requires exactly one create operation')
  body = operations[0].get('request_body')
  description = body.get('description') if isinstance(body, Mapping) else None
  description_status = _description_status(description)
  if description_status is not None:
    return PreviewResult(description_status, None, None, None, drafts)
  actor = client.get_user()
  repository = client.get_repository(workspace, repo)
  source = client.get_branch(workspace, repo, source_branch)
  destination = client.get_branch(workspace, repo, destination_branch)
  source_sha = _nested_string(source, 'target', 'hash')
  destination_sha = _nested_string(destination, 'target', 'hash')
  if not is_full_sha(source_sha) or not is_full_sha(destination_sha):
    raise ValueError('branch snapshot requires full commit hashes')
  repo_uuid = _required_string(repository, 'uuid')
  source_repo_uuid = _nested_string(source, 'target', 'repository', 'uuid')
  destination_repo_uuid = _nested_string(destination, 'target', 'repository', 'uuid')
  if source_repo_uuid != repo_uuid or destination_repo_uuid != repo_uuid:
    raise ValueError('branch repository mismatch')
  target = {
    'workspace': workspace,
    'repo': repo,
    'repo_uuid': repo_uuid,
    'source_branch': source_branch,
    'destination_branch': destination_branch,
  }
  snapshot = {
    'workspace': _nested_string(repository, 'workspace', 'slug'),
    'repo': _required_string(repository, 'slug'),
    'repo_uuid': repo_uuid,
    'actor_uuid': _required_string(actor, 'uuid'),
    'source_branch': source_branch,
    'destination_branch': destination_branch,
    'source_repo_uuid': source_repo_uuid,
    'destination_repo_uuid': destination_repo_uuid,
    'source_sha': source_sha,
    'destination_sha': destination_sha,
  }
  normalized = _normalize_operations(operations, target)
  proposal = {
    'version': 1,
    'purpose': _required_string(candidate, 'purpose'),
    'target': target,
    'snapshot': snapshot,
    'reviewed_source_sha': None,
    'reviewed_destination_sha': None,
    'operations': normalized,
  }
  return _ready_preview(proposal, drafts, existing_batch_ids)


def apply_proposal(
  client: Any,
  envelope: Mapping[str, Any],
  approval: Mapping[str, Any],
  state_root: Path,
  session_id: str,
) -> ApplyResult:
  proposal, digest, batch_id = _validate_envelope(envelope)
  operation_ids = [operation['operation_id'] for operation in proposal['operations']]
  validate_approval(session_id, digest, operation_ids, approval)
  if not _valid_session_id(session_id):
    raise ValueError('invalid session id')
  target = proposal['target']
  with target_lock(state_root, target):
    existing_path, existing = _existing_journal(state_root, batch_id)
    if existing is not None:
      state = existing.get('batch_state')
      operation_states = [
        value.get('state')
        for value in existing.get('operations', {}).values()
        if isinstance(value, Mapping)
      ]
      if state in {'applying', 'outcome_unknown'} or any(
        value in {'started', 'outcome_unknown'} for value in operation_states
      ):
        raise ReconciliationRequired(str(existing_path))
      if existing.get('session_id') != session_id:
        return ApplyResult(
          'invalid',
          _result_operations(proposal, existing),
          str(existing_path),
        )
      if state == 'pending' and any((
        existing.get('batch_id') != batch_id,
        existing.get('proposal_sha256') != digest,
        existing.get('proposal') != proposal,
      )):
        return ApplyResult(
          'invalid',
          _result_operations(proposal, existing),
          str(existing_path),
        )
      if state != 'pending':
        return ApplyResult(
          'invalid',
          _result_operations(proposal, existing),
          str(existing_path),
        )
      journal_path = existing_path
      journal = dict(existing)
    else:
      journal_path = session_journal_path(state_root, session_id, batch_id)
      journal = _new_journal(proposal, digest, batch_id, session_id)
    allow_comment_only = not any(
      operation.get('type') in OWNER_ONLY_OPERATIONS
      for operation in proposal['operations']
    )
    try:
      current = _load_current_snapshot(client, proposal)
    except (ApiError, ApiTransportError, ValueError):
      journal['batch_state'] = 'invalid'
      write_journal(journal_path, journal)
      return ApplyResult(
        'invalid',
        _result_operations(proposal, journal),
        str(journal_path),
      )
    if not _snapshot_matches(proposal['snapshot'], current, allow_comment_only):
      journal['batch_state'] = 'invalid'
      write_journal(journal_path, journal)
      return ApplyResult(
        'invalid',
        _result_operations(proposal, journal),
        str(journal_path),
      )
    if journal.get('batch_state') != 'pending':
      journal['batch_state'] = 'pending'
      write_journal(journal_path, journal)
    else:
      write_journal(journal_path, journal)
    journal['batch_state'] = 'applying'
    write_journal(journal_path, journal)
    expected_snapshot = dict(proposal['snapshot'])
    for index, operation in enumerate(proposal['operations']):
      try:
        current = _load_current_snapshot(client, proposal)
      except (ApiError, ApiTransportError, ValueError):
        _mark_remaining(journal, proposal['operations'], index)
        journal['batch_state'] = 'invalid'
        write_journal(journal_path, journal)
        return ApplyResult(
          'invalid',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      if not _snapshot_matches(expected_snapshot, current, allow_comment_only):
        _mark_remaining(journal, proposal['operations'], index)
        journal['batch_state'] = 'invalid'
        write_journal(journal_path, journal)
        return ApplyResult(
          'invalid',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      if operation['type'] == 'update_description':
        try:
          status = _description_status(_current_description(client, proposal))
        except (ApiError, ApiTransportError, ValueError):
          status = 'DRAFT_ONLY_INVALID_MARKERS'
        if status is not None:
          _mark_remaining(journal, proposal['operations'], index)
          journal['batch_state'] = 'invalid'
          write_journal(journal_path, journal)
          return ApplyResult(
            'invalid',
            _result_operations(proposal, journal),
            str(journal_path),
          )
      operation_id = operation['operation_id']
      journal_operation = journal['operations'][operation_id]
      journal_operation['state'] = 'started'
      journal_operation['outcome'] = None
      write_journal(journal_path, journal)
      try:
        response = _dispatch_operation(client, proposal, operation)
      except ApiTransportError:
        _set_operation(journal_operation, 'outcome_unknown', 'outcome_unknown', None)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'outcome_unknown'
        write_journal(journal_path, journal)
        return ApplyResult(
          'outcome_unknown',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      except ApiError as error:
        if error.status == 408 or 500 <= error.status <= 599:
          _set_operation(journal_operation, 'outcome_unknown', 'outcome_unknown', None)
          _mark_remaining(journal, proposal['operations'], index + 1)
          journal['batch_state'] = 'outcome_unknown'
          write_journal(journal_path, journal)
          return ApplyResult(
            'outcome_unknown',
            _result_operations(proposal, journal),
            str(journal_path),
          )
        _set_operation(journal_operation, 'failed', 'failed', None)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'completed'
        write_journal(journal_path, journal)
        return ApplyResult(
          'completed',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      resource_id = response.get('id') if isinstance(response, Mapping) else None
      if type(resource_id) is int and resource_id > 0:
        journal_operation['resource_id'] = resource_id
        write_journal(journal_path, journal)
      try:
        resource = _read_back_resource(client, proposal, operation, resource_id)
        resource_url = _resource_url(resource)
        read_back_matches = _read_back_matches(
          proposal,
          operation,
          resource,
          allow_create_commit_drift=True,
        )
        exact_read_back_matches = _read_back_matches(
          proposal,
          operation,
          resource,
        )
        after = _load_pr_after_write(client, proposal, resource)
      except (ApiTransportError, ValueError):
        _set_operation(journal_operation, 'outcome_unknown', 'outcome_unknown', None)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'outcome_unknown'
        write_journal(journal_path, journal)
        return ApplyResult(
          'outcome_unknown',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      except ApiError:
        _set_operation(journal_operation, 'outcome_unknown', 'outcome_unknown', None)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'outcome_unknown'
        write_journal(journal_path, journal)
        return ApplyResult(
          'outcome_unknown',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      if not read_back_matches:
        _set_operation(journal_operation, 'failed', 'failed', resource_url)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'completed'
        write_journal(journal_path, journal)
        return ApplyResult(
          'completed',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      if _commit_pair(after) != _commit_pair(expected_snapshot):
        _set_operation(journal_operation, 'completed', 'post_write_drift', resource_url)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'completed'
        write_journal(journal_path, journal)
        return ApplyResult(
          'completed',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      if not exact_read_back_matches:
        _set_operation(journal_operation, 'failed', 'failed', resource_url)
        _mark_remaining(journal, proposal['operations'], index + 1)
        journal['batch_state'] = 'completed'
        write_journal(journal_path, journal)
        return ApplyResult(
          'completed',
          _result_operations(proposal, journal),
          str(journal_path),
        )
      _set_operation(journal_operation, 'completed', 'completed', resource_url)
      if operation['type'] == 'update_description':
        expected_snapshot['description_sha256'] = hashlib.sha256(
          operation['request_body']['description'].encode('utf-8'),
        ).hexdigest()
      if operation['type'] == 'update_title':
        expected_snapshot['title_sha256'] = hashlib.sha256(
          operation['request_body']['title'].encode('utf-8'),
        ).hexdigest()
      write_journal(journal_path, journal)
    journal['batch_state'] = 'completed'
    write_journal(journal_path, journal)
    return ApplyResult(
      'completed',
      _result_operations(proposal, journal),
      str(journal_path),
    )


def _journal_operation_phase(facts: Mapping[str, Any]) -> str:
  state = facts.get('state')
  outcome = facts.get('outcome')
  if state == 'completed' and outcome == 'completed':
    return 'known_completed'
  if state == 'completed' and outcome == 'post_write_drift':
    return 'terminal'
  if state == 'failed' and outcome == 'failed':
    return 'terminal'
  if state == 'not_attempted' and outcome == 'not_attempted':
    return 'not_attempted'
  if state == 'started' and outcome is None:
    return 'started'
  if state == 'outcome_unknown' and outcome == 'outcome_unknown':
    return 'outcome_unknown'
  raise ValueError('invalid journal operation phase')


def _validate_batch_operation_states(
  batch_state: str,
  operation_facts: Sequence[Mapping[str, Any]],
) -> None:
  phases = tuple(_journal_operation_phase(facts) for facts in operation_facts)
  if batch_state == 'pending':
    if any(phase != 'not_attempted' for phase in phases):
      raise ValueError('journal pending state mismatch')
    return

  cursor = 0
  while cursor < len(phases) and phases[cursor] == 'known_completed':
    cursor += 1

  if batch_state == 'applying':
    if cursor < len(phases) and phases[cursor] == 'started':
      cursor += 1
    if any(phase != 'not_attempted' for phase in phases[cursor:]):
      raise ValueError('journal applying state mismatch')
    return

  if batch_state == 'outcome_unknown':
    if cursor >= len(phases) or phases[cursor] != 'outcome_unknown':
      raise ValueError('journal unknown state mismatch')
    cursor += 1
    if any(phase != 'not_attempted' for phase in phases[cursor:]):
      raise ValueError('journal unknown state mismatch')
    return

  if batch_state == 'completed':
    if cursor == len(phases):
      return
    if phases[cursor] != 'terminal':
      raise ValueError('journal completed state mismatch')
    cursor += 1
    if any(phase != 'not_attempted' for phase in phases[cursor:]):
      raise ValueError('journal completed state mismatch')
    return

  if batch_state == 'invalid':
    if cursor == len(phases):
      raise ValueError('journal invalid state mismatch')
    if any(phase != 'not_attempted' for phase in phases[cursor:]):
      raise ValueError('journal invalid state mismatch')
    return

  raise ValueError('invalid journal batch state')


def _validate_reconcile_journal(
  journal: Mapping[str, Any],
  path: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
  if set(journal) != JOURNAL_KEYS or journal.get('version') != 1:
    raise ValueError('invalid journal schema')
  session_id = journal.get('session_id')
  batch_id = journal.get('batch_id')
  digest = journal.get('proposal_sha256')
  batch_state = journal.get('batch_state')
  if not isinstance(session_id, str) or not _valid_session_id(session_id):
    raise ValueError('invalid journal session')
  if not isinstance(batch_id, str) or BATCH_ID.fullmatch(batch_id) is None:
    raise ValueError('invalid journal batch')
  absolute_path = _absolute(path)
  if absolute_path.suffix != '.json' or absolute_path.stem != batch_id:
    raise ValueError('journal path batch mismatch')
  if absolute_path.parent.name != session_id:
    raise ValueError('journal path session mismatch')
  if absolute_path.parent.parent.name != 'sessions':
    raise ValueError('journal path root mismatch')
  proposal = journal.get('proposal')
  if not isinstance(proposal, Mapping):
    raise ValueError('invalid journal proposal')
  _validate_proposal(proposal)
  expected_digest = proposal_sha256(proposal)
  if digest != expected_digest or not expected_digest.startswith(batch_id):
    raise ValueError('journal proposal binding mismatch')
  if journal.get('target') != proposal.get('target'):
    raise ValueError('journal target mismatch')
  if journal.get('snapshot') != proposal.get('snapshot'):
    raise ValueError('journal snapshot mismatch')
  if batch_state not in BATCH_STATES:
    raise ValueError('invalid journal batch state')
  operations = journal.get('operations')
  if not isinstance(operations, Mapping):
    raise ValueError('invalid journal operations')
  proposal_operations = {
    operation['operation_id']: operation
    for operation in proposal['operations']
  }
  if set(operations) != set(proposal_operations):
    raise ValueError('journal operation binding mismatch')
  ordered_facts = []
  for operation_id, operation in proposal_operations.items():
    facts = operations.get(operation_id)
    if not isinstance(facts, Mapping) or set(facts) != JOURNAL_OPERATION_KEYS:
      raise ValueError('invalid journal operation facts')
    if facts.get('type') != operation['type']:
      raise ValueError('journal operation type mismatch')
    state = facts.get('state')
    if state not in OPERATION_STATE_OUTCOMES:
      raise ValueError('invalid journal operation state')
    if facts.get('outcome') not in OPERATION_STATE_OUTCOMES[state]:
      raise ValueError('invalid journal operation outcome')
    resource_id = facts.get('resource_id')
    resource_url = facts.get('resource_url')
    if resource_id is not None and (type(resource_id) is not int or resource_id <= 0):
      raise ValueError('invalid journal resource id')
    if resource_url is not None and (not isinstance(resource_url, str) or not resource_url):
      raise ValueError('invalid journal resource url')
    ordered_facts.append(facts)
  _validate_batch_operation_states(batch_state, ordered_facts)
  return proposal, operations


def reconcile_journal(client: Any, path: Path) -> Mapping[str, Any]:
  journal = read_journal(path)
  if journal is None:
    raise ValueError('journal does not exist')
  report = {
    'journal_path': str(_absolute(path)),
    'journal_state': 'invalid',
    'batch_id': None,
    'batch_state': None,
    'operations': [],
    'candidates': [],
    'candidate_count': 0,
    'ambiguous': True,
    'reason_code': 'INVALID_JOURNAL',
  }
  try:
    proposal, operation_facts = _validate_reconcile_journal(journal, path)
  except ValueError:
    return report
  report['journal_state'] = 'valid'
  report['batch_id'] = journal['batch_id']
  report['batch_state'] = journal['batch_state']
  report['reason_code'] = None
  for operation in proposal['operations']:
    operation_id = operation['operation_id']
    facts = operation_facts.get(operation_id)
    if not isinstance(facts, Mapping):
      continue
    report['operations'].append({
      'operation_id': operation_id,
      'type': operation['type'],
      'state': facts.get('state') if isinstance(facts.get('state'), str) else None,
      'outcome': facts.get('outcome') if isinstance(facts.get('outcome'), str) else None,
    })
    if facts.get('state') not in {'started', 'outcome_unknown'}:
      continue
    candidate = _reconcile_operation(client, proposal, operation, facts)
    if candidate is not None:
      report['candidates'].append(candidate)
  report['candidate_count'] = len(report['candidates'])
  report['ambiguous'] = (
    report['candidate_count'] != 1
    or not all(candidate['matches'] for candidate in report['candidates'])
  )
  return report


def _drafts(candidate: Mapping[str, Any]) -> tuple[str, ...]:
  value = candidate.get('drafts', ())
  if isinstance(value, str):
    return (value,)
  if not isinstance(value, Sequence):
    raise ValueError('drafts must be strings')
  if any(not isinstance(item, str) for item in value):
    raise ValueError('drafts must be strings')
  return tuple(value)


def _candidate_operations(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
  value = candidate.get('operations')
  if not isinstance(value, list) or not value:
    raise ValueError('candidate requires operations')
  if any(not isinstance(operation, Mapping) for operation in value):
    raise ValueError('candidate operations must be mappings')
  return value


def _normalize_operations(
  operations: Sequence[Mapping[str, Any]],
  target: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
  normalized = []
  operation_ids = []
  for operation in operations:
    operation_type = operation.get('type')
    rule = OPERATION_RULES.get(operation_type)
    if rule is None:
      raise ValueError('unsupported operation type')
    request_body = operation.get('request_body')
    if not isinstance(request_body, Mapping):
      raise ValueError('operation request body missing')
    validate_no_credentials(request_body, f'$.operations[{len(normalized)}].request_body')
    _validate_request_body(operation_type, request_body)
    operation_id = operation.get('operation_id')
    if not isinstance(operation_id, str) or not operation_id:
      raise ValueError('operation id missing')
    finding_uid = operation.get('finding_uid')
    if finding_uid is not None and not isinstance(finding_uid, str):
      raise ValueError('invalid finding uid')
    operation_ids.append(operation_id)
    normalized.append({
      'operation_id': operation_id,
      'type': operation_type,
      'finding_uid': finding_uid,
      'method': rule['method'],
      'endpoint': expected_endpoint(operation_type, target),
      'request_body': _canonical_clone(request_body),
      'read_back': {'fields': list(rule['read_back'])},
    })
  if len(operation_ids) != len(set(operation_ids)):
    raise ValueError('duplicate operation id')
  return normalized


def _ready_preview(
  proposal: Mapping[str, Any],
  drafts: tuple[str, ...],
  known_batch_ids: set[str] | None,
) -> PreviewResult:
  _validate_proposal(proposal)
  digest = proposal_sha256(proposal)
  batch_id = unique_batch_id(digest, set(known_batch_ids or ()))
  envelope = {
    'proposal': proposal,
    'proposal_sha256': digest,
    'batch_id': batch_id,
  }
  return PreviewResult('READY', envelope, digest, batch_id, drafts)


def _validate_envelope(
  envelope: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str, str]:
  if not isinstance(envelope, Mapping):
    raise ValueError('invalid proposal envelope')
  if set(envelope) != {'proposal', 'proposal_sha256', 'batch_id'}:
    raise ValueError('invalid proposal envelope')
  proposal = envelope.get('proposal')
  if not isinstance(proposal, Mapping):
    raise ValueError('invalid proposal')
  _validate_proposal(proposal)
  digest = proposal_sha256(proposal)
  if envelope.get('proposal_sha256') != digest:
    raise ValueError('proposal hash mismatch')
  batch_id = envelope.get('batch_id')
  if not isinstance(batch_id, str) or not BATCH_ID.fullmatch(batch_id):
    raise ValueError('invalid batch id')
  if not digest.startswith(batch_id):
    raise ValueError('batch id does not match proposal')
  return proposal, digest, batch_id


def _validate_proposal(proposal: Mapping[str, Any]) -> None:
  validate_no_credentials(proposal)
  if set(proposal) != {
    'version',
    'purpose',
    'target',
    'snapshot',
    'reviewed_source_sha',
    'reviewed_destination_sha',
    'operations',
  }:
    raise ValueError('proposal fields mismatch')
  if proposal.get('version') != 1:
    raise ValueError('unsupported proposal version')
  _required_string(proposal, 'purpose')
  target = proposal.get('target')
  snapshot = proposal.get('snapshot')
  operations = proposal.get('operations')
  if not isinstance(target, Mapping) or not isinstance(snapshot, Mapping):
    raise ValueError('proposal target or snapshot missing')
  if not isinstance(operations, list) or not operations:
    raise ValueError('proposal operations missing')
  operation_ids = []
  for index, operation in enumerate(operations):
    if not isinstance(operation, Mapping):
      raise ValueError('invalid proposal operation')
    validate_no_credentials(
      operation.get('request_body'),
      f'$.operations[{index}].request_body',
    )
    validate_operation(operation, target)
    operation_ids.append(operation['operation_id'])
  if len(operation_ids) != len(set(operation_ids)):
    raise ValueError('duplicate operation id')
  operation_types = {operation['type'] for operation in operations}
  reviewed_source_sha = proposal.get('reviewed_source_sha')
  reviewed_destination_sha = proposal.get('reviewed_destination_sha')
  if 'create_pr' in operation_types:
    if len(operations) != 1 or operation_types != {'create_pr'}:
      raise ValueError('create operation cannot be combined')
    if reviewed_source_sha is not None or reviewed_destination_sha is not None:
      raise ValueError('create proposal must not claim review basis')
    _validate_create_target_snapshot(target, snapshot)
  else:
    null_review_basis = reviewed_source_sha is None and reviewed_destination_sha is None
    full_review_basis = (
      is_full_sha(reviewed_source_sha)
      and is_full_sha(reviewed_destination_sha)
    )
    if not null_review_basis and not full_review_basis:
      raise ValueError('review basis must be both null or both full hashes')
    if 'create_inline_comment' in operation_types:
      if not full_review_basis:
        raise ValueError('inline comment requires full review basis')
      if reviewed_source_sha != snapshot.get('source_sha'):
        raise ValueError('inline reviewed source mismatch')
      if reviewed_destination_sha != snapshot.get('destination_sha'):
        raise ValueError('inline reviewed destination mismatch')
    _validate_existing_target_snapshot(target, snapshot)


def _validate_existing_target_snapshot(
  target: Mapping[str, Any],
  snapshot: Mapping[str, Any],
) -> None:
  if set(target) != {'workspace', 'repo', 'repo_uuid', 'pr_id'}:
    raise ValueError('existing target fields mismatch')
  expected = {
    'workspace',
    'repo',
    'repo_uuid',
    'pr_id',
    'actor_uuid',
    'author_uuid',
    'state',
    'source_branch',
    'destination_branch',
    'source_repo_uuid',
    'destination_repo_uuid',
    'source_sha',
    'destination_sha',
    'description_sha256',
    'title_sha256',
  }
  if set(snapshot) != expected:
    raise ValueError('existing snapshot fields mismatch')
  if any(target[key] != snapshot[key] for key in target):
    raise ValueError('target snapshot mismatch')
  _validate_snapshot_hashes(snapshot)


def _validate_create_target_snapshot(
  target: Mapping[str, Any],
  snapshot: Mapping[str, Any],
) -> None:
  expected_target = {
    'workspace',
    'repo',
    'repo_uuid',
    'source_branch',
    'destination_branch',
  }
  expected_snapshot = expected_target | {
    'actor_uuid',
    'source_repo_uuid',
    'destination_repo_uuid',
    'source_sha',
    'destination_sha',
  }
  if set(target) != expected_target or set(snapshot) != expected_snapshot:
    raise ValueError('create target or snapshot fields mismatch')
  if any(target[key] != snapshot[key] for key in expected_target):
    raise ValueError('target snapshot mismatch')
  _validate_snapshot_hashes(snapshot)


def _validate_snapshot_hashes(snapshot: Mapping[str, Any]) -> None:
  if not is_full_sha(snapshot.get('source_sha')):
    raise ValueError('source snapshot is unresolved')
  if not is_full_sha(snapshot.get('destination_sha')):
    raise ValueError('destination snapshot is unresolved')
  if 'description_sha256' in snapshot:
    value = snapshot.get('description_sha256')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
      raise ValueError('description snapshot is unresolved')
  if 'title_sha256' in snapshot:
    value = snapshot.get('title_sha256')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
      raise ValueError('title snapshot is unresolved')


def _validate_request_body(operation_type: str, body: Mapping[str, Any]) -> None:
  if operation_type == 'update_description':
    if set(body) != {'description'} or not isinstance(body.get('description'), str):
      raise ValueError('invalid description request body')
    return
  if operation_type == 'update_title':
    # Empty titles are rejected the same way create_pr rejects them: Bitbucket shows the
    # title everywhere the PR is referenced, so blanking it is never the intent.
    if set(body) != {'title'} or not isinstance(body.get('title'), str) or not body['title']:
      raise ValueError('invalid title request body')
    return
  if operation_type == 'create_pr':
    if set(body) != {'title', 'description', 'source', 'destination'}:
      raise ValueError('invalid create request body')
    if not isinstance(body.get('title'), str) or not body['title']:
      raise ValueError('invalid create request body')
    if not isinstance(body.get('description'), str):
      raise ValueError('invalid create request body')
    _branch_body(body.get('source'))
    _branch_body(body.get('destination'))
    return
  if operation_type == 'create_pr_comment':
    if set(body) != {'content'}:
      raise ValueError('invalid pull request comment body')
    _content_raw(body.get('content'))
    return
  if operation_type == 'create_inline_comment':
    if set(body) != {'content', 'inline'}:
      raise ValueError('invalid inline comment body')
    _content_raw(body.get('content'))
    inline = body.get('inline')
    if not isinstance(inline, Mapping):
      raise ValueError('invalid inline comment body')
    if set(inline) not in ({'path', 'to'}, {'path', 'from'}):
      raise ValueError('invalid inline comment anchor')
    if not isinstance(inline.get('path'), str) or not inline['path']:
      raise ValueError('invalid inline comment anchor')
    anchor_name = 'to' if 'to' in inline else 'from'
    if type(inline.get(anchor_name)) is not int or inline[anchor_name] <= 0:
      raise ValueError('invalid inline comment anchor')
    return
  raise ValueError('unsupported operation type')


def _branch_body(value: Any) -> str:
  if not isinstance(value, Mapping) or set(value) != {'branch'}:
    raise ValueError('invalid branch body')
  branch = value.get('branch')
  if not isinstance(branch, Mapping) or set(branch) != {'name'}:
    raise ValueError('invalid branch body')
  return _required_string(branch, 'name')


def _content_raw(value: Any) -> str:
  if not isinstance(value, Mapping) or set(value) != {'raw'}:
    raise ValueError('invalid comment content')
  return _required_string(value, 'raw')


def _description_status(value: Any) -> str | None:
  if not isinstance(value, str):
    return 'DRAFT_ONLY_INVALID_MARKERS'
  try:
    parsed = parse_description(value)
  except ValueError:
    return 'DRAFT_ONLY_INVALID_MARKERS'
  if not is_put_eligible(parsed):
    return 'DRAFT_ONLY_UNMANAGED_DESCRIPTION'
  return None


def _new_journal(
  proposal: Mapping[str, Any],
  digest: str,
  batch_id: str,
  session_id: str,
) -> dict[str, Any]:
  return {
    'version': 1,
    'session_id': session_id,
    'batch_id': batch_id,
    'proposal_sha256': digest,
    'batch_state': 'pending',
    'target': _canonical_clone(proposal['target']),
    'snapshot': _canonical_clone(proposal['snapshot']),
    'proposal': _canonical_clone(proposal),
    'operations': {
      operation['operation_id']: {
        'type': operation['type'],
        'state': 'not_attempted',
        'outcome': 'not_attempted',
        'resource_id': None,
        'resource_url': None,
      }
      for operation in proposal['operations']
    },
  }


def _existing_journal(
  root: Path,
  batch_id: str,
) -> tuple[Path | None, Mapping[str, Any] | None]:
  sessions = _absolute(root) / 'sessions'
  if not sessions.exists():
    return None, None
  if sessions.is_symlink() or not sessions.is_dir():
    raise ValueError('unsafe sessions directory')
  matches = []
  for session in sessions.iterdir():
    if session.is_symlink() or not session.is_dir():
      continue
    path = session / f'{batch_id}.json'
    if path.exists():
      matches.append(path)
  if len(matches) > 1:
    raise ReconciliationRequired('duplicate batch journals')
  if not matches:
    return None, None
  return matches[0], read_journal(matches[0])


def _load_current_snapshot(
  client: Any,
  proposal: Mapping[str, Any],
) -> Mapping[str, Any]:
  target = proposal['target']
  if proposal['operations'][0]['type'] == 'create_pr':
    actor = client.get_user()
    repository = client.get_repository(target['workspace'], target['repo'])
    source = client.get_branch(
      target['workspace'],
      target['repo'],
      target['source_branch'],
    )
    destination = client.get_branch(
      target['workspace'],
      target['repo'],
      target['destination_branch'],
    )
    return {
      'workspace': _nested_string(repository, 'workspace', 'slug'),
      'repo': _required_string(repository, 'slug'),
      'repo_uuid': _required_string(repository, 'uuid'),
      'actor_uuid': _required_string(actor, 'uuid'),
      'source_branch': _required_string(source, 'name'),
      'destination_branch': _required_string(destination, 'name'),
      'source_repo_uuid': _nested_string(source, 'target', 'repository', 'uuid'),
      'destination_repo_uuid': _nested_string(destination, 'target', 'repository', 'uuid'),
      'source_sha': _nested_string(source, 'target', 'hash'),
      'destination_sha': _nested_string(destination, 'target', 'hash'),
    }
  workspace = target['workspace']
  repo = target['repo']
  actor = client.get_user()
  repository = client.get_repository(workspace, repo)
  pr = client.get_pr(workspace, repo, target['pr_id'])
  source_sha, destination_sha = _resolved_pr_commit_pair(
    client,
    workspace,
    repo,
    pr,
  )
  return snapshot_from_pr(
    actor,
    repository,
    pr,
    source_sha,
    destination_sha,
  )


def _snapshot_matches(
  expected: Mapping[str, Any],
  actual: Mapping[str, Any],
  allow_comment_only: bool = False,
) -> bool:
  if set(expected) != set(actual):
    return False
  if expected != actual:
    return False
  if not allow_comment_only:
    if expected.get('actor_uuid') != expected.get('author_uuid', expected.get('actor_uuid')):
      return False
    if expected.get('state', 'OPEN') != 'OPEN':
      return False
  if expected.get('repo_uuid') != expected.get('source_repo_uuid'):
    return False
  if expected.get('repo_uuid') != expected.get('destination_repo_uuid'):
    return False
  return is_full_sha(expected.get('source_sha')) and is_full_sha(expected.get('destination_sha'))


def _current_description(client: Any, proposal: Mapping[str, Any]) -> Any:
  target = proposal['target']
  pr = client.get_pr(target['workspace'], target['repo'], target['pr_id'])
  return pr.get('description')


def _dispatch_operation(
  client: Any,
  proposal: Mapping[str, Any],
  operation: Mapping[str, Any],
) -> Mapping[str, Any]:
  target = proposal['target']
  body = _canonical_clone(operation['request_body'])
  operation_type = operation['type']
  if operation_type == 'create_pr':
    return client.create_pr(target['workspace'], target['repo'], body)
  if operation_type in {'update_description', 'update_title'}:
    return client.update_pr(
      target['workspace'],
      target['repo'],
      target['pr_id'],
      body,
    )
  if operation_type in {'create_inline_comment', 'create_pr_comment'}:
    return client.create_comment(
      target['workspace'],
      target['repo'],
      target['pr_id'],
      body,
    )
  raise ValueError('unsupported operation type')


def _read_back_resource(
  client: Any,
  proposal: Mapping[str, Any],
  operation: Mapping[str, Any],
  resource_id: Any,
) -> Mapping[str, Any]:
  target = proposal['target']
  operation_type = operation['type']
  if operation_type == 'create_pr':
    if type(resource_id) is not int or resource_id <= 0:
      raise ApiTransportError('created pull request id unavailable')
    workspace = target['workspace']
    repo = target['repo']
    return _pr_with_resolved_commits(
      client,
      workspace,
      repo,
      client.get_pr(workspace, repo, resource_id),
    )
  if operation_type in {'update_description', 'update_title'}:
    return client.get_pr(
      target['workspace'],
      target['repo'],
      target['pr_id'],
    )
  if type(resource_id) is not int or resource_id <= 0:
    raise ApiTransportError('created comment id unavailable')
  return client.get_comment(
    target['workspace'],
    target['repo'],
    target['pr_id'],
    resource_id,
  )


def _read_back_matches(
  proposal: Mapping[str, Any],
  operation: Mapping[str, Any],
  resource: Mapping[str, Any],
  allow_create_commit_drift: bool = False,
) -> bool:
  body = operation['request_body']
  operation_type = operation['type']
  if operation_type == 'create_pr':
    snapshot = proposal['snapshot']
    commit_pair_matches = all((
      _nested_value(resource, 'source', 'commit', 'hash') == snapshot['source_sha'],
      _nested_value(resource, 'destination', 'commit', 'hash') == snapshot['destination_sha'],
    ))
    return all((
      _nested_value(resource, 'author', 'uuid') == snapshot['actor_uuid'],
      _nested_value(resource, 'source', 'repository', 'uuid') == snapshot['repo_uuid'],
      _nested_value(resource, 'destination', 'repository', 'uuid') == snapshot['repo_uuid'],
      _nested_value(resource, 'source', 'branch', 'name') == snapshot['source_branch'],
      _nested_value(resource, 'destination', 'branch', 'name') == snapshot['destination_branch'],
      allow_create_commit_drift or commit_pair_matches,
      resource.get('title') == body['title'],
      resource.get('description') == body['description'],
      resource.get('state') == 'OPEN',
      isinstance(_nested_value(resource, 'links', 'html', 'href'), str),
      bool(_nested_value(resource, 'links', 'html', 'href')),
    ))
  if operation_type == 'update_description':
    return resource.get('description') == body['description']
  if operation_type == 'update_title':
    return resource.get('title') == body['title']
  if operation_type == 'create_pr_comment':
    return (
      _nested_value(resource, 'content', 'raw') == body['content']['raw']
      and resource.get('inline') in (None, {})
    )
  proposed_inline = body['inline']
  actual_inline = resource.get('inline')
  if not isinstance(actual_inline, Mapping):
    return False
  anchor = 'to' if 'to' in proposed_inline else 'from'
  other = 'from' if anchor == 'to' else 'to'
  # Bitbucket always returns both sides of the anchor and nulls the unused one, so the
  # opposite side must be checked for a null value rather than for key absence.
  return all((
    _nested_value(resource, 'content', 'raw') == body['content']['raw'],
    actual_inline.get('path') == proposed_inline['path'],
    actual_inline.get(anchor) == proposed_inline[anchor],
    actual_inline.get(other) is None,
  ))


def _load_pr_after_write(
  client: Any,
  proposal: Mapping[str, Any],
  resource: Mapping[str, Any],
) -> Mapping[str, Any]:
  target = proposal['target']
  workspace = target['workspace']
  repo = target['repo']
  if proposal['operations'][0]['type'] == 'create_pr':
    pr_id = _required_positive_int(resource, 'id')
    pr = _pr_with_resolved_commits(
      client,
      workspace,
      repo,
      client.get_pr(workspace, repo, pr_id),
    )
    return {
      'source_sha': _nested_string(pr, 'source', 'commit', 'hash'),
      'destination_sha': _nested_string(pr, 'destination', 'commit', 'hash'),
    }
  pr = client.get_pr(workspace, repo, target['pr_id'])
  source_sha, destination_sha = _resolved_pr_commit_pair(
    client,
    workspace,
    repo,
    pr,
  )
  return {
    'source_sha': source_sha,
    'destination_sha': destination_sha,
  }


def _commit_pair(snapshot: Mapping[str, Any]) -> tuple[Any, Any]:
  return snapshot.get('source_sha'), snapshot.get('destination_sha')


def _set_operation(
  operation: dict[str, Any],
  state: str,
  outcome: str,
  resource_url: str | None,
) -> None:
  operation['state'] = state
  operation['outcome'] = outcome
  operation['resource_url'] = resource_url


def _mark_remaining(
  journal: dict[str, Any],
  operations: Sequence[Mapping[str, Any]],
  start: int,
) -> None:
  for operation in operations[start:]:
    current = journal['operations'][operation['operation_id']]
    if current.get('state') == 'not_attempted':
      current['outcome'] = 'not_attempted'


def _result_operations(
  proposal: Mapping[str, Any],
  journal: Mapping[str, Any],
) -> Mapping[str, OperationResult]:
  stored = journal.get('operations', {})
  result = {}
  for operation in proposal['operations']:
    value = stored.get(operation['operation_id'], {}) if isinstance(stored, Mapping) else {}
    state = value.get('state', 'not_attempted')
    outcome = value.get('outcome', 'not_attempted')
    if not isinstance(state, str):
      state = 'not_attempted'
    if not isinstance(outcome, str):
      outcome = state
    resource_url = value.get('resource_url')
    result[operation['operation_id']] = OperationResult(
      state,
      outcome,
      resource_url if isinstance(resource_url, str) else None,
    )
  return result


def _resource_url(resource: Mapping[str, Any]) -> str | None:
  value = _nested_value(resource, 'links', 'html', 'href')
  return value if isinstance(value, str) and value else None


def _reconcile_operation(
  client: Any,
  proposal: Mapping[str, Any],
  operation: Mapping[str, Any],
  facts: Mapping[str, Any],
) -> Mapping[str, Any] | None:
  target = proposal['target']
  operation_type = operation['type']
  try:
    if operation_type in {'update_description', 'update_title'}:
      resource = client.get_pr(
        target['workspace'],
        target['repo'],
        target['pr_id'],
      )
    elif operation_type == 'create_pr':
      resource_id = facts.get('resource_id')
      if type(resource_id) is not int or resource_id <= 0:
        return None
      workspace = target['workspace']
      repo = target['repo']
      resource = _pr_with_resolved_commits(
        client,
        workspace,
        repo,
        client.get_pr(workspace, repo, resource_id),
      )
    else:
      resource_id = facts.get('resource_id')
      if type(resource_id) is not int or resource_id <= 0:
        return None
      resource = client.get_comment(
        target['workspace'],
        target['repo'],
        target['pr_id'],
        resource_id,
      )
  except (ApiError, ApiTransportError, ValueError):
    return None
  return {
    'operation_id': operation['operation_id'],
    'resource_id': resource.get('id'),
    'resource_url': _resource_url(resource),
    'matches': _read_back_matches(proposal, operation, resource),
  }


def _canonical_clone(value: Mapping[str, Any]) -> Mapping[str, Any]:
  cloned = json.loads(canonical_json_bytes(value).decode('utf-8'))
  if not isinstance(cloned, dict):
    raise ValueError('value must be a mapping')
  return cloned


def _required_string(value: Mapping[str, Any], key: str) -> str:
  result = value.get(key)
  if not isinstance(result, str) or not result:
    raise ValueError(f'missing {key}')
  return result


def _required_positive_int(value: Mapping[str, Any], key: str) -> int:
  result = value.get(key)
  if type(result) is not int or result <= 0:
    raise ValueError(f'invalid {key}')
  return result


def _nested_value(value: Any, *keys: str) -> Any:
  current = value
  for key in keys:
    if not isinstance(current, Mapping):
      return None
    current = current.get(key)
  return current


def _nested_string(value: Any, *keys: str) -> str:
  result = _nested_value(value, *keys)
  if not isinstance(result, str) or not result:
    raise ValueError(f'missing {".".join(keys)}')
  return result
