#!/usr/bin/env python3

import argparse
import fcntl
import datetime
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt


CLAUDE_ROOT = Path(__file__).resolve().parent.parent
REPORT_ROOT = (CLAUDE_ROOT / "pr-review-reports").resolve()
REPORT_PATTERNS = (
  ("main", re.compile(r"pr-(\d+)-review\.md")),
  ("audit", re.compile(r"pr-(\d+)-review\.audit\.md")),
  ("draft", re.compile(r"pr-(\d+)-review\.audit\.draft\.md")),
)
GROUP_PATTERNS = (
  ("main", re.compile(r"set-([0-9a-f]{12})-review\.md")),
  ("audit", re.compile(r"set-([0-9a-f]{12})-review\.audit\.md")),
  ("draft", re.compile(r"set-([0-9a-f]{12})-review\.audit\.draft\.md")),
)
GROUP_ROOT_NAME = "sets"
ZERO_FINDING_MARKER = "本輪零 finding：跨目標位置表沒有資料列，這是正常結果，不是表格損壞。"
GROUP_SCHEMA_LINE = "**Report projection schema**: 3"
UID_PATTERN = re.compile(r"(?<![0-9a-f])([0-9a-f]{20})(?![0-9a-f])")
SCHEMA_LINES = {
  "**Report projection schema**: 1": 1,
  "**Report projection schema**: 2": 2,
  GROUP_SCHEMA_LINE: 3,
}
SET_IDENTITY_PATTERN = re.compile(
  r"\*\*Review set identity\*\*: (sha256:[0-9a-f]{64})"
)
TARGET_VERSIONS_PATTERN = re.compile(
  r"\*\*Target versions\*\*: (.+)"
)
VERSION_ENTRY_PATTERN = re.compile(
  r"(\S+)=head:([0-9a-f]{40}|none)\|base:([0-9a-f]{40}|none)\|state:(current|drifted|unavailable)"
)
PRIOR_REVIEW_TITLE = "上一輪 findings 對帳"
INLINE_TITLES = {
  "Inline Comments per Finding",
  "Inline Comments per Finding（直接複製貼到 PR review）",
  "Inline Comments per Finding（複製貼到 PR）",
}
SET_LOCATIONS_TITLE = "跨目標位置"
GROUP_KEEP_TITLES = (
  "發現總覽",
  SET_LOCATIONS_TITLE,
  PRIOR_REVIEW_TITLE,
  *INLINE_TITLES,
)
LOCATION_HEADERS = ("group_id", "finding_uid", "target_identity", "file", "line", "source")
LINE_SENTINEL = "需人工確認（anchor 未在綁定來源中比中或證據 binding 失效）"
LOCATION_UID_HEADING = "### finding_uid 索引"
KEEP_TITLES = (
  "發現總覽",
  PRIOR_REVIEW_TITLE,
  "React-doctor 機械掃描",
  *INLINE_TITLES,
  "本輪限制",
  "本輪限制（沒做的部分）",
  "本輪未做/限制（對帳）",
  "沒做的部分",
  "沒做的部分（結案對帳）",
  "Review continuity",
  "Review continuity（產報告前重驗）",
)
STATE_PREFIXES = (
  "**Prior review continuity**:",
  "**審查工具**:",
  "**Reviewer models**:",
  "**覆蓋 (ENH-A)**:",
  "**定位 (ENH-B)**:",
  "**Formal spec traceability (2.65)**:",
)
PREAMBLE_METADATA_PATTERN = re.compile(r"\*\*[^*\r\n]+\*\*: .+")
SUMMARY_NOTICE = "auto-fix 只是處置建議；沒有使用者另行下令，不修改 code、commit、push 或 PR。"
PRIORITY_RANK = {
  "Must Fix": 0,
  "Should Fix": 1,
  "Nice to Have": 2,
  "參考用": 3,
}


def scan_markdown_lines(text):
  lines = text.splitlines(keepends=True)
  flags = [None] * len(lines)
  fence_boundaries = set()
  unclosed_fence = None
  for token in MarkdownIt("commonmark").parse(text):
    if token.map is None or token.type not in {"fence", "html_block", "code_block"}:
      continue
    start, end = token.map
    if token.type == "fence":
      marker = token.markup
      closing = end - 1
      closing_line = lines[closing].rstrip("\r\n")
      if token.level > 0:
        closing_line = re.sub(r"^(?: {0,3}> ?)+", "", closing_line)
        closing_pattern = rf"\s*{re.escape(marker[0])}{{{len(marker)},}}\s*"
      else:
        closing_pattern = rf" {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*"
      closed = closing > start and bool(re.fullmatch(closing_pattern, closing_line))
      if closed:
        fence_boundaries.update((start, closing))
      else:
        unclosed_fence = marker[0]
      for index in range(start, end):
        flags[index] = "fence"
    elif token.type == "html_block":
      for index in range(start, end):
        flags[index] = "html"
    else:
      for index in range(start, end):
        flags[index] = "indented"
  records = []
  offset = 0
  for index, line in enumerate(lines):
    kind = flags[index]
    boundary = index in fence_boundaries
    records.append({
      "offset": offset,
      "line": line,
      "structural": kind is None,
      "fence": kind == "fence",
      "fence_boundary": boundary,
      "html": kind == "html",
      "indented": kind == "indented",
    })
    offset += len(line)
  return records, unclosed_fence


def assert_balanced_fences(text):
  _, fence_char = scan_markdown_lines(text)
  if fence_char is not None:
    raise ValueError("unclosed Markdown fence")


def outside_fence_lines(text):
  records, _ = scan_markdown_lines(text)
  return [(record["offset"], record["line"]) for record in records if record["structural"]]


def find_headings(text, level, top_level=True):
  lines = text.splitlines(keepends=True)
  offsets = [0]
  for line in lines:
    offsets.append(offsets[-1] + len(line))
  tokens = MarkdownIt("commonmark").parse(text)
  headings = []
  for index, token in enumerate(tokens):
    if (
      token.type != "heading_open"
      or token.tag != f"h{level}"
      or (top_level and token.level != 0)
      or token.map is None
    ):
      continue
    start_line, end_line = token.map
    title = tokens[index + 1].content.strip()
    headings.append((offsets[start_line], offsets[end_line], title))
  return headings


def table_cells(line):
  if not line.startswith("|"):
    return []
  raw_cells = []
  current = []
  backslashes = 0
  for character in line.strip():
    if character == "\\":
      current.append(character)
      backslashes += 1
      continue
    escaped = backslashes % 2 == 1
    backslashes = 0
    if character == "|" and not escaped:
      raw_cells.append("".join(current))
      current = []
    else:
      current.append(character)
  raw_cells.append("".join(current))
  if raw_cells and not raw_cells[0]:
    raw_cells.pop(0)
  if raw_cells and not raw_cells[-1]:
    raw_cells.pop()
  return [cell.strip().strip("`") for cell in raw_cells]


def is_table_separator(cells):
  return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def extract_ordinal(line):
  cells = table_cells(line)
  if cells:
    match = re.fullmatch(r"(?:F-)?(0[1-9]|[1-9]\d*)", cells[0])
    return int(match.group(1)) if match else None
  match = re.search(r"\bF-(0[1-9]|[1-9]\d*)\b", line)
  return int(match.group(1)) if match else None


