#!/usr/bin/env python3

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path


def decision(value, reason):
  return {
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": value,
      "permissionDecisionReason": f"[pr-review-c4-gate] {reason}",
    }
  }


def deny(reason):
  print(json.dumps(decision("deny", reason)))
  return 0


def canonical_json_hash(value):
  encoded = json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
  ).encode()
  return hashlib.sha256(encoded).hexdigest()


def runtime_dir():
  root = Path(
    os.environ.get(
      "PR_REVIEW_C4_RUNTIME_DIR",
      f"/private/tmp/claude-pr-review-c4-{os.getuid()}",
    )
  )
  metadata = root.lstat()
  if (
    metadata.st_uid != os.getuid()
    or not stat.S_ISDIR(metadata.st_mode)
    or stat.S_ISLNK(metadata.st_mode)
    or stat.S_IMODE(metadata.st_mode) & 0o077
  ):
    raise OSError
  return root


def permit_path(root, session_id):
  if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9._-]+", session_id) is None:
    raise ValueError
  return root / f"{session_id}.json"


def validate_state(state, session_id, tool_name, tool_input):
  required = {
    "version",
    "permit_id",
    "session_id",
    "issued_at",
    "expires_at",
    "consumed",
    "agent_sha256",
    "packet_sha256",
    "prompt_sha256",
  }
  if not isinstance(state, dict) or set(state) != required:
    return False
  if state["version"] != 1 or state["session_id"] != session_id or state["consumed"] is not False:
    return False
  try:
    expires = datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00"))
  except (AttributeError, TypeError, ValueError):
    return False
  if expires <= datetime.now(timezone.utc):
    return False
  if not isinstance(state["permit_id"], str) or re.fullmatch(r"[0-9a-f]{32}", state["permit_id"]) is None:
    return False
  for key in ("agent_sha256", "packet_sha256", "prompt_sha256"):
    if not isinstance(state[key], str) or re.fullmatch(r"[0-9a-f]{64}", state[key]) is None:
      return False
  allowed_keys = {"description", "subagent_type", "model", "prompt"}
  if tool_name != "Agent" or set(tool_input) != allowed_keys:
    return False
  if any(not isinstance(tool_input[key], str) for key in allowed_keys):
    return False
  return canonical_json_hash(tool_input) == state["agent_sha256"]


def consume(path, session_id, tool_name, tool_input):
  descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
  try:
    metadata = os.fstat(descriptor)
    if (
      metadata.st_uid != os.getuid()
      or not stat.S_ISREG(metadata.st_mode)
      or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
      raise OSError
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
      descriptor = -1
      fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
      try:
        state = json.load(handle)
      except json.JSONDecodeError as error:
        raise OSError from error
      if not validate_state(state, session_id, tool_name, tool_input):
        return False
      state["consumed"] = True
      handle.seek(0)
      json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
      handle.write("\n")
      handle.truncate()
      handle.flush()
      os.fsync(handle.fileno())
    return True
  finally:
    if descriptor >= 0:
      os.close(descriptor)


def main():
  try:
    payload = json.load(sys.stdin)
  except json.JSONDecodeError:
    return deny("Invalid hook input.")
  if not isinstance(payload, dict) or payload.get("tool_name") not in {"Agent", "Task"}:
    print("{}")
    return 0
  tool_input = payload.get("tool_input")
  if not isinstance(tool_input, dict) or tool_input.get("subagent_type") != "spec-compliance-reviewer":
    print("{}")
    return 0
  session_id = payload.get("session_id")
  tool_name = payload.get("tool_name")
  try:
    path = permit_path(runtime_dir(), session_id)
    allowed = consume(path, session_id, tool_name, tool_input)
  except (OSError, TypeError, ValueError):
    allowed = False
  if not allowed:
    return deny("C4 Agent input requires one matching unconsumed session permit.")
  print(json.dumps(decision("allow", "C4 Agent input consumed its matching session permit.")))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
