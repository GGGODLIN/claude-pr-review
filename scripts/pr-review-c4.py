#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator


CLASSIFICATIONS = (
  "full_match",
  "partial_match",
  "mismatch",
  "missing_in_code",
  "code_stronger_than_spec",
  "code_weaker_than_spec",
  "undocumented_behavior",
  "AMBIGUOUS",
)
CONTRACT_TYPES = (
  "NORMATIVE_KEYWORD",
  "INVARIANT",
  "FORMULA",
  "STATE_TRANSITION",
  "ERROR_CONTRACT",
)
FINDING_CLASSIFICATIONS = {
  "partial_match",
  "mismatch",
  "missing_in_code",
  "code_weaker_than_spec",
  "undocumented_behavior",
}


def strict_object(properties, required):
  return {
    "type": "object",
    "properties": properties,
    "required": required,
    "additionalProperties": False,
  }


ANCHOR_SCHEMA = strict_object(
  {
    "path": {"type": "string", "minLength": 1},
    "line_start": {"type": "integer", "minimum": 1},
    "line_end": {"type": "integer", "minimum": 1},
  },
  ["path", "line_start", "line_end"],
)
TRACE_ANCHOR_SCHEMA = strict_object(
  {
    "binding_id": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
    "side": {"enum": ["head", "base"]},
    **ANCHOR_SCHEMA["properties"],
    "quote": {"type": "string", "minLength": 1},
    "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
  },
  [
    "binding_id",
    "side",
    "path",
    "line_start",
    "line_end",
    "quote",
    "content_hash",
  ],
)
FLOW_HINT_SCHEMA = strict_object(
  {
    "actor_or_entity": {"type": "string", "minLength": 1},
    "operation_or_event": {"type": "string", "minLength": 1},
    "precondition": {"type": "string", "minLength": 1},
    "observable_result": {"type": "string", "minLength": 1},
  },
  ["actor_or_entity", "operation_or_event", "precondition", "observable_result"],
)
AUTHORITY_CANDIDATE_SCHEMA = strict_object(
  {
    "clause_id": {"type": "string", "pattern": "^C4-[0-9]{3,}$"},
    "contract_type": {"enum": list(CONTRACT_TYPES)},
    "spec_path": {"type": "string", "minLength": 1},
    "line_start": {"type": "integer", "minimum": 1},
    "line_end": {"type": "integer", "minimum": 1},
    "exact_quote": {"type": "string", "minLength": 1},
    "source_excerpt": {"type": "string", "minLength": 1},
    "changed_flow_hint": FLOW_HINT_SCHEMA,
  },
  [
    "clause_id",
    "contract_type",
    "spec_path",
    "line_start",
    "line_end",
    "exact_quote",
    "source_excerpt",
    "changed_flow_hint",
  ],
)
PACKET_CLAUSE_SCHEMA = strict_object(
  {
    **AUTHORITY_CANDIDATE_SCHEMA["properties"],
    "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "authority_alias_path": {"type": "string", "minLength": 1},
  },
  [*AUTHORITY_CANDIDATE_SCHEMA["required"], "source_hash"],
)
HEAD_BINDING_SCHEMA = strict_object(
  {
    "binding_id": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
    "side": {"const": "head"},
    **ANCHOR_SCHEMA["properties"],
    "quote": {"type": "string", "minLength": 1},
    "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
  },
  [
    "binding_id",
    "side",
    "path",
    "line_start",
    "line_end",
    "quote",
    "content_hash",
  ],
)
BASE_BINDING_SCHEMA = strict_object(
  {
    **HEAD_BINDING_SCHEMA["properties"],
    "side": {"const": "base"},
    "provenance_base_tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
    "old_path": {"type": "string", "minLength": 1},
    "blob_oid": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
    "blob_size_bytes": {"type": "integer", "minimum": 0, "maximum": 120000},
  },
  [
    *HEAD_BINDING_SCHEMA["required"],
    "provenance_base_tree",
    "old_path",
    "blob_oid",
    "blob_size_bytes",
  ],
)
EVIDENCE_BINDING_SCHEMA = {"oneOf": [HEAD_BINDING_SCHEMA, BASE_BINDING_SCHEMA]}
CHANGED_FILE_SCHEMA = strict_object(
  {
    "path": {"type": "string", "minLength": 1},
    "provenance": {"enum": ["authored", "inherited"]},
  },
  ["path", "provenance"],
)
CLAUSE_TRACE_SCHEMA = strict_object(
  {
    "clause_id": {"type": "string", "pattern": "^C4-[0-9]{3,}$"},
    "authored_binding_ids": {
      "type": "array",
      "minItems": 1,
      "items": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
      "uniqueItems": True,
    },
    "connected_guard_binding_ids": {
      "type": "array",
      "items": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
      "uniqueItems": True,
    },
  },
  ["clause_id", "authored_binding_ids", "connected_guard_binding_ids"],
)
TRACE_CONTEXT_SCHEMA = strict_object(
  {
    "authored_diff_binding_ids": {
      "type": "array",
      "minItems": 1,
      "items": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
      "uniqueItems": True,
    },
    "connected_guard_binding_ids": {
      "type": "array",
      "items": {"type": "string", "pattern": "^C4-BIND-[0-9]{3,}$"},
      "uniqueItems": True,
    },
    "connected_guard_status": {"enum": ["SUPPLIED", "NONE_REQUIRED"]},
    "clause_traces": {
      "type": "array",
      "minItems": 1,
      "items": CLAUSE_TRACE_SCHEMA,
    },
  },
  [
    "authored_diff_binding_ids",
    "connected_guard_binding_ids",
    "connected_guard_status",
    "clause_traces",
  ],
)
PREDISPATCH_VERIFICATION_SCHEMA = strict_object(
  {
    "canonical_spec_bound": {"const": True},
    "head_bindings_verified": {"const": True},
    "base_bindings_status": {"enum": ["VERIFIED", "NOT_APPLICABLE"]},
    "hunk_provenance_verified": {"const": True},
  },
  [
    "canonical_spec_bound",
    "head_bindings_verified",
    "base_bindings_status",
    "hunk_provenance_verified",
  ],
)
PACKET_SCHEMA = strict_object(
  {
    "dispatch_id": {"type": "string", "minLength": 8},
    "clauses": {
      "type": "array",
      "minItems": 1,
      "maxItems": 50,
      "items": PACKET_CLAUSE_SCHEMA,
    },
    "spec_files": {
      "type": "array",
      "minItems": 1,
      "items": strict_object(
        {"path": {"type": "string", "minLength": 1}},
        ["path"],
      ),
    },
    "changed_files": {
      "type": "array",
      "minItems": 1,
      "items": CHANGED_FILE_SCHEMA,
    },
    "evidence_bindings": {
      "type": "array",
      "minItems": 1,
      "items": EVIDENCE_BINDING_SCHEMA,
    },
    "trace_context": TRACE_CONTEXT_SCHEMA,
    "predispatch_verification": PREDISPATCH_VERIFICATION_SCHEMA,
  },
  [
    "dispatch_id",
    "clauses",
    "spec_files",
    "changed_files",
    "evidence_bindings",
    "trace_context",
    "predispatch_verification",
  ],
)
CONTRACT_ACCOUNTING_SCHEMA = strict_object(
  {
    "clause_id": {"type": "string", "pattern": "^C4-[0-9]{3,}$"},
    "contract_type": {"enum": list(CONTRACT_TYPES)},
    "classification": {"enum": list(CLASSIFICATIONS)},
    "reason_code": {"type": "string", "minLength": 1},
    "normative_quote": {"type": "string", "minLength": 1},
    "spec_anchor": ANCHOR_SCHEMA,
    "finding_id": {
      "anyOf": [
        {"type": "string", "pattern": "^SPEC-[0-9]{3,}$"},
        {"type": "null"},
      ]
    },
  },
  [
    "clause_id",
    "contract_type",
    "classification",
    "reason_code",
    "normative_quote",
    "spec_anchor",
    "finding_id",
  ],
)
SAME_FLOW_SCHEMA = strict_object(
  {
    "actor_or_entity": {"type": "string", "minLength": 1},
    "operation_or_event": {"type": "string", "minLength": 1},
    "precondition": {"type": "string", "minLength": 1},
    "observable_result": {"type": "string", "minLength": 1},
    "mapping_evidence": {"type": "string", "minLength": 1},
  },
  [
    "actor_or_entity",
    "operation_or_event",
    "precondition",
    "observable_result",
    "mapping_evidence",
  ],
)
BEHAVIORAL_EVIDENCE_SCHEMA = strict_object(
  {
    "kind": {"type": "string", "minLength": 1},
    "given": {"type": "string", "minLength": 1},
    "when": {"type": "string", "minLength": 1},
    "actual": {"type": "string", "minLength": 1},
    "required": {"type": "string", "minLength": 1},
    "observable_delta": {"type": "string", "minLength": 1},
  },
  ["kind", "given", "when", "actual", "required", "observable_delta"],
)
FINDING_SCHEMA = strict_object(
  {
    "id": {"type": "string", "pattern": "^SPEC-[0-9]{3,}$"},
    "reviewer": {"const": "spec-compliance-reviewer"},
    "severity": {"enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
    "classification": {"enum": list(CLASSIFICATIONS)},
    "contract_type": {"enum": list(CONTRACT_TYPES)},
    "title": {"type": "string", "minLength": 1},
    "normative_quote": {"type": "string", "minLength": 1},
    "spec_anchor": ANCHOR_SCHEMA,
    "same_flow": SAME_FLOW_SCHEMA,
    "file": {"type": "string", "minLength": 1},
    "line_start": {"type": "integer", "minimum": 1},
    "line_end": {"type": "integer", "minimum": 1},
    "anchor": {"type": "string", "minLength": 1},
    "trace_anchors": {
      "type": "array",
      "minItems": 1,
      "items": TRACE_ANCHOR_SCHEMA,
    },
    "behavioral_evidence": BEHAVIORAL_EVIDENCE_SCHEMA,
    "problem": {"type": "string", "minLength": 1},
    "impact": {"type": "string", "minLength": 1},
    "suggested_fix": {"type": "string", "minLength": 1},
  },
  [
    "id",
    "reviewer",
    "severity",
    "classification",
    "contract_type",
    "title",
    "normative_quote",
    "spec_anchor",
    "same_flow",
    "file",
    "line_start",
    "line_end",
    "anchor",
    "trace_anchors",
    "behavioral_evidence",
    "problem",
    "impact",
    "suggested_fix",
  ],
)
SPEC_FILE_ACCOUNTING_SCHEMA = strict_object(
  {
    "path": {"type": "string", "minLength": 1},
    "status": {"enum": ["SPEC_REVIEWED", "SPEC_NOT_REVIEWED"]},
  },
  ["path", "status"],
)
SUMMARY_SCHEMA = strict_object(
  {
    "candidates": {"type": "integer", "minimum": 0},
    "findings": {"type": "integer", "minimum": 0},
    **{
      classification: {"type": "integer", "minimum": 0}
      for classification in CLASSIFICATIONS
    },
  },
  ["candidates", "findings", *CLASSIFICATIONS],
)
ERROR_DETAIL_SCHEMA = strict_object(
  {
    "code": {"type": "string", "minLength": 1},
    "message": {"type": "string", "minLength": 1},
    "dispatch_id": {"type": "string", "minLength": 1},
  },
  ["code", "message"],
)
REVIEWER_OUTPUT_SCHEMA = strict_object(
  {
    "reviewer": {"const": "spec-compliance-reviewer"},
    "dispatch_id": {"type": "string", "minLength": 8},
    "packet_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "status": {"enum": ["COMPLETE", "FAILED"]},
    "contract_accounting": {
      "type": "array",
      "items": CONTRACT_ACCOUNTING_SCHEMA,
    },
    "findings": {"type": "array", "items": FINDING_SCHEMA},
    "spec_file_accounting": {
      "type": "array",
      "items": SPEC_FILE_ACCOUNTING_SCHEMA,
    },
    "summary": SUMMARY_SCHEMA,
    "errors": {
      "type": "array",
      "items": {
        "oneOf": [
          {"type": "string", "minLength": 1},
          ERROR_DETAIL_SCHEMA,
        ]
      },
    },
  },
  [
    "reviewer",
    "dispatch_id",
    "packet_sha256",
    "status",
    "contract_accounting",
    "findings",
    "spec_file_accounting",
    "summary",
    "errors",
  ],
)
RUNTIME_RECEIPT_SCHEMA = strict_object(
  {
    "status": {"const": "COMPLETE"},
    "reason_code": {"const": "C4_RUNTIME_RECEIPT_OK"},
    "requested_model": {"const": "opus"},
    "requested_effort": {"const": "xhigh"},
    "dispatch_id": {"type": "string", "minLength": 8},
    "packet_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "agent_id": {"type": "string", "minLength": 1},
    "transcript_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "reviewer_output_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "binding_status": {"const": "BOUND"},
    "observed_model": {"type": "string", "pattern": "^claude-opus-"},
    "effort": {"const": "xhigh"},
    "assistant_records": {"type": "integer", "minimum": 1},
    "tool_call_count": {"const": 0},
    "tool_calls": {"const": []},
    "tool_calls_by_name": {"const": {}},
  },
  [
    "status",
    "reason_code",
    "requested_model",
    "requested_effort",
    "dispatch_id",
    "packet_sha256",
    "agent_id",
    "transcript_sha256",
    "reviewer_output_sha256",
    "binding_status",
    "observed_model",
    "effort",
    "assistant_records",
    "tool_call_count",
    "tool_calls",
    "tool_calls_by_name",
  ],
)
RUNTIME_INPUT_SCHEMA = strict_object(
  {
    "transcript_path": {"type": "string", "minLength": 1},
    "dispatch_id": {"type": "string", "minLength": 8},
    "expected_agent_id": {"type": "string", "minLength": 1},
    "packet_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "packet": PACKET_SCHEMA,
    "requested_model": {"const": "opus"},
    "requested_effort": {"const": "xhigh"},
  },
  [
    "transcript_path",
    "dispatch_id",
    "expected_agent_id",
    "packet_sha256",
    "packet",
    "requested_model",
    "requested_effort",
  ],
)
BINDING_CONTEXT_SCHEMA = strict_object(
  {
    "review_root": {"type": "string", "minLength": 1},
    "authored_diff_base": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
    "review_head": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
  },
  ["review_root", "authored_diff_base", "review_head"],
)
RESOLVE_CLI_SCHEMA = strict_object(
  {
    "review_root": {"type": "string", "minLength": 1},
    "candidate": {"type": "object"},
  },
  ["review_root", "candidate"],
)
VALIDATE_CLI_SCHEMA = strict_object(
  {
    "packet": {"type": "object"},
    "reviewer_output": {"anyOf": [{"type": "object"}, {"type": "string"}]},
    "runtime_input": {"type": "object"},
    "binding_context": {"type": "object"},
  },
  ["packet", "reviewer_output", "runtime_input", "binding_context"],
)
EMIT_CLI_SCHEMA = strict_object(
  {
    "packet": {"type": "object"},
  },
  ["packet"],
)
AUTHORITY_CANDIDATE_VALIDATOR = Draft202012Validator(AUTHORITY_CANDIDATE_SCHEMA)
PACKET_VALIDATOR = Draft202012Validator(PACKET_SCHEMA)
REVIEWER_OUTPUT_VALIDATOR = Draft202012Validator(REVIEWER_OUTPUT_SCHEMA)
RUNTIME_RECEIPT_VALIDATOR = Draft202012Validator(RUNTIME_RECEIPT_SCHEMA)
RUNTIME_INPUT_VALIDATOR = Draft202012Validator(RUNTIME_INPUT_SCHEMA)
BINDING_CONTEXT_VALIDATOR = Draft202012Validator(BINDING_CONTEXT_SCHEMA)
RESOLVE_CLI_VALIDATOR = Draft202012Validator(RESOLVE_CLI_SCHEMA)
VALIDATE_CLI_VALIDATOR = Draft202012Validator(VALIDATE_CLI_SCHEMA)
EMIT_CLI_VALIDATOR = Draft202012Validator(EMIT_CLI_SCHEMA)


def sha256_text(value):
  return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_hash(value):
  serialized = json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
  )
  return sha256_text(serialized)