def extract_finding_rows(text):
  result = []
  invalid = []
  canonical_tables = 0
  in_finding_table = False
  awaiting_separator = False
  header_width = 0
  for _, line in outside_fence_lines(finding_summary_scope(text)):
    cells = table_cells(line)
    if not cells:
      if awaiting_separator:
        invalid.append("missing finding table separator")
      in_finding_table = False
      awaiting_separator = False
      header_width = 0
      continue
    normalized = [cell.lower() for cell in cells]
    if not in_finding_table:
      if normalized[0] == "#":
        canonical_tables += 1
        in_finding_table = True
        awaiting_separator = True
        header_width = len(cells)
        required_headers = {"#", "問題", "最終建議", "action", "action 理由"}
        if (
          not required_headers.issubset(set(normalized))
          or normalized[0] != "#"
          or any(normalized.count(header) != 1 for header in required_headers)
        ):
          invalid.append(line)
      elif "action" in normalized:
        invalid.append(line)
      continue
    if awaiting_separator:
      if is_table_separator(cells) and len(cells) == header_width:
        awaiting_separator = False
      else:
        invalid.append(line)
        in_finding_table = False
      continue
    if is_table_separator(cells) or len(cells) != header_width:
      invalid.append(line)
      continue
    ordinal = extract_ordinal(line)
    if ordinal is None:
      invalid.append(line)
    else:
      result.append(ordinal)
  if awaiting_separator:
    invalid.append("missing finding table separator")
  return result, invalid, canonical_tables


def extract_finding_inventory(text):
  inventory, _, _ = extract_finding_rows(text)
  return inventory


def extract_finding_actions(text):
  inventory = extract_finding_inventory(text)
  order = inventory.copy()
  actions = {}
  priorities = []
  action_column = None
  suggestion_column = None
  reason_column = None
  in_finding_table = False
  awaiting_separator = False
  for _, line in outside_fence_lines(finding_summary_scope(text)):
    cells = table_cells(line)
    if cells:
      if cells[0].lower() == "#" and any(cell.lower() == "action" for cell in cells):
        header_map = {cell.lower(): index for index, cell in enumerate(cells)}
        required = {"action", "最終建議", "action 理由"}
        if not required.issubset(header_map):
          continue
        in_finding_table = True
        awaiting_separator = True
        action_column = header_map["action"]
        suggestion_column = header_map["最終建議"]
        reason_column = header_map["action 理由"]
        continue
      if in_finding_table and awaiting_separator:
        awaiting_separator = not is_table_separator(cells)
        continue
      if in_finding_table and action_column is not None:
        ordinal = extract_ordinal(line)
        value = cells[action_column] if action_column < len(cells) else ""
        action_match = re.fullmatch(r"`?(auto-fix|ask-user|no-op)`?", value, re.IGNORECASE)
        suggestion = cells[suggestion_column] if suggestion_column < len(cells) else ""
        reason = cells[reason_column] if reason_column < len(cells) else ""
        if (
          ordinal is not None
          and action_match is not None
          and suggestion in PRIORITY_RANK
          and bool(reason.strip())
        ):
          actions.setdefault(ordinal, []).append(action_match.group(1).lower())
          priorities.append(PRIORITY_RANK[suggestion])
      continue
    in_finding_table = False
    awaiting_separator = False
    action_column = None
    suggestion_column = None
    reason_column = None
    if inventory:
      continue
    action_match = re.search(r"\baction\s*=\s*`?(auto-fix|ask-user|no-op)`?", line, re.IGNORECASE)
    ordinal = extract_ordinal(line)
    if action_match is None or ordinal is None:
      continue
    if ordinal not in actions:
      order.append(ordinal)
    actions.setdefault(ordinal, []).append(action_match.group(1).lower())
  if not inventory and not actions:
    for line in finding_summary_scope(text).splitlines():
      action_match = re.search(r"\baction\s*=\s*`?(auto-fix|ask-user|no-op)`?", line, re.IGNORECASE)
      ordinal = extract_ordinal(line)
      if action_match is None or ordinal is None:
        continue
      if ordinal not in actions:
        order.append(ordinal)
      actions.setdefault(ordinal, []).append(action_match.group(1).lower())
  return order, actions, priorities


def inline_comment_scopes(text):
  _, sections = split_h2_sections(text)
  result = [section for title, section in sections if title in INLINE_TITLES]
  for title, section in sections:
    if title != "發現總覽":
      continue
    headings = find_headings(section, 3)
    for index, (start, _, heading_title) in enumerate(headings):
      if heading_title not in INLINE_TITLES:
        continue
      end = headings[index + 1][0] if index + 1 < len(headings) else len(section)
      result.append(section[start:end])
  return result


def inline_comment_preambles_are_empty(text):
  for scope in inline_comment_scopes(text):
    openings = [
      heading
      for level in (2, 3)
      for heading in find_headings(scope, level)
      if heading[2] in INLINE_TITLES
    ]
    if len(openings) != 1:
      return False
    _, body_start, _ = openings[0]
    finding_headings = find_headings(scope, 4)
    body_end = finding_headings[0][0] if finding_headings else len(scope)
    if scope[body_start:body_end].strip():
      return False
  return True


def strip_inline_comment_preamble(section):
  openings = [
    heading
    for level in (2, 3)
    for heading in find_headings(section, level)
    if heading[2] in INLINE_TITLES
  ]
  if len(openings) != 1:
    return section
  _, body_start, _ = openings[0]
  finding_headings = find_headings(section, 4)
  body_end = next((start for start, _, _ in finding_headings if start >= body_start), len(section))
  return section[:body_start] + section[body_end:]


def extract_finding_block_records(text):
  result = []
  invalid = []
  for scope in inline_comment_scopes(text):
    headings = [(start, title) for start, _, title in find_headings(scope, 4)]
    for index, (start, title) in enumerate(headings):
      end = headings[index + 1][0] if index + 1 < len(headings) else len(scope)
      block = scope[start:end]
      ordinal = re.fullmatch(r"#?(?:F-)?(0[1-9]|[1-9]\d*)([a-z]?)\s+.+", title)
      if ordinal:
        result.append((int(ordinal.group(1)), ordinal.group(2), block))
      else:
        invalid.append(block)
  return result, invalid


def extract_finding_blocks(text):
  blocks, _ = extract_finding_block_records(text)
  return [(ordinal, block) for ordinal, _, block in blocks]


def extract_strict_uid_records(text):
  result = {}
  actions = {}
  no_inline = set()
  record_order = []
  invalid = []
  for _, line in outside_fence_lines(finding_summary_scope(text)):
    content = line.rstrip("\r\n")
    match = re.fullmatch(
      r"F-(0[1-9]|[1-9]\d+) finding_uid: ([0-9a-f]{20}) action=(auto-fix|ask-user|no-op)(?: inline=(none))?",
      content,
    )
    if match:
      ordinal = int(match.group(1))
      result.setdefault(ordinal, []).append(match.group(2))
      actions.setdefault(ordinal, []).append(match.group(3))
      record_order.append(ordinal)
      if match.group(4):
        no_inline.add(ordinal)
    elif re.match(r" {0,3}F-\S+\s+finding_uid\b", content, re.IGNORECASE):
      invalid.append(content)
  return result, actions, no_inline, record_order, invalid


def extract_uid_map(text, strict=False):
  if strict:
    result, _, _, _, _ = extract_strict_uid_records(text)
    return result
  result = {}
  for line in finding_summary_scope(text).splitlines():
    pairs = re.findall(r"\bF-(\d+)\b.*?([0-9a-f]{20})", line)
    if pairs:
      for ordinal, uid in pairs:
        result.setdefault(int(ordinal), []).append(uid)
      continue
    ordinal = extract_ordinal(line)
    if ordinal is None:
      continue
    for uid in UID_PATTERN.findall(line):
      result.setdefault(ordinal, []).append(uid)
  for ordinal, block in extract_finding_blocks(text):
    for _, line in outside_fence_lines(block):
      if not re.search(r"finding_uid|內部 uid|內部結構", line, re.IGNORECASE):
        continue
      for uid in UID_PATTERN.findall(line):
        result.setdefault(ordinal, []).append(uid)
  return {ordinal: list(dict.fromkeys(uids)) for ordinal, uids in result.items()}


