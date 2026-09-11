#!/usr/bin/env python3

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


SCHEME_REMOTE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+/(.+)$", re.IGNORECASE)
SCP_REMOTE = re.compile(r"^[^/@:]+@[^/:]+:(.+)$")
LIST_FIELDS = ("spec_globs", "conventions_docs")


def default():
  return {"trunk": None, "spec_globs": [], "conventions_docs": [], "source": "default"}


def remote_path(remote):
  for pattern in (SCHEME_REMOTE, SCP_REMOTE):
    match = pattern.match(remote.strip())
    if match:
      return match.group(1)
  return None


def remote_to_key(remote):
  path = remote_path(remote or "")
  if path is None:
    return None
  segments = [s for s in path.strip("/").split("/") if s]
  if len(segments) < 2:
    return None
  repo = re.sub(r"\.git$", "", segments[-1])
  return f"{segments[-2]}/{repo}"


def as_list(field, value):
  if value is None:
    return []
  if isinstance(value, str):
    return [value]
  if isinstance(value, (list, tuple)):
    return [str(item) for item in value]
  raise ValueError(f"{field} must be a string or a list, got {type(value).__name__}")


def load_profiles(path):
  profile_path = Path(path).expanduser()
  if not profile_path.is_file():
    return {}
  try:
    data = yaml.safe_load(profile_path.read_text()) or {}
  except yaml.YAMLError as error:
    raise ValueError(f"parse error: {error}")
  if not isinstance(data, dict):
    raise ValueError("top level must be a mapping of owner/repo keys")
  return data


def resolve(remote, profile_path):
  key = remote_to_key(remote)
  if key is None:
    return default()
  entry = load_profiles(profile_path).get(key)
  if not isinstance(entry, dict) or not isinstance(entry.get("trunk"), str) or not entry["trunk"]:
    return default()
  resolved = default()
  resolved["trunk"] = entry["trunk"]
  for field in LIST_FIELDS:
    resolved[field] = as_list(field, entry.get(field))
  resolved["source"] = str(Path(profile_path).expanduser())
  return resolved


def main(argv=None):
  parser = argparse.ArgumentParser(description="Resolve /pr-review repo profile from git remote URL")
  parser.add_argument("--remote", default="", help="git remote URL (https, ssh:// or scp-like)")
  parser.add_argument("--profile", default="~/.claude/pr-review/repos.yaml", help="path to repos.yaml")
  args = parser.parse_args(argv)
  try:
    print(json.dumps(resolve(args.remote, args.profile), ensure_ascii=False))
  except ValueError as error:
    print(f"repos.yaml ({Path(args.profile).expanduser()}): {error}", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
