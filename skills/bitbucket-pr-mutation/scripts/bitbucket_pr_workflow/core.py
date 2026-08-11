from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
  return json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(',', ':'),
  ).encode('utf-8')


def proposal_sha256(proposal: Mapping[str, Any]) -> str:
  return hashlib.sha256(canonical_json_bytes(proposal)).hexdigest()


def unique_batch_id(digest: str, existing: set[str]) -> str:
  for length in range(12, len(digest) + 1):
    candidate = digest[:length]
    if candidate not in existing:
      return candidate
  raise ValueError('proposal hash collision')


def make_finding_uid(file_path: str, anchor: str, root_cause: str) -> str:
  normalized_cause = re.sub(r'\s+', ' ', root_cause).strip()
  payload = '\0'.join((file_path, anchor, normalized_cause))
  return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


def validate_approval(
  expected_session_id: str,
  expected_hash: str,
  expected_operation_ids: Iterable[str],
  approval: Mapping[str, Any],
) -> None:
  keys = {
    'session_id',
    'user_message_id',
    'proposal_sha256',
    'approved_operation_ids',
  }
  if not isinstance(approval, Mapping) or set(approval) != keys:
    raise ValueError('approval fields mismatch')
  session_id = approval.get('session_id')
  message_id = approval.get('user_message_id')
  digest = approval.get('proposal_sha256')
  approved_ids = approval.get('approved_operation_ids')
  if not isinstance(session_id, str) or not session_id:
    raise ValueError('approval session missing')
  if session_id != expected_session_id:
    raise ValueError('approval session mismatch')
  if not isinstance(message_id, str) or not message_id:
    raise ValueError('approval user message missing')
  if not isinstance(digest, str) or not digest:
    raise ValueError('approval proposal hash missing')
  if digest != expected_hash:
    raise ValueError('proposal hash mismatch')
  if not isinstance(approved_ids, list):
    raise ValueError('approved operations must be a list')
  if any(not isinstance(value, str) or not value for value in approved_ids):
    raise ValueError('approved operations must be strings')
  if len(approved_ids) != len(set(approved_ids)):
    raise ValueError('approved operations contain duplicates')
  expected_ids = list(expected_operation_ids)
  if set(approved_ids) != set(expected_ids):
    raise ValueError('approved operations mismatch')
