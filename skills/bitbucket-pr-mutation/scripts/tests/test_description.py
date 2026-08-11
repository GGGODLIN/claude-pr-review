import unittest

from bitbucket_pr_workflow.description import (
  is_put_eligible,
  parse_description,
  render_description,
)


TESTING = '<!-- pr-review-testing:start -->\n## Testing\nold\n<!-- pr-review-testing:end -->'
RISK = '<!-- pr-review-risk:start -->\n## Risk assessment\nold\n<!-- pr-review-risk:end -->'
REVIEW = '<!-- pr-review-review-basis:start -->\n## Review basis\nold\n<!-- pr-review-review-basis:end -->'


class DescriptionTests(unittest.TestCase):
  def test_review_basis_update_preserves_other_blocks_and_author_text(self):
    testing = TESTING.replace('\n', '\r\n')
    risk = RISK.replace('\n', '\r\n')
    review = REVIEW.replace('\n', '\r\n')
    original = f'作者前言\r\n\r\n{testing}\r\n\r\n{risk}\r\n\r\n{review}\r\n尾端  '
    result = render_description(
      original,
      {'review_basis': '## Review basis\nnew'},
      {'review_basis'},
    )
    self.assertIn('作者前言\r\n\r\n', result.description)
    self.assertIn(testing, result.description)
    self.assertIn(risk, result.description)
    self.assertIn('## Review basis\r\nnew', result.description)
    self.assertTrue(result.description.endswith('\r\n尾端  '))
    self.assertFalse(result.put_eligible)

  def test_marker_inside_fenced_code_is_not_managed(self):
    original = '```markdown\n<!-- pr-review-testing:start -->\ntext\n```'
    parsed = parse_description(original)
    self.assertEqual(parsed.blocks, {})
    self.assertFalse(is_put_eligible(parsed))

  def test_unmanaged_author_text_is_draft_only(self):
    parsed = parse_description(f'作者文字\n\n{TESTING}')
    self.assertFalse(is_put_eligible(parsed))

  def test_empty_and_fully_managed_descriptions_are_put_eligible(self):
    self.assertTrue(is_put_eligible(parse_description('')))
    self.assertTrue(is_put_eligible(parse_description(f'{TESTING}\n\n{RISK}\n\n{REVIEW}')))

  def test_partial_or_duplicate_markers_fail_closed(self):
    with self.assertRaises(ValueError):
      parse_description('<!-- pr-review-testing:start -->')
    with self.assertRaises(ValueError):
      parse_description(f'{TESTING}\n{TESTING}')

  def test_render_is_idempotent(self):
    first = render_description('', {'testing': '## Testing\nnone'}, {'testing'})
    second = render_description(first.description, {'testing': '## Testing\nnone'}, {'testing'})
    self.assertEqual(first.description, second.description)

  def test_no_final_newline_and_unicode_author_text_are_byte_preserved(self):
    original = '作者文字 🧪'
    result = render_description(original, {'testing': '## Testing\nnone'}, {'testing'})
    self.assertTrue(result.description.startswith(original + '\n\n'))
    self.assertFalse(result.put_eligible)

  def test_generated_reserved_marker_fails_closed(self):
    with self.assertRaises(ValueError):
      render_description('', {'testing': '<!-- pr-review-risk:start -->'}, {'testing'})

  def test_overlap_and_nested_markers_fail_closed(self):
    with self.assertRaises(ValueError):
      parse_description(
        '<!-- pr-review-testing:start -->\n'
        '<!-- pr-review-risk:start -->\n'
        '<!-- pr-review-testing:end -->\n'
        '<!-- pr-review-risk:end -->'
      )

  def test_whitespace_only_nonempty_description_is_not_empty(self):
    self.assertFalse(is_put_eligible(parse_description('  \n')))
