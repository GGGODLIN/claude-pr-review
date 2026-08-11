from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


MARKERS = {
  'testing': (
    '<!-- pr-review-testing:start -->',
    '<!-- pr-review-testing:end -->',
  ),
  'risk': (
    '<!-- pr-review-risk:start -->',
    '<!-- pr-review-risk:end -->',
  ),
  'review_basis': (
    '<!-- pr-review-review-basis:start -->',
    '<!-- pr-review-review-basis:end -->',
  ),
}


@dataclass(frozen=True)
class ManagedRange:
  name: str
  block_start: int
  body_start: int
  body_end: int
  block_end: int
  newline: str


@dataclass(frozen=True)
class ParsedDescription:
  text: str
  blocks: Mapping[str, ManagedRange]
  unmanaged_segments: tuple[str, ...]
  newline: str


@dataclass(frozen=True)
class RenderResult:
  description: str
  put_eligible: bool
  reason: str


def line_text_and_newline(line: str) -> tuple[str, str]:
  if line.endswith('\r\n'):
    return line[:-2], '\r\n'
  if line.endswith('\n') or line.endswith('\r'):
    return line[:-1], line[-1]
  return line, ''


def parse_description(text: str) -> ParsedDescription:
  lines = text.splitlines(keepends=True)
  newline = next((line_text_and_newline(line)[1] for line in lines if line_text_and_newline(line)[1]), '\n')
  marker_lookup = {
    marker: (name, kind)
    for name, pair in MARKERS.items()
    for marker, kind in zip(pair, ('start', 'end'), strict=True)
  }
  blocks = {}
  active = None
  offset = 0
  fence = None
  for line in lines:
    content, line_newline = line_text_and_newline(line)
    stripped = content.lstrip(' ')
    indent = len(content) - len(stripped)
    fence_match = None
    if indent <= 3 and stripped:
      character = stripped[0]
      length = len(stripped) - len(stripped.lstrip(character))
      if character in ('`', '~') and length >= 3:
        fence_match = (character, length, stripped[length:])
    if fence is not None:
      if fence_match and fence_match[0] == fence[0] and fence_match[1] >= fence[1] and not fence_match[2].strip():
        fence = None
      offset += len(line)
      continue
    if fence_match:
      fence = fence_match[:2]
      offset += len(line)
      continue
    marker = marker_lookup.get(content)
    if marker is not None:
      name, kind = marker
      if kind == 'start':
        if active is not None or name in blocks:
          raise ValueError('nested or duplicate managed marker')
        active = (name, offset, offset + len(line), line_newline or newline)
      else:
        if active is None or active[0] != name:
          raise ValueError('partial or overlapping managed marker')
        marker_end = offset + len(content)
        blocks[name] = ManagedRange(
          name,
          active[1],
          active[2],
          offset,
          marker_end,
          active[3],
        )
        active = None
    offset += len(line)
  if active is not None:
    raise ValueError('partial managed marker')
  ordered = sorted(blocks.values(), key=lambda value: value.block_start)
  cursor = 0
  unmanaged = []
  for block in ordered:
    unmanaged.append(text[cursor:block.block_start])
    cursor = block.block_end
  unmanaged.append(text[cursor:])
  return ParsedDescription(text, blocks, tuple(unmanaged), newline)


def render_block(name: str, body: str, newline: str) -> str:
  if any(marker in body for pair in MARKERS.values() for marker in pair):
    raise ValueError('generated content contains reserved marker')
  normalized = body.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n').replace('\n', newline)
  start, end = MARKERS[name]
  return f'{start}{newline}{normalized}{newline}{end}'


def is_put_eligible(parsed: ParsedDescription) -> bool:
  if parsed.text == '':
    return True
  ordered = sorted(parsed.blocks.values(), key=lambda value: value.block_start)
  if not ordered:
    return False
  cursor = 0
  for index, block in enumerate(ordered):
    expected = '' if index == 0 else parsed.newline * 2
    if parsed.text[cursor:block.block_start] != expected:
      return False
    cursor = block.block_end
  return parsed.text[cursor:] == ''


def render_description(
  text: str,
  rendered_blocks: Mapping[str, str],
  owned_blocks: set[str],
) -> RenderResult:
  unknown = owned_blocks.difference(MARKERS)
  missing_payloads = owned_blocks.difference(rendered_blocks)
  extra_payloads = set(rendered_blocks).difference(owned_blocks)
  if unknown or missing_payloads or extra_payloads:
    raise ValueError('owned block input mismatch')
  parsed = parse_description(text)
  pieces = []
  cursor = 0
  for block in sorted(parsed.blocks.values(), key=lambda value: value.body_start):
    pieces.append(text[cursor:block.body_start])
    if block.name in owned_blocks:
      rendered = render_block(block.name, rendered_blocks[block.name], block.newline)
      start, end = MARKERS[block.name]
      body = rendered[len(start) + len(block.newline):-(len(end))]
      pieces.append(body)
    else:
      pieces.append(text[block.body_start:block.body_end])
    cursor = block.body_end
  pieces.append(text[cursor:])
  result = ''.join(pieces)
  missing = [name for name in MARKERS if name in owned_blocks and name not in parsed.blocks]
  if missing:
    suffix = parsed.newline * 2 if result else ''
    result = result + suffix + (parsed.newline * 2).join(
      render_block(name, rendered_blocks[name], parsed.newline)
      for name in missing
    )
  eligibility = is_put_eligible(parse_description(result))
  reason = 'PUT_ELIGIBLE' if eligibility else 'DRAFT_ONLY_UNMANAGED_DESCRIPTION'
  return RenderResult(result, eligibility, reason)