def skipped(reason_code):
  return {"status": "SKIPPED", "reason_code": reason_code, "clause": None}


def safe_file(root, relative_path):
  root_path = Path(root).expanduser().resolve()
  candidate = Path(relative_path)
  if candidate.is_absolute():
    raise ValueError("C4_SPEC_PATH_OUTSIDE_REVIEW_ROOT")
  resolved = (root_path / candidate).resolve()
  if not resolved.is_relative_to(root_path):
    raise ValueError("C4_SPEC_PATH_OUTSIDE_REVIEW_ROOT")
  if not resolved.is_file():
    raise ValueError("C4_SPEC_PATH_NOT_FOUND")
  return root_path, resolved


def read_utf8_exact(path):
  return path.read_bytes().decode("utf-8")


def quote_location(text, quote):
  if not quote or text.count(quote) != 1:
    return None
  offset = text.index(quote)
  line_start = text[:offset].count("\n") + 1
  line_end = line_start + quote.count("\n")
  return line_start, line_end


def requirement_blocks(text):
  lines = text.splitlines(keepends=True)
  starts = [
    index
    for index, line in enumerate(lines)
    if re.match(r"^###\s+Requirement:\s*", line)
  ]
  blocks = []
  for start in starts:
    end = len(lines)
    for index in range(start + 1, len(lines)):
      if re.match(r"^#{1,3}\s+", lines[index]):
        end = index
        break
    blocks.append({
      "text": "".join(lines[start:end]),
      "line_start": start + 1,
      "line_end": end,
    })
  return blocks