def extract_finding_uids(text):
  order, _, _ = extract_finding_actions(text)
  strict = projection_schema_version(text) is not None
  uid_map = extract_uid_map(text, strict=strict)
  return [uid_map[ordinal][0] for ordinal in order if len(uid_map.get(ordinal, [])) == 1]


def extract_noop_ordinals(text):
  _, actions, _ = extract_finding_actions(text)
  return {
    ordinal
    for ordinal, values in actions.items()
    if set(values) == {"no-op"}
  }


def filter_noop_inline_blocks(section, no_ops):
  h2_headings = find_headings(section, 2)
  if h2_headings and h2_headings[0][2] in INLINE_TITLES:
    scope_start, scope_end = 0, len(section)
  else:
    h3_headings = find_headings(section, 3)
    inline_indexes = [index for index, heading in enumerate(h3_headings) if heading[2] in INLINE_TITLES]
    if len(inline_indexes) != 1:
      return section
    inline_index = inline_indexes[0]
    scope_start = h3_headings[inline_index][0]
    scope_end = h3_headings[inline_index + 1][0] if inline_index + 1 < len(h3_headings) else len(section)
  scope = section[scope_start:scope_end]
  headings = []
  for start, _, title in find_headings(scope, 4):
    ordinal = re.match(r"#?(?:F-)?(\d+)[a-z]?\b", title)
    if ordinal:
      headings.append((start, int(ordinal.group(1))))
  if not headings:
    return section
  parts = [scope[:headings[0][0]]]
  for index, (start, ordinal) in enumerate(headings):
    end = headings[index + 1][0] if index + 1 < len(headings) else len(scope)
    if ordinal not in no_ops:
      parts.append(scope[start:end])
  return section[:scope_start] + "".join(parts) + section[scope_end:]


def trim_after_inline_comments(section):
  headings = find_headings(section, 3)
  for index, (start, _, title) in enumerate(headings):
    if title in INLINE_TITLES and index + 1 < len(headings):
      return section[:headings[index + 1][0]]
  return section


def remove_external_blank_lines(text):
  records, _ = scan_markdown_lines(text)
  boundary_flags = [
    record["fence_boundary"]
    or (record["structural"] and bool(re.match(r" {0,3}#{1,6}\s", record["line"])))
    for record in records
  ]
  result = []
  for index, record in enumerate(records):
    line = record["line"]
    content = line.rstrip("\r\n")
    if record["fence"] or record["html"] or record["indented"]:
      result.append(line)
      continue
    if content.strip():
      result.append(content + "\n")
      continue
    previous_index = next((position for position in range(index - 1, -1, -1) if records[position]["line"].strip()), None)
    following_index = next((position for position in range(index + 1, len(records)) if records[position]["line"].strip()), None)
    adjacent_html = any(
      position is not None and records[position]["html"]
      for position in (previous_index, following_index)
    )
    adjacent_boundary = any(
      position is not None and boundary_flags[position]
      for position in (previous_index, following_index)
    )
    if not adjacent_html and adjacent_boundary:
      continue
    if result and not result[-1].strip():
      continue
    result.append("\n")
  return "".join(result).rstrip("\r\n")


def normalize_structure(text):
  return "\n".join(line.rstrip("\r\n") for line in text.splitlines(keepends=True) if line.strip())


def split_h2_sections(text):
  headings = find_headings(text, 2)
  if not headings:
    return text, []
  preamble = text[:headings[0][0]]
  sections = []
  for index, (start, _, title) in enumerate(headings):
    end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
    sections.append((title, text[start:end]))
  return preamble, sections


def unclosed_html_block_hides_heading(text):
  records, _ = scan_markdown_lines(text)
  for index, record in enumerate(records):
    if not record["html"]:
      continue
    content = record["line"].rstrip("\r\n")
    if re.fullmatch(r" {0,3}#{1,6}(?:\s+.*)?", content):
      return True
    if re.fullmatch(r" {0,3}(?:=+|-+)\s*", content):
      previous = next(
        (
          prior["line"].strip()
          for prior in reversed(records[:index])
          if prior["html"] and prior["line"].strip()
        ),
        "",
      )
      if previous:
        return True
  return False


def finding_summary_scope(text):
  _, sections = split_h2_sections(text)
  for title, section in sections:
    if title != "發現總覽":
      continue
    h3_headings = find_headings(section, 3)
    inline_headings = [heading for heading in h3_headings if heading[2] in INLINE_TITLES]
    if len(inline_headings) == 1:
      return section[:inline_headings[0][0]]
    return section
  return ""


def finding_summary_structure_is_valid(text):
  _, sections = split_h2_sections(text)
  summaries = [section for title, section in sections if title == "發現總覽"]
  if len(summaries) != 1:
    return False
  section = summaries[0]
  h3_headings = find_headings(section, 3)
  inline_indexes = [index for index, heading in enumerate(h3_headings) if heading[2] in INLINE_TITLES]
  if len(inline_indexes) != 1 or inline_indexes[0] != 0:
    return False
  inline_start = h3_headings[0][0]
  summary_prefix = section[:inline_start]
  records, unclosed_fence = scan_markdown_lines(summary_prefix)
  if unclosed_fence is not None or any(not record["structural"] for record in records):
    return False
  lines = [record["line"].rstrip("\r\n") for record in records]
  if not lines or lines[0] != "## 發現總覽":
    return False
  index = 1
  while index < len(lines) and not lines[index].strip():
    index += 1
  if index >= len(lines):
    return False
  header = table_cells(lines[index])
  normalized = [cell.lower() for cell in header]
  required_headers = {"#", "問題", "最終建議", "action", "action 理由"}
  if not required_headers.issubset(set(normalized)):
    return False
  width = len(header)
  index += 1
  if index >= len(lines):
    return False
  separator = table_cells(lines[index])
  if len(separator) != width or not is_table_separator(separator):
    return False
  index += 1
  row_count = 0
  while index < len(lines):
    cells = table_cells(lines[index])
    if not cells:
      break
    if len(cells) != width or is_table_separator(cells) or extract_ordinal(lines[index]) is None:
      return False
    row_count += 1
    index += 1
  while index < len(lines) and not lines[index].strip():
    index += 1
  if index < len(lines) and lines[index] == SUMMARY_NOTICE:
    index += 1
    while index < len(lines) and not lines[index].strip():
      index += 1
  uid_count = 0
  while index < len(lines):
    if not lines[index].strip():
      index += 1
      continue
    if not re.fullmatch(
      r"F-(?:0[1-9]|[1-9]\d+) finding_uid: [0-9a-f]{20} action=(?:auto-fix|ask-user|no-op)(?: inline=none)?",
      lines[index],
    ):
      return False
    uid_count += 1
    index += 1
  return uid_count == row_count


