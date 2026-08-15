import os
import re
import unittest
from pathlib import Path


ROOT = Path(os.environ.get('PR_REVIEW_ROOT') or Path(__file__).resolve().parents[4]).resolve()
BITBUCKET_REVIEW = ROOT / 'skills/bitbucket-pr-review/SKILL.md'
PR_REVIEW = Path(os.environ.get('PR_REVIEW_COMMAND') or ROOT / 'commands/pr-review.md')
REFERENCE = Path(os.environ.get('BITBUCKET_API_REFERENCE') or ROOT / 'references/bitbucket-api.md')
SCAN_ROOTS = (ROOT / 'commands', ROOT / 'skills', ROOT / 'scripts')
WRITE_PATTERNS = (
  re.compile(r'curl\b(?:[^\n]|\\\r?\n){0,600}?(?:-X|--request)\s*(?:POST|PUT|DELETE)\b', re.IGNORECASE),
  re.compile(
    r'curl\b'
    r'(?=(?:[^\n]|\\\r?\n){0,600}?https?://api\.bitbucket\.org\b)'
    r'(?=(?:[^\n]|\\\r?\n){0,600}?(?:(?<!\S)--data(?:-binary)?(?:\s|=)|(?<!\S)--json(?:\s|=)|(?<!\S)-d(?:\s|=|@|[^\s-])))',
    re.IGNORECASE,
  ),
  re.compile(r'\b(?:requests|httpx)\.(?:post|put|delete)\s*\(', re.IGNORECASE),
  re.compile(r'urllib\.request\.Request\([\s\S]{0,1000}?method\s*=\s*["\'](?:POST|PUT|DELETE)["\']', re.IGNORECASE),
  re.compile(r'\.request\(\s*["\'](?:POST|PUT|DELETE)["\']', re.IGNORECASE),
  re.compile(r'POST/PUT/DELETE[\s\S]{0,200}?direct\s+`?curl', re.IGNORECASE),
  re.compile(r'post_comments\.py|subprocess\.run\(\s*\[\s*["\']curl', re.IGNORECASE),
)


def is_allowed_write_owner(path: Path) -> bool:
  relative = path.relative_to(ROOT).as_posix()
  return (
    relative == 'skills/bitbucket-pr-mutation/scripts/bitbucket_pr_workflow/api.py'
    or relative.startswith('skills/bitbucket-pr-mutation/scripts/tests/')
  )


def section(text: str, start: str, end: str) -> str:
  return text.split(start, 1)[1].split(end, 1)[0]


class WorkflowContractTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.standalone = BITBUCKET_REVIEW.read_text(encoding='utf-8')
    cls.command = PR_REVIEW.read_text(encoding='utf-8')

  def test_standalone_followup_binds_exact_commit_pair(self):
    self.assertIn('{source_commit}%0D{dest_commit}', self.standalone)
    self.assertIn('source_repo_uuid', self.standalone)
    self.assertIn('destination_repo_uuid', self.standalone)
    self.assertIn('input_binding: verified', self.standalone)
    self.assertIn('## 定點複查結果', self.standalone)
    self.assertIn('git diff "{destination_sha}...{source_sha}"', self.standalone)
    self.assertIn('不得退回 moving branch ref', self.standalone)
    self.assertNotIn('git diff origin/{dest}...origin/{source}', self.standalone)
    self.assertIn('refetch PR details', self.standalone)
    self.assertIn('bitbucket-pr-mutation', self.standalone)
    self.assertIn('without a formal finding may omit `finding_uid`', self.standalone)
    self.assertIn('mutation helper treats it as optional', self.standalone)

  def test_standalone_followup_does_not_expand_into_full_scan(self):
    self.assertIn('standalone 定點複查不執行全 PR React-doctor', self.standalone)
    self.assertNotIn('#### 3.1 React Mechanical Scan', self.standalone)
    self.assertIn('完整機械掃描由 `/pr-review` 負責', self.standalone)

  def test_formal_report_contract_belongs_only_to_pr_review(self):
    for marker in (
      'finding_uid',
      'display_ordinal',
      'action_reason',
      'auto-fix',
      'ask-user',
      'no-op',
      'auto-fix 只是處置建議',
      '不修改 code、commit、push 或 PR',
      'skill-verify:pr-review',
    ):
      with self.subTest(marker=marker):
        self.assertIn(marker, self.command)
    self.assertNotIn('skill-verify:bitbucket-pr-review', self.standalone)
    self.assertNotIn('### 8. Self-Verify', self.standalone)
    self.assertIn('完整 PR review 請改用 `/pr-review`', self.standalone)
    self.assertIn('定點複查', self.standalone)

  def test_github_fetch_supplies_exact_binding_values(self):
    self.assertIn('headRefOid,baseRefOid,headRepository', self.command)
    self.assertIn('gh repo view --json id,nameWithOwner', self.command)
    self.assertIn('Map `headRefOid` to `source_sha`', self.command)
    self.assertIn('`baseRefOid` to `destination_sha`', self.command)
    self.assertIn('`headRepository.id` to `source_repo_uuid`', self.command)
    self.assertIn('do not substitute a moving branch ref', self.command)

  def test_bitbucket_step_has_strict_preview_apply_sequence(self):
    step = section(self.command, '## Step 8:', '## Error Handling')
    markers = (
      'bitbucket-pr-mutation preview --mode existing',
      'READ_ONLY_FOREIGN_AUTHOR',
      '**Scope**',
      '**Operations**',
      '**Stale inline fallback／re-anchor new proposal**',
      '**Proposal preview**',
      '**Display**',
      '**Confirmation**',
      '**typed approval**',
      '**helper apply**',
      '**Outcome table**',
    )
    positions = [step.index(marker) for marker in markers]
    self.assertEqual(positions, sorted(positions))
    self.assertIn('不要求 review basis 或 operations', step)
    self.assertNotIn('bitbucket-pr-mutation inspect', step)
    self.assertGreaterEqual(step.count('bitbucket-pr-mutation preview --mode existing'), 2)
    self.assertIn('bitbucket-pr-mutation apply', step)
    self.assertIn('post_write_drift', step)
    self.assertIn('outcome_unknown', step)
    self.assertIn('not_attempted', step)

  def test_bitbucket_display_step_keeps_ceremony_tier_guarantees(self):
    """Display/Confirmation were renamed when ceremony was risk-tiered; the tier's
    safety properties must stay asserted or the rename silently disarms this file."""
    step = section(self.command, '## Step 8:', '## Error Handling')
    self.assertIn('不派 Self-Verify subagent', step)
    self.assertIn('顯示完整 exact proposal', step)
    self.assertIn('不得在選 scope 的同一則訊息上 apply', step)

  def test_github_post_path_still_uses_gh(self):
    step = section(self.command, '## Step 8:', '## Error Handling')
    self.assertIn('GitHub', step)
    self.assertIn('gh pr comment', step)
    self.assertIn('Bitbucket', step)
    self.assertIn('bitbucket-pr-mutation', step)


class WriteOwnerTests(unittest.TestCase):
  def test_curl_implicit_post_options_are_write_patterns(self):
    commands = (
      'curl --data "{}" https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/1/comments',
      'curl --data-binary @payload.json https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/1/comments',
      'curl --json "{}" https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/1/comments',
      'curl -d "{}" https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/1/comments',
      'curl -d@payload.json https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/1/comments',
    )
    for command in commands:
      with self.subTest(command=command):
        self.assertTrue(any(pattern.search(command) for pattern in WRITE_PATTERNS))

  def test_no_executable_bitbucket_write_outside_mutation_helper(self):
    findings = []
    for root in SCAN_ROOTS:
      for path in root.rglob('*'):
        if not path.is_file() or any(part in {'.git', 'node_modules', '__pycache__'} for part in path.parts):
          continue
        try:
          text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
          continue
        if 'bitbucket' not in text.lower() and 'api.bitbucket.org' not in text.lower():
          continue
        for pattern in WRITE_PATTERNS:
          for match in pattern.finditer(text):
            if is_allowed_write_owner(path):
              continue
            line = text.count('\n', 0, match.start()) + 1
            excerpt = match.group(0).replace('\n', ' ')[:120]
            findings.append(f'{path}:{line}: {excerpt}')
    self.assertEqual(findings, [], '\n'.join(findings))

  @unittest.skipUnless(
    REFERENCE.exists(),
    f'no Bitbucket API reference doc at {REFERENCE}; set BITBUCKET_API_REFERENCE to point at yours',
  )
  def test_reference_is_schema_only(self):
    text = REFERENCE.read_text(encoding='utf-8')
    self.assertIn('不是可直接執行的 mutation 指令', text)
    self.assertIn('bitbucket-pr-mutation', text)
    for pattern in WRITE_PATTERNS[:4]:
      match = pattern.search(text)
      line = text.count('\n', 0, match.start()) + 1 if match else 0
      self.assertIsNone(match, f'{REFERENCE}:{line}: executable write recipe')


if __name__ == '__main__':
  unittest.main()