def excerpt_for_quote(text, quote):
  location = quote_location(text, quote)
  if location is None:
    return None
  offset = text.index(quote)
  for block in requirement_blocks(text):
    block_offset = text.find(block["text"])
    if block_offset <= offset < block_offset + len(block["text"]):
      return block["text"]
  lines = text.splitlines()
  start = max(0, location[0] - 3)
  end = min(len(lines), location[1] + 2)
  return "\n".join(lines[start:end])


def requirement_block_for_quote(text, quote):
  offset = text.index(quote)
  for block in requirement_blocks(text):
    block_offset = text.find(block["text"])
    if block_offset <= offset < block_offset + len(block["text"]):
      return block
  return None


def resolved_clause(candidate, spec_path, text, reason_code, original_path=None):
  quote = candidate["exact_quote"]
  location = quote_location(text, quote)
  excerpt = excerpt_for_quote(text, quote)
  result = {
    **candidate,
    "spec_path": spec_path,
    "line_start": location[0],
    "line_end": location[1],
    "source_excerpt": excerpt,
    "source_hash": sha256_text(excerpt),
  }
  if original_path is not None:
    result["authority_alias_path"] = original_path
  return {
    "status": "RESOLVED",
    "reason_code": reason_code,
    "clause": result,
  }


def resolve_authority(root, candidate):
  if not isinstance(root, (str, Path)):
    return skipped("C4_AUTHORITY_INPUT_INVALID")
  if not isinstance(candidate, dict) or not AUTHORITY_CANDIDATE_VALIDATOR.is_valid(candidate):
    return skipped("C4_AUTHORITY_INPUT_INVALID")
  if candidate["line_start"] > candidate["line_end"]:
    return skipped("C4_AUTHORITY_INPUT_INVALID")
  lexical_path = Path(candidate["spec_path"])
  lexical_posix = lexical_path.as_posix()
  is_archive = lexical_posix.startswith("openspec/changes/archive/")
  is_live = lexical_posix.startswith("openspec/specs/")
  if not relative_path_valid(lexical_posix) or not (is_archive or is_live):
    return skipped("C4_AUTHORITY_PATH_NOT_ALLOWED")
  try:
    root_path, source_path = safe_file(root, candidate["spec_path"])
    if path_has_symlink(root_path, lexical_path):
      return skipped("C4_AUTHORITY_SYMLINK_NOT_ALLOWED")
    if is_live:
      live_root = (root_path / "openspec/specs").resolve()
      if not source_path.is_relative_to(live_root):
        return skipped("C4_SPEC_PATH_OUTSIDE_LIVE_ROOT")
    else:
      archive_root = (root_path / "openspec/changes/archive").resolve()
      if not source_path.is_relative_to(archive_root):
        return skipped("C4_SPEC_PATH_OUTSIDE_ARCHIVE_ROOT")
    text = read_utf8_exact(source_path)
  except ValueError as error:
    return skipped(str(error))
  except (OSError, UnicodeDecodeError):
    return skipped("C4_SPEC_CONTENT_INVALID")
  location = quote_location(text, candidate["exact_quote"])
  if location is None:
    return skipped("C4_CANONICAL_QUOTE_NOT_UNIQUE")
  if location != (candidate["line_start"], candidate["line_end"]):
    return skipped("C4_SPEC_ANCHOR_MISMATCH")
  relative_source = source_path.relative_to(root_path).as_posix()
  if not is_archive:
    return resolved_clause(
      candidate,
      relative_source,
      text,
      "C4_CURRENT_AUTHORITY_RESOLVED",
    )
  archived_block = requirement_block_for_quote(text, candidate["exact_quote"])
  if archived_block is None:
    return skipped("C4_ARCHIVED_REQUIREMENT_BLOCK_MISSING")
  live_root = root_path / "openspec/specs"
  matches = []
  if live_root.is_dir():
    live_root_resolved = live_root.resolve()
    for path in sorted(live_root.rglob("*.md")):
      try:
        resolved = path.resolve()
        if not resolved.is_relative_to(live_root_resolved) or not resolved.is_file():
          continue
        live_text = read_utf8_exact(resolved)
      except (OSError, UnicodeDecodeError):
        continue
      for block in requirement_blocks(live_text):
        if block["text"] == archived_block["text"]:
          matches.append((resolved, live_text))
  if not matches:
    return skipped("C4_ARCHIVED_ONLY_NO_LIVE_CANONICAL")
  if len(matches) != 1:
    return skipped("C4_LIVE_CANONICAL_AMBIGUOUS")
  live_path, live_text = matches[0]
  if quote_location(live_text, candidate["exact_quote"]) is None:
    return skipped("C4_CANONICAL_QUOTE_NOT_UNIQUE")
  return resolved_clause(
    candidate,
    live_path.relative_to(root_path).as_posix(),
    live_text,
    "C4_LIVE_CANONICAL_RESOLVED",
    original_path=lexical_posix,
  )