def extract_comment_payload_records(text):
  records, _ = scan_markdown_lines(text)
  result = []
  index = 0
  while index < len(records):
    record = records[index]
    if not record["structural"] or record["line"].rstrip("\r\n") != "**Comment**:":
      index += 1
      continue
    index += 1
    while index < len(records) and not records[index]["line"].strip():
      index += 1
    if index >= len(records) or not records[index]["fence_boundary"]:
      raise ValueError("projection integrity mismatch: comment fence missing")
    index += 1
    payload = []
    while index < len(records) and not records[index]["fence_boundary"]:
      payload.append(records[index]["line"])
      index += 1
    if index >= len(records):
      raise ValueError("unclosed Markdown fence")
    value = "".join(payload)
    if value.endswith("\r\n"):
      value = value[:-2]
    elif value.endswith(("\n", "\r")):
      value = value[:-1]
    end = records[index]["offset"] + len(records[index]["line"])
    result.append((value, end))
    index += 1
  return result


def extract_comment_payloads(text):
  return [payload for payload, _ in extract_comment_payload_records(text)]


def extract_comment_payload_map(text):
  result = {}
  for ordinal, block in extract_finding_blocks(text):
    result.setdefault(ordinal, []).extend(extract_comment_payloads(block))
  return result


def line_value_is_valid(line):
  match = re.fullmatch(r"\*\*Line\*\*: (.+)", line)
  if not match:
    return False
  value = match.group(1)
  if value == LINE_SENTINEL:
    return True
  numeric = re.fullmatch(r"(\d+)(?:-(\d+))?", value)
  if not numeric:
    return False
  start = int(numeric.group(1))
  end = int(numeric.group(2) or numeric.group(1))
  return start >= 1 and end >= start


def finding_block_metadata_is_valid(block):
  records, _ = scan_markdown_lines(block)
  index = 0

  def skip_blank_lines(position):
    while position < len(records) and not records[position]["line"].strip():
      position += 1
    return position

  if not records or not records[0]["structural"]:
    return False
  heading = records[0]["line"].rstrip("\r\n")
  if not re.fullmatch(r" {0,3}#### #?(?:F-)?(?:0[1-9]|[1-9]\d*)[a-z]?\s+.+", heading):
    return False
  index = skip_blank_lines(1)
  if index >= len(records) or not records[index]["structural"]:
    return False
  file_match = re.fullmatch(r"\*\*File\*\*: (.+)", records[index]["line"].rstrip("\r\n"))
  if not file_match or not file_match.group(1).strip():
    return False
  index += 1
  if index >= len(records) or not records[index]["structural"]:
    return False
  if not line_value_is_valid(records[index]["line"].rstrip("\r\n")):
    return False
  index = skip_blank_lines(index + 1)
  if index >= len(records) or not records[index]["structural"]:
    return False
  if records[index]["line"].rstrip("\r\n") != "**Comment**:":
    return False
  index = skip_blank_lines(index + 1)
  if index >= len(records) or not records[index]["fence_boundary"]:
    return False
  index += 1
  while index < len(records) and not records[index]["fence_boundary"]:
    index += 1
  if index >= len(records):
    return False
  index += 1
  return all(not record["line"].strip() for record in records[index:])


def extract_comment_block_contract(text):
  result = {}
  blocks, invalid = extract_finding_block_records(text)
  identities = {}
  for ordinal, suffix, block in blocks:
    payload_records = extract_comment_payload_records(block)
    payloads = [payload for payload, _ in payload_records]
    trailing_content = payload_records and block[payload_records[-1][1]:].strip()
    if (
      len(payloads) != 1
      or not payloads[0].strip()
      or trailing_content
      or not finding_block_metadata_is_valid(block)
    ):
      invalid.append(block)
    result.setdefault(ordinal, []).extend(payloads)
    identities.setdefault(ordinal, []).append((suffix, block))
  for entries in identities.values():
    suffixes = [suffix for suffix, _ in entries]
    expected = [chr(ord("a") + index) for index in range(len(entries))]
    if (len(entries) == 1 and suffixes != [""]) or (
      len(entries) > 1 and (len(entries) > 26 or suffixes != expected)
    ):
      invalid.extend(block for _, block in entries)
  return result, invalid


def extract_actionable_comment_payloads(text):
  no_ops = extract_noop_ordinals(text)
  result = []
  for ordinal, block in extract_finding_blocks(text):
    if ordinal not in no_ops:
      result.extend(extract_comment_payloads(block))
  return result


def extract_header_lines(text):
  preamble, _ = split_h2_sections(text)
  return [line.rstrip("\r\n") for _, line in outside_fence_lines(preamble)]


def projection_schema_version(text):
  header_lines = extract_header_lines(text)
  versions = [
    version
    for line, version in SCHEMA_LINES.items()
    for _ in range(header_lines.count(line))
  ]
  return versions[0] if len(versions) == 1 else None


def prior_review_continuity_is_valid(text):
  return continuity_state_is_valid(text, PRIOR_REVIEW_TITLE)


def extract_location_column(text, column):
  _, sections = split_h2_sections(text)
  matches = [section for title, section in sections if title == SET_LOCATIONS_TITLE]
  if len(matches) != 1:
    return []
  headers = list(LOCATION_HEADERS)
  if column not in headers:
    return []
  lines = [line for _, line in outside_fence_lines(matches[0])]
  header_indexes = [index for index, line in enumerate(lines) if table_cells(line) == headers]
  if len(header_indexes) != 1:
    return []
  result = []
  index = header_indexes[0]
  if index + 1 >= len(lines) or not is_table_separator(table_cells(lines[index + 1])):
    return []
  for line in lines[index + 2:]:
    cells = table_cells(line)
    if not cells:
      break
    if len(cells) != len(headers) or is_table_separator(cells):
      return []
    result.append(cells[headers.index(column)])
  return result


def extract_location_uids(text):
  return extract_location_column(text, "finding_uid")


def extract_location_group_ids(text):
  return extract_location_column(text, "group_id")


def set_locations_is_valid(text):
  _, sections = split_h2_sections(text)
  matches = [section for title, section in sections if title == SET_LOCATIONS_TITLE]
  if len(matches) != 1:
    return False
  headers = list(LOCATION_HEADERS)
  lines = [line for _, line in outside_fence_lines(matches[0])]
  header_indexes = [index for index, line in enumerate(lines) if table_cells(line) == headers]
  if len(header_indexes) != 1:
    return False
  index = header_indexes[0]
  if index + 1 >= len(lines) or not is_table_separator(table_cells(lines[index + 1])):
    return False
  rows = []
  for line in lines[index + 2:]:
    cells = table_cells(line)
    if not cells:
      break
    if len(cells) != len(headers) or is_table_separator(cells):
      return False
    rows.append(cells)
  if not rows:
    return ZERO_FINDING_MARKER in matches[0]
  columns = {name: position for position, name in enumerate(headers)}
  group_ids = [cells[columns["group_id"]] for cells in rows]
  uids = [cells[columns["finding_uid"]] for cells in rows]
  if not all(UID_PATTERN.fullmatch(group_id) for group_id in group_ids):
    return False
  if len(uids) != len(set(uids)) or not all(UID_PATTERN.fullmatch(uid) for uid in uids):
    return False
  if not all(
    cells[columns["target_identity"]] and cells[columns["file"]] and cells[columns["source"]]
    for cells in rows
  ):
    return False
  return all(
    cells[columns["line"]] == LINE_SENTINEL or re.fullmatch(r"[1-9]\d*", cells[columns["line"]])
    for cells in rows
  )


