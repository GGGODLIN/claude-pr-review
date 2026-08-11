from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path

from bitbucket_pr_workflow.api import BitbucketClient
from bitbucket_pr_workflow.description import render_description
from bitbucket_pr_workflow.executor import (
  apply_proposal,
  existing_batch_ids,
  inspect_existing_pr,
  preview_create_pr,
  preview_existing_pr,
  journal_path,
  reconcile_journal,
)
from bitbucket_pr_workflow.review import compute_review_context


BASE_URL = 'https://api.bitbucket.org/2.0'
STATE_ROOT = Path.home() / '.claude/session-state/bitbucket-pr-mutation'


def reject_json_constant(_value: str) -> None:
  raise ValueError('non-standard JSON constant')


def load_json(path: str) -> dict:
  value = json.loads(
    Path(path).read_text(encoding='utf-8'),
    parse_constant=reject_json_constant,
  )
  if not isinstance(value, dict):
    raise ValueError('JSON root must be an object')
  return value


def emit(value: object, stream=sys.stdout) -> None:
  serializable = asdict(value) if is_dataclass(value) else value
  payload = json.dumps(
    serializable,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
  ).encode('utf-8') + b'\n'
  buffer = getattr(stream, 'buffer', None)
  if buffer is not None:
    buffer.write(payload)
    buffer.flush()
    return
  stream.write(payload.decode('utf-8'))
  stream.flush()


def client_from_env() -> BitbucketClient:
  username = os.environ.get('BITBUCKET_API_USERNAME') or os.environ.get('BITBUCKET_EMAIL')
  token = os.environ.get('BITBUCKET_API_TOKEN')
  if not username or not token:
    raise ValueError(
      'BITBUCKET_API_USERNAME (or BITBUCKET_EMAIL) and BITBUCKET_API_TOKEN are required',
    )
  return BitbucketClient(BASE_URL, username, token)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers(dest='command', required=True)

  render = subparsers.add_parser('render-description')
  render.add_argument('--input', required=True)

  review = subparsers.add_parser('review-context')
  review.add_argument('--input', required=True)

  preview = subparsers.add_parser('preview')
  preview.add_argument('--input', required=True)
  preview.add_argument('--mode', choices=('existing', 'create'), required=True)

  apply = subparsers.add_parser('apply')
  apply.add_argument('--proposal', required=True)
  apply.add_argument('--approval', required=True)
  apply.add_argument('--session-id', required=True)

  reconcile = subparsers.add_parser('reconcile')
  reconcile.add_argument('--session-id', required=True)
  reconcile.add_argument('--batch-id', required=True)

  return parser


def run(args: argparse.Namespace) -> object:
  if args.command == 'render-description':
    value = load_json(args.input)
    return render_description(
      value['description'],
      value['blocks'],
      set(value['owned_blocks']),
    )
  if args.command == 'review-context':
    value = load_json(args.input)
    ancestry = value.get('ancestry')
    commits = tuple(value.get('new_commits', ()))
    return compute_review_context(
      value['reviewed'],
      value['current'],
      lambda _old, _new: ancestry,
      lambda _old, _new: commits,
    )
  if args.command == 'preview':
    value = load_json(args.input)
    client = client_from_env()
    if args.mode == 'existing' and (
      'operations' not in value or value['operations'] == []
    ):
      return inspect_existing_pr(client, value)
    batches = existing_batch_ids(STATE_ROOT)
    if args.mode == 'create':
      return preview_create_pr(client, value, batches)
    return preview_existing_pr(client, value, batches)
  if args.command == 'apply':
    client = client_from_env()
    return apply_proposal(
      client,
      load_json(args.proposal),
      load_json(args.approval),
      STATE_ROOT,
      args.session_id,
    )
  client = client_from_env()
  path = journal_path(STATE_ROOT, args.session_id, args.batch_id)
  return reconcile_journal(client, path)


def field(value: object, name: str) -> object:
  if isinstance(value, Mapping):
    return value.get(name)
  return getattr(value, name, None)


def result_exit_code(command: str, result: object) -> int:
  if command != 'apply':
    return 0
  operations = field(result, 'operations')
  if field(result, 'batch_state') != 'completed' or not isinstance(operations, Mapping) or not operations:
    return 2
  for operation in operations.values():
    if field(operation, 'state') != 'completed':
      return 2
    if field(operation, 'outcome') != 'completed':
      return 2
  return 0


def main() -> int:
  try:
    args = build_parser().parse_args()
    result = run(args)
    emit(result)
    return result_exit_code(args.command, result)
  except Exception as error:
    emit({'error': str(error)}, stream=sys.stderr)
    return 1


if __name__ == '__main__':
  raise SystemExit(main())