def parse_reviewer_output(raw_output):
  if isinstance(raw_output, dict):
    return raw_output
  if not isinstance(raw_output, str):
    return None
  text = raw_output.strip()
  match = re.fullmatch(r"```json\s*\n(.*)\n```", text, flags=re.DOTALL)
  payload = match.group(1) if match else text
  try:
    parsed = json.loads(payload)
  except json.JSONDecodeError:
    return None
  return parsed if isinstance(parsed, dict) else None


def raw_output_hash(raw_output):
  try:
    if isinstance(raw_output, str):
      return sha256_text(raw_output)
    return canonical_json_hash(raw_output)
  except UnicodeEncodeError:
    escaped = json.dumps(
      raw_output,
      ensure_ascii=True,
      sort_keys=True,
      separators=(",", ":"),
    )
    return sha256_text(escaped)


def empty_classification_counts():
  return {name: 0 for name in CLASSIFICATIONS}


def failure_result(raw_output, reason_code, parsed_output=None):
  parsed = parsed_output if isinstance(parsed_output, dict) else None
  raw_hash = raw_output_hash(raw_output)
  findings = parsed.get("findings") if parsed else None
  candidate_count = len(findings) if isinstance(findings, list) else 0
  invalidated_count = max(1, candidate_count)
  return {
    "status": "FAILED",
    "reason_code": reason_code,
    "raw_output_hash": raw_hash,
    "candidate_count": candidate_count,
    "admitted_findings": [],
    "observations": [],
    "invalidated": [
      {
        "id": "C4-BATCH",
        "reason_code": reason_code,
        "raw_output_hash": raw_hash,
      }
    ],
    "human_projection": {
      "classification_counts": empty_classification_counts(),
      "clause_accounting": [],
      "invalidated_count": invalidated_count,
      "invalidated_reason_codes": [reason_code],
      "findings": [],
      "observations": [],
    },
  }


def unique_index(items, key):
  if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
    return None
  values = [item.get(key) for item in items]
  try:
    unique_values = set(values)
  except TypeError:
    return None
  if None in values or len(values) != len(unique_values):
    return None
  return {item[key]: item for item in items}


def validate_summary(output):
  accounting_items = output["contract_accounting"]
  summary = output["summary"]
  expected = Counter(item["classification"] for item in accounting_items)
  if summary["candidates"] != len(accounting_items):
    return False
  if summary["findings"] != len(output["findings"]):
    return False
  return all(summary[name] == expected[name] for name in CLASSIFICATIONS)


def schema_valid(validator, value):
  return not any(validator.iter_errors(value))


def line_range_valid(value):
  return value["line_start"] <= value["line_end"]


def utf8_valid(value):
  if isinstance(value, str):
    try:
      value.encode("utf-8")
    except UnicodeEncodeError:
      return False
    return True
  if isinstance(value, list):
    return all(utf8_valid(item) for item in value)
  if isinstance(value, dict):
    return all(utf8_valid(key) and utf8_valid(item) for key, item in value.items())
  return True


def relative_path_valid(value):
  path = Path(value)
  return (
    not path.is_absolute()
    and ".." not in path.parts
    and not any(ord(character) < 32 or ord(character) == 127 for character in value)
  )


def path_has_symlink(root, relative_path):
  current = Path(root).expanduser().resolve()
  for part in Path(relative_path).parts:
    current = current / part
    if current.is_symlink():
      return True
  return False


def packet_valid(packet):
  if not schema_valid(PACKET_VALIDATOR, packet) or not utf8_valid(packet):
    return False
  serialized = json.dumps(
    packet,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
  ).encode("utf-8")
  if len(serialized) > 120000:
    return False
  if unique_index(packet["clauses"], "clause_id") is None:
    return False
  if unique_index(packet["evidence_bindings"], "binding_id") is None:
    return False
  spec_paths = [item["path"] for item in packet["spec_files"]]
  changed_paths = [item["path"] for item in packet["changed_files"]]
  if len(spec_paths) != len(set(spec_paths)) or len(changed_paths) != len(set(changed_paths)):
    return False
  all_paths = [
    *spec_paths,
    *changed_paths,
    *(item["path"] for item in packet["evidence_bindings"]),
    *(
      item["old_path"]
      for item in packet["evidence_bindings"]
      if item["side"] == "base"
    ),
    *(
      item["authority_alias_path"]
      for item in packet["clauses"]
      if "authority_alias_path" in item
    ),
  ]
  if any(not relative_path_valid(path) for path in all_paths):
    return False
  if set(spec_paths) != {item["spec_path"] for item in packet["clauses"]}:
    return False
  for item in packet["clauses"]:
    spec_path = item["spec_path"]
    alias_path = item.get("authority_alias_path")
    if spec_path.startswith("openspec/changes/archive/"):
      return False
    if alias_path is not None and (
      not alias_path.startswith("openspec/changes/archive/")
      or not spec_path.startswith("openspec/specs/")
    ):
      return False
  if any(not line_range_valid(item) for item in packet["clauses"]):
    return False
  if any(
    item["source_hash"] != sha256_text(item["source_excerpt"])
    or item["source_excerpt"].count(item["exact_quote"]) != 1
    for item in packet["clauses"]
  ):
    return False
  binding_index = unique_index(packet["evidence_bindings"], "binding_id")
  for item in packet["evidence_bindings"]:
    if not line_range_valid(item) or item["content_hash"] != sha256_text(item["quote"]):
      return False
    if item["side"] == "base" and item["path"] != item["old_path"]:
      return False
  authored_paths = {
    item["path"]
    for item in packet["changed_files"]
    if item["provenance"] == "authored"
  }
  context = packet["trace_context"]
  authored_ids = context["authored_diff_binding_ids"]
  guard_ids = context["connected_guard_binding_ids"]
  if any(identifier not in binding_index for identifier in [*authored_ids, *guard_ids]):
    return False
  if set(authored_ids).intersection(guard_ids):
    return False
  if any(binding_index[identifier]["path"] not in authored_paths for identifier in authored_ids):
    return False
  clause_trace_index = unique_index(context["clause_traces"], "clause_id")
  if clause_trace_index is None or set(clause_trace_index) != {
    item["clause_id"] for item in packet["clauses"]
  }:
    return False
  traced_authored_ids = {
    identifier
    for item in context["clause_traces"]
    for identifier in item["authored_binding_ids"]
  }
  traced_guard_ids = {
    identifier
    for item in context["clause_traces"]
    for identifier in item["connected_guard_binding_ids"]
  }
  if traced_authored_ids != set(authored_ids) or traced_guard_ids != set(guard_ids):
    return False
  if any(
    not set(item["authored_binding_ids"]).issubset(authored_ids)
    or not set(item["connected_guard_binding_ids"]).issubset(guard_ids)
    for item in context["clause_traces"]
  ):
    return False
  if context["connected_guard_status"] == "SUPPLIED" and not guard_ids:
    return False
  if context["connected_guard_status"] == "NONE_REQUIRED" and guard_ids:
    return False
  has_base = any(item["side"] == "base" for item in packet["evidence_bindings"])
  base_status = packet["predispatch_verification"]["base_bindings_status"]
  if has_base != (base_status == "VERIFIED"):
    return False
  return True