def continuity_table_matches(section, expected):
  lines = [line for _, line in outside_fence_lines(section)]
  headers = ["前輪 finding_uid", "問題", "狀態", "本輪證據"]
  header_indexes = [index for index, line in enumerate(lines) if table_cells(line) == headers]
  if len(header_indexes) != 1:
    return False
  header_index = header_indexes[0]
  if header_index + 1 >= len(lines) or not is_table_separator(table_cells(lines[header_index + 1])):
    return False
  rows = []
  for line in lines[header_index + 2:]:
    cells = table_cells(line)
    if not cells:
      break
    if len(cells) != len(headers) or is_table_separator(cells):
      return False
    rows.append(cells)
  uids = [cells[0] for cells in rows]
  statuses = [cells[2] for cells in rows]
  actual = {status: statuses.count(status) for status in expected}
  return (
    len(rows) == sum(expected.values())
    and len(uids) == len(set(uids))
    and all(UID_PATTERN.fullmatch(uid) for uid in uids)
    and all(cells[1] and cells[3] for cells in rows)
    and set(statuses) <= set(expected)
    and actual == expected
  )


def continuity_state_is_valid(text, title):
  state_lines = [
    line
    for line in extract_header_lines(text)
    if line.startswith("**Prior review continuity**:")
  ]
  sections = [
    section
    for section_title, section in split_h2_sections(text)[1]
    if section_title == title
  ]
  if len(state_lines) != 1 or len(sections) != 1:
    return False
  state = state_lines[0]
  section = sections[0]
  if state == "**Prior review continuity**: N-A (no prior audit)":
    return "N-A — no prior audit" in section
  skipped = re.fullmatch(r"\*\*Prior review continuity\*\*: SKIPPED \((.+)\)", state)
  if skipped:
    return f"SKIPPED — {skipped.group(1)}" in section
  checked = re.fullmatch(
    r"\*\*Prior review continuity\*\*: CHECKED \(fixed (\d+) / still-open (\d+) / stale (\d+)\)",
    state,
  )
  if not checked:
    return False
  expected = {
    "FIXED": int(checked.group(1)),
    "STILL_OPEN": int(checked.group(2)),
    "STALE": int(checked.group(3)),
  }
  if sum(expected.values()) == 0:
    return "CHECKED — 0 actionable findings" in section
  return continuity_table_matches(section, expected)


def group_source_checks(text):
  identities = group_identity_lines(text)
  header_lines = extract_header_lines(text)
  required_state_counts = (
    sum(line.startswith("**Review set identity**:") for line in header_lines),
    sum(line.startswith("**Target versions**:") for line in header_lines),
    sum(line.startswith("**Prior review continuity**:") for line in header_lines),
  )
  targets = [entry.split("=", 1)[0] for entry in identities[1]] if identities else []
  targets_line = [
    line[len("**Review set targets**: "):]
    for line in header_lines if line.startswith("**Review set targets**: ")
  ]
  declared = sorted(part.strip() for part in targets_line[0].split(" · ")) if len(targets_line) == 1 else []
  canonical_identity = None
  if declared:
    joined = "\n".join(declared)
    canonical_identity = "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()
  return {
    "review-set-identity": identities is not None and bool(targets),
    "target-version-coverage": bool(targets) and len(targets) == len(set(targets)),
    "targets-line-unique": len(targets_line) == 1,
    "targets-match-versions": bool(declared) and sorted(targets) == declared,
    "identity-derived": bool(canonical_identity) and identities is not None
    and identities[0] == canonical_identity,
    "set-locations": set_locations_is_valid(text),
    "required-state": required_state_counts == (1, 1, 1),
    "prior-review-continuity": continuity_state_is_valid(text, PRIOR_REVIEW_TITLE),
  }


def preamble_structure_is_valid(text, group=False, expected_stable_id=None):
  preamble, _ = split_h2_sections(text)
  records, unclosed_fence = scan_markdown_lines(preamble)
  if unclosed_fence is not None or any(not record["structural"] for record in records):
    return False
  nonblank = [record["line"].rstrip("\r\n") for record in records if record["line"].strip()]
  if not nonblank:
    return False
  if group:
    title = re.fullmatch(rf"# Review set ({re.escape(expected_stable_id)}) Code Review 比較報告", nonblank[0]) \
      if expected_stable_id else \
      re.fullmatch(r"# Review set [0-9a-f]{12} Code Review 比較報告", nonblank[0])
  else:
    title = re.fullmatch(r"# PR #\d+ Code Review(?: 比較報告)?(?: · SHA [0-9a-f]+)?", nonblank[0])
  if not title:
    return False
  metadata = nonblank[1:]
  if metadata and metadata[-1] == "---":
    metadata = metadata[:-1]
  return bool(metadata) and all(PREAMBLE_METADATA_PATTERN.fullmatch(line) for line in metadata)


def group_identity_lines(text):
  header_lines = extract_header_lines(text)
  identities = [
    match.group(1)
    for line in header_lines
    for match in [SET_IDENTITY_PATTERN.fullmatch(line) or SET_IDENTITY_PATTERN.match(line)]
    if match
  ]
  versions = [
    match.group(1)
    for line in header_lines
    if line.startswith("**Target versions**:")
    for match in [TARGET_VERSIONS_PATTERN.fullmatch(line)]
    if match
  ]
  if len(identities) != 1 or len(versions) != 1:
    return None
  entries = versions[0].split(";")
  if any(not VERSION_ENTRY_PATTERN.fullmatch(entry.strip()) for entry in entries) or not entries:
    return None
  return identities[0], [entry.strip() for entry in entries]


def validate_source_contract(text, require_schema=False, allow_generation=True):
  header_lines = extract_header_lines(text)
  schema_count = sum(header_lines.count(line) for line in SCHEMA_LINES)
  schema_version = projection_schema_version(text)
  generation_count = sum(
    line.rstrip("\r\n").startswith("**Report generation**:")
    for _, line in outside_fence_lines(text)
  )
  if require_schema and schema_count != 1:
    raise ValueError("source report contract mismatch: projection-schema")
  if schema_count == 0:
    return
  inventory, invalid_rows, canonical_tables = extract_finding_rows(text)
  order, actions, priorities = extract_finding_actions(text)
  uid_map, uid_actions, no_inline, uid_order, invalid_uid_records = extract_strict_uid_records(text)
  payload_map, invalid_comment_blocks = extract_comment_block_contract(text)
  block_records, _ = extract_finding_block_records(text)
  block_group_order = []
  for ordinal, _, _ in block_records:
    if not block_group_order or block_group_order[-1] != ordinal:
      block_group_order.append(ordinal)
  unique_inventory = set(inventory)
  uid_values = [uid_map[ordinal][0] for ordinal in unique_inventory if len(uid_map.get(ordinal, [])) == 1]
  required_state_counts = (
    sum(line.startswith("**覆蓋 (ENH-A)**:") for line in header_lines),
    sum(line.startswith("**Formal spec traceability (2.65)**:") for line in header_lines),
    sum(line.startswith("**Prior review continuity**:") for line in header_lines),
  )
  expected_state_counts = (1, 1, 1) if schema_version == 2 else (1, 1, 0)
  checks = {
    "projection-schema": schema_count == 1 and schema_version is not None,
    "generation-cardinality": generation_count <= 1 if allow_generation else generation_count == 0,
    "schema-family": schema_version != 3,
    "html-hidden-heading": not unclosed_html_block_hides_heading(text),
    "preamble-structure": preamble_structure_is_valid(text),
    "finding-table": canonical_tables == 1,
    "finding-summary-structure": finding_summary_structure_is_valid(text),
    "finding-inventory": len(inventory) == len(unique_inventory),
    "finding-sequence": inventory == list(range(1, len(inventory) + 1)),
    "finding-rows": not invalid_rows,
    "action-order": order == inventory,
    "priority-order": priorities == sorted(priorities),
    "action-coverage": set(actions) == unique_inventory,
    "action-cardinality": all(len(actions.get(ordinal, [])) == 1 for ordinal in unique_inventory),
    "uid-coverage": set(uid_map) == unique_inventory,
    "uid-order": uid_order == inventory,
    "uid-records": not invalid_uid_records,
    "uid-cardinality": all(len(uid_map.get(ordinal, [])) == 1 for ordinal in unique_inventory),
    "uid-uniqueness": len(uid_values) == len(set(uid_values)),
    "uid-action": all(
      len(uid_actions.get(ordinal, [])) == 1
      and len(actions.get(ordinal, [])) == 1
      and uid_actions[ordinal][0] == actions[ordinal][0]
      for ordinal in unique_inventory
    ),
    "inline-exemption": no_inline <= unique_inventory,
    "inline-exemption-action": all(actions.get(ordinal) == ["no-op"] for ordinal in no_inline),
    "comment-coverage": set(payload_map) == unique_inventory - no_inline,
    "comment-cardinality": all(
      len(payload_map.get(ordinal, [])) >= 1
      for ordinal in unique_inventory - no_inline
    ),
    "comment-preamble": inline_comment_preambles_are_empty(text),
    "comment-order": (
      block_group_order == [ordinal for ordinal in inventory if ordinal not in no_inline]
      and len(block_group_order) == len(set(block_group_order))
    ),
    "comment-blocks": not invalid_comment_blocks,
    "required-state": required_state_counts == expected_state_counts,
    "prior-review-continuity": schema_version != 2 or prior_review_continuity_is_valid(text),
  }
  failed = [name for name, passed in checks.items() if not passed]
  if failed:
    raise ValueError(f"source report contract mismatch: {', '.join(failed)}")


