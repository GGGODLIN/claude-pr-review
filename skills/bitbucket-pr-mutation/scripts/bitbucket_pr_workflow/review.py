from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


ACTIONS = frozenset({'auto-fix', 'ask-user', 'no-op'})
CURRENT_SNAPSHOT_FIELDS = (
  'source_repo_uuid',
  'destination_repo_uuid',
  'source_branch',
  'destination_branch',
  'source_sha',
  'destination_sha',
)


def is_full_sha(value: object) -> bool:
  return (
    isinstance(value, str)
    and len(value) == 40
    and all(character in '0123456789abcdef' for character in value.lower())
  )


@dataclass(frozen=True)
class ReviewContext:
  snapshot_resolved: bool
  source_continuity: str
  base_changed: bool | None
  review_context_changed: bool
  new_commits: tuple[str, ...]


def resolve_finding_action(action: str | None, is_uncertain: bool) -> str:
  if is_uncertain or action is None:
    return 'ask-user'
  if action not in ACTIONS:
    raise ValueError('unsupported finding action')
  return action


def compute_review_context(
  reviewed: Mapping[str, str],
  current: Mapping[str, str],
  is_ancestor: Callable[[str, str], bool | None],
  list_new_commits: Callable[[str, str], Sequence[str] | None],
) -> ReviewContext:
  complete = all(current.get(key) for key in CURRENT_SNAPSHOT_FIELDS)
  full_shas = all(is_full_sha(current.get(key)) for key in ('source_sha', 'destination_sha'))
  if not complete or not full_shas:
    return ReviewContext(False, 'UNKNOWN', None, True, ())
  reviewed_source = reviewed.get('source_sha', '')
  reviewed_destination = reviewed.get('destination_sha', '')
  if not is_full_sha(reviewed_source):
    continuity = 'UNKNOWN'
    new_commits = ()
  elif reviewed_source == current['source_sha']:
    continuity = 'CURRENT'
    new_commits = ()
  else:
    ancestor = is_ancestor(reviewed_source, current['source_sha'])
    continuity = 'NEW_COMMITS' if ancestor is True else 'HISTORY_REWRITE' if ancestor is False else 'UNKNOWN'
    listed = list_new_commits(reviewed_source, current['source_sha']) if continuity == 'NEW_COMMITS' else None
    new_commits = tuple(listed or ())
  base_changed = None if not is_full_sha(reviewed_destination) else reviewed_destination != current['destination_sha']
  return ReviewContext(
    True,
    continuity,
    base_changed,
    continuity != 'CURRENT' or base_changed is not False,
    new_commits,
  )


def automated_status(item: Mapping[str, Any]) -> str:
  status = item.get('status', 'unknown')
  if status not in {'passed', 'failed', 'not_run', 'unknown'}:
    return 'unknown'
  if status in {'passed', 'failed'}:
    exit_code = item.get('exit_code')
    if item.get('provenance') != 'agent-observed':
      return 'unknown'
    if not item.get('run_ref') or type(exit_code) is not int:
      return 'unknown'
    if status == 'passed' and exit_code != 0:
      return 'unknown'
    if status == 'failed' and exit_code == 0:
      return 'unknown'
  return status


def render_testing(
  automated: Sequence[Mapping[str, Any]],
  manual: Sequence[Mapping[str, Any]],
) -> str:
  automated_lines = []
  for item in automated:
    command = item.get('command', '未提供命令')
    status = automated_status(item)
    worktree_keys = (
      'index_clean_before',
      'worktree_clean_before',
      'index_clean_after',
      'worktree_clean_after',
    )
    worktree_known = all(type(item.get(key)) is bool for key in worktree_keys)
    stable_head = (
      item.get('head_before') == item.get('head_after')
      and is_full_sha(item.get('head_before'))
    )
    digest = item.get('dirty_patch_sha256')
    clean = (
      worktree_known
      and stable_head
      and digest is None
      and all(item.get(key) is True for key in worktree_keys)
    )
    valid_digest = (
      isinstance(digest, str)
      and len(digest) == 64
      and all(character in '0123456789abcdef' for character in digest.lower())
    )
    dirty = stable_head and worktree_known and any(item.get(key) is False for key in worktree_keys) and valid_digest
    if status == 'not_run':
      detail = '⏭️ 未執行'
    elif status == 'unknown' or not clean and not dirty:
      detail = '❔ 結果未知'
    elif clean:
      label = '通過' if status == 'passed' else '失敗'
      detail = f'{label}，clean SHA `{item["head_after"][:7]}`'
    else:
      label = '通過' if status == 'passed' else '失敗'
      detail = f'{label}，但含未提交變更，patch `{digest[:7]}`'
    automated_lines.append(f'- `{command}` — {detail}')
  if not automated_lines:
    automated_lines.append('- 本次未記錄 Agent 實際執行的自動檢查')
  manual_rows = []
  versions = []
  for item in manual:
    source_valid = bool(
      item.get('reported_by') == 'user'
      and item.get('source_ref')
      and item.get('source_summary')
    )
    result = item.get('result')
    result_valid = source_valid and result in {'passed', 'failed'}
    if result_valid:
      rendered_result = '通過（使用者確認）' if result == 'passed' else '失敗（使用者確認）'
    else:
      rendered_result = '未提供'
    manual_rows.append('| {scenario} | {environment} | {result} | {evidence} |'.format(
      scenario=item.get('scenario', '未提供'),
      environment=item.get('environment', '未提供'),
      result=rendered_result,
      evidence=item.get('evidence') if result_valid and item.get('evidence') else '未提供',
    ))
    if result_valid and is_full_sha(item.get('tested_source_sha')):
      versions.append(f'SHA `{item["tested_source_sha"]}`')
    if result_valid and item.get('deployment_ref'):
      versions.append(str(item['deployment_ref']))
  if not manual_rows:
    manual_rows.append('| 未提供使用者人工 E2E | 未提供 | 未提供 | 未提供 |')
  version_text = '、'.join(dict.fromkeys(versions)) if versions else '未提供'
  return '\n'.join((
    '## Testing',
    '',
    '### Automated',
    *automated_lines,
    '',
    '### Manual E2E',
    '',
    '| 測試情境 | 環境／店家 | 結果 | 證據 |',
    '|---|---|---|---|',
    *manual_rows,
    '',
    f'被測版本：{version_text}',
  ))


def render_risk(value: Mapping[str, str | None]) -> str:
  return '\n'.join((
    '## Risk assessment',
    '',
    f'- 可能影響：{value.get("possible_impact") or "未確認"}',
    f'- 不影響：{value.get("unaffected") or "未確認"}',
    f'- 回復方式：{value.get("rollback") or "未確認"}',
  ))


def render_review_basis(value: Mapping[str, Any]) -> str:
  if value.get('status') != 'reviewed':
    return '## Review basis\n\n尚未執行 PR review。'
  if value.get('input_binding') != 'verified' or not is_full_sha(value.get('source_sha')):
    return '## Review basis\n\nreview input 未驗證；不宣稱已審查的 SHA。'
  lines = [
    '## Review basis',
    '',
    f'- Reviewed SHA：`{value["source_sha"][:7]}`',
  ]
  report = value.get('review_report')
  if report:
    label = f'local-only `{report}`' if value.get('report_local_only') else str(report)
    lines.append(f'- Review report：{label}')
  lines.append(f'- Reviewed at：{value.get("reviewed_at", "未確認")}')
  return '\n'.join(lines)