def output_line_ranges_valid(output):
  for item in output["contract_accounting"]:
    if not line_range_valid(item["spec_anchor"]):
      return False
  for item in output["findings"]:
    if not line_range_valid(item) or not line_range_valid(item["spec_anchor"]):
      return False
    if any(not line_range_valid(anchor) for anchor in item["trace_anchors"]):
      return False
  return True


def ranges_overlap(line_start, line_end, ranges):
  return any(line_start <= end and start <= line_end for start, end in ranges)


def finding_code_binding_valid(
  finding,
  binding_index,
  authored_binding_ids,
  required_binding_ids,
  authored_hunk_ranges,
):
  trace_ids = [item["binding_id"] for item in finding["trace_anchors"]]
  if len(trace_ids) != len(set(trace_ids)) or set(trace_ids) != set(required_binding_ids):
    return False
  for trace in finding["trace_anchors"]:
    binding = binding_index.get(trace["binding_id"])
    if binding is None:
      return False
    expected = {
      key: binding[key]
      for key in (
        "binding_id",
        "side",
        "path",
        "line_start",
        "line_end",
        "quote",
        "content_hash",
      )
    }
    if trace != expected or trace["content_hash"] != sha256_text(trace["quote"]):
      return False
  for trace in finding["trace_anchors"]:
    binding_id = trace["binding_id"]
    if binding_id not in authored_binding_ids or trace["path"] != finding["file"]:
      continue
    location = quote_location(trace["quote"], finding["anchor"])
    if location is None:
      continue
    line_start = trace["line_start"] + location[0] - 1
    line_end = trace["line_start"] + location[1] - 1
    if (
      (finding["line_start"], finding["line_end"]) == (line_start, line_end)
      and ranges_overlap(line_start, line_end, authored_hunk_ranges.get(binding_id, []))
    ):
      return True
  return False


def git_tree_entry(review_root, tree, path):
  listed = git_output(review_root, ["ls-tree", "-z", tree, "--", path])
  if listed is None:
    return None
  rows = [row for row in listed.split(b"\0") if row]
  if len(rows) != 1 or b"\t" not in rows[0]:
    return None
  metadata_bytes, observed_path_bytes = rows[0].split(b"\t", 1)
  try:
    metadata = metadata_bytes.decode("ascii")
    observed_path = observed_path_bytes.decode("utf-8")
  except UnicodeDecodeError:
    return None
  parts = metadata.split()
  if (
    len(parts) != 3
    or parts[0] not in {"100644", "100755"}
    or parts[1] != "blob"
    or observed_path != path
  ):
    return None
  observed_oid = parts[2]
  size = git_output(review_root, ["cat-file", "-s", observed_oid])
  try:
    observed_size = int(size.decode("ascii").strip())
  except (AttributeError, UnicodeDecodeError, ValueError):
    return None
  if observed_size > 120000:
    return None
  blob = git_output(review_root, ["cat-file", "blob", observed_oid])
  if blob is None or len(blob) != observed_size:
    return None
  try:
    text = blob.decode("utf-8")
  except UnicodeDecodeError:
    return None
  return {
    "oid": observed_oid,
    "size": observed_size,
    "text": text,
  }


def base_binding_tree_matches(review_root, authored_diff_base, binding):
  expected = git_output(
    review_root,
    ["rev-parse", "--verify", f"{authored_diff_base}^{{tree}}"],
  )
  observed = git_output(
    review_root,
    ["rev-parse", "--verify", f"{binding['provenance_base_tree']}^{{tree}}"],
  )
  return expected is not None and observed is not None and expected == observed


def packet_authority_valid(binding_context, packet):
  review_root = binding_context["review_root"]
  review_head = binding_context["review_head"]
  for clause in packet["clauses"]:
    if not clause["spec_path"].startswith("openspec/specs/"):
      return False
    entry = git_tree_entry(review_root, review_head, clause["spec_path"])
    if entry is None:
      return False
    text = entry["text"]
    location = quote_location(text, clause["exact_quote"])
    if (
      location != (clause["line_start"], clause["line_end"])
      or text.count(clause["source_excerpt"]) != 1
      or clause["exact_quote"] not in clause["source_excerpt"]
      or sha256_text(clause["source_excerpt"]) != clause["source_hash"]
    ):
      return False
    alias_path = clause.get("authority_alias_path")
    if alias_path is None:
      continue
    if not alias_path.startswith("openspec/changes/archive/"):
      return False
    archive_entry = git_tree_entry(review_root, review_head, alias_path)
    if archive_entry is None:
      return False
    archive_text = archive_entry["text"]
    canonical_block = requirement_block_for_quote(text, clause["exact_quote"])
    archive_location = quote_location(archive_text, clause["exact_quote"])
    archive_block = (
      requirement_block_for_quote(archive_text, clause["exact_quote"])
      if archive_location is not None
      else None
    )
    if canonical_block is None or archive_block is None or canonical_block["text"] != archive_block["text"]:
      return False
  return True


def git_blob_text(review_root, binding):
  entry = git_tree_entry(
    review_root,
    binding["provenance_base_tree"],
    binding["old_path"],
  )
  if (
    entry is None
    or entry["oid"] != binding["blob_oid"]
    or entry["size"] != binding["blob_size_bytes"]
  ):
    return None
  return entry["text"]


def binding_failure_reason(binding_context, binding):
  review_root = binding_context["review_root"]
  if binding["side"] == "head":
    entry = git_tree_entry(review_root, binding_context["review_head"], binding["path"])
    text = entry["text"] if entry is not None else None
  else:
    if not base_binding_tree_matches(
      review_root,
      binding_context["authored_diff_base"],
      binding,
    ):
      return "C4_TRACE_PROVENANCE_MISMATCH"
    text = git_blob_text(review_root, binding)
  if text is None:
    return "C4_TRACE_PROVENANCE_MISMATCH"
  location = quote_location(text, binding["quote"])
  if location is None:
    return "C4_TRACE_HASH_MISMATCH"
  if location != (binding["line_start"], binding["line_end"]):
    return "C4_TRACE_RANGE_MISMATCH"
  return None