def validate_group_source_contract(text, require_schema=False, allow_generation=True,
                                   expected_stable_id=None):
  header_lines = extract_header_lines(text)
  schema_count = sum(header_lines.count(line) for line in SCHEMA_LINES)
  schema_version = projection_schema_version(text)
  generation_count = sum(
    line.rstrip("\r\n").startswith("**Report generation**:")
    for _, line in outside_fence_lines(text)
  )
  if require_schema and schema_count != 1:
    raise ValueError("source report contract mismatch: projection-schema")
  if schema_count == 0:
    return
  if expected_stable_id is not None:
    title_match = re.match(r"(?m)^# Review set ([0-9a-f]{12}) Code Review 比較報告$", text)
    if not title_match or title_match.group(1) != expected_stable_id:
      raise ValueError("source report contract mismatch: title-stable-id")
    identity = group_identity_lines(text)
    if identity is not None and identity[0][7:19] != expected_stable_id:
      raise ValueError("source report contract mismatch: identity-stable-id")
  inventory, invalid_rows, canonical_tables = extract_finding_rows(text)
  order, actions, priorities = extract_finding_actions(text)
  uid_map, uid_actions, no_inline, uid_order, invalid_uid_records = extract_strict_uid_records(text)
  payload_map, invalid_comment_blocks = extract_comment_block_contract(text)
  block_records, _ = extract_finding_block_records(text)
  block_group_order = []
  for ordinal, _, _ in block_records:
    if not block_group_order or block_group_order[-1] != ordinal:
      block_group_order.append(ordinal)
  unique_inventory = set(inventory)
  uid_values = [uid_map[ordinal][0] for ordinal in unique_inventory if len(uid_map.get(ordinal, [])) == 1]
  location_group_ids = extract_location_group_ids(text)
  checks = {
    "projection-schema": schema_count == 1 and schema_version == 3,
    "generation-cardinality": generation_count <= 1 if allow_generation else generation_count == 0,
    "html-hidden-heading": not unclosed_html_block_hides_heading(text),
    "preamble-structure": preamble_structure_is_valid(text, group=True, expected_stable_id=expected_stable_id),
    "finding-table": canonical_tables == 1,
    "finding-summary-structure": finding_summary_structure_is_valid(text),
    "finding-inventory": len(inventory) == len(unique_inventory),
    "finding-sequence": inventory == list(range(1, len(inventory) + 1)),
    "finding-rows": not invalid_rows,
    "action-order": order == inventory,
    "priority-order": priorities == sorted(priorities),
    "action-coverage": set(actions) == unique_inventory,
    "action-cardinality": all(len(actions.get(ordinal, [])) == 1 for ordinal in unique_inventory),
    "uid-coverage": set(uid_map) == unique_inventory,
    "uid-order": uid_order == inventory,
    "uid-records": not invalid_uid_records,
    "uid-cardinality": all(len(uid_map.get(ordinal, [])) == 1 for ordinal in unique_inventory),
    "uid-uniqueness": len(uid_values) == len(set(uid_values)),
    "uid-action": all(
      len(uid_actions.get(ordinal, [])) == 1
      and len(actions.get(ordinal, [])) == 1
      and uid_actions[ordinal][0] == actions[ordinal][0]
      for ordinal in unique_inventory
    ),
    "inline-exemption": no_inline <= unique_inventory,
    "inline-exemption-action": all(actions.get(ordinal) == ["no-op"] for ordinal in no_inline),
    "comment-coverage": set(payload_map) == unique_inventory - no_inline,
    "comment-cardinality": all(
      len(payload_map.get(ordinal, [])) >= 1
      for ordinal in unique_inventory - no_inline
    ),
    "comment-preamble": inline_comment_preambles_are_empty(text),
    "comment-order": (
      block_group_order == [ordinal for ordinal in inventory if ordinal not in no_inline]
      and len(block_group_order) == len(set(block_group_order))
    ),
    "comment-blocks": not invalid_comment_blocks,
    "location-group-coverage": set(uid_values) == set(location_group_ids),
  }
  checks.update(group_source_checks(text))
  failed = [name for name, passed in checks.items() if not passed]
  if failed:
    raise ValueError(f"source report contract mismatch: {', '.join(failed)}")


def extract_state_lines(text):
  return [line for line in extract_header_lines(text) if line.startswith(STATE_PREFIXES)]


def validate_projection(source, projected):
  assert_balanced_fences(source)
  assert_balanced_fences(projected)
  source_preamble, source_sections = split_h2_sections(source)
  projected_preamble, projected_sections = split_h2_sections(projected)
  source_summaries = [section for title, section in source_sections if title == "發現總覽"]
  projected_summaries = [section for title, section in projected_sections if title == "發現總覽"]
  source_inline = sum(title in INLINE_TITLES for title, _ in source_sections)
  source_inline += sum(
    title in INLINE_TITLES
    for section in source_summaries
    for _, _, title in find_headings(section, 3)
  )
  projected_inline = sum(title in INLINE_TITLES for title, _ in projected_sections)
  projected_inline += sum(
    title in INLINE_TITLES
    for section in projected_summaries
    for _, _, title in find_headings(section, 3)
  )
  source_uids = extract_finding_uids(source)
  projected_titles = [title for title, _ in projected_sections]
  evidence_titles = [title for title in projected_titles if title.startswith("[完整證據副檔](")]
  checks = {
    "source-summary": len(source_summaries) == 1,
    "projected-section-allowlist": all(title.startswith("[完整證據副檔](") or should_keep(title) for title in projected_titles),
    "projected-evidence-section": len(evidence_titles) == 1,
    "projected-summary": len(projected_summaries) == 1,
    "source-inline-comments": source_inline == 1,
    "projected-inline-comments": projected_inline == 1,
    "preamble": normalize_structure(source_preamble) == normalize_structure(projected_preamble),
    "finding-summary": normalize_structure(finding_summary_scope(source)) == normalize_structure(finding_summary_scope(projected)),
    "finding-uids": all(uid in projected for uid in source_uids),
    "comment-payloads": extract_actionable_comment_payloads(source) == extract_actionable_comment_payloads(projected),
    "header-state": extract_state_lines(source) == extract_state_lines(projected),
  }
  failed = [name for name, passed in checks.items() if not passed]
  if failed:
    raise ValueError(f"projection integrity mismatch: {', '.join(failed)}")