def binding_failures(binding_context, packet, authored_hunk_ranges):
  failures = {
    item["binding_id"]: reason
    for item in packet["evidence_bindings"]
    if (reason := binding_failure_reason(binding_context, item)) is not None
  }
  for binding_id in packet["trace_context"]["authored_diff_binding_ids"]:
    if not authored_hunk_ranges.get(binding_id):
      failures[binding_id] = "C4_TRACE_PROVENANCE_MISMATCH"
  return failures


def git_output(review_root, args):
  try:
    result = subprocess.run(
      ["git", "-C", str(review_root), *args],
      capture_output=True,
      check=False,
    )
  except (OSError, ValueError):
    return None
  return result.stdout if result.returncode == 0 else None


def diff_line_ranges(review_root, base, head, path, side):
  output = git_output(
    review_root,
    ["diff", "--unified=0", "--no-color", "--no-ext-diff", base, head, "--", path],
  )
  if output is None:
    return None
  try:
    text = output.decode("utf-8")
  except UnicodeDecodeError:
    return None
  ranges = []
  pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
  for match in pattern.finditer(text):
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    new_start = int(match.group(3))
    new_count = int(match.group(4) or "1")
    start, count = (new_start, new_count) if side == "head" else (old_start, old_count)
    if count:
      ranges.append((start, start + count - 1))
  return ranges


def authoritative_hunk_ranges(binding_context, packet):
  review_root = Path(binding_context["review_root"]).expanduser().resolve()
  top_level = git_output(review_root, ["rev-parse", "--show-toplevel"])
  head = git_output(review_root, ["rev-parse", "HEAD"])
  base_tree = git_output(
    review_root,
    ["rev-parse", "--verify", f"{binding_context['authored_diff_base']}^{{tree}}"],
  )
  head_tree = git_output(
    review_root,
    ["rev-parse", "--verify", f"{binding_context['review_head']}^{{tree}}"],
  )
  try:
    top_level_path = Path(top_level.decode("utf-8").strip()).resolve()
    observed_head = head.decode("ascii").strip()
  except (AttributeError, UnicodeDecodeError):
    return None
  if (
    top_level_path != review_root
    or observed_head != binding_context["review_head"]
    or base_tree is None
    or head_tree is None
  ):
    return None
  binding_index = unique_index(packet["evidence_bindings"], "binding_id")
  ranges = {}
  cache = {}
  for binding_id in packet["trace_context"]["authored_diff_binding_ids"]:
    binding = binding_index[binding_id]
    path = binding["path"] if binding["side"] == "head" else binding["old_path"]
    key = (path, binding["side"])
    if key not in cache:
      cache[key] = diff_line_ranges(
        review_root,
        binding_context["authored_diff_base"],
        binding_context["review_head"],
        path,
        binding["side"],
      )
    if cache[key] is None:
      return None
    ranges[binding_id] = [
      item
      for item in cache[key]
      if ranges_overlap(binding["line_start"], binding["line_end"], [item])
    ]
  return ranges


def same_flow_valid(finding, clause):
  return all(
    finding["same_flow"][key] == clause["changed_flow_hint"][key]
    for key in (
      "actor_or_entity",
      "operation_or_event",
      "precondition",
      "observable_result",
    )
  )