def validate_group_projection(source, projected):
  assert_balanced_fences(source)
  assert_balanced_fences(projected)
  source_preamble, source_sections = split_h2_sections(source)
  projected_preamble, projected_sections = split_h2_sections(projected)
  source_summaries = [section for title, section in source_sections if title == "發現總覽"]
  projected_summaries = [section for title, section in projected_sections if title == "發現總覽"]
  source_locations = [section for title, section in source_sections if title == SET_LOCATIONS_TITLE]
  projected_locations = [section for title, section in projected_sections if title == SET_LOCATIONS_TITLE]
  source_inline = sum(title in INLINE_TITLES for title, _ in source_sections)
  source_inline += sum(
    title in INLINE_TITLES
    for section in source_summaries
    for _, _, title in find_headings(section, 3)
  )
  projected_inline = sum(title in INLINE_TITLES for title, _ in projected_sections)
  projected_inline += sum(
    title in INLINE_TITLES
    for section in projected_summaries
    for _, _, title in find_headings(section, 3)
  )
  source_uids = extract_finding_uids(source)
  projected_titles = [title for title, _ in projected_sections]
  evidence_titles = [title for title in projected_titles if title.startswith("[完整證據副檔](")]
  checks = {
    "projected-section-allowlist": all(
      title.startswith("[完整證據副檔](") or should_keep(title, group=True)
      for title in projected_titles
    ),
    "projected-evidence-section": len(evidence_titles) == 1,
    "projected-summary": len(projected_summaries) == 1,
    "projected-locations": len(projected_locations) == 1,
    "source-inline-comments": source_inline == 1,
    "projected-inline-comments": projected_inline == 1,
    "preamble": normalize_structure(source_preamble) == normalize_structure(projected_preamble),
    "finding-summary": normalize_structure(finding_summary_scope(source)) == normalize_structure(finding_summary_scope(projected)),
    "locations": normalize_structure(source_locations[0]) == normalize_structure(projected_locations[0])
    if source_locations and projected_locations else False,
    "finding-uids": all(uid in projected for uid in source_uids),
    "comment-payloads": extract_actionable_comment_payloads(source) == extract_actionable_comment_payloads(projected),
    "header-state": extract_state_lines(source) == extract_state_lines(projected),
  }
  failed = [name for name, passed in checks.items() if not passed]
  if failed:
    raise ValueError(f"projection integrity mismatch: {', '.join(failed)}")


def should_keep(title, group=False):
  return title in (GROUP_KEEP_TITLES if group else KEEP_TITLES)


def add_generation_binding(source, generation):
  preamble, sections = split_h2_sections(source)
  line = f"**Report generation**: sha256:{generation}"
  preamble_lines = preamble.rstrip().splitlines()
  insertion = len(preamble_lines)
  if preamble_lines and preamble_lines[-1] == "---":
    insertion -= 1
    preamble_lines[insertion:insertion] = [line, ""]
  else:
    preamble_lines.insert(insertion, line)
  bound_preamble = "\n".join(preamble_lines) + "\n\n"
  return bound_preamble + "".join(section for _, section in sections)


def project_report(source, audit_name, require_schema=False):
  assert_balanced_fences(source)
  validate_source_contract(source, require_schema=require_schema)
  preamble, sections = split_h2_sections(source)
  uids = extract_finding_uids(source)
  no_ops = extract_noop_ordinals(source)
  audit_section = [f"## [完整證據副檔]({audit_name})"]
  if uids:
    links = " · ".join(f"[{uid}]({audit_name}#發現總覽)" for uid in uids)
    audit_section.extend(["", "### finding_uid 索引", "", links])
  kept = []
  for title, section in sections:
    if not should_keep(title):
      continue
    if title == "發現總覽" or title in INLINE_TITLES:
      section = filter_noop_inline_blocks(
        strip_inline_comment_preamble(trim_after_inline_comments(section)),
        no_ops,
      )
    kept.append(section.rstrip())
  parts = [preamble.rstrip(), "\n".join(audit_section), *kept]
  joined = "\n\n".join(part for part in parts if part).rstrip()
  projected = remove_external_blank_lines(joined) + "\n"
  validate_projection(source, projected)
  return projected


def project_group_report(source, audit_name, require_schema=False):
  assert_balanced_fences(source)
  validate_group_source_contract(source, require_schema=require_schema)
  preamble, sections = split_h2_sections(source)
  uids = extract_finding_uids(source)
  no_ops = extract_noop_ordinals(source)
  audit_section = [f"## [完整證據副檔]({audit_name})"]
  if uids:
    links = " · ".join(f"[{uid}]({audit_name}#發現總覽)" for uid in uids)
    audit_section.extend(["", LOCATION_UID_HEADING, "", links])
  kept = []
  for title, section in sections:
    if not should_keep(title, group=True):
      continue
    if title == "發現總覽" or title in INLINE_TITLES:
      section = filter_noop_inline_blocks(
        strip_inline_comment_preamble(trim_after_inline_comments(section)),
        no_ops,
      )
    kept.append(section.rstrip())
  parts = [preamble.rstrip(), "\n".join(audit_section), *kept]
  joined = "\n\n".join(part for part in parts if part).rstrip()
  projected = remove_external_blank_lines(joined) + "\n"
  validate_group_projection(source, projected)
  return projected


def write_main_report(path, content, before_replace=None):
  descriptor, temp_name = tempfile.mkstemp(
    dir=path.parent,
    prefix=f".{path.name}.",
    suffix=".tmp",
  )
  temp_path = Path(temp_name)
  try:
    with os.fdopen(descriptor, "w", newline="") as temp_file:
      temp_file.write(content)
    if before_replace:
      before_replace()
    temp_path.replace(path)
  except Exception:
    temp_path.unlink(missing_ok=True)
    raise


def write_report_bytes(path, content):
  descriptor, temp_name = tempfile.mkstemp(
    dir=path.parent,
    prefix=f".{path.name}.",
    suffix=".tmp",
  )
  temp_path = Path(temp_name)
  try:
    with os.fdopen(descriptor, "wb") as temp_file:
      temp_file.write(content)
    temp_path.replace(path)
  except Exception:
    temp_path.unlink(missing_ok=True)
    raise


def read_report(path):
  with path.open(newline="") as source_file:
    return source_file.read()


def paths_alias(left, right):
  if left.resolve(strict=False) == right.resolve(strict=False):
    return True
  try:
    return left.samefile(right)
  except FileNotFoundError:
    return False


def absolute_path(path):
  return Path(os.path.abspath(path))


def canonical_report_identity(path):
  normalized = path.resolve(strict=False)
  single_error = None
  try:
    relative = normalized.relative_to(REPORT_ROOT)
  except ValueError as error:
    raise ValueError("report paths must be under the canonical report root") from error
  if len(relative.parts) == 2 and relative.parts[0] == GROUP_ROOT_NAME:
    for kind, pattern in GROUP_PATTERNS:
      match = pattern.fullmatch(path.name)
      if match:
        return "group", kind, match.group(1)
    raise ValueError("report paths must use canonical set report names")
  if len(relative.parts) != 2:
    raise ValueError("report paths must use exactly one repository directory")
  for kind, pattern in REPORT_PATTERNS:
    match = pattern.fullmatch(path.name)
    if match:
      return "single", kind, int(match.group(1))
  raise ValueError("report paths must use canonical PR report names")