def validate_output(packet, raw_output, runtime_input=None, binding_context=None):
  parsed = parse_reviewer_output(raw_output)
  if parsed is None:
    return failure_result(raw_output, "C4_OUTPUT_JSON_INVALID")
  if not utf8_valid(parsed):
    return failure_result(raw_output, "C4_OUTPUT_SCHEMA_INVALID", parsed)
  if not packet_valid(packet):
    return failure_result(raw_output, "C4_PACKET_SCHEMA_INVALID", parsed)
  if not schema_valid(RUNTIME_INPUT_VALIDATOR, runtime_input):
    return failure_result(raw_output, "C4_RUNTIME_RECEIPT_INVALID", parsed)
  packet_hash = canonical_json_hash(packet)
  if runtime_input["packet"] != packet or runtime_input["packet_sha256"] != packet_hash:
    return failure_result(raw_output, "C4_RUNTIME_PACKET_MISMATCH", parsed)
  runtime = runtime_receipt(
    runtime_input["transcript_path"],
    dispatch_id=runtime_input["dispatch_id"],
    expected_agent_id=runtime_input["expected_agent_id"],
    packet_sha256=runtime_input["packet_sha256"],
    packet_value=runtime_input["packet"],
    requested_model=runtime_input["requested_model"],
    requested_effort=runtime_input["requested_effort"],
  )
  if not schema_valid(RUNTIME_RECEIPT_VALIDATOR, runtime):
    return failure_result(raw_output, runtime["reason_code"], parsed)
  if runtime["dispatch_id"] != packet["dispatch_id"]:
    return failure_result(raw_output, "C4_RUNTIME_DISPATCH_MISMATCH", parsed)
  if runtime["reviewer_output_sha256"] != canonical_json_hash(parsed):
    return failure_result(raw_output, "C4_RUNTIME_OUTPUT_MISMATCH", parsed)
  if not schema_valid(BINDING_CONTEXT_VALIDATOR, binding_context):
    return failure_result(raw_output, "C4_BINDING_CONTEXT_INVALID", parsed)
  if not packet_authority_valid(binding_context, packet):
    return failure_result(raw_output, "C4_AUTHORITY_BINDING_INVALID", parsed)
  authored_hunk_ranges = authoritative_hunk_ranges(binding_context, packet)
  if authored_hunk_ranges is None:
    return failure_result(raw_output, "C4_HUNK_CONTEXT_INVALID", parsed)
  if not schema_valid(REVIEWER_OUTPUT_VALIDATOR, parsed):
    return failure_result(raw_output, "C4_OUTPUT_SCHEMA_INVALID", parsed)
  if (
    parsed["dispatch_id"] != packet["dispatch_id"]
    or parsed["packet_sha256"] != packet_hash
  ):
    return failure_result(raw_output, "C4_OUTPUT_BINDING_MISMATCH", parsed)
  if not output_line_ranges_valid(parsed):
    return failure_result(raw_output, "C4_LINE_RANGE_INVALID", parsed)
  if parsed["status"] != "COMPLETE" or parsed["errors"]:
    return failure_result(raw_output, "C4_REVIEWER_FAILED", parsed)
  if any(item["status"] != "SPEC_REVIEWED" for item in parsed["spec_file_accounting"]):
    return failure_result(raw_output, "C4_SPEC_FILE_ACCOUNTING_MISMATCH", parsed)
  clauses = packet["clauses"]
  spec_files = packet["spec_files"]
  binding_index = unique_index(packet["evidence_bindings"], "binding_id")
  clause_index = unique_index(clauses, "clause_id")
  accounting_index = unique_index(parsed["contract_accounting"], "clause_id")
  if accounting_index is None or set(clause_index) != set(accounting_index):
    return failure_result(raw_output, "C4_CLAUSE_ACCOUNTING_MISMATCH", parsed)
  expected_spec_paths = [item["path"] for item in spec_files]
  actual_spec_paths = [item["path"] for item in parsed["spec_file_accounting"]]
  if set(expected_spec_paths) != set(actual_spec_paths) or len(actual_spec_paths) != len(set(actual_spec_paths)):
    return failure_result(raw_output, "C4_SPEC_FILE_ACCOUNTING_MISMATCH", parsed)
  for clause_id, item in accounting_index.items():
    source = clause_index[clause_id]
    anchor = item["spec_anchor"]
    if (
      item["contract_type"] != source["contract_type"]
      or item["normative_quote"] != source["exact_quote"]
      or anchor["path"] != source["spec_path"]
      or anchor["line_start"] != source["line_start"]
      or anchor["line_end"] != source["line_end"]
    ):
      return failure_result(raw_output, "C4_CLAUSE_BINDING_MISMATCH", parsed)
  finding_index = unique_index(parsed["findings"], "id")
  finding_ids = [
    item["finding_id"]
    for item in parsed["contract_accounting"]
    if item["finding_id"] is not None
  ]
  if finding_index is None or len(finding_ids) != len(set(finding_ids)) or set(finding_ids) != set(finding_index):
    return failure_result(raw_output, "C4_FINDING_ACCOUNTING_MISMATCH", parsed)
  accounting_by_finding = {
    item["finding_id"]: item
    for item in parsed["contract_accounting"]
    if item["finding_id"] is not None
  }
  authored_binding_ids = set(packet["trace_context"]["authored_diff_binding_ids"])
  clause_trace_index = unique_index(packet["trace_context"]["clause_traces"], "clause_id")
  for finding_id, item in finding_index.items():
    source = accounting_by_finding[finding_id]
    clause_id = source["clause_id"]
    clause_source = clause_index[clause_id]
    clause_trace = clause_trace_index[clause_id]
    required_binding_ids = [
      *clause_trace["authored_binding_ids"],
      *clause_trace["connected_guard_binding_ids"],
    ]
    if item["classification"] not in FINDING_CLASSIFICATIONS:
      return failure_result(raw_output, "C4_FINDING_CLASSIFICATION_INVALID", parsed)
    if (
      item["classification"] != source["classification"]
      or item["contract_type"] != source["contract_type"]
      or item["normative_quote"] != source["normative_quote"]
      or item["spec_anchor"] != source["spec_anchor"]
    ):
      return failure_result(raw_output, "C4_FINDING_BINDING_MISMATCH", parsed)
    if not same_flow_valid(item, clause_source):
      return failure_result(raw_output, "C4_SAME_FLOW_MISMATCH", parsed)
    if not finding_code_binding_valid(
      item,
      binding_index,
      authored_binding_ids,
      required_binding_ids,
      authored_hunk_ranges,
    ):
      return failure_result(raw_output, "C4_CODE_BINDING_MISMATCH", parsed)
  if not validate_summary(parsed):
    return failure_result(raw_output, "C4_SUMMARY_MISMATCH", parsed)
  observed_failures = binding_failures(
    binding_context,
    packet,
    authored_hunk_ranges,
  )
  raw_hash = raw_output_hash(raw_output)
  invalidated = []
  admitted = []
  for item in parsed["findings"]:
    failed_ids = [
      trace["binding_id"]
      for trace in item["trace_anchors"]
      if trace["binding_id"] in observed_failures
    ]
    if failed_ids:
      invalidated.append({
        "id": item["id"],
        "reason_code": observed_failures[failed_ids[0]],
        "raw_output_hash": raw_hash,
      })
      continue
    admitted.append({**item, "clause_id": accounting_by_finding[item["id"]]["clause_id"]})
  observation_candidates = [
    item
    for item in parsed["contract_accounting"]
    if item["finding_id"] is None
  ]
  observations = []
  for item in observation_candidates:
    clause_trace = clause_trace_index[item["clause_id"]]
    required_ids = [
      *clause_trace["authored_binding_ids"],
      *clause_trace["connected_guard_binding_ids"],
    ]
    failed_ids = [
      binding_id
      for binding_id in required_ids
      if binding_id in observed_failures
    ]
    if failed_ids:
      invalidated.append({
        "id": f"C4-OBSERVATION-{item['clause_id']}",
        "reason_code": observed_failures[failed_ids[0]],
        "raw_output_hash": raw_hash,
      })
    else:
      observations.append(item)
  visible_clause_ids = {
    item["clause_id"] for item in observations
  } | {
    item["clause_id"] for item in admitted
  }
  clause_accounting = [
    item
    for item in parsed["contract_accounting"]
    if item["clause_id"] in visible_clause_ids
  ]
  classification_counter = Counter(
    item["classification"] for item in clause_accounting
  )
  classification_counts = {
    name: classification_counter[name]
    for name in CLASSIFICATIONS
  }
  reason_codes = sorted({item["reason_code"] for item in invalidated})
  return {
    "status": "COMPLETE",
    "reason_code": "C4_VALIDATED",
    "raw_output_hash": raw_hash,
    "candidate_count": len(parsed["findings"]),
    "admitted_findings": admitted,
    "observations": observations,
    "invalidated": invalidated,
    "human_projection": {
      "classification_counts": classification_counts,
      "clause_accounting": clause_accounting,
      "invalidated_count": len(invalidated),
      "invalidated_reason_codes": reason_codes,
      "findings": admitted,
      "observations": observations,
    },
  }


def model_matches(requested_model, observed_model):
  if requested_model == "opus":
    return observed_model.startswith("claude-opus-")
  return requested_model == observed_model


def runtime_receipt(
  transcript_path,
  dispatch_id,
  expected_agent_id,
  packet_sha256,
  packet_value,
  requested_model,
  requested_effort,
):
  path = Path(transcript_path).expanduser()
  base = {
    "requested_model": requested_model,
    "requested_effort": requested_effort,
    "dispatch_id": dispatch_id,
    "packet_sha256": packet_sha256,
    "agent_id": "UNAVAILABLE",
    "transcript_sha256": "0" * 64,
    "reviewer_output_sha256": "0" * 64,
    "binding_status": "UNBOUND",
    "observed_model": "UNAVAILABLE",
    "effort": "UNAVAILABLE",
    "assistant_records": 0,
    "tool_call_count": 0,
    "tool_calls": [],
    "tool_calls_by_name": {},
  }
  if not path.is_file():
    return {**base, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_UNAVAILABLE"}
  try:
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8")
    rows = []
    for line in text.split("\n"):
      if not line:
        continue
      row = json.loads(line)
      if not isinstance(row, dict):
        raise ValueError
      rows.append(row)
  except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
    return {**base, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_INVALID"}
  transcript_hash = hashlib.sha256(raw_bytes).hexdigest()
  indexed_records = []
  for index, row in enumerate(rows):
    message = row.get("message", {})
    if not isinstance(message, dict):
      return {**base, "transcript_sha256": transcript_hash, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_INVALID"}
    if row.get("attributionAgent") == "spec-compliance-reviewer" and message.get("role") == "assistant":
      indexed_records.append((index, row))
  if not indexed_records:
    return {**base, "transcript_sha256": transcript_hash, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_UNAVAILABLE"}
  records = [row for _, row in indexed_records]
  models = [row.get("message", {}).get("model") for row in records]
  efforts = [
    row.get("effort") or row.get("message", {}).get("effort")
    for row in records
  ]
  agent_ids = [row.get("agentId") for row in records]
  tools = []
  reviewer_outputs = []
  for row in records:
    content = row.get("message", {}).get("content", [])
    if not isinstance(content, list):
      return {**base, "transcript_sha256": transcript_hash, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_INVALID"}
    for block in content:
      if not isinstance(block, dict):
        return {**base, "transcript_sha256": transcript_hash, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_INVALID"}
      if block.get("type") == "tool_use":
        name = block.get("name")
        if not isinstance(name, str):
          return {**base, "transcript_sha256": transcript_hash, "status": "FAILED", "reason_code": "C4_RUNTIME_RECEIPT_INVALID"}
        tools.append(name)
      if block.get("type") == "text" and isinstance(block.get("text"), str):
        candidate = parse_reviewer_output(block["text"])
        if candidate is not None and candidate.get("reviewer") == "spec-compliance-reviewer":
          reviewer_outputs.append(candidate)
  model_values = sorted(set(models)) if all(isinstance(value, str) for value in models) else []
  effort_values = sorted(set(efforts)) if all(isinstance(value, str) for value in efforts) else []
  agent_id_values = sorted(set(agent_ids)) if all(isinstance(value, str) for value in agent_ids) else []
  observed_model = model_values[0] if len(model_values) == 1 else "UNAVAILABLE"
  effort = effort_values[0] if len(effort_values) == 1 else "UNAVAILABLE"
  observed_agent_id = agent_id_values[0] if len(agent_id_values) == 1 else "UNAVAILABLE"
  reviewer_output_hash = (
    canonical_json_hash(reviewer_outputs[0])
    if len(reviewer_outputs) == 1
    else "0" * 64
  )
  packet_json = json.dumps(
    packet_value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
  )
  expected_lines = [
    f"C4_PACKET_SHA256={packet_sha256}",
    f"C4_PACKET_JSON={packet_json}",
  ]
  prompt_bound = False
  first_assistant_index = indexed_records[0][0]
  if (
    observed_agent_id != "UNAVAILABLE"
    and isinstance(packet_value, dict)
    and canonical_json_hash(packet_value) == packet_sha256
    and packet_value.get("dispatch_id") == dispatch_id
  ):
    for row in rows[:first_assistant_index]:
      message = row.get("message", {})
      if not isinstance(message, dict) or message.get("role") != "user":
        continue
      if row.get("agentId") != observed_agent_id:
        continue
      content = message.get("content")
      if not isinstance(content, str):
        continue
      lines = content.split("\n")
      if any(lines[index:index + 2] == expected_lines for index in range(len(lines) - 1)):
        prompt_bound = True
        break
  agent_identity_bound = (
    observed_agent_id != "UNAVAILABLE"
    and observed_agent_id == expected_agent_id
  )
  bound = prompt_bound and agent_identity_bound
  result = {
    **base,
    "agent_id": observed_agent_id,
    "transcript_sha256": transcript_hash,
    "reviewer_output_sha256": reviewer_output_hash,
    "binding_status": "BOUND" if bound else "UNBOUND",
    "observed_model": observed_model,
    "effort": effort,
    "assistant_records": len(records),
    "tool_call_count": len(tools),
    "tool_calls": tools,
    "tool_calls_by_name": dict(sorted(Counter(tools).items())),
  }
  if not model_values:
    reason_code = "C4_RUNTIME_MODEL_UNAVAILABLE"
  elif len(model_values) > 1:
    reason_code = "C4_RUNTIME_MODEL_AMBIGUOUS"
  elif not model_matches(requested_model, observed_model):
    reason_code = "C4_RUNTIME_MODEL_MISMATCH"
  elif not effort_values:
    reason_code = "C4_RUNTIME_EFFORT_UNAVAILABLE"
  elif len(effort_values) > 1:
    reason_code = "C4_RUNTIME_EFFORT_AMBIGUOUS"
  elif effort != requested_effort:
    reason_code = "C4_RUNTIME_EFFORT_MISMATCH"
  elif not agent_id_values or len(agent_id_values) > 1:
    reason_code = "C4_RUNTIME_AGENT_ID_MISMATCH"
  elif not reviewer_outputs:
    reason_code = "C4_RUNTIME_OUTPUT_UNAVAILABLE"
  elif len(reviewer_outputs) > 1:
    reason_code = "C4_RUNTIME_OUTPUT_AMBIGUOUS"
  elif tools:
    reason_code = "C4_TOOL_SURFACE_VIOLATION"
  elif not bound:
    reason_code = "C4_RUNTIME_DISPATCH_MISMATCH"
  else:
    reason_code = "C4_RUNTIME_RECEIPT_OK"
  return {
    **result,
    "status": "COMPLETE" if reason_code == "C4_RUNTIME_RECEIPT_OK" else "FAILED",
    "reason_code": reason_code,
  }


def read_stdin_json():
  return json.load(sys.stdin)


def emit(value):
  json.dump(value, sys.stdout, ensure_ascii=False, sort_keys=True)
  sys.stdout.write("\n")


def main(argv=None):
  parser = argparse.ArgumentParser()
  parser.add_argument(
    "mode",
    choices=("resolve-authority", "emit-prompt", "validate", "runtime-receipt"),
  )
  args = parser.parse_args(argv)
  try:
    data = read_stdin_json()
  except (json.JSONDecodeError, OSError, UnicodeDecodeError):
    result = {"status": "FAILED", "reason_code": "C4_CLI_INPUT_INVALID"}
    emit(result)
    return 1
  if not isinstance(data, dict) or not utf8_valid(data):
    result = {"status": "FAILED", "reason_code": "C4_CLI_INPUT_INVALID"}
  elif args.mode == "resolve-authority":
    if not schema_valid(RESOLVE_CLI_VALIDATOR, data):
      result = {"status": "FAILED", "reason_code": "C4_CLI_INPUT_INVALID"}
    else:
      result = resolve_authority(data["review_root"], data["candidate"])
  elif args.mode == "emit-prompt":
    if not schema_valid(EMIT_CLI_VALIDATOR, data) or not packet_valid(data["packet"]):
      result = {"status": "FAILED", "reason_code": "C4_CLI_INPUT_INVALID"}
    else:
      packet_json = json.dumps(
        data["packet"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
      )
      sys.stdout.write(
        f"C4_PACKET_SHA256={sha256_text(packet_json)}\n"
        f"C4_PACKET_JSON={packet_json}\n"
      )
      return 0
  elif args.mode == "validate":
    if not schema_valid(VALIDATE_CLI_VALIDATOR, data):
      result = {"status": "FAILED", "reason_code": "C4_CLI_INPUT_INVALID"}
    else:
      result = validate_output(
        data["packet"],
        data["reviewer_output"],
        runtime_input=data["runtime_input"],
        binding_context=data["binding_context"],
      )
  elif not schema_valid(RUNTIME_INPUT_VALIDATOR, data):
    result = {
      "status": "FAILED",
      "reason_code": "C4_RUNTIME_RECEIPT_INVALID",
    }
  else:
    result = runtime_receipt(
      data["transcript_path"],
      dispatch_id=data["dispatch_id"],
      expected_agent_id=data["expected_agent_id"],
      packet_sha256=data["packet_sha256"],
      packet_value=data["packet"],
      requested_model=data["requested_model"],
      requested_effort=data["requested_effort"],
    )
  emit(result)
  return 0 if result["status"] in {"RESOLVED", "COMPLETE"} else 1


if __name__ == "__main__":
  raise SystemExit(main())