def assert_canonical_report_paths(paths):
  identities = [canonical_report_identity(path) for path in paths]
  families = {family for family, _, _ in identities}
  if len(families) != 1:
    raise ValueError("draft, audit, and main paths must belong to one report family")
  if [kind for _, kind, _ in identities] != ["draft", "audit", "main"]:
    raise ValueError("draft, audit, and main paths must be the canonical three-file set")
  keys = {key for _, _, key in identities}
  if len(keys) != 1:
    raise ValueError("draft, audit, and main paths must use one report identity")
  return families.pop()


def assert_distinct_report_paths(paths, lock_path):
  draft_path, audit_path, main_path = paths
  parents = {path.parent.resolve() for path in (*paths, lock_path)}
  if len(parents) != 1:
    raise ValueError("draft, audit, main, and lock must share one directory")
  main_parent = main_path.parent.resolve(strict=False)
  if any(path.parent.resolve(strict=False) != main_parent for path in (*paths, lock_path)):
    raise ValueError("report paths must use the same canonical parent path")
  if absolute_path(audit_path) != absolute_path(main_path.with_suffix(".audit.md")):
    raise ValueError("audit path must be the paired main .audit.md path")
  all_paths = (*paths, lock_path)
  for path in all_paths:
    if path.is_symlink():
      raise ValueError("report paths must not be symbolic links")
    if path.exists() and path.stat().st_nlink != 1:
      raise ValueError("report paths must not have multiple hard links")
  for index, left in enumerate(all_paths):
    for right in all_paths[index + 1:]:
      if paths_alias(left, right):
        raise ValueError("draft, audit, and main paths must be distinct")


def claim_draft(draft_path):
  claim_directory = Path(tempfile.mkdtemp(dir=draft_path.parent, prefix=f".{draft_path.name}.claim."))
  claimed_path = claim_directory / "draft.md"
  try:
    draft_path.replace(claimed_path)
  except Exception:
    claim_directory.rmdir()
    raise
  return claim_directory, claimed_path


def assert_safe_claimed_path(path):
  claim_directory = path.parent
  if claim_directory.is_symlink():
    raise ValueError("claim directory must not be a symbolic link")
  directory_metadata = claim_directory.stat()
  if directory_metadata.st_uid != os.getuid() or not stat.S_ISDIR(directory_metadata.st_mode):
    raise ValueError("claim directory must be owned by the current user")
  if path.is_symlink():
    raise ValueError("claimed draft must not be symbolic links")
  metadata = path.stat()
  if (
    metadata.st_uid != os.getuid()
    or not stat.S_ISREG(metadata.st_mode)
    or metadata.st_nlink != 1
  ):
    raise ValueError("claimed draft must be a single-link regular file")


def recover_claimed_draft(draft_path):
  claims = sorted(draft_path.parent.glob(f".{draft_path.name}.claim.*/draft.md"))
  if not claims:
    return
  if draft_path.exists() or draft_path.is_symlink() or len(claims) != 1:
    raise ValueError("ambiguous interrupted draft claim")
  claimed_path = claims[0]
  claim_directory = claimed_path.parent
  assert_safe_claimed_path(claimed_path)
  claimed_path.replace(draft_path)
  claim_directory.rmdir()


def validate_bound_contract(source, group, audit_path=None, expected_stable_id=None):
  if group:
    validate_group_source_contract(source, require_schema=True, allow_generation=False,
                                   expected_stable_id=expected_stable_id)
    if audit_path is not None and audit_path.exists():
      existing = read_report(audit_path)
      validate_group_source_contract(existing, require_schema=True, allow_generation=True,
                                     expected_stable_id=expected_stable_id)
      draft_identity = group_identity_lines(source)
      audit_identity = group_identity_lines(existing)
      if draft_identity is None or audit_identity is None:
        raise ValueError("source report contract mismatch: set-identity-unreadable")
      # 只比 set 身分，不比版本清單：同一組 PR 推了新 commit 之後重審，版本本來就會變，
      # 那正是重審的理由。比版本會讓合法的新一輪永遠發不出去。覆蓋前先備份舊報告（下方）。
      if draft_identity[0] != audit_identity[0]:
        raise ValueError("source report contract mismatch: set-identity-mismatch")
  else:
    validate_source_contract(source, require_schema=True, allow_generation=False)


def project_bound_report(source, audit_name, group):
  if group:
    return project_group_report(source, audit_name, require_schema=True)
  return project_report(source, audit_name, require_schema=True)


def archive_previous_group_reports(audit_path, main_path):
  """覆蓋前把上一輪的群組報告改名成帶 UTC 時間戳的備份，留在同一個目錄。

  檔名維持固定（一組一份、可被下一輪覆蓋），所以下一輪找前輪報告的路徑不用改；
  歷史則以 <name>.<YYYYmmddTHHMMSSZ>.bak.md 保留。備份失敗就讓發布失敗，
  不靜默把舊報告蓋掉。
  """
  stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
  for path in (audit_path, main_path):
    if not path.exists():
      continue
    backup = path.with_name(f"{path.name}.{stamp}.bak.md")
    if backup.exists():
      raise ValueError("group report archive collision: {}".format(backup.name))
    path.replace(backup)


def publish_report_pair(draft_path, audit_path, main_path):
  report_paths = (draft_path, audit_path, main_path)
  family = assert_canonical_report_paths(report_paths)
  group = family == "group"
  lock_path = main_path.with_name(f".{main_path.name}.lock")
  assert_distinct_report_paths(report_paths, lock_path)
  flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
  lock_descriptor = os.open(lock_path, flags, 0o600)
  with os.fdopen(lock_descriptor, "a") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    recover_claimed_draft(draft_path)
    claim_directory, claimed_path = claim_draft(draft_path)
    try:
      source = read_report(claimed_path)
      group_key = None
      if group:
        group_key = assert_canonical_report_paths(report_paths)
        group_key = [key for family, kind, key in
                     [canonical_report_identity(path) for path in report_paths]][0]
      validate_bound_contract(source, group, audit_path, expected_stable_id=group_key)
      generation = hashlib.sha256(source.encode()).hexdigest()
      bound_source = add_generation_binding(source, generation)
      projected = project_bound_report(bound_source, audit_path.name, group)
      generation_line = f"**Report generation**: sha256:{generation}"
      if bound_source.count(generation_line) != 1 or projected.count(generation_line) != 1:
        raise ValueError("projection integrity mismatch: report-generation")
      if group:
        archive_previous_group_reports(audit_path, main_path)
      audit_existed = audit_path.exists()
      audit_backup = audit_path.read_bytes() if audit_existed else None
      audit_mode = audit_path.stat().st_mode & 0o777 if audit_existed else None
      write_main_report(audit_path, bound_source)
      try:
        write_main_report(main_path, projected)
      except Exception:
        if audit_existed:
          write_report_bytes(audit_path, audit_backup)
          audit_path.chmod(audit_mode)
        else:
          audit_path.unlink(missing_ok=True)
        raise
    except Exception:
      if not draft_path.exists() and not draft_path.is_symlink():
        claimed_path.replace(draft_path)
        claim_directory.rmdir()
      raise
    claimed_path.unlink()
    claim_directory.rmdir()


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("paths", nargs="+", type=Path)
  args = parser.parse_args()
  if len(args.paths) == 3:
    publish_report_pair(*args.paths)
    return
  parser.error("expected DRAFT AUDIT MAIN")


if __name__ == "__main__":
  main()
